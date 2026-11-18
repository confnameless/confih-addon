"""Isolated server harness, deterministic seeders, and Playwright worker fixtures."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import zlib
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = REPO_ROOT / ".venv" / "bin" / "python"
PW_WORKER_PATH = Path(__file__).resolve().parent / "pw_worker.mjs"


def _make_1x1_png(r: int = 32, g: int = 178, b: int = 170, a: int = 255) -> bytes:
    """Build a valid 1x1 RGBA PNG using stdlib struct + zlib (zero external dependencies)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    raw = bytes([0, r, g, b, a])
    idat = zlib.compress(raw)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# Valid 1x1 dark-teal PNG for offline /art/{key} serving (zero external network calls).
_VALID_1X1_PNG = _make_1x1_png()

_SEEDED_COVERS = (
    "https://static.qobuz.com/images/covers/e2e_cover_1.jpg",
    "https://static.qobuz.com/images/covers/e2e_cover_2.jpg",
    "https://static.qobuz.com/images/covers/e2e_cover_3.jpg",
)

DEFAULT_PATH_SECRET = "e2e_addon_secret_0123456789abcdef"
DEFAULT_STATS_TOKEN = "e2e_stats_token_0123456789abcdef"
DEFAULT_ADMIN_SECRET = "e2e_admin_vault_0123456789abcdef"

_ACTIVE_PROCS: set[subprocess.Popen[Any]] = set()
_PROCS_LOCK = threading.Lock()


def _cleanup_dangling_procs() -> None:
    with _PROCS_LOCK:
        procs = list(_ACTIVE_PROCS)
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass


atexit.register(_cleanup_dangling_procs)


