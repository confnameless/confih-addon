"""Configuration for the BitChord Qobuz addon.

Everything is environment-driven so the same image can run on a laptop, the
small Ryzen box, or the Ampere server without edits. The only required values
are a Qobuz app id/secret pair and either a user token or e-mail/password.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

_SECRET_PATH_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Addon ────────────────────────────────────────────────────────────
    addon_id: str = "app.bitchord.confih"
    addon_name: str = "Confih Hi-Res"
    addon_version: str = "2.0.0"
    public_base_url: str = ""
    addon_path_secret: str = ""
    admin_path_secret: str = ""
    addon_auth_enabled: bool = False
    addon_hosts: str = ""
    primary_provider: str = "tidal"
    # Cross-provider failover must prove it found the same recording. Keep
    # this strict by default; the escape hatch only relaxes title/duration
    # checks, it never falls back to the first unrelated search result.
    failover_strict_match: bool = True
    failover_duration_tolerance_seconds: int = Field(default=5, ge=0, le=60)
    # Optional Telegram alerts on account/token failure
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Optional bearer token for GET /stats. Empty hides the route entirely.
    stats_token: str = ""
    stats_log_interval_seconds: int = Field(default=60, ge=0)
    allow_downloads: bool = True
    check_valid_lossless: bool = False
    search_limit: int = Field(default=25, ge=1, le=50)
    log_level: str = "INFO"
    data_dir: Path = Path("./data")
    enable_docs: bool = False
    # ── Public play feed ────────────────────────────────────────────────────
    # The /plays page and /api/plays report aggregate play counts and a short
    # anonymous recent feed. Set PUBLIC_PLAYS=false to publish neither.
    public_plays: bool = True
    # Extra https hosts cover art may be fetched from, comma separated. Provider
    # CDN hosts (qobuz.com, tidal.com, ...) are always allowed. Extend this only
    # if a provider moves its artwork to a new domain.
    artwork_hosts: str = ""
    # ── Public support / donations ───────────────────────────────────────
    # Plain crypto addresses. Empty means the Support page shows an honest
    # "not configured" state instead of a placeholder address.
    donate_btc: str = ""
    donate_eth: str = ""
    donate_usdc_erc20: str = ""
    # One Solana address can receive SOL, USDC (SPL) and USDT (SPL); the same is
    # true of Polygon for USDC and USDT, so addresses are grouped per network
    # rather than repeated per token.
    donate_sol: str = ""
    donate_polygon: str = ""
    # Test deployments can opt into clearly-labelled placeholder addresses.
    donate_demo: bool = False
    # Contact routes shown on /support, /about and in the footer. Empty means
    # "derive from the host this deployment answers on" (support@gw.example.com);
    # set these three to pin them to a real mailbox.
    support_email: str = ""
    security_email: str = ""
    abuse_email: str = ""

    # Set only when a reverse proxy (Caddy, nginx) is the sole way in. The
    # proxy's X-Forwarded-For is then trusted for per-client rate limiting.
    trust_proxy_headers: bool = False
    # Set only when Cloudflare sits in front and injects CF-Ray + CF-Connecting-IP.
    trust_cf_headers: bool = False

    # ── Qobuz identity / auth ────────────────────────────────────────────
    qobuz_app_id: str = "312369995"
    qobuz_app_secret: str = ""
    qobuz_user_auth_token: str = ""
    qobuz_user_id: str = ""
    qobuz_email: str = ""
    qobuz_password: str = ""
    qobuz_password_md5: str = ""

    # ── Qobuz transport ──────────────────────────────────────────────────
    qobuz_base_url: str = "https://www.qobuz.com/api.json/0.2"
    qobuz_proxy_url: str = ""
    # Tidal does not need a geo-unlocking proxy; empty means a direct connection.
    tidal_proxy_url: str = ""
    qobuz_user_agent: str = DEFAULT_USER_AGENT
    qobuz_timeout_seconds: float = Field(default=25.0, ge=5.0, le=120.0)
    qobuz_max_retries: int = Field(default=2, ge=0, le=5)
    qobuz_rate_limit_per_second: float = Field(default=10.0, ge=0.0, le=50.0)
    qobuz_rate_limit_burst: int = Field(default=20, ge=1, le=100)
    # Maximum in-flight Qobuz API requests. Raise for bursty multi-listener
    # deployments; lower it if Qobuz starts answering 429.
    qobuz_max_concurrency: int = Field(default=16, ge=1, le=64)
    qobuz_self_check: bool = True
    qobuz_token_check_interval_seconds: int = Field(default=21_600, ge=0)
    qobuz_token_file: str = ""

    # ── Caching / limits ─────────────────────────────────────────────────
    search_cache_ttl_seconds: int = Field(default=600, ge=0, le=3_600)
    stream_cache_ttl_seconds: int = Field(default=300, ge=0, le=3_600)
    cache_max_entries: int = Field(default=4096, ge=16, le=10_000)
    rate_limit_per_minute: int = Field(default=600, ge=0, le=10_000)

    # ── Derived values ───────────────────────────────────────────────────
    @field_validator("public_base_url", "qobuz_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("qobuz_app_id", "qobuz_app_secret", "qobuz_user_auth_token", "qobuz_user_id")
    @classmethod
    def _strip_secret_whitespace(cls, value: str) -> str:
        return value.strip()

    @field_validator("addon_path_secret", "stats_token", "admin_path_secret")
    @classmethod
    def _validate_path_secret(cls, value: str) -> str:
        value = value.strip()
        if value and not _SECRET_PATH_RE.fullmatch(value):
            raise ValueError(
                "ADDON_PATH_SECRET, STATS_TOKEN and ADMIN_PATH_SECRET must be 16-128 characters of "
                "A-Z, a-z, 0-9, _ or -"
            )
        return value

    @model_validator(mode="after")
    def _validate_auth_hosts(self) -> Settings:
        if self.addon_auth_enabled and not self.hosts:
            raise ValueError(
                "ADDON_AUTH_ENABLED=true needs ADDON_HOSTS (or PUBLIC_BASE_URL) so the "
                "verifier knows which hostname BitChord signs. Example: ADDON_HOSTS=music.example.com"
            )
        return self

    @property
    def route_prefix(self) -> str:
        """Where the addon routes live, e.g. ``/s/<secret>`` or ``""``."""
        return f"/s/{self.addon_path_secret}" if self.addon_path_secret else ""

    @property
    def hosts(self) -> list[str]:
        """Hostnames the addon-auth verifier accepts, without scheme or port."""
        if self.addon_hosts.strip():
            return [
                _normalise_host(host)
                for host in self.addon_hosts.split(",")
                if host.strip()
            ]
        if self.public_base_url:
            host = urlparse(self.public_base_url).hostname
            if host:
                return [_normalise_host(host)]
        return []

    @property
    def can_password_login(self) -> bool:
        return bool(self.qobuz_email and (self.qobuz_password or self.qobuz_password_md5))

    @property
    def qobuz_configured(self) -> bool:
        return bool(
            self.qobuz_app_id
            and self.qobuz_app_secret
            and (self.qobuz_user_auth_token or self.can_password_login)
        )

    @property
    def qobuz_password_value(self) -> str:
        """The password value Qobuz expects: an MD5 hex digest.

        Qobuz's web login sends MD5(password). Users who already have a digest
        can provide ``QOBUZ_PASSWORD_MD5`` instead of the plaintext password.
        """
        import hashlib

        if self.qobuz_password_md5.strip():
            return self.qobuz_password_md5.strip().lower()
        if not self.qobuz_password:
            return ""
        return hashlib.md5(self.qobuz_password.encode("utf-8")).hexdigest()

    @property
    def token_path(self) -> Path:
        if self.qobuz_token_file.strip():
            return Path(self.qobuz_token_file).expanduser()
        return Path(self.data_dir).expanduser() / "qobuz-token.json"


def _normalise_host(host: str) -> str:
    host = host.strip().lower().strip("[]").rstrip(".")
    return re.sub(r":\d+$", "", host)
