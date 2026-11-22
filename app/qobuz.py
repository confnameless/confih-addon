"""Qobuz API client: search, signed stream URLs, token refresh, rate limits.

The Qobuz API is undocumented. Two details matter and are easy to get wrong:

1. ``track/getFileUrl`` requires a ``request_sig``: MD5 of
   ``trackgetFileUrlformat_id<id>intent<stream>track_id<id><ts><app_secret>``.
   The timestamp in the signature must be the exact string sent as
   ``request_ts``.
2. Qobuz returns a 30-second sample with HTTP 200 and a ``restrictions`` array
   when the token is missing, invalid or unentitled. That must never be handed
   to BitChord as a playable stream.

Only the JSON API is contacted from this server. The audio URL Qobuz signs is
returned to the phone, which fetches it directly from Qobuz's CDN. That keeps
the server's bandwidth near zero and is why a 2 GB / 5 MB/s box is enough.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .quality import format_plan
from .ratelimit import TokenBucket

log = logging.getLogger("qobuz")

QOBUZ_BASE_URL = "https://www.qobuz.com/api.json/0.2"

# Restriction codes that mean "the account cannot stream", not "this track is
# unavailable". Matched case-insensitively and by substring because Qobuz has
# used several spellings over the years.
_AUTH_RESTRICTION_MARKERS = (
    "unauthenticated",
    "notauthenticated",
    "invalidtoken",
    "tokenexpired",
    "subscription",
    "entitle",
    "sample",
    "preview",
)


class QobuzError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int = 502,
        code: str = "qobuz_error",
        retryable: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.retryable = retryable


class QobuzAuthError(QobuzError):
    def __init__(self, message: str, *, code: str = "qobuz_auth"):
        super().__init__(message, status=503, code=code, retryable=False)


class QobuzConfigError(QobuzError):
    def __init__(self, message: str, *, code: str = "qobuz_config"):
        super().__init__(message, status=503, code=code, retryable=False)


class QobuzNotFound(QobuzError):
    def __init__(self, message: str = "Not held by Qobuz", *, code: str = "not_found"):
        super().__init__(message, status=404, code=code, retryable=False)


class QobuzRateLimited(QobuzError):
    def __init__(self, message: str, *, retry_after: int = 5):
        super().__init__(message, status=429, code="rate_limited", retryable=True)
        self.retry_after = max(1, int(retry_after))


class QobuzUnavailable(QobuzError):
    def __init__(self, message: str, *, code: str = "qobuz_unavailable"):
        super().__init__(message, status=503, code=code, retryable=True)


class QobuzGeoBlocked(QobuzError):
    def __init__(self, message: str):
        super().__init__(message, status=503, code="qobuz_geo_blocked", retryable=False)


@dataclass(frozen=True)
class QobuzTrack:
    id: str
    title: str
    artist: str
    album: str
    duration: int | None
    artwork_url: str | None
    isrc: str | None
    streamable: bool
    hires_streamable: bool
    max_bit_depth: int | None
    max_sampling_rate_khz: float | None


@dataclass(frozen=True)
class QobuzStream:
    url: str
    format_id: int
    mime_type: str
    bit_depth: int | None
    sampling_rate_khz: float | None
    restrictions: tuple[str, ...]
    requested_format_id: int


def _track_title(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").strip()
    version = str(item.get("version") or "").strip()
    if version and version.lower() not in title.lower():
        return f"{title} ({version})"
    return title


def _track_artist(item: dict[str, Any]) -> str:
    performer = (item.get("performer") or {}).get("name")
    album_artist = ((item.get("album") or {}).get("artist") or {}).get("name")
    return str(performer or album_artist or "").strip()


def _track_artwork(item: dict[str, Any]) -> str | None:
    album = item.get("album") or {}
    image = album.get("image") or {}
    for key in ("large", "small", "thumbnail"):
        value = image.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return None


def map_track(item: dict[str, Any]) -> QobuzTrack | None:
    try:
        track_id = item.get("id")
        title = _track_title(item)
        if track_id is None or not title:
            return None
        streamable = item.get("streamable")
        if streamable is False:
            return None
        bit_depth = _as_int(item.get("maximum_bit_depth"))
        sampling_rate = _as_float(item.get("maximum_sampling_rate"))
        hires = bool(item.get("hires_streamable") or item.get("hires"))
        if bit_depth is not None and bit_depth < 24:
            hires = False
        return QobuzTrack(
            id=str(track_id),
            title=title,
            artist=_track_artist(item),
            album=str(((item.get("album") or {}).get("title")) or "").strip(),
            duration=_as_int(item.get("duration")),
            artwork_url=_track_artwork(item),
            isrc=(str(item.get("isrc")).strip() if item.get("isrc") else None),
            streamable=streamable is not False,
            hires_streamable=hires,
            max_bit_depth=bit_depth,
            max_sampling_rate_khz=sampling_rate,
        )
    except Exception:  # pragma: no cover - one malformed row must not kill a search
        return None


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


class QobuzClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._started = False
        self._stop = asyncio.Event()
        self._background_task: asyncio.Task[None] | None = None
        self._startup_task: asyncio.Task[None] | None = None

        self._token = settings.qobuz_user_auth_token.strip()
        self._user_id = settings.qobuz_user_id.strip()
        self._last_user: dict[str, Any] = {}

        self._refresh_lock = asyncio.Lock()
        self._auth_attempt_at: float = 0.0
        self._save_tasks: set[asyncio.Task[None]] = set()
        self._token_checked_at: float | None = None
        self._token_ok: bool | None = None
        self._token_error: str | None = None
        self._geo_checked_at: float | None = None
        self._geo_ok: bool | None = None
        self._geo_error: str | None = None
        self._health_latency_ms: float | None = None

        self._bucket = TokenBucket(
            settings.qobuz_rate_limit_per_second,
            settings.qobuz_rate_limit_burst,
        )
        self._sem = asyncio.Semaphore(settings.qobuz_max_concurrency)

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._started:
            return
        # Read even when the environment already has a token: the file may
        # carry the numeric user id, and a token refreshed by a previous run is
        # useful if the environment value has gone stale.
        self._load_token_file()

        kwargs: dict[str, Any] = {
            "base_url": self.settings.qobuz_base_url.rstrip("/") + "/",
            "headers": {
                "Accept": "application/json",
                "User-Agent": self.settings.qobuz_user_agent,
            },
            "timeout": httpx.Timeout(self.settings.qobuz_timeout_seconds, connect=10.0),
            "follow_redirects": False,
            "limits": httpx.Limits(
                max_connections=40, max_keepalive_connections=10, keepalive_expiry=120.0
            ),
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        elif self.settings.qobuz_proxy_url.strip():
            kwargs["proxy"] = self.settings.qobuz_proxy_url.strip()
        self._http = httpx.AsyncClient(**kwargs)
        self._started = True

        if self.settings.qobuz_self_check and self.settings.qobuz_configured:
            self._startup_task = asyncio.create_task(self._startup_check())
        if (
            self.settings.qobuz_token_check_interval_seconds > 0
            and self.settings.qobuz_configured
        ):
            self._background_task = asyncio.create_task(self._background_loop())

    async def warm(self) -> None:
        """Keep a TLS connection to Qobuz open (best effort)."""
        if self._http is None:
            return
        try:
            await self._http.get("", timeout=8.0)
        except Exception:
            pass

    async def stop(self) -> None:
        self._stop.set()
        for task in (self._startup_task, self._background_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._startup_task = None
        self._background_task = None
        # Let any fire-and-forget token-file write finish before the loop that
        # owns it goes away.
        for task in list(self._save_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._save_tasks.clear()
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._started = False

    async def _startup_check(self) -> None:
        try:
            await asyncio.wait_for(self._check_health(force=True), timeout=30)
        except TimeoutError:
            log.warning("Qobuz startup check timed out")
        except Exception as exc:  # never take the addon down for a check
            log.warning("Qobuz startup check failed: %s", exc)

    async def _background_loop(self) -> None:
        interval = self.settings.qobuz_token_check_interval_seconds
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                await self.refresh_token(force=True)
                await self.check_geo(force=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Scheduled Qobuz check failed: %s", exc)

    # ── Token handling ───────────────────────────────────────────────────

    def _read_token_file(self) -> dict[str, Any] | None:
        path = self.settings.token_path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:
            log.warning("Could not read Qobuz token file %s: %s", path, exc)
            return None
        return payload if isinstance(payload, dict) else None

    def _load_token_file(self) -> None:
        payload = self._read_token_file()
        if payload is None:
            return
        token = str(payload.get("user_auth_token") or "").strip()
        if token and not self._token:
            self._token = token
            log.info("Loaded Qobuz token from %s", self.settings.token_path)
        if not self._user_id and payload.get("user_id"):
            self._user_id = str(payload["user_id"]).strip()

    async def _save_token_file(self) -> None:
        if not self._token:
            return
        path = self.settings.token_path
        payload = {
            "user_auth_token": self._token,
            "user_id": self._user_id,
            "updated_at": int(time.time()),
        }
        try:
            await asyncio.to_thread(self._write_token_file, path, payload)
        except Exception as exc:
            log.warning("Could not write Qobuz token file %s: %s", path, exc)

    @staticmethod
    def _write_token_file(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    async def ensure_token(self) -> None:
        """Make sure a token exists and has not been proven dead.

        A validated token is left alone. A missing or failed one is refreshed,
        but at most once every 30 seconds: a dead account behind a search box
        must not become a login loop against Qobuz.
        """
        if self._token and self._token_ok is True:
            return
        now = time.monotonic()
        if self._auth_attempt_at and now - self._auth_attempt_at < 30:
            raise QobuzAuthError(
                self._token_error
                or "Qobuz credentials are not valid. Refresh QOBUZ_USER_AUTH_TOKEN."
            )
        self._auth_attempt_at = now
        await self.refresh_token(force=True)

    async def refresh_token(self, *, force: bool = False) -> None:
        async with self._refresh_lock:
            if (
                not force
                and self._token_checked_at is not None
                and time.monotonic() - self._token_checked_at < 60
                and self._token_ok is True
            ):
                return

            if not self.settings.qobuz_configured:
                raise QobuzConfigError(
                    "Qobuz is not configured. Set QOBUZ_APP_ID, QOBUZ_APP_SECRET and "
                    "QOBUZ_USER_AUTH_TOKEN (+ QOBUZ_USER_ID), or QOBUZ_EMAIL + QOBUZ_PASSWORD."
                )

            errors: list[str] = []

            # 1. Exchange the current token for a fresh one. This both refreshes
            #    and validates it, and needs the numeric user id.
            if self._token and self._user_id:
                try:
                    data = await self._login_with_token()
                    self._apply_login(data, "token")
                    return
                except QobuzError as exc:
                    errors.append(f"token login: {exc.message}")

            # 2. A token file survives restarts; use it when the environment
            #    token has expired. This is what makes browser-token-only setups
            #    keep working without a new QOBUZ_USER_AUTH_TOKEN in .env.
            file_payload = self._read_token_file()
            file_token = (
                str(file_payload.get("user_auth_token") or "").strip()
                if file_payload
                else ""
            )
            if file_token and file_token != self._token and self._user_id:
                previous_token = self._token
                self._token = file_token
                try:
                    data = await self._login_with_token()
                    self._apply_login(data, "token-file")
                    return
                except QobuzError as exc:
                    errors.append(f"token-file login: {exc.message}")
                    self._token = previous_token

            # 3. E-mail / password login. This is the only route back from an
            #    expired token without a human.
            if self.settings.can_password_login:
                try:
                    data = await self._login_with_password()
                    self._apply_login(data, "password")
                    return
                except QobuzError as exc:
                    errors.append(f"password login: {exc.message}")

            # 4. Authenticated probe: a favorites call or user/get confirms
            #    whether the token is actively accepted by Qobuz even if token-login endpoint failed.
            if self._token:
                try:
                    await self._probe_auth()
                    self._token_ok = True
                    self._token_error = None
                    self._token_checked_at = time.monotonic()
                    log.info("Qobuz token validated via authenticated probe")
                    return
                except QobuzError as exc:
                    errors.append(f"token probe: {exc.message}")

            self._token_ok = False
            self._token_error = "; ".join(errors) or "Could not obtain a Qobuz token"
            self._token_checked_at = time.monotonic()
            raise QobuzAuthError(
                "Qobuz did not accept the account credentials. "
                + self._token_error
                + " Get a fresh token from https://play.qobuz.com or set QOBUZ_EMAIL/QOBUZ_PASSWORD."
            )

    async def _login_with_token(self) -> dict[str, Any]:
        params = {
            "user_id": self._user_id,
            "user_auth_token": self._token,
            "app_id": self.settings.qobuz_app_id,
        }
        return await self._request(
            "user/login",
            params,
            auth=False,
            allow_refresh=False,
            retries=1,
        )

    async def _login_with_password(self) -> dict[str, Any]:
        params = {
            "email": self.settings.qobuz_email,
            "password": self.settings.qobuz_password_value,
            "app_id": self.settings.qobuz_app_id,
        }
        return await self._request(
            "user/login",
            params,
            auth=False,
            allow_refresh=False,
            retries=1,
        )

    async def _probe_auth(self) -> None:
        if self._user_id:
            try:
                data = await self._request(
                    "user/get",
                    {"user_id": self._user_id},
                    auth=True,
                    allow_refresh=False,
                    retries=1,
                )
                if isinstance(data, dict):
                    user = data.get("user") or data
                    sub = (
                        (user.get("credential") or {}).get("parameters", {}).get("label")
                        or (user.get("subscription") or {}).get("label")
                    )
                    if sub:
                        self._subscription = str(sub)
                    return
            except Exception as exc:
                log.debug("user/get probe failed: %s, falling back to getUserFavorites", exc)
        await self._request(
            "favorite/getUserFavorites",
            {"type": "tracks", "limit": 1},
            auth=True,
            allow_refresh=False,
            retries=1,
        )

    def _apply_login(self, data: dict[str, Any], source: str) -> None:
        token = str(data.get("user_auth_token") or "").strip()
        if not token:
            raise QobuzAuthError("Qobuz login succeeded but returned no user_auth_token")
        self._token = token
        self._token_ok = True
        self._token_error = None
        self._token_checked_at = time.monotonic()
        self._auth_attempt_at = time.monotonic()
        user = data.get("user")
        if isinstance(user, dict):
            self._last_user = user
            if user.get("id") and not self._user_id:
                self._user_id = str(user["id"])
        log.info("Qobuz token refreshed via %s login", source)
        # Fire-and-forget is acceptable here; the in-memory token is already
        # updated and the file is only a restart optimisation. Keep a reference
        # so the loop does not garbage-collect the write, and so stop() can
        # drain it.
        try:
            task = asyncio.get_running_loop().create_task(self._save_token_file())
        except RuntimeError:  # pragma: no cover - no loop during unit tests
            pass
        else:
            self._save_tasks.add(task)
            task.add_done_callback(self._save_tasks.discard)

    async def login_with_password(self) -> dict[str, Any]:
        """Log in with the configured e-mail/password and persist the token.

        Used by ``scripts/qobuz_login.py``. Unlike the automatic refresh path
        this awaits the token-file write, so a script can log in and exit
        knowing the next process will find the token on disk.
        """
        if not self.settings.can_password_login:
            raise QobuzConfigError(
                "QOBUZ_EMAIL and QOBUZ_PASSWORD (or QOBUZ_PASSWORD_MD5) are required."
            )
        if not self._started:
            await self.start()
        data = await self._login_with_password()
        self._apply_login(data, "password")
        await self._save_token_file()
        return data

    def _subscription_summary(self) -> str | None:
        user = self._last_user
        credential = user.get("credential") if isinstance(user, dict) else None
        if not isinstance(credential, dict):
            return None
        params = credential.get("parameters")
        candidates: list[Any] = []
        if isinstance(params, dict):
            candidates.extend(
                [
                    params.get("label"),
                    params.get("name"),
                    params.get("subscription"),
                    params.get("expires_at"),
                    params.get("expiry"),
                ]
            )
        elif isinstance(params, list):
            for entry in params:
                if isinstance(entry, dict):
                    candidates.extend(
                        [entry.get("label"), entry.get("name"), entry.get("value")]
                    )
        for value in candidates:
            if isinstance(value, str) and value.strip():
                return value.strip()
        label = credential.get("label")
        return str(label).strip() if isinstance(label, str) and label.strip() else None

    # ── HTTP ─────────────────────────────────────────────────────────────

    def _headers(self, *, auth: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.settings.qobuz_user_agent,
            "X-App-Id": self.settings.qobuz_app_id,
        }
        if auth and self._token:
            headers["X-User-Auth-Token"] = self._token
        return headers

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        auth: bool = True,
        allow_refresh: bool = True,
        retries: int | None = None,
    ) -> Any:
        if not self._started or self._http is None:
            await self.start()
        assert self._http is not None

        if auth and not self._token:
            await self.refresh_token(force=True)

        retries = self.settings.qobuz_max_retries if retries is None else retries
        query = dict(params or {})
        query.setdefault("app_id", self.settings.qobuz_app_id)
        last_error: QobuzError | None = None

        for attempt in range(retries + 1):
            await self._bucket.acquire()
            try:
                async with self._sem:
                    response = await self._http.get(
                        path,
                        params=query,
                        headers=self._headers(auth=auth and bool(self._token)),
                    )
            except httpx.RequestError as exc:
                last_error = QobuzUnavailable(
                    f"Could not reach Qobuz ({exc.__class__.__name__})"
                )
                if attempt < retries:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise last_error from exc

            if response.status_code == 401 and auth and allow_refresh:
                try:
                    await self.refresh_token(force=True)
                except QobuzError as exc:
                    raise QobuzAuthError(
                        f"Qobuz rejected the account token: {exc.message}"
                    ) from exc
                return await self._request(
                    path,
                    params,
                    auth=auth,
                    allow_refresh=False,
                    retries=retries,
                )

            data = self._decode(response)

            if response.status_code == 429:
                retry_after = self._retry_after(response)
                if attempt < retries:
                    await asyncio.sleep(retry_after)
                    continue
                raise QobuzRateLimited(
                    "Qobuz is rate limiting this server", retry_after=retry_after
                )

            if response.status_code >= 500:
                last_error = QobuzUnavailable(
                    f"Qobuz returned HTTP {response.status_code}"
                )
                if attempt < retries:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise last_error

            if response.status_code >= 400:
                raise self._error_from(response.status_code, data, response)

            if isinstance(data, dict) and data.get("status") == "error":
                raise self._error_from(response.status_code, data, response)

            if (
                auth
                and self._token
                and self._token_ok is None
                and path not in ("track/search", "track/getFileUrl")
            ):
                self._token_ok = True
                self._token_error = None
            return data

        raise last_error or QobuzUnavailable("Qobuz request failed")

    def _decode(self, response: httpx.Response) -> Any:
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise QobuzUnavailable(
                f"Qobuz returned a non-JSON response (HTTP {response.status_code})"
            ) from exc

    def _error_from(
        self, status_code: int, data: Any, response: httpx.Response
    ) -> QobuzError:
        message = ""
        code = str(status_code)
        if isinstance(data, dict):
            message = str(data.get("message") or data.get("error") or "")
            code = str(data.get("code") or status_code)
        message = message or f"Qobuz returned HTTP {status_code}"
        lower = message.lower()

        if (
            status_code in (401, 403)
            or "authentication" in lower
            or "auth token" in lower
            or "invalid token" in lower
        ):
            self._token_ok = False
            self._token_error = message
            return QobuzAuthError(message, code=code)

        if (
            "signature" in lower
            or "app secret" in lower
            or ("secret" in lower and "invalid" in lower)
        ):
            return QobuzConfigError(
                "Qobuz rejected the app secret used to sign the request. "
                "Update QOBUZ_APP_SECRET, or run scripts/qobuz_app_credentials.py.",
                code="qobuz_app_secret",
            )

        if status_code == 404:
            return QobuzNotFound(message, code=code)
        if status_code == 400:
            return QobuzError(message, status=400, code=code)
        if status_code == 429:
            return QobuzRateLimited(message, retry_after=self._retry_after(response))
        if status_code >= 500:
            return QobuzUnavailable(message, code=code)
        return QobuzError(message, status=502, code=code)

    @staticmethod
    def _retry_after(response: httpx.Response) -> int:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return max(1, min(60, int(float(raw))))
            except (TypeError, ValueError):
                pass
        return 5

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(8.0, 0.5 * (2**attempt))

    # ── Qobuz operations ─────────────────────────────────────────────────

    async def search_tracks(self, query: str, limit: int) -> list[QobuzTrack]:
        await self.ensure_token()
        if self._geo_ok is False:
            raise QobuzGeoBlocked(self._geo_message())

        data = await self._request(
            "track/search",
            {"query": query, "limit": max(1, min(limit, 50))},
        )
        items = ((data.get("tracks") or {}).get("items") or []) if isinstance(data, dict) else []
        tracks = [track for item in items if (track := map_track(item)) is not None]

        # A supported-country server returns hundreds of results even for a
        # broad query. If the first search is empty and the geo check has not
        # run, distinguish "no such song" from "this IP gets no catalogue".
        if not tracks and self._geo_ok is None and query.strip().lower() != "daft punk":
            await self.check_geo(force=True)
            if self._geo_ok is False:
                raise QobuzGeoBlocked(self._geo_message())
        return tracks

    async def get_track(self, track_id: str) -> QobuzTrack | None:
        try:
            data = await self._request(
                "track/get",
                {"track_id": str(track_id)},
                auth=False,
                allow_refresh=False,
                retries=1,
            )
            return map_track(data) if isinstance(data, dict) else None
        except Exception:
            return None

    async def get_file_url(self, track_id: str, format_id: int, intent: str = "stream") -> dict[str, Any]:
        if not self.settings.qobuz_app_secret:
            raise QobuzConfigError(
                "QOBUZ_APP_SECRET is empty, so Qobuz stream URLs cannot be signed."
            )
        # Qobuz signs integer Unix seconds, not a float. The exact string sent
        # as request_ts is the one that must appear in the signature.
        timestamp_text = str(int(time.time()))
        signature = hashlib.md5(
            (
                f"trackgetFileUrlformat_id{format_id}intent{intent}"
                f"track_id{track_id}{timestamp_text}{self.settings.qobuz_app_secret}"
            ).encode()
        ).hexdigest()
        params = {
            "track_id": str(track_id),
            "format_id": str(format_id),
            "intent": intent,
            "request_ts": timestamp_text,
            "request_sig": signature,
        }
        data = await self._request("track/getFileUrl", params)
        if not isinstance(data, dict):
            raise QobuzUnavailable("Qobuz returned an unexpected stream response")
        return data

    async def resolve(self, track_id: str, quality: str) -> QobuzStream:
        await self.ensure_token()
        if self._geo_ok is False:
            raise QobuzGeoBlocked(self._geo_message())

        last_error: QobuzError | None = None
        # A rendition Qobuz annotated with a caveat code but still delivered in
        # full. Held back so a lower rung, or a failover provider, only wins
        # when it is genuinely better.
        restricted_fallback: QobuzStream | None = None
        for format_id in format_plan(quality):
            try:
                data = await self.get_file_url(track_id, format_id)
            except QobuzNotFound as exc:
                # This track does not exist in that rendition (or at all). Try
                # the next rung before deciding the track is a miss.
                last_error = exc
                continue
            except QobuzError as exc:
                # Qobuz answers some unavailable renditions with a plain 400
                # ("No result matching given argument") rather than an empty
                # URL. That is a miss for this rung, not a reason to abandon
                # the lower-quality fallbacks.
                if exc.status == 400:
                    last_error = QobuzNotFound(
                        f"Qobuz has no {quality} stream for this track ({exc.message})"
                    )
                    continue
                raise
            restrictions = data.get("restrictions") or []
            codes = _restriction_codes(restrictions)
            message = str(data.get("message") or data.get("error") or "").strip()
            lower_message = message.lower()

            if "signature" in lower_message or "app secret" in lower_message:
                raise QobuzConfigError(
                    "Qobuz rejected the app secret used to sign the request. "
                    "Update QOBUZ_APP_SECRET, or run scripts/qobuz_app_credentials.py.",
                    code="qobuz_app_secret",
                )
            if any(marker in lower_message for marker in _AUTH_RESTRICTION_MARKERS):
                err_msg = (
                    f"Qobuz refused the stream: {message}"
                    if message
                    else "Qobuz refused the stream because the account is not entitled."
                )
                self._token_ok = False
                self._token_error = err_msg
                self._token_checked_at = time.monotonic()
                raise QobuzAuthError(err_msg)

            if codes and all(_is_auth_restriction(code) for code in codes):
                err_msg = (
                    "Qobuz refused the stream ("
                    + ", ".join(codes)
                    + "). The account token is not valid, or the subscription does not "
                    "include streaming."
                )
                self._token_ok = False
                self._token_error = err_msg
                self._token_checked_at = time.monotonic()
                raise QobuzAuthError(err_msg)

            url = str(data.get("url") or "").strip()
            if not url:
                last_error = QobuzNotFound("Qobuz returned no stream URL")
                continue

            # A sample is HTTP 200, so the trustworthy signals are the sample
            # flag and the 20-30 second range. A restriction code alone is not
            # one of them: Qobuz attaches codes to full-length streams too.
            if (
                data.get("sample") is True
                or "range=20-30" in url
                or "range%3D20-30" in url
            ):
                err_msg = (
                    "Qobuz returned a 30-second sample instead of the full track. "
                    "The token is invalid or the Qobuz subscription has expired."
                )
                self._token_ok = False
                self._token_error = err_msg
                self._token_checked_at = time.monotonic()
                raise QobuzAuthError(err_msg)

            self._token_ok = True
            self._token_error = None
            actual_format = _as_int(data.get("format_id")) or format_id
            stream = QobuzStream(
                url=url,
                format_id=actual_format,
                mime_type=str(data.get("mime_type") or ""),
                bit_depth=_as_int(data.get("bit_depth")),
                sampling_rate_khz=_as_float(data.get("sampling_rate")),
                restrictions=tuple(codes),
                requested_format_id=format_id,
            )
            if not codes:
                # A clean rendition only settles the request when it is at
                # least as good as an annotated one already in hand.
                if restricted_fallback is None or _rendition_score(
                    stream
                ) >= _rendition_score(restricted_fallback):
                    return stream
                continue

            # Qobuz annotates perfectly usable renditions with codes such as
            # FormatRestrictedByFormatAvailability (192 kHz was asked for and
            # the master is 24-bit/44.1) and SampleRestrictedByRightHolders.
            # Both arrive with a full-length URL, and one of its edge nodes
            # adds them at random: discarding them is what sent a play to
            # Tidal's CD master while Qobuz held the 24-bit file. Keep the best
            # annotated rendition and let a clean rung beat it.
            last_error = QobuzNotFound(
                "Qobuz has no clean " + quality + " stream for this track ("
                + ", ".join(codes) + ")"
            )
            if restricted_fallback is None or _rendition_score(stream) > _rendition_score(
                restricted_fallback
            ):
                restricted_fallback = stream
            log.info(
                "qobuz: %s format %s came back annotated (%s) but playable at %s-bit; "
                "trying the next rung for a clean answer",
                track_id,
                format_id,
                ", ".join(codes),
                stream.bit_depth,
            )

        if restricted_fallback is not None:
            log.warning(
                "qobuz: serving %s at %s-bit with %s attached; no clean rendition "
                "was on offer",
                track_id,
                restricted_fallback.bit_depth,
                ", ".join(restricted_fallback.restrictions),
            )
            return restricted_fallback
        raise last_error or QobuzNotFound("Qobuz has no playable stream for this track")

    # ── Health ───────────────────────────────────────────────────────────

    async def _check_health(self, *, force: bool) -> dict[str, Any]:
        started = time.monotonic()
        try:
            try:
                await self.refresh_token(force=force)
            except QobuzError as exc:
                self._token_ok = False
                self._token_error = exc.message
                self._token_checked_at = time.monotonic()
            await self.check_geo(force=force)
        finally:
            self._health_latency_ms = round(
                (time.monotonic() - started) * 1000.0, 1
            )
        return self.health_snapshot()

    async def check_geo(self, *, force: bool = False) -> bool | None:
        now = time.monotonic()
        if (
            not force
            and self._geo_checked_at is not None
            and now - self._geo_checked_at < 300
        ):
            return self._geo_ok
        try:
            probe_auth = bool(self._token and self._token_ok is not False)
            try:
                data = await self._request(
                    "track/search",
                    # A limit of 1 can hit a CDN cache entry and return a
                    # result even from a blocked country; 5 is a real
                    # catalogue query.
                    {"query": "daft punk", "limit": 5},
                    auth=probe_auth,
                    allow_refresh=False,
                    retries=1,
                )
            except QobuzAuthError:
                # The token may be dead while the IP is fine. Ask the
                # catalogue anonymously so the two failures do not get
                # conflated in health output.
                data = await self._request(
                    "track/search",
                    {"query": "daft punk", "limit": 5},
                    auth=False,
                    allow_refresh=False,
                    retries=1,
                )
            total = 0
            if isinstance(data, dict):
                total = int((data.get("tracks") or {}).get("total") or 0)
            self._geo_ok = total > 0
            self._geo_error = None if self._geo_ok else "Qobuz returned an empty catalogue"
            if not self._geo_ok:
                log.warning(
                    "Qobuz catalogue search returned 0 tracks. This server's egress IP is "
                    "probably in a country Qobuz does not serve. Deploy in FR/DE/NL/UK/US or set "
                    "QOBUZ_PROXY_URL to a proxy in one of those countries."
                )
        except QobuzError as exc:
            self._geo_ok = None
            self._geo_error = exc.message
            log.warning("Qobuz geo check failed: %s", exc.message)
        self._geo_checked_at = time.monotonic()
        return self._geo_ok

    def _geo_message(self) -> str:
        return (
            "This server's IP is in a country Qobuz does not serve (catalogue search "
            "returns 0 tracks). Run the addon on a VPS in FR/DE/NL/UK/US, or set "
            "QOBUZ_PROXY_URL to a proxy in one of those countries. Qobuz audio is "
            "fetched directly from its CDN, so the proxy is only used for API calls."
        )

    @property
    def is_healthy(self) -> bool:
        return bool(
            self.settings.qobuz_configured
            and self._token
            and self._token_ok is not False
            and self._geo_ok is not False
        )

    def health_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "configured": self.settings.qobuz_configured,
            "token_ok": self._token_ok,
            "token_error": self._token_error,
            "token_checked_seconds_ago": _age(now, self._token_checked_at),
            "geo_ok": self._geo_ok,
            "geo_error": self._geo_error,
            "geo_checked_seconds_ago": _age(now, self._geo_checked_at),
            "proxy_enabled": bool(self.settings.qobuz_proxy_url),
            "health_latency_ms": self._health_latency_ms,
            "subscription": self._subscription_summary(),
        }


def _age(now: float, then: float | None) -> int | None:
    return int(now - then) if then is not None else None


def _restriction_codes(restrictions: Any) -> list[str]:
    if not isinstance(restrictions, list):
        return []
    codes: list[str] = []
    for entry in restrictions:
        if isinstance(entry, dict):
            code = entry.get("code") or entry.get("name")
        else:
            code = entry
        if isinstance(code, str) and code.strip():
            codes.append(code.strip())
    return codes


def _rendition_score(stream: QobuzStream) -> tuple[int, float]:
    """Rank renditions the way a listener would: depth first, then rate."""
    return (stream.bit_depth or 0, stream.sampling_rate_khz or 0.0)


def _is_auth_restriction(code: str) -> bool:
    lower = code.lower()
    if "rightholder" in lower or "formatavailability" in lower:
        return False
    return any(marker in lower for marker in _AUTH_RESTRICTION_MARKERS)