def find_free_port() -> int:
    """Allocate an ephemeral free TCP port on 127.0.0.1 (never port 8899)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    assert port != 8899
    return port


def artwork_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def seed_test_data(
    data_dir: Path,
    *,
    seed_plays: bool = True,
    seed_artwork: bool = True,
    seed_stats: bool = True,
) -> dict[str, Any]:
    """Populate isolated DATA_DIR with time.time()-relative plays, stats, and offline artwork."""
    data_dir.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    summary: dict[str, Any] = {"artwork_keys": [], "tracks": []}

    if seed_artwork:
        art_dir = data_dir / "artwork"
        art_dir.mkdir(parents=True, exist_ok=True)
        for url in _SEEDED_COVERS:
            key = artwork_key(url)
            (art_dir / f"{key}.json").write_text(
                json.dumps({"url": url, "at": now - 120, "type": "image/png"}),
                encoding="utf-8",
            )
            (art_dir / f"{key}.img").write_bytes(_VALID_1X1_PNG)
            summary["artwork_keys"].append(key)

    if seed_plays:
        cur_hour = now - (now % 3600)
        cur_min = now - (now % 60)
        tracks_spec = [
            ("1001", "Instant Crush", "Daft Punk", "Random Access Memories", _SEEDED_COVERS[0], 42, {"hires_24bit": 30, "lossless_16bit": 12}, 18),
            ("1002", "Teardrop", "Massive Attack", "Mezzanine", _SEEDED_COVERS[1], 31, {"lossless_16bit": 25, "high_320": 6}, 14),
            ("1003", "Bad Guy", "Billie Eilish", "When We All Fall Asleep", _SEEDED_COVERS[2], 24, {"dolby_atmos": 16, "hires_24bit": 8}, 11),
            ("1004", "Weightless", "Marconi Union", "Weightless", None, 19, {"lossless_16bit": 14, "high_320": 5}, 9),
            ("1005", "Clair de Lune", "Claude Debussy", "Suite Bergamasque", _SEEDED_COVERS[0], 15, {"hires_24bit": 15}, 7),
            ("1006", "Money", "Pink Floyd", "The Dark Side of the Moon", _SEEDED_COVERS[1], 12, {"dolby_atmos": 7, "hires_24bit": 5}, 6),
            ("1007", "Midnight City", "M83", "Hurry Up, We're Dreaming", _SEEDED_COVERS[2], 9, {"high_320": 9}, 5),
            ("1008", "Hotel California", "Eagles", "Hotel California", _SEEDED_COVERS[0], 6, {"hires_24bit": 4, "lossless_16bit": 2}, 4),
        ]
        tracks_json = []
        for idx, (tid, title, artist, album, art, plays, qual, listeners) in enumerate(tracks_spec):
            tracks_json.append(
                {
                    "id": tid,
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "artwork": art,
                    "plays": plays,
                    "quality": qual,
                    "listeners": listeners,
                    "first_seen": now - 86400 + idx * 300,
                    "last_seen": now - idx * 120,
                }
            )
            summary["tracks"].append({"id": tid, "title": title, "artist": artist})

        recent_json = [
            {
                "id": "1001",
                "title": "Instant Crush",
                "artist": "Daft Punk",
                "artwork": _SEEDED_COVERS[0],
                "tier": "hires_24bit",
                "at": cur_min - 60,
            },
            {
                "id": "1003",
                "title": "Bad Guy",
                "artist": "Billie Eilish",
                "artwork": _SEEDED_COVERS[2],
                "tier": "dolby_atmos",
                "at": cur_min - 180,
            },
            {
                "id": "1004",
                "title": "Weightless",
                "artist": "Marconi Union",
                "artwork": None,
                "tier": "lossless_16bit",
                "at": cur_min - 300,
            },
            {
                "id": "1002",
                "title": "Teardrop",
                "artist": "Massive Attack",
                "artwork": _SEEDED_COVERS[1],
                "tier": "lossless_16bit",
                "at": cur_min - 420,
            },
            {
                "id": "1007",
                "title": "Midnight City",
                "artist": "M83",
                "artwork": _SEEDED_COVERS[2],
                "tier": "high_320",
                "at": cur_min - 600,
            },
        ]

        hourly_json = []
        for offset in range(23, -1, -1):
            h_epoch = cur_hour - (offset * 3600)
            hourly_json.append(
                {
                    "hour": h_epoch,
                    "tiers": {
                        "dolby_atmos": 1 if offset % 4 == 0 else 0,
                        "hires_24bit": 2 + (offset % 3),
                        "lossless_16bit": 1 + (offset % 2),
                        "high_320": 1 if offset % 3 == 0 else 0,
                    },
                }
            )

        total_plays = sum(t["plays"] for t in tracks_json)
        (data_dir / "plays.json").write_text(
            json.dumps(
                {
                    "total": total_plays,
                    "tracks": tracks_json,
                    "recent": recent_json,
                    "hourly": hourly_json,
                }
            ),
            encoding="utf-8",
        )
        summary["total_plays"] = total_plays
        summary["total_tracks"] = len(tracks_json)

    if seed_stats:
        cur_hour_idx = int(now // 3600)
        cur_day_idx = int(now // 86400)
        hourly_stats = {
            str(cur_hour_idx - offset): {
                "req": 18 + (offset % 7),
                "ok": 17 + (offset % 7),
                "client_err": 1 if offset % 5 == 0 else 0,
                "err": 0,
            }
            for offset in range(24)
        }
        daily_stats = {
            str(cur_day_idx - offset): {
                "req": 240 + (offset * 3),
                "ok": 238 + (offset * 3),
                "client_err": 2 if offset % 4 == 0 else 0,
                "err": 1 if offset == 7 else 0,
            }
            for offset in range(30)
        }
        (data_dir / "stats.json").write_text(
            json.dumps(
                {
                    "total_requests_all_time": 7420,
                    "total_searches_all_time": 1890,
                    "total_streams_all_time": 158,
                    "qobuz_streams_served": 112,
                    "tidal_streams_served": 46,
                    "failover_resolutions": 14,
                    "failover_streams_served": 12,
                    "last_failover_at": now - 7200,
                    "precache_resolutions": 28,
                    "failover_reasons": {"geo_blocked": 5, "token_expired": 9},
                    "quality_tiers": {
                        "dolby_atmos": 23,
                        "hires_24bit": 62,
                        "lossless_16bit": 53,
                        "high_320": 20,
                    },
                    "observed_quality": {
                        "qobuz": {"max_bit_depth": 24, "max_sample_rate": 192000},
                        "tidal": {"max_bit_depth": 24, "max_sample_rate": 96000},
                    },
                    "all_time_users": [f"anon_client_{i}" for i in range(42)],
                    "user_last_seen": {f"anon_client_{i}": float(now - i * 25) for i in range(6)},
                    "streamer_last_seen": {f"anon_client_{i}": float(now - i * 40) for i in range(4)},
                    "hourly": hourly_stats,
                    "daily": daily_stats,
                    "incidents": [
                        {
                            "at": now - 7200,
                            "kind": "failover",
                            "detail": "Primary Qobuz route rotated to Tidal standby",
                        },
                        {
                            "at": now - 86400,
                            "kind": "recovery",
                            "detail": "Primary gateway pool healthy",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

    return summary


def build_isolated_env(data_dir: Path, overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal sanitized environment preventing repo .env inheritance."""
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(data_dir),
        "PYTHONPATH": str(REPO_ROOT),
        "DATA_DIR": str(data_dir),
        "QOBUZ_SELF_CHECK": "false",
        "QOBUZ_TOKEN_CHECK_INTERVAL_SECONDS": "0",
        "STATS_LOG_INTERVAL_SECONDS": "0",
        "LOG_LEVEL": "WARNING",
        "PUBLIC_PLAYS": "true",
        "DONATE_DEMO": "false",
        "ADDON_PATH_SECRET": "",
        "ADMIN_PATH_SECRET": "",
        "STATS_TOKEN": "",
        "PUBLIC_BASE_URL": "",
        "PRIMARY_PROVIDER": "tidal",
        "ADDON_AUTH_ENABLED": "false",
        "ADDON_HOSTS": "",
        "RATE_LIMIT_PER_MINUTE": "600",
        "ENABLE_DOCS": "false",
    }
    if overrides:
        env.update(overrides)
    return env


