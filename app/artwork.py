"""Artwork cache: serves provider cover art from this host.

Why this exists instead of hotlinking: the site's content-security-policy is
``img-src 'self'``, and the status page promises no third-party requests.
Letting the browser fetch covers straight from a provider would break both.
So covers are fetched server-side once, stored on disk, and served from here.

The cache is keyed by a hash of the source URL and bounded in both file count
and age, so it cannot grow without limit. A miss that cannot be fetched is
reported as missing and the frontend falls back to a letter tile; it never
falls back to a third-party request.

Only https URLs on public hosts are ever fetched, so a poisoned provider
response cannot turn this into an SSRF proxy.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
import unicodedata
from pathlib import Path

import httpx

log = logging.getLogger("confih.artwork")

# Bounds. Cover art is small; anything larger is refused rather than stored.
_MAX_BYTES = 3 * 1024 * 1024
_MAX_FILES = 1500
_MAX_AGE_SECONDS = 60 * 60 * 24 * 180  # six months
_FETCH_TIMEOUT = 8.0
_DEFAULT_ALLOWED_HOSTS = (
    "qobuz.com",
    "tidal.com",
    "tidalhifi.com",
    "picdn.net",
)


_MAX_REDIRECTS = 3
_NAT64_NET = ipaddress.IPv6Network("64:ff9b::/96")
_HOSTNAME_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ALLOWED_IMAGE_TYPES = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/avif",
})


def _parse_hosts(extra: str | None) -> tuple[str, ...]:
    """Default provider hosts, plus any the operator adds."""
    names = list(_DEFAULT_ALLOWED_HOSTS)
    for part in (extra or "").replace(";", ",").split(","):
        clean = part.strip().lower().lstrip(".").rstrip(".")
        if not clean or _has_disallowed_unicode(clean) or any(ch.isspace() for ch in clean):
            continue
        labels = clean.split(".")
        ascii_labels: list[str] = []
        valid = True
        for raw_label in labels:
            try:
                enc = raw_label.encode("idna").decode("ascii").lower()
            except (UnicodeError, ValueError):
                valid = False
                break
            if not enc or "." in enc or not (1 <= len(enc) <= 63) or not _HOSTNAME_LABEL_RE.fullmatch(enc):
                valid = False
                break
            ascii_labels.append(enc)
        if not valid:
            continue
        ascii_clean = ".".join(ascii_labels)
        if "." in ascii_clean and 1 <= len(ascii_clean) <= 253 and ascii_clean not in names:
            names.append(ascii_clean)
    return tuple(names)


def key_for(url: str) -> str:
    """Stable, content-addressed cache key for a source URL."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def _has_disallowed_unicode(text: str) -> bool:
    return any(
        ord(ch) < 0x20
        or ord(ch) == 0x7F
        or 0x80 <= ord(ch) <= 0x9F
        or 0xD800 <= ord(ch) <= 0xDFFF
        or ch == "\ufffd"
        or unicodedata.category(ch) == "Cf"
        for ch in text
    )


