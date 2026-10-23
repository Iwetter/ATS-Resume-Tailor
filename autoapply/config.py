"""Environment-driven settings. Secrets are read here and nowhere else."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from hunter.service import DEFAULT_DB


def _bool(name, default=False):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name, default, low=None, high=None):
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        value = default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _float(name, default=None):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    db_path: Path = DEFAULT_DB
    # Discovery cadence. 10–15 minutes is the target; 5 minutes is a hard floor.
    scan_interval: int = 720
    scan_jitter: int = 60
    listing_ttl: int = 540
    detail_ttl: int = 6 * 3600
    workday_interval: int = 3600
    scan_lease_minutes: int = 30
    days: int = 7
    # Application safety.
    allow_real_submission: bool = False
    emergency_stop_env: bool = False
    max_per_day: int = 5
    max_per_company_per_day: int = 1
    max_per_company_total: int = 3
    min_fit_review: float = 45.0
    min_fit_auto: float = 80.0
    trust_model_mappings: bool = False
    auto_accept_validated_rewrites: bool = False
    headless: bool = True
    chromium_executable: str = ""
    browser_timeout_ms: int = 30000
    # Claude.
    anthropic_api_key: str = field(default="", repr=False)
    claude_model: str = ""
    claude_max_output_tokens: int = 1800
    claude_max_input_tokens: int = 12000
    claude_max_retries: int = 2
    claude_daily_usd: float = 2.0
    claude_monthly_usd: float = 20.0
    claude_input_usd_per_mtok: float | None = None
    claude_output_usd_per_mtok: float | None = None
    claude_enabled: bool = False
    health_port: int = 0
    log_level: str = "INFO"
    # Twilio SMS (official SDK). The auth token also validates webhook signatures.
    twilio_account_sid: str = ""
    twilio_auth_token: str = field(default="", repr=False)
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: str = field(default="", repr=False)
    twilio_from_number: str = ""
    twilio_messaging_service_sid: str = ""
    sms_approval_ttl_hours: int = 24
    review_link_ttl_hours: int = 72
    # Public gateway (Twilio webhooks + phone review pages). The dashboard stays private.
    public_base_url: str = ""
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8080
    gateway_secret_key: str = field(default="", repr=False)
    gateway_password_hash: str = field(default="", repr=False)
    gateway_session_hours: int = 12
    gateway_cookie_secure: bool = True

    @property
    def data_dir(self) -> Path:
        return Path(self.db_path).parent

    @property
    def stop_file(self) -> Path:
        return self.data_dir / "STOP"


def _secret(name):
    """Env var, or ``NAME_FILE`` pointing at a mounted secret (Docker/K8s/Vault agent)."""
    path = os.environ.get(name + "_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.environ.get(name, "")


def load(**overrides) -> Settings:
    values = dict(
        db_path=Path(os.environ.get("HUNTER_DB") or DEFAULT_DB),
        scan_interval=_int("SCAN_INTERVAL_SECONDS", 720, 300, 6 * 3600),
        scan_jitter=_int("SCAN_JITTER_SECONDS", 60, 0, 300),
        listing_ttl=_int("LISTING_CACHE_SECONDS", 540, 60, 3600),
        detail_ttl=_int("DETAIL_CACHE_SECONDS", 6 * 3600, 600, 7 * 86400),
        workday_interval=_int("WORKDAY_SCAN_INTERVAL_SECONDS", 3600, 600, 86400),
        scan_lease_minutes=_int("SCAN_LEASE_MINUTES", 30, 10, 180),
        days=_int("PUBLICATION_WINDOW_DAYS", 7, 1, 30),
        allow_real_submission=_bool("AUTOAPPLY_ALLOW_REAL_SUBMISSION"),
        emergency_stop_env=_bool("AUTOAPPLY_EMERGENCY_STOP"),
        max_per_day=_int("AUTOAPPLY_MAX_PER_DAY", 5, 0, 50),
        max_per_company_per_day=_int("AUTOAPPLY_MAX_PER_COMPANY_PER_DAY", 1, 0, 10),
        max_per_company_total=_int("AUTOAPPLY_MAX_PER_COMPANY_TOTAL", 3, 0, 50),
        min_fit_review=float(_float("AUTOAPPLY_MIN_FIT_REVIEW", 45.0)),
        min_fit_auto=float(_float("AUTOAPPLY_MIN_FIT_AUTO", 80.0)),
        trust_model_mappings=_bool("AUTOAPPLY_TRUST_MODEL_MAPPINGS"),
        auto_accept_validated_rewrites=_bool("AUTOAPPLY_AUTO_ACCEPT_VALIDATED_REWRITES"),
        headless=_bool("BROWSER_HEADLESS", True),
        chromium_executable=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", ""),
        browser_timeout_ms=_int("BROWSER_TIMEOUT_MS", 30000, 5000, 120000),
        anthropic_api_key=_secret("ANTHROPIC_API_KEY"),
        claude_model=os.environ.get("CLAUDE_MODEL", ""),
        claude_max_output_tokens=_int("CLAUDE_MAX_OUTPUT_TOKENS", 1800, 256, 8000),
        claude_max_input_tokens=_int("CLAUDE_MAX_INPUT_TOKENS", 12000, 1000, 100000),
        claude_max_retries=_int("CLAUDE_MAX_RETRIES", 2, 0, 4),
        claude_daily_usd=float(_float("CLAUDE_DAILY_USD_LIMIT", 2.0)),
        claude_monthly_usd=float(_float("CLAUDE_MONTHLY_USD_LIMIT", 20.0)),
        claude_input_usd_per_mtok=_float("CLAUDE_INPUT_USD_PER_MTOK"),
        claude_output_usd_per_mtok=_float("CLAUDE_OUTPUT_USD_PER_MTOK"),
        claude_enabled=_bool("CLAUDE_ENABLED"),
        health_port=_int("HEALTH_PORT", 0, 0, 65535),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", "").strip(),
        twilio_auth_token=_secret("TWILIO_AUTH_TOKEN").strip(),
        twilio_api_key_sid=os.environ.get("TWILIO_API_KEY_SID", "").strip(),
        twilio_api_key_secret=_secret("TWILIO_API_KEY_SECRET").strip(),
        twilio_from_number=os.environ.get("TWILIO_FROM_NUMBER", "").strip(),
        twilio_messaging_service_sid=os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "").strip(),
        sms_approval_ttl_hours=_int("SMS_APPROVAL_TTL_HOURS", 24, 1, 72),
        review_link_ttl_hours=_int("REVIEW_LINK_TTL_HOURS", 72, 1, 14 * 24),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        gateway_host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
        gateway_port=_int("GATEWAY_PORT", 8080, 1, 65535),
        gateway_secret_key=_secret("GATEWAY_SECRET_KEY").strip(),
        gateway_password_hash=_secret("GATEWAY_PASSWORD_HASH").strip(),
        gateway_session_hours=_int("GATEWAY_SESSION_HOURS", 12, 1, 24 * 30),
        gateway_cookie_secure=_bool("GATEWAY_COOKIE_SECURE", True),
    )
    values.update(overrides)
    return Settings(**values)
