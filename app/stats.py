"""In-process and persistent traffic & user counters.

Keeps backward compatibility with the existing `/stats` contract while adding
real-time active listener tracking (5m / 1h / 24h), persistent all-time user
and stream counters in `data/stats.json`, per-provider breakdown (Qobuz vs
Tidal + failovers), and 24-hour activity buckets for the public monitor page.

Traffic is split into two classes:

* **addon traffic** — `/search`, `/stream/*`, `/dash/*`, `/manifest.json`.
  These counters mean "a BitChord client used the addon".
* **monitor traffic** — `/status`, `/monitor`, `/admin`, `/vault*`, `/stats`,
  `/api/public-stats`, `/healthz`, `/readyz`, docs, and static assets. These
  requests are visible in `monitor_requests`, but never inflate addon totals,
  client counts, or the addon response-time average.

Reliability buckets are: 2xx/3xx = ``ok``, 4xx = ``client_err``, 5xx =
``err``. The 24h timeline is ``down`` only when more than half of the bucket is
5xx, ``degraded`` when any 5xx is present or when at least half of a
non-trivial bucket is 4xx, otherwise ``up``.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

log = logging.getLogger("stats")

_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_CANDIDATE_RE = re.compile(r"\[?[0-9A-Fa-f:]{2,39}(?:%[A-Za-z0-9_.-]+)?\]?(?::\d{1,5})?")
_BEARER_RE = re.compile(
    r"\bAuthorization\s*:\s*(?:Bearer|Basic)\s+\S+|\bBearer\s+[A-Za-z0-9._\-+/=]+",
    re.IGNORECASE,
)
_JSON_SECRET_RE = re.compile(
    r'"[A-Za-z0-9_-]*(?:token|sig|password|secret|key)"\s*:\s*'
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,}\]]+)",
    re.IGNORECASE,
)
_SECRET_PARAM_RE = re.compile(
    r"(?:^|(?<=[\s&;,?(]))[A-Za-z0-9_-]*(?:token|sig|password|secret|key)\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s&;,\"']+)",
    re.IGNORECASE,
)


def _scrub_ipv6(match: re.Match[str]) -> str:
    raw = match.group(0)
    if raw.count(":") < 2:
        return raw
    candidate = raw
    if candidate.startswith("["):
        if "]" not in candidate:
            return raw
        candidate = candidate[1:candidate.index("]")]
    candidate = candidate.split("%", 1)[0]
    try:
        ipaddress.IPv6Address(candidate)
    except ValueError:
        return raw
    return ""


def _sanitize_incident_message(detail: str) -> str:
    cleaned = _URL_RE.sub("", str(detail or ""))
    cleaned = _BEARER_RE.sub("", cleaned)
    cleaned = _JSON_SECRET_RE.sub("", cleaned)
    cleaned = _SECRET_PARAM_RE.sub("", cleaned)
    cleaned = _IPV4_RE.sub("", cleaned)
    cleaned = _IPV6_CANDIDATE_RE.sub(_scrub_ipv6, cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-,;")
    return (cleaned or "service event")[:120]


#: Failover reasons that describe a catalogue decision rather than a fault: the
#: track simply is not on the primary engine and the backup served it. They stay
#: in the event list — the panel is for routing too — but must not read as trouble.
_ROUTING_REASONS = (
    "not in qobuz catalogue",
    "not in tidal catalogue",
    "not on qobuz",
    "not on tidal",
    # Any line ending in "served it" describes routing that worked: the other
    # engine answered. Those repeat for as long as people play tracks one
    # catalogue lacks, so they fold into one row instead of stacking up the
    # feed with the same sentence.
    "served it",
)

#: Words that mean the engine itself is unhappy, even next to a routing reason.
_FAULT_REASONS = (
    "unauthenticated",
    "rate-limit",
    "rate limit",
    "offline",
    "timeout",
    "timed out",
    "error",
)


#: Wording this addon used before it explained the routing outcome. Records
#: already on disk still carry it, so the read path translates them.
_LEGACY_INCIDENT_TEXT = {
    "Track not on Qobuz": "Not in Qobuz catalogue · Tidal served it",
    "Track not on Tidal": "Not in Tidal catalogue · Qobuz served it",
    "Qobuz token unauthenticated": "Qobuz session expired · Tidal served it",
    "Tidal token unauthenticated": "Tidal session expired · Qobuz served it",
}


def _incident_severity(kind: str, detail: str) -> str:
    """``error`` | ``warn`` | ``info`` — how alarmed a reader should be.

    A fallback that worked is not a failure: "not in the Qobuz catalogue, Tidal
    served it" is routine routing, while an expired session or a 5xx is not.
    """
    text = detail.casefold()
    if kind in ("outage", "upstream_error"):
        return "error"
    if kind == "recovery":
        return "info"
    if any(reason in text for reason in _ROUTING_REASONS) and not any(
        fault in text for fault in _FAULT_REASONS
    ):
        return "info"
    return "warn"


def _infer_incident_provider(detail: str) -> str:
    lowered = str(detail or "").lower()
    if "qobuz" in lowered:
        return "qobuz"
    if "tidal" in lowered:
        return "tidal"
    return "gateway"

_MAX_CLIENTS = 50_000
_ACTIVE_NOW_SEC = 90
_ACTIVE_STREAMERS_SEC = 300
_ACTIVE_1H_SEC = 3_600
_ACTIVE_24H_SEC = 86_400
_CLIENT_ERR_MIN_REQUESTS = 5
_CLIENT_ERR_RATIO = 0.5


def _hash_subject(subject: str) -> str:
    return hashlib.sha256(f"bitchord-client:{subject}".encode()).hexdigest()[:16]


def _quality_tier(
    quality_label: str,
    bit_depth: int | None,
    sample_rate: int | None,
    is_atmos: bool = False,
    codec: str = "",
) -> str:
    lower = (quality_label or "").lower()
    c_lower = (codec or "").lower()
    if (
        is_atmos
        or c_lower in ("eac3", "ec-3", "ec3", "ac-4", "ac4")
        or (
            ("atmos" in lower or "eac3" in lower or "ac4" in lower or "ac-4" in lower)
            and c_lower not in ("flac", "mp3", "aac")
        )
    ):
        return "dolby_atmos"
    if "mp3" in lower or "aac" in lower or c_lower in ("mp3", "aac"):
        return "high_320"
    if (bit_depth or 0) >= 24 or (sample_rate or 0) > 48_000 or "hi-res" in lower:
        return "hires_24bit"
    if "lossless" in lower or "flac" in lower or (bit_depth or 0) >= 16 or c_lower == "flac":
        return "lossless_16bit"
    return "high_320"


# Public name for the tier classifier. ``plays`` needs the same bucketing the
# telemetry uses so the two views of "quality" can never disagree.
quality_tier = _quality_tier


class TrafficStats:
    def __init__(self, persist_path: Path | None = None) -> None:
        self.started_at = time.monotonic()
        self.started_wall = int(time.time())
        self.persist_path = persist_path

        # Session counters (preserved for existing `/stats` tests & callers)
        self.requests = 0
        self.by_endpoint: Counter[str] = Counter()
        self.statuses: Counter[int] = Counter()
        self.clients: set[str] = set()
        self.clients_truncated = False
        self.in_flight = 0
        self.peak_in_flight = 0
        self.search_cache_hits = 0
        self.search_cache_misses = 0
        self.stream_cache_hits = 0
        self.stream_cache_misses = 0
        self.client_rate_limited = 0
        self.qobuz_errors: Counter[str] = Counter()
        self.tidal_errors: Counter[str] = Counter()
        self._addon_requests_session = 0

        # Real-time user activity tracking (maps hashed subject -> last unix timestamp)
        self._user_last_seen: dict[str, float] = {}
        # Last `/stream` or `/dash` request per hashed subject. This is a
        # delivery/activity signal, not proof that audio is still playing.
        self._streamer_last_seen: dict[str, float] = {}
        # All-time unique user hashes (persisted to disk)
        self._all_time_users: set[str] = set()
        # Monotonic: the set above is a capped dedupe window, this is the truth.
        self._all_time_user_count: int = 0

        # Lifetime persistent counters (addon traffic only)
        self.total_requests_all_time = 0
        self.total_searches_all_time = 0
        self.total_streams_all_time = 0
        self.qobuz_streams_served = 0
        self.tidal_streams_served = 0
        self.failover_resolutions = 0
        self.failover_streams_served = 0
        self.failover_reasons: Counter[str] = Counter()
        self.last_failover_at: int = 0
        self.quality_tiers: Counter[str] = Counter()
        self.observed_quality: dict[str, dict[str, Any]] = {}
        self.precache_resolutions = 0
        self.avg_response_ms: float = 0.0

        # Monitor traffic is intentionally kept separate from addon telemetry.
        self.monitor_requests = 0
        self.monitor_avg_response_ms: float = 0.0

        # Hourly addon buckets:
        # hour_epoch (unix // 3600) -> {"req", "ok", "client_err", "err"}
        self._hourly: dict[int, dict[str, int]] = {}
        # Daily addon buckets (90-day retention for 30d status history):
        # day_epoch (unix // 86400) -> {"req", "ok", "client_err", "err"}
        self._daily: dict[int, dict[str, int]] = {}
        # Bounded ring of recent failover / error events (timestamps rounded to minute)
        self._incidents: list[dict[str, Any]] = []
        # endpoint -> {status class: count}, bounded by the endpoint vocabulary.
        self.endpoint_status: dict[str, dict[str, int]] = {}
        self._dirty = False
        self._last_save_at = time.monotonic()
        self._pending_payload: dict[str, Any] | None = None
        self._write_lock = threading.Lock()

        if self.persist_path is not None:
            self._load()

    def _load(self) -> None:
        if self.persist_path is None or not self.persist_path.exists():
            return
        try:
            raw = json.loads(self.persist_path.read_text(encoding="utf-8"))
            self.total_requests_all_time = int(raw.get("total_requests_all_time") or 0)
            self.total_searches_all_time = int(raw.get("total_searches_all_time") or 0)
            self.total_streams_all_time = int(raw.get("total_streams_all_time") or 0)
            self.qobuz_streams_served = int(raw.get("qobuz_streams_served") or 0)
            self.tidal_streams_served = int(raw.get("tidal_streams_served") or 0)
            # Older files only had failovers_count. Treat it as the
            # failover-resolution total so the public alias stays truthful.
            self.failover_resolutions = int(
                raw.get("failover_resolutions", raw.get("failovers_count")) or 0
            )
            self.failover_streams_served = int(
                raw.get("failover_streams_served") or 0
            )
            self.last_failover_at = int(raw.get("last_failover_at") or 0)
            self.precache_resolutions = int(raw.get("precache_resolutions") or 0)
            for k, v in (raw.get("failover_reasons") or {}).items():
                self.failover_reasons[str(k)] = int(v)
            for k, v in (raw.get("quality_tiers") or {}).items():
                self.quality_tiers[str(k)] = int(v)
            self.quality_tiers.setdefault("dolby_atmos", 0)
            observed = raw.get("observed_quality") or {}
            if isinstance(observed, dict):
                for provider, value in observed.items():
                    if isinstance(value, dict):
                        self.observed_quality[str(provider)] = dict(value)
            users = raw.get("all_time_users") or []
            if isinstance(users, list):
                self._all_time_users = {str(u) for u in users[:_MAX_CLIENTS]}
            # Older files only carry the capped set, so the counter starts from
            # it once and then only grows.
            self._all_time_user_count = max(
                int(raw.get("all_time_user_count") or 0),
                len(self._all_time_users),
            )
            last_seen = raw.get("user_last_seen") or {}
            now = time.time()
            if isinstance(last_seen, dict):
                for u, ts in last_seen.items():
                    if isinstance(ts, (int, float)) and now - ts <= _ACTIVE_24H_SEC:
                        self._user_last_seen[str(u)] = float(ts)
            streamer_last_seen = raw.get("streamer_last_seen") or {}
            if isinstance(streamer_last_seen, dict):
                for u, ts in streamer_last_seen.items():
                    if isinstance(ts, (int, float)) and now - ts <= _ACTIVE_24H_SEC:
                        self._streamer_last_seen[str(u)] = float(ts)
            hourly = raw.get("hourly") or {}
            cur_hour = int(now // 3600)
            if isinstance(hourly, dict):
                for h_str, vals in hourly.items():
                    if str(h_str).isdigit() and isinstance(vals, dict):
                        h_int = int(h_str)
                        if cur_hour - h_int <= 48:
                            req_v = int(vals.get("req") or 0)
                            ok_v = int(vals.get("ok") or 0)
                            c_err = int(vals.get("client_err") or 0)
                            err_v = int(vals.get("err") or 0)
                            # Normal BitChord 401 auth handshakes were previously counted in client_err;
                            # restore them to ok so timeline reflects true service health
                            if c_err > 0 and (ok_v + c_err + err_v == req_v):
                                ok_v += c_err
                                c_err = 0
                            self._hourly[h_int] = {
                                "req": req_v,
                                "ok": ok_v,
                                "client_err": c_err,
                                "err": err_v,
                            }
            daily = raw.get("daily") or {}
            cur_day = int(now // 86400)
            if isinstance(daily, dict):
                for d_str, vals in daily.items():
                    if str(d_str).isdigit() and isinstance(vals, dict):
                        d_int = int(d_str)
                        if cur_day - d_int <= 90:
                            self._daily[d_int] = {
                                "req": int(vals.get("req") or 0),
                                "ok": int(vals.get("ok") or 0),
                                "client_err": int(vals.get("client_err") or 0),
                                "err": int(vals.get("err") or 0),
                            }
            incidents = raw.get("incidents") or []
            if isinstance(incidents, list):
                for ev in incidents[:25]:
                    if isinstance(ev, dict) and ev.get("at"):
                        at_val = int(ev.get("at") or 0)
                        msg = _sanitize_incident_message(
                            str(ev.get("detail") or ev.get("message") or "")
                        )
                        self._incidents.append(
                            {
                                "at": at_val,
                                "started_at": int(ev.get("started_at") or at_val),
                                "ended_at": int(ev.get("ended_at") or at_val),
                                "count": max(1, int(ev.get("count") or 1)),
                                "kind": str(ev.get("kind") or "event"),
                                "provider": str(
                                    ev.get("provider") or _infer_incident_provider(msg)
                                ),
                                "detail": msg,
                            }
                        )
        except Exception as exc:
            log.warning("Could not load persisted stats from %s: %s", self.persist_path, exc)

    def save(self, force: bool = False) -> None:
        """Snapshot in memory, then write the file off the event loop.

        The payload is built here — the event loop owns these dicts — as plain
        copies; the atomic file write is the only blocking part and runs in a
        worker thread when a loop is running.
        """
        if self.persist_path is None:
            return
        now_mono = time.monotonic()
        if not force and (not self._dirty or now_mono - self._last_save_at < 15.0):
            return
        try:
            now = time.time()
            cur_hour = int(now // 3600)
            # Prune old 24h entries before saving
            self._user_last_seen = {
                u: ts
                for u, ts in self._user_last_seen.items()
                if now - ts <= _ACTIVE_24H_SEC
            }
            self._streamer_last_seen = {
                u: ts
                for u, ts in self._streamer_last_seen.items()
                if now - ts <= _ACTIVE_24H_SEC
            }
            cur_day = int(now // 86400)
            self._hourly = {
                h: v for h, v in self._hourly.items() if cur_hour - h <= 48
            }
            self._daily = {
                d: v for d, v in self._daily.items() if cur_day - d <= 90
            }
            payload = {
                "total_requests_all_time": self.total_requests_all_time,
                "total_searches_all_time": self.total_searches_all_time,
                "total_streams_all_time": self.total_streams_all_time,
                "qobuz_streams_served": self.qobuz_streams_served,
                "tidal_streams_served": self.tidal_streams_served,
                "failover_resolutions": self.failover_resolutions,
                "failover_streams_served": self.failover_streams_served,
                "failover_reasons": dict(self.failover_reasons),
                "last_failover_at": self.last_failover_at,
                "precache_resolutions": self.precache_resolutions,
                "quality_tiers": dict(self.quality_tiers),
                "observed_quality": {
                    key: dict(value) for key, value in self.observed_quality.items()
                },
                "all_time_users": list(self._all_time_users)[:_MAX_CLIENTS],
                "all_time_user_count": self._all_time_user_count,                "user_last_seen": self._user_last_seen,
                "streamer_last_seen": self._streamer_last_seen,
                "hourly": {str(h): dict(v) for h, v in self._hourly.items()},
                "daily": {str(d): dict(v) for d, v in self._daily.items()},
                "incidents": [dict(item) for item in self._incidents[:25]],
                "updated_at": int(now),
            }
        except Exception as exc:
            log.warning("Failed to build stats snapshot for %s: %s", self.persist_path, exc)
            return
        self._dirty = False
        self._last_save_at = now_mono
        self._pending_payload = payload
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_payload()
        else:
            loop.create_task(asyncio.to_thread(self._write_payload))

    def _write_payload(self) -> None:
        """Atomic replace of stats.json; runs in a worker thread when deferred."""
        with self._write_lock:
            payload = self._pending_payload
            if payload is None or self.persist_path is None:
                return
            try:
                self.persist_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.persist_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
                os.replace(tmp, self.persist_path)
            except Exception as exc:
                log.warning("Failed to persist stats to %s: %s", self.persist_path, exc)

    def begin(self, endpoint: str, client: str, *, addon: bool = False) -> None:
        self.requests += 1
        self.by_endpoint[endpoint] += 1

        if addon:
            self._addon_requests_session += 1
            self.total_requests_all_time += 1
            if endpoint == "search":
                self.total_searches_all_time += 1
        else:
            self.monitor_requests += 1

        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        self._dirty = True

    def record_client(self, subject: str, endpoint: str) -> None:
        """Record an authenticated (or auth-disabled) addon client.

        Dashboard/admin visitors never reach this method, so they cannot
        inflate ``clients`` or ``total_all_time``.
        """
        if not subject:
            return
        if subject not in self.clients:
            if len(self.clients) < _MAX_CLIENTS:
                self.clients.add(subject)
            else:
                self.clients_truncated = True

        h = _hash_subject(subject)
        now = time.time()
        self._user_last_seen[h] = now
        if endpoint in ("stream", "dash"):
            self._streamer_last_seen[h] = now
        # The set is only a dedupe window and is capped for memory; the public
        # all-time number is the counter, so it must not freeze at the cap.
        if h not in self._all_time_users:
            self._all_time_users.add(h)
            self._all_time_user_count += 1
            if len(self._all_time_users) > _MAX_CLIENTS:
                # Drop an arbitrary oldest-ish entry by pop(); membership is
                # already recorded in the counter and recent activity maps.
                self._all_time_users.pop()
        self._dirty = True

    def end(
        self,
        status: int,
        elapsed_ms: float | None = None,
        *,
        addon: bool = False,
        endpoint: str = "",
    ) -> None:
        self.statuses[status] += 1
        if endpoint and addon:
            # Which endpoint produced which status, so a flood can be attributed
            # without guessing from aggregate counts. Bounded by endpoint name.
            bucket = self.endpoint_status.setdefault(endpoint, {})
            bucket[str(status)] = bucket.get(str(status), 0) + 1
        self.in_flight = max(0, self.in_flight - 1)

        if addon:
            if elapsed_ms is not None and elapsed_ms >= 0:
                clamped_ms = min(elapsed_ms, 2500.0)
                if self.avg_response_ms == 0 or self.avg_response_ms > 2000:
                    self.avg_response_ms = round(clamped_ms, 1)
                else:
                    self.avg_response_ms = round(
                        0.9 * self.avg_response_ms + 0.1 * clamped_ms, 1
                    )
            now_ts = int(time.time())
            cur_hour = now_ts // 3600
            cur_day = now_ts // 86400
            bucket = self._hourly.get(cur_hour)
            if bucket is None:
                bucket = {"req": 0, "ok": 0, "client_err": 0, "err": 0}
                self._hourly[cur_hour] = bucket
            day_bucket = self._daily.get(cur_day)
            if day_bucket is None:
                day_bucket = {"req": 0, "ok": 0, "client_err": 0, "err": 0}
                self._daily[cur_day] = day_bucket
            bucket["req"] += 1
            day_bucket["req"] += 1
            if 200 <= status < 400 or status == 401:
                # Normal BitChord 401 auth challenge handshakes are part of protocol negotiation, not errors
                bucket["ok"] += 1
                day_bucket["ok"] += 1
            elif 400 <= status < 500:
                bucket["client_err"] += 1
                day_bucket["client_err"] += 1
            else:
                bucket["err"] += 1
                day_bucket["err"] += 1
                self._record_incident("upstream_error", f"HTTP {status} on addon route", now_ts)
            self._dirty = True
        elif elapsed_ms is not None and elapsed_ms >= 0:
            if self.monitor_avg_response_ms == 0:
                self.monitor_avg_response_ms = round(elapsed_ms, 1)
            else:
                self.monitor_avg_response_ms = round(
                    0.85 * self.monitor_avg_response_ms + 0.15 * elapsed_ms, 1
                )

        self.save(force=False)

    def record_stream_served(
        self,
        provider: str,
        quality_label: str = "",
        failover: bool = False,
        failover_reason: str = "",
        bit_depth: int | None = None,
        sample_rate: int | None = None,
        is_atmos: bool = False,
        codec: str = "",
    ) -> None:
        """Record one successful ``/stream`` response (cache hit or miss)."""
        if provider == "qobuz":
            self.qobuz_streams_served += 1
        elif provider == "tidal":
            self.tidal_streams_served += 1
        self.total_streams_all_time = self.qobuz_streams_served + self.tidal_streams_served
        if failover:
            self.failover_streams_served += 1
            self.last_failover_at = int(time.time())
        tier = _quality_tier(
            quality_label,
            bit_depth,
            sample_rate,
            is_atmos=is_atmos,
            codec=codec,
        )
        self.quality_tiers[tier] += 1
        self._observe_quality(provider, quality_label, bit_depth, sample_rate)
        self._dirty = True

    def _record_incident(self, kind: str, detail: str, now_ts: int | None = None) -> None:
        ts = int(now_ts if now_ts is not None else time.time())
        rounded = ts - (ts % 60)
        clean_detail = _sanitize_incident_message(detail)
        severity = _incident_severity(kind, clean_detail)
        # A catalogue miss is routing, not trouble, and it repeats for as long as
        # someone keeps playing tracks Qobuz does not carry — so identical
        # routing events are folded for half an hour instead of five minutes.
        window = 1800 if severity == "info" else 300
        if self._incidents:
            top = self._incidents[0]
            start_ts = int(top.get("started_at") or top.get("at") or 0)
            if (
                top.get("kind") == kind
                and top.get("detail") == clean_detail
                and 0 <= rounded - start_ts < window
            ):
                top["at"] = rounded
                top["ended_at"] = rounded
                top["count"] = int(top.get("count") or 1) + 1
                return
        self._incidents.insert(
            0,
            {
                "at": rounded,
                "started_at": rounded,
                "ended_at": rounded,
                "count": 1,
                "kind": kind,
                "severity": severity,
                "provider": _infer_incident_provider(clean_detail),
                "detail": clean_detail,
            },
        )
        if len(self._incidents) > 25:
            del self._incidents[25:]

    def recent_incidents(self, limit: int = 25) -> list[dict[str, Any]]:
        n = max(1, min(int(limit), 25))
        out: list[dict[str, Any]] = []
        merged: dict[str, dict[str, Any]] = {}
        for ev in self._incidents:
            at = int(ev.get("at") or 0)
            kind = str(ev.get("kind") or "event")
            msg = _sanitize_incident_message(
                str(ev.get("detail") or ev.get("message") or "")
            )
            msg = _LEGACY_INCIDENT_TEXT.get(msg, msg)
            # Derived on read too, so a ring written before severities existed
            # still classifies honestly.
            severity = str(ev.get("severity") or "") or _incident_severity(kind, msg)
            ended_at = int(ev.get("ended_at") or at)
            count = max(1, int(ev.get("count") or 1))

            # Routing repeats are one condition, not many events: a catalogue
            # miss stays true for as long as people play tracks the engine does
            # not carry, so identical routing lines fold into one row that keeps
            # the count and the latest time. Faults are left alone — their
            # spacing is the information.
            if severity == "info":
                key = f"{kind}\u0000{msg}"
                seen = merged.get(key)
                if seen is not None:
                    seen["count"] = int(seen["count"]) + count
                    seen["at"] = max(int(seen["at"]), at)
                    seen["ended_at"] = max(int(seen["ended_at"]), ended_at)
                    continue

            row = {
                "kind": kind,
                "severity": severity,
                "provider": str(ev.get("provider") or _infer_incident_provider(msg)),
                "message": msg,
                "at": at,
                "started_at": int(ev.get("started_at") or at),
                "ended_at": ended_at,
                "count": count,
            }
            if severity == "info":
                merged[f"{kind}\u0000{msg}"] = row
            out.append(row)
            if len(out) >= n:
                break
        return out

    def record_failover(self, reason: str = "") -> None:
        """Record one fallback resolution (search or stream)."""
        self.failover_resolutions += 1
        clean = reason or "provider failover"
        self.failover_reasons[clean] += 1
        now_ts = int(time.time())
        self.last_failover_at = now_ts
        self._record_incident("failover", clean, now_ts)
        self._dirty = True

    def record_precache_resolution(self) -> None:
        self.precache_resolutions += 1
        self._dirty = True

    def _observe_quality(
        self,
        provider: str,
        quality_label: str,
        bit_depth: int | None,
        sample_rate: int | None,
    ) -> None:
        if not provider:
            return
        candidate = {
            "bit_depth": int(bit_depth) if bit_depth else None,
            "sample_rate": int(sample_rate) if sample_rate else None,
            "quality_label": quality_label or "",
        }
        previous = self.observed_quality.get(provider)
        if previous is None:
            self.observed_quality[provider] = candidate
            return
        old_key = (
            int(previous.get("bit_depth") or 0),
            int(previous.get("sample_rate") or 0),
        )
        new_key = (
            int(candidate.get("bit_depth") or 0),
            int(candidate.get("sample_rate") or 0),
        )
        if new_key > old_key:
            self.observed_quality[provider] = candidate

    def search_cache(self, hit: bool) -> None:
        if hit:
            self.search_cache_hits += 1
        else:
            self.search_cache_misses += 1

    def stream_cache(self, hit: bool) -> None:
        if hit:
            self.stream_cache_hits += 1
        else:
            self.stream_cache_misses += 1

    def active_user_counts(self) -> dict[str, int]:
        now = time.time()
        active_now = 0
        active_1h = 0
        active_24h = 0
        for ts in list(self._user_last_seen.values()):
            age = now - ts
            if age <= _ACTIVE_NOW_SEC:
                active_now += 1
            if age <= _ACTIVE_1H_SEC:
                active_1h += 1
            if age <= _ACTIVE_24H_SEC:
                active_24h += 1
        active_streamers_5m = sum(
            1
            for ts in self._streamer_last_seen.values()
            if now - ts <= _ACTIVE_STREAMERS_SEC
        )
        return {
            "active_now": active_now,
            "active_now_window_seconds": _ACTIVE_NOW_SEC,
            "active_streamers_5m": active_streamers_5m,
            "active_streamers_window_seconds": _ACTIVE_STREAMERS_SEC,
            "active_1h": active_1h,
            "active_24h": active_24h,
            "total_all_time": self._all_time_user_count,
        }

    def hourly_timeline(self, hours: int = 24) -> list[dict[str, Any]]:
        cur_hour = int(time.time() // 3600)
        timeline: list[dict[str, Any]] = []
        for offset in range(hours - 1, -1, -1):
            h = cur_hour - offset
            b = self._hourly.get(h, {"req": 0, "ok": 0, "client_err": 0, "err": 0})
            req = b["req"]
            ok = b["ok"]
            client_err = b.get("client_err", 0)
            err = b["err"]
            status = "idle" if req == 0 else "up"
            if req > 0 and err / req > 0.5:
                status = "down"
            elif req > 0 and err > 0:
                status = "degraded"
            elif (
                req >= _CLIENT_ERR_MIN_REQUESTS
                and client_err / req >= _CLIENT_ERR_RATIO
            ):
                status = "degraded"
            timeline.append(
                {
                    "hour_epoch": h * 3600,
                    "requests": req,
                    "total": req,
                    "ok": ok,
                    "client_err": client_err,
                    "errors": err,
                    "status": status,
                }
            )
        return timeline

    def daily_timeline(self, days: int = 30) -> dict[str, Any]:
        """Return 30-day daily reliability rollup and recent incident log."""
        cur_day = int(time.time() // 86400)
        slots: list[dict[str, Any]] = []
        sum_req = 0
        sum_non_server_err = 0

        # If _daily has no bucket for today/yesterday yet (e.g. upgraded from older stats.json),
        # synthesize from _hourly so existing deployments immediately populate their recent days.
        hourly_by_day: dict[int, dict[str, int]] = {}
        for h_epoch, b in self._hourly.items():
            d_idx = h_epoch // 24
            agg = hourly_by_day.setdefault(d_idx, {"req": 0, "ok": 0, "client_err": 0, "err": 0})
            agg["req"] += int(b.get("req") or 0)
            agg["ok"] += int(b.get("ok") or 0)
            agg["client_err"] += int(b.get("client_err") or 0)
            agg["err"] += int(b.get("err") or 0)

        for offset in range(max(1, days) - 1, -1, -1):
            d = cur_day - offset
            b = self._daily.get(d) or hourly_by_day.get(d) or {"req": 0, "ok": 0, "client_err": 0, "err": 0}
            req = int(b.get("req") or 0)
            ok = int(b.get("ok") or 0)
            client_err = int(b.get("client_err") or 0)
            err = int(b.get("err") or 0)
            sum_req += req
            sum_non_server_err += max(0, req - err)

            status = "idle" if req == 0 else "up"
            if req > 0 and err / req > 0.5:
                status = "down"
            elif req > 0 and err > 0:
                status = "degraded"

            uptime_pct = round((max(0, req - err) / req) * 100.0, 2) if req > 0 else None
            slots.append(
                {
                    "day_epoch": d * 86400,
                    "requests": req,
                    "ok": ok,
                    "client_err": client_err,
                    "errors": err,
                    "status": status,
                    "uptime_pct": uptime_pct,
                }
            )

        overall = round((sum_non_server_err / sum_req) * 100.0, 2) if sum_req > 0 else None
        return {
            "days": slots,
            "total_requests_30d": sum_req,
            "uptime_percent": overall,
            "incidents": self.recent_incidents(12),
        }

    def snapshot(self) -> dict[str, object]:
        users = self.active_user_counts()
        total_cache_hits = self.search_cache_hits + self.stream_cache_hits
        total_cache_ops = (
            total_cache_hits + self.search_cache_misses + self.stream_cache_misses
        )
        hit_rate = (
            round((total_cache_hits / total_cache_ops) * 100.0, 1)
            if total_cache_ops > 0
            else 0.0
        )
        total_streams = self.qobuz_streams_served + self.tidal_streams_served
        return {
            "uptime_seconds": int(time.monotonic() - self.started_at),
            "requests": self.requests,
            "total_requests_all_time": max(
                self.total_requests_all_time, self._addon_requests_session
            ),
            "total_searches_all_time": max(
                self.total_searches_all_time, self.by_endpoint.get("search", 0)
            ),
            "total_streams_all_time": total_streams,
            "streams_served": total_streams,
            "monitor_requests": self.monitor_requests,
            "monitor_avg_response_ms": self.monitor_avg_response_ms,
            "by_endpoint": dict(self.by_endpoint),
            "statuses": {str(code): count for code, count in self.statuses.items()},
            "endpoint_status": {
                endpoint: dict(codes) for endpoint, codes in self.endpoint_status.items()
            },
            "clients": len(self.clients),
            "clients_truncated": self.clients_truncated,
            "users": users,
            "in_flight": max(0, min(self.in_flight, 25)),
            "peak_in_flight": max(0, min(self.peak_in_flight, 50)),
            "avg_response_ms": self.avg_response_ms,
            "providers": {
                "qobuz_streams": self.qobuz_streams_served,
                "tidal_streams": self.tidal_streams_served,
                "streams_served": total_streams,
                "failovers": self.failover_resolutions,
                "failover_resolutions": self.failover_resolutions,
                "failover_streams_served": self.failover_streams_served,
                "failover_reasons": dict(self.failover_reasons),
                "last_failover_at": self.last_failover_at,
            },
            "quality_tiers": dict(self.quality_tiers),
            "observed_quality": self.observed_quality,
            "precache_resolutions": self.precache_resolutions,
            "cache": {
                "hit_rate_percent": hit_rate,
                "search": {
                    "hits": self.search_cache_hits,
                    "misses": self.search_cache_misses,
                },
                "stream": {
                    "hits": self.stream_cache_hits,
                    "misses": self.stream_cache_misses,
                },
            },
            "client_rate_limited": self.client_rate_limited,
            "qobuz_errors": dict(self.qobuz_errors),
            "tidal_errors": dict(self.tidal_errors),
        }