def _is_public_ip_obj(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        if address in _NAT64_NET:
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        elif address.ipv4_mapped is not None:
            address = address.ipv4_mapped
    return not (
        not address.is_global
        or address.is_multicast
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


def _host_is_public(
    host: str,
    allowed: tuple[str, ...],
    *,
    resolve_dns: bool = True,
) -> bool:
    if (
        not isinstance(host, str)
        or not host
        or host.startswith(".")
        or ".." in host
        or host.endswith("..")
    ):
        return False
    if _has_disallowed_unicode(host) or any(ch.isspace() for ch in host):
        return False
    lowered = (host[:-1] if host.endswith(".") else host).lower()
    if not lowered or lowered in {"localhost", "localhost.localdomain"}:
        return False
    stripped_ip = lowered[1:-1] if lowered.startswith("[") and lowered.endswith("]") else lowered
    try:
        literal_ip = ipaddress.ip_address(stripped_ip)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not _is_public_ip_obj(literal_ip):
        return False

    ascii_labels: list[str] = []
    for raw_label in lowered.split("."):
        try:
            enc = raw_label.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            return False
        if "." in enc or not (1 <= len(enc) <= 63) or not _HOSTNAME_LABEL_RE.fullmatch(enc):
            return False
        if enc.startswith("xn--"):
            try:
                enc.encode("ascii").decode("idna")
            except (UnicodeError, ValueError):
                return False
        ascii_labels.append(enc)
    ascii_host = ".".join(ascii_labels)
    if not (1 <= len(ascii_host) <= 253):
        return False
    if not any(ascii_host == suffix or ascii_host.endswith("." + suffix) for suffix in allowed):
        return False
    if not resolve_dns:
        return True
    try:
        infos = socket.getaddrinfo(ascii_host, None)
    except (OSError, UnicodeError, ValueError):
        return False
    checked = 0
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError, TypeError):
            continue
        checked += 1
        if not _is_public_ip_obj(address):
            return False
    return checked > 0


def _safe_url(
    url: str,
    allowed: tuple[str, ...],
    *,
    resolve_dns: bool = True,
) -> str | None:
    """Return the URL if it may be fetched, else None."""
    if not isinstance(url, str):
        return None
    if _has_disallowed_unicode(url):
        return None
    cleaned = url.strip()
    if not cleaned:
        return None
    from urllib.parse import urlparse

    try:
        parsed = urlparse(cleaned)
        port = parsed.port
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    if username or password or "@" in parsed.netloc:
        return None
    if port not in (None, 443):
        return None
    if parsed.netloc.startswith("["):
        if "]" not in parsed.netloc:
            return None
        _, _, after_bracket = parsed.netloc.partition("]")
        if after_bracket and after_bracket != ":443":
            return None
    elif ":" in parsed.netloc:
        _, _, raw_port = parsed.netloc.partition(":")
        if raw_port != "443":
            return None
    try:
        httpx.URL(cleaned)
    except (httpx.InvalidURL, UnicodeError, ValueError):
        return None
    if not _host_is_public(hostname or "", allowed, resolve_dns=resolve_dns):
        return None
    return cleaned


class ArtworkCache:
    """Disk-backed cover cache with a bounded footprint."""

    def __init__(
        self,
        root: Path,
        client: httpx.AsyncClient | None = None,
        allowed_hosts: str | None = None,
    ) -> None:
        self.root = Path(root)
        self.allowed = _parse_hosts(allowed_hosts)
        self.root.mkdir(parents=True, exist_ok=True)
        self._client = client
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._key_lock_users: dict[str, int] = {}
        self._pruned_at = 0.0

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._key_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._key_locks[key] = lock
        self._key_lock_users[key] = self._key_lock_users.get(key, 0) + 1
        return lock

    def _release_lock(self, key: str) -> None:
        users = self._key_lock_users.get(key, 1) - 1
        if users <= 0:
            self._key_lock_users.pop(key, None)
            self._key_locks.pop(key, None)
        else:
            self._key_lock_users[key] = users

    # ── registration ───────────────────────────────────────────────────────

    def register(self, url: str | None) -> str | None:
        """Record a source URL and return its cache key, or None if unusable."""
        clean = _safe_url(url or "", self.allowed, resolve_dns=False)
        if not clean:
            return None
        key = key_for(clean)
        meta_path = self.root / f"{key}.json"
        if meta_path.exists():
            return key
        if not _safe_url(clean, self.allowed, resolve_dns=True):
            return None
        try:
            meta_path.write_text(json.dumps({"url": clean, "at": int(time.time())}), "utf-8")
        except OSError as exc:  # cache is best-effort; never break a page
            log.warning("artwork: could not register %s: %s", key, exc)
            return None
        return key

    # ── lookup ─────────────────────────────────────────────────────────────

    def path_for(self, key: str) -> Path | None:
        if not key or not key.replace("-", "").isalnum() or len(key) > 32:
            return None
        return self.root / f"{key}.img"

    async def get(self, key: str) -> tuple[bytes, str] | None:
        """Return (bytes, content_type) for a key, fetching once on a miss."""
        data_path = self.path_for(key)
        if data_path is None:
            return None

        meta_path = self.root / f"{key}.json"
        if not meta_path.exists():
            return None

        if data_path.exists():
            try:
                content_type = self._content_type(meta_path, data_path)
                return data_path.read_bytes(), content_type
            except OSError:
                pass

        lock = self._lock_for(key)
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            if data_path.exists():
                try:
                    content_type = self._content_type(meta_path, data_path)
                    return data_path.read_bytes(), content_type
                except OSError:
                    pass

            try:
                meta = json.loads(meta_path.read_text("utf-8"))
            except (OSError, ValueError):
                return None
            if not isinstance(meta, dict):
                return None
            raw_url = meta.get("url")
            source = _safe_url(raw_url, self.allowed, resolve_dns=False) if isinstance(raw_url, str) else None
            if not source:
                return None

            fetched = await self._fetch(source)
            if fetched is None:
                return None
            body, content_type = fetched
            tmp_meta = self.root / f".{key}.json.tmp"
            tmp_data = self.root / f".{key}.img.tmp"
            try:
                meta["at"] = int(time.time())
                meta["type"] = content_type
                tmp_meta.write_text(json.dumps(meta), "utf-8")
                os.replace(tmp_meta, meta_path)
                tmp_data.write_bytes(body)
                os.replace(tmp_data, data_path)
            except OSError as exc:
                log.warning("artwork: could not store %s: %s", key, exc)
                for tmp in (tmp_meta, tmp_data):
                    with contextlib.suppress(OSError):
                        tmp.unlink(missing_ok=True)
            self._prune()
            return body, content_type
        finally:
            if acquired:
                lock.release()
            self._release_lock(key)

    def _content_type(self, meta_path: Path, data_path: Path) -> str:
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
            if isinstance(meta, dict):
                declared = meta.get("type")
                if isinstance(declared, str):
                    clean_ct = declared.split(";", 1)[0].strip().lower()
                    if clean_ct in _ALLOWED_IMAGE_TYPES:
                        return clean_ct
        except (OSError, ValueError):
            pass
        suffix = data_path.suffix.lower()
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".avif": "image/avif",
        }.get(suffix, "image/jpeg")

    async def _fetch(self, url: str) -> tuple[bytes, str] | None:
        from urllib.parse import urljoin

        current_url = await asyncio.to_thread(_safe_url, url, self.allowed, resolve_dns=True)
        if not current_url:
            return None

        client = self._client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(follow_redirects=False, timeout=_FETCH_TIMEOUT)
        try:
            headers = {"Accept": "image/*,*/*;q=0.8"}
            for _ in range(_MAX_REDIRECTS + 1):
                if hasattr(client, "stream"):
                    async with client.stream(
                        "GET",
                        current_url,
                        headers=headers,
                        follow_redirects=False,
                    ) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = (response.headers.get("location") or "").strip()
                            if not location:
                                return None
                            next_url = urljoin(current_url, location)
                            safe_next = await asyncio.to_thread(
                                _safe_url, next_url, self.allowed, resolve_dns=True
                            )
                            if not safe_next:
                                log.warning(
                                    "artwork: blocked unsafe redirect from %s to %s",
                                    current_url,
                                    next_url,
                                )
                                return None
                            current_url = safe_next
                            continue
                        if response.status_code != 200:
                            return None
                        content_type = (
                            (response.headers.get("content-type") or "image/jpeg")
                            .split(";", 1)[0]
                            .strip()
                            .lower()
                        )
                        if content_type not in _ALLOWED_IMAGE_TYPES:
                            return None
                        raw_len = (response.headers.get("content-length") or "").strip()
                        if raw_len.isdigit() and int(raw_len) > _MAX_BYTES:
                            return None
                        buf = bytearray()
                        async for chunk in response.aiter_bytes():
                            buf.extend(chunk)
                            if len(buf) > _MAX_BYTES:
                                return None
                        body = bytes(buf)
                        if not body:
                            return None
                        return body, content_type
                else:
                    response = await client.get(
                        current_url,
                        headers=headers,
                        follow_redirects=False,
                    )
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = (response.headers.get("location") or "").strip()
                        if not location:
                            return None
                        next_url = urljoin(current_url, location)
                        safe_next = await asyncio.to_thread(
                            _safe_url, next_url, self.allowed, resolve_dns=True
                        )
                        if not safe_next:
                            log.warning(
                                "artwork: blocked unsafe redirect from %s to %s",
                                current_url,
                                next_url,
                            )
                            return None
                        current_url = safe_next
                        continue
                    if response.status_code != 200:
                        return None
                    body = response.content
                    if not body or len(body) > _MAX_BYTES:
                        return None
                    content_type = (
                        (response.headers.get("content-type") or "image/jpeg")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type not in _ALLOWED_IMAGE_TYPES:
                        return None
                    return body, content_type
            return None
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            log.debug("artwork: fetch failed for %s: %s", url, exc)
            return None
        finally:
            if owns_client:
                await client.aclose()

    # ── bounds ─────────────────────────────────────────────────────────────

    def _prune(self) -> None:
        now = time.time()
        if now - self._pruned_at < 30:
            return
        self._pruned_at = now
        items: list[tuple[float, Path]] = []
        try:
            for path in self.root.glob("*.img"):
                try:
                    items.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        except OSError:
            return

        items.sort(key=lambda item: item[0])
        stale_cutoff = now - _MAX_AGE_SECONDS
        remaining = len(items)
        for mtime, path in items:
            if mtime >= stale_cutoff and remaining <= _MAX_FILES:
                break
            try:
                path.unlink(missing_ok=True)
                (self.root / f"{path.stem}.json").unlink(missing_ok=True)
                remaining -= 1
            except OSError:
                continue

    def footprint(self) -> dict[str, int | float]:
        """Current cached cover count and disk usage, bounded by _MAX_FILES."""
        if not self.root.is_dir():
            return {"files": 0, "bytes": 0, "megabytes": 0.0, "max_files": _MAX_FILES}
        files = 0
        total_bytes = 0
        try:
            for path in self.root.glob("*.img"):
                try:
                    total_bytes += path.stat().st_size
                    files += 1
                except OSError:
                    continue
        except OSError:
            pass
        return {
            "files": files,
            "bytes": total_bytes,
            "megabytes": round(total_bytes / (1024.0 * 1024.0), 2),
            "max_files": _MAX_FILES,
        }