class IsolatedUvicornServer:
    """Spawn and manage an isolated uvicorn subprocess on an ephemeral 127.0.0.1 port."""

    def __init__(
        self,
        env_overrides: dict[str, str] | None = None,
        *,
        seed_plays: bool = True,
        seed_artwork: bool = True,
        seed_stats: bool = True,
        startup_timeout: float = 12.0,
    ) -> None:
        self.env_overrides = dict(env_overrides or {})
        self.seed_plays = seed_plays
        self.seed_artwork = seed_artwork
        self.seed_stats = seed_stats
        self.startup_timeout = startup_timeout
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self.data_dir: Path | None = None
        self.port: int = 0
        self.base_url: str = ""
        self.seed_summary: dict[str, Any] = {}
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_fh: Any = None
        self.log_path: Path | None = None

    def __enter__(self) -> IsolatedUvicornServer:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="confih_e2e_")
        self.data_dir = Path(self._tmpdir.name)
        self.seed_summary = seed_test_data(
            self.data_dir,
            seed_plays=self.seed_plays,
            seed_artwork=self.seed_artwork,
            seed_stats=self.seed_stats,
        )
        self.port = find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log_path = self.data_dir / "uvicorn.log"
        self._log_fh = self.log_path.open("wb")
        env = build_isolated_env(self.data_dir, self.env_overrides)

        cmd = [
            str(PYTHON_BIN),
            "-m",
            "uvicorn",
            "app.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--log-level",
            "warning",
        ]
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self.data_dir),
            env=env,
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
        )
        with _PROCS_LOCK:
            _ACTIVE_PROCS.add(self._proc)

        try:
            self._wait_ready()
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def _wait_ready(self) -> None:
        assert self._proc is not None
        deadline = time.monotonic() + self.startup_timeout
        health_url = f"{self.base_url}/healthz"
        with httpx.Client(timeout=1.0) as client:
            while time.monotonic() < deadline:
                code = self._proc.poll()
                if code is not None:
                    log_text = self.read_log()
                    raise RuntimeError(
                        f"uvicorn exited prematurely with code {code}.\nLog:\n{log_text}"
                    )
                try:
                    resp = client.get(health_url)
                    if resp.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
        log_text = self.read_log()
        raise RuntimeError(
            f"uvicorn failed to become ready at {health_url} within {self.startup_timeout}s.\nLog:\n{log_text}"
        )

    def read_log(self) -> str:
        if self.log_path and self.log_path.exists():
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        return ""

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        proc = self._proc
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
            with _PROCS_LOCK:
                _ACTIVE_PROCS.discard(proc)
            self._proc = None
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None
        if self._tmpdir is not None:
            try:
                self._tmpdir.cleanup()
            except Exception:
                pass
            self._tmpdir = None


def probe_boot_failure(env_overrides: dict[str, str], timeout: float = 8.0) -> subprocess.CompletedProcess[str]:
    """Run app.main:create_app once in an isolated temp dir to verify config validation rejects boot."""
    with tempfile.TemporaryDirectory(prefix="confih_boot_fail_") as tmp:
        data_dir = Path(tmp)
        env = build_isolated_env(data_dir, env_overrides)
        return subprocess.run(
            [
                str(PYTHON_BIN),
                "-c",
                "from app.main import create_app; create_app()",
            ],
            cwd=tmp,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


class PlaywrightWorker:
    """Persistent Node ESM playwright-core worker communicating over NDJSON stdio."""

    def __init__(self, worker_path: Path = PW_WORKER_PATH) -> None:
        self.worker_path = worker_path
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            ["node", str(self.worker_path)],
            cwd=str(REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        with _PROCS_LOCK:
            _ACTIVE_PROCS.add(self._proc)
        try:
            # Verify worker handshake / Chromium readiness immediately.
            res = self.call("ping")
            if not res.get("ok"):
                raise RuntimeError(f"Playwright worker failed ping: {res}")
        except Exception:
            self.close()
            raise

    def call(self, cmd: str, **payload: Any) -> dict[str, Any]:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                stderr = ""
                if self._proc and self._proc.stderr:
                    stderr = self._proc.stderr.read()
                raise RuntimeError(f"Playwright worker is not running. stderr={stderr}")
            req_id = self._next_id
            self._next_id += 1
            msg = {"id": req_id, "cmd": cmd, **payload}
            assert self._proc.stdin is not None and self._proc.stdout is not None
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
            if not line:
                stderr = self._proc.stderr.read() if self._proc.stderr else ""
                raise RuntimeError(
                    f"Playwright worker closed stdout unexpectedly (cmd={cmd}). stderr={stderr}"
                )
            resp: dict[str, Any] = json.loads(line)
            if resp.get("id") != req_id:
                raise RuntimeError(f"Out-of-sync RPC id: expected {req_id}, got {resp.get('id')}")
            return resp

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin is not None:
                proc.stdin.write(json.dumps({"id": 0, "cmd": "shutdown"}) + "\n")
                proc.stdin.flush()
                proc.stdin.close()
                proc.wait(timeout=5.0)
        except Exception:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.0)
        finally:
            with _PROCS_LOCK:
                _ACTIVE_PROCS.discard(proc)
            self._proc = None


@pytest.fixture(scope="session")
def std_server() -> Generator[IsolatedUvicornServer, None, None]:
    """Standard public gateway server (PUBLIC_PLAYS=true, DONATE_DEMO=true, seeded)."""
    with IsolatedUvicornServer(
        {"PUBLIC_PLAYS": "true", "DONATE_DEMO": "true"},
        seed_plays=True,
        seed_artwork=True,
        seed_stats=True,
    ) as srv:
        yield srv


@pytest.fixture(scope="session")
def noplays_server() -> Generator[IsolatedUvicornServer, None, None]:
    """Gateway server with PUBLIC_PLAYS=false and DONATE_DEMO=false."""
    with IsolatedUvicornServer(
        {"PUBLIC_PLAYS": "false", "DONATE_DEMO": "false"},
        seed_plays=False,
        seed_artwork=False,
        seed_stats=True,
    ) as srv:
        yield srv


@pytest.fixture(scope="session")
def secret_server() -> Generator[IsolatedUvicornServer, None, None]:
    """Gateway server with ADDON_PATH_SECRET enabled."""
    with IsolatedUvicornServer(
        {
            "PUBLIC_PLAYS": "true",
            "DONATE_DEMO": "true",
            "ADDON_PATH_SECRET": DEFAULT_PATH_SECRET,
            "PUBLIC_BASE_URL": f"https://gw.example.test/s/{DEFAULT_PATH_SECRET}",
        },
        seed_plays=True,
        seed_artwork=True,
        seed_stats=True,
    ) as srv:
        yield srv


@pytest.fixture(scope="session")
def pw_worker() -> Generator[PlaywrightWorker, None, None]:
    """Session-scoped Chromium worker using playwright-core."""
    worker = PlaywrightWorker()
    try:
        worker.start()
        yield worker
    finally:
        worker.close()
