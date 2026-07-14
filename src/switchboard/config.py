"""Operational (non-secret) configuration.

Everything here is safe to log. Secrets live in :mod:`switchboard.credentials`
and are reached only through the :class:`~switchboard.credentials.Credentials`
instance this module holds. One env namespace is loaded (by Credentials, from
``switchboard.env`` by default); config reads its non-secret values through the
same accessor so there is a single source of truth.

Brand facts encode what the reference repos actually query:

* published-performance table ``pubinsights_consum_data.auto_new_article_analysis``
  keys brand by SHORT code (``HC`` / ``CB`` / ``TPS``);
* Discover table ``pubinsights_ods_data.new_article_analysis`` keys brand by
  FULL name (``HotCars`` / ``CarBuzz`` / ``TopSpeed``);
* Sentinel ``propertyId`` is the site domain (``www.<brand>.com``);
* GSC export table is ``gsc.<brand>_com_searchdata_url_impression`` (empty today
  for the Auto trio — PRD §13.13).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from .credentials import Credentials
from .logging_ import get_logger

log = get_logger("config")


@dataclass(frozen=True)
class BrandConfig:
    key: str                 # 'hotcars' | 'carbuzz' | 'topspeed'
    display_name: str        # 'HotCars'
    short_code: str          # consum table `Brand` value: HC | CB | TPS
    discover_name: str       # ODS table `brandName` value: HotCars | CarBuzz | TopSpeed
    domain: str              # 'hotcars.com'

    @property
    def sentinel_property_id(self) -> str:
        return f"www.{self.domain}"

    @property
    def gsc_table(self) -> str:
        return f"gsc.{self.domain.replace('.', '_')}_searchdata_url_impression"


# The three Auto-portfolio brands (PRD §4). 'portfolio' is a cross-brand scope
# used for aggregate entries, not a BrandConfig.
_BRANDS: dict[str, BrandConfig] = {
    "hotcars": BrandConfig("hotcars", "HotCars", "HC", "HotCars", "hotcars.com"),
    "carbuzz": BrandConfig("carbuzz", "CarBuzz", "CB", "CarBuzz", "carbuzz.com"),
    "topspeed": BrandConfig("topspeed", "TopSpeed", "TPS", "TopSpeed", "topspeed.com"),
}


@dataclass(frozen=True)
class ModelConfig:
    """Per-task model IDs (PRD §11 — "keep model IDs in config"). Defaults match
    what the reference repos actually deploy; override via env."""

    default: str = "claude-sonnet-4-6"       # most agents
    synthesis: str = "claude-opus-4-5"        # orchestrator planning + opportunity scouting
    factcheck: str = "claude-haiku-4-5"       # cheap fact-gate verification


@dataclass(frozen=True)
class SpendCaps:
    """Hard caps the governor enforces (PRD §8). Config, not code. When
    ``enabled`` is false the caps are not enforced — ``per_day``/``per_run``
    return ``None`` so the governor never refuses on budget (spend is still
    metered + recorded in the ledger for observability)."""

    enabled: bool = True
    ahrefs_units_per_day: int = 5000
    ahrefs_units_per_run: int = 1000
    llm_micros_per_day: int = 20_000_000      # $20.00/day
    llm_micros_per_run: int = 5_000_000       # $5.00/run
    bq_bytes_per_day: int = 100 * 1024**3     # 100 GiB scanned/day
    bq_bytes_per_run: int = 20 * 1024**3      # 20 GiB/run

    def per_day(self, metric: str) -> int | None:
        if not self.enabled:
            return None
        return {
            "ahrefs_units": self.ahrefs_units_per_day,
            "llm_micros": self.llm_micros_per_day,
            "bq_bytes": self.bq_bytes_per_day,
        }.get(metric)

    def per_run(self, metric: str) -> int | None:
        if not self.enabled:
            return None
        return {
            "ahrefs_units": self.ahrefs_units_per_run,
            "llm_micros": self.llm_micros_per_run,
            "bq_bytes": self.bq_bytes_per_run,
        }.get(metric)


@dataclass(frozen=True)
class AuthConfig:
    """Google OAuth / OIDC login (PRD §9.1). Identity is for attribution only."""

    allowed_domains: tuple[str, ...] = ("valnetinc.com",)
    allowlist: tuple[str, ...] = ()
    redirect_uri: str = "http://localhost:8080/auth/callback"
    admins: tuple[str, ...] = ()               # emails bootstrapped to global_admin
    default_role: str = "portfolio_admin"      # role granted to a new allowlisted user

    def is_allowed(self, email: str, hd: str | None = None) -> bool:
        """Server-side check: hd/domain must match AND (if an allowlist is set)
        the email must be on it."""
        email = (email or "").lower().strip()
        if not email or "@" not in email:
            return False
        domain = email.split("@", 1)[1]
        if self.allowed_domains and domain not in self.allowed_domains:
            return False
        if hd is not None and self.allowed_domains and hd not in self.allowed_domains:
            return False
        if self.allowlist and email not in {a.lower() for a in self.allowlist}:
            return False
        return True


@dataclass(frozen=True)
class ArtifactConfig:
    backend: str = "local"                    # 'local' | 'gcs'
    gcs_bucket: str = "switchboard-artifacts"
    local_dir: str = "./local_artifacts"


# Content types the trend pipeline can generate, with their default transport.
# Transports: 'llm' (built-in governed drafting — always available),
# 'hc_viral_hits' (force-add-from-url → brief → full pipeline → Emaki draft),
# 'social_api' (social-media-posts-creator), 'newsletter_api'
# (newsletter-creator-auto), 'shellagent_run' (generic POST /run contract).
_TREND_CONTENT_TYPES: dict[str, str] = {
    "article": "llm",
    "social_post": "llm",
    "newsletter_blurb": "llm",
    "video_script": "llm",
}


@dataclass(frozen=True)
class TrendConfig:
    """Competitor-trend pipeline knobs (docs/trend-pipeline.md). Config, not code."""

    enabled: bool = True
    scan_interval_min: int = 120              # trend_scan cadence
    score_threshold: float = 60.0             # propose a pipeline at/above this
    min_sources: int = 2                      # distinct outlets before a cluster is a trend
    max_open_pipelines: int = 5               # per brand; scout stops proposing beyond this
    ttl_hours: int = 48                       # perishability: unactioned trends expire
    dedup_days: int = 5                       # don't re-propose a dismissed/expired cluster within this
    auto_dossier: bool = True                 # build the dossier as soon as a trend is proposed
    base_query: str = "automotive industry news"  # what the source APIs are asked for
    watchlist: tuple[str, ...] = ()           # boosted terms (TREND_WATCHLIST, csv)
    default_content_types: tuple[str, ...] = ("article", "social_post")
    transports: dict[str, str] = field(default_factory=lambda: dict(_TREND_CONTENT_TYPES))

    def transport_for(self, content_type: str) -> str:
        return self.transports.get(content_type, "llm")


# Default TTLs (seconds) per entry type — freshness scoping for the TTL sweep
# (PRD §7.1). Facts/decisions are durable; metrics/context are short-lived.
_DEFAULT_TTLS: dict[str, int | None] = {
    "metric": 3 * 24 * 3600,
    "flag": 7 * 24 * 3600,
    "context": 2 * 24 * 3600,
    "claim": 14 * 24 * 3600,
    "fact": None,                             # verified facts don't auto-expire
    "decision": None,
    "plan_item": 30 * 24 * 3600,
    "report": 14 * 24 * 3600,
    "distribution_draft": 14 * 24 * 3600,
}


@dataclass
class Settings:
    creds: Credentials
    env: str = "local"
    port: int = 8080
    base_path: str = ""
    dry_run_default: bool = True
    kill_switch: bool = False
    brand_keys: tuple[str, ...] = ("hotcars", "carbuzz", "topspeed")
    models: ModelConfig = field(default_factory=ModelConfig)
    caps: SpendCaps = field(default_factory=SpendCaps)
    auth: AuthConfig = field(default_factory=AuthConfig)
    artifacts: ArtifactConfig = field(default_factory=ArtifactConfig)
    trends: TrendConfig = field(default_factory=TrendConfig)
    ttls: dict[str, int | None] = field(default_factory=lambda: dict(_DEFAULT_TTLS))
    endpoints: dict[str, str] = field(default_factory=dict)

    # -- brands ---------------------------------------------------------------

    @property
    def brands(self) -> dict[str, BrandConfig]:
        return {k: _BRANDS[k] for k in self.brand_keys if k in _BRANDS}

    def brand(self, key: str) -> BrandConfig:
        if key not in _BRANDS:
            raise KeyError(f"Unknown brand '{key}'")
        return _BRANDS[key]

    def is_valid_scope(self, brand: str) -> bool:
        return brand == "portfolio" or brand in _BRANDS

    # -- secrets pass-through (never stores values) ---------------------------

    @property
    def database_url(self) -> str:
        return self.creds.database_url()

    def ttl_for(self, entry_type: str) -> int | None:
        return self.ttls.get(entry_type)


def _get_bool(creds: Credentials, key: str, default: bool) -> bool:
    raw = creds.resolve(key, secret=False)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _get_int(creds: Credentials, key: str, default: int) -> int:
    raw = creds.resolve(key, secret=False)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        log.warning("Config %s is not an int; using default", key)
        return default


def _csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(x.strip() for x in raw.split(",") if x.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build (and cache) the process-wide settings from the env namespace."""
    env_file = os.environ.get("SWITCHBOARD_ENV_FILE", "switchboard.env")
    creds = Credentials(env_file=env_file)

    brand_keys = _csv(creds.resolve("SWITCHBOARD_BRANDS", secret=False)) or (
        "hotcars",
        "carbuzz",
        "topspeed",
    )

    caps = SpendCaps(
        enabled=_get_bool(creds, "SPEND_CAPS_ENABLED", True),
        ahrefs_units_per_day=_get_int(creds, "CAP_AHREFS_UNITS_PER_DAY", 5000),
        ahrefs_units_per_run=_get_int(creds, "CAP_AHREFS_UNITS_PER_RUN", 1000),
        llm_micros_per_day=_get_int(creds, "CAP_LLM_MICROS_PER_DAY", 20_000_000),
        llm_micros_per_run=_get_int(creds, "CAP_LLM_MICROS_PER_RUN", 5_000_000),
        bq_bytes_per_day=_get_int(creds, "CAP_BQ_BYTES_PER_DAY", 100 * 1024**3),
        bq_bytes_per_run=_get_int(creds, "CAP_BQ_BYTES_PER_RUN", 20 * 1024**3),
    )

    models = ModelConfig(
        default=creds.resolve("MODEL_DEFAULT", secret=False) or "claude-sonnet-4-6",
        synthesis=creds.resolve("MODEL_SYNTHESIS", secret=False) or "claude-opus-4-5",
        factcheck=creds.resolve("MODEL_FACTCHECK", secret=False) or "claude-haiku-4-5",
    )

    auth = AuthConfig(
        allowed_domains=_csv(creds.resolve("AUTH_ALLOWED_DOMAINS", secret=False)) or ("valnetinc.com",),
        allowlist=_csv(creds.resolve("AUTH_ALLOWLIST", secret=False)),
        redirect_uri=creds.resolve("GOOGLE_OAUTH_REDIRECT_URI", secret=False)
        or "http://localhost:8080/auth/callback",
        admins=_csv(creds.resolve("AUTH_ADMINS", secret=False)),
        default_role=creds.resolve("AUTH_DEFAULT_ROLE", secret=False) or "portfolio_admin",
    )

    artifacts = ArtifactConfig(
        backend=creds.resolve("ARTIFACT_STORE", secret=False) or "local",
        gcs_bucket=creds.resolve("ARTIFACT_GCS_BUCKET", secret=False) or "switchboard-artifacts",
        local_dir=creds.resolve("ARTIFACT_LOCAL_DIR", secret=False) or "./local_artifacts",
    )

    def _get_float(key: str, default: float) -> float:
        raw = creds.resolve(key, secret=False)
        try:
            return float(raw) if raw not in (None, "") else default
        except ValueError:
            log.warning("Config %s is not a float; using default", key)
            return default

    transports = dict(_TREND_CONTENT_TYPES)
    for ct in transports:
        override = creds.resolve(f"TREND_TRANSPORT_{ct.upper()}", secret=False)
        if override:
            transports[ct] = override.strip().lower()
    # Validate the default content types at load time — a typo here must degrade
    # to the built-in defaults, not abort (and re-bill) every scheduled scan.
    raw_defaults = _csv(creds.resolve("TREND_DEFAULT_CONTENT_TYPES", secret=False))
    default_types = tuple(t.lower() for t in raw_defaults if t.lower() in _TREND_CONTENT_TYPES)
    dropped = [t for t in raw_defaults if t.lower() not in _TREND_CONTENT_TYPES]
    if dropped:
        log.warning("TREND_DEFAULT_CONTENT_TYPES: dropping unknown type(s) %s (valid: %s)",
                    dropped, list(_TREND_CONTENT_TYPES))
    trends = TrendConfig(
        enabled=_get_bool(creds, "TREND_PIPELINE_ENABLED", True),
        scan_interval_min=_get_int(creds, "TREND_SCAN_INTERVAL_MIN", 120),
        score_threshold=_get_float("TREND_SCORE_THRESHOLD", 60.0),
        min_sources=_get_int(creds, "TREND_MIN_SOURCES", 2),
        max_open_pipelines=_get_int(creds, "TREND_MAX_OPEN_PIPELINES", 5),
        ttl_hours=_get_int(creds, "TREND_TTL_HOURS", 48),
        dedup_days=_get_int(creds, "TREND_DEDUP_DAYS", 5),
        auto_dossier=_get_bool(creds, "TREND_AUTO_DOSSIER", True),
        base_query=creds.resolve("TREND_BASE_QUERY", secret=False) or "automotive industry news",
        watchlist=_csv(creds.resolve("TREND_WATCHLIST", secret=False)),
        default_content_types=default_types or ("article", "social_post"),
        transports=transports,
    )

    endpoints = {
        "albert": creds.resolve("ALBERT_API_URL", secret=False) or "http://localhost:3100",
        "seona": creds.resolve("SEONA_API_URL", secret=False) or "http://localhost:3110",
        "hc_viral_hits": creds.resolve("HC_VIRAL_HITS_API_URL", secret=False) or "http://127.0.0.1:8001",
        "writers_dashboard": creds.resolve("WRITERS_DASHBOARD_URL", secret=False) or "http://localhost:3001",
        # Same env names the assemble actions already use (adapters/actions.py).
        "social": creds.resolve("SOCIAL_API_URL", secret=False) or "http://localhost:3145",
        "newsletter": creds.resolve("NEWSLETTER_API_URL", secret=False) or "http://localhost:5200",
    }

    # Caddy on the ShellAgent VM serves this under /agents/<slug>/ (strip_prefix),
    # so generated links + redirects need the base path (APP_BASE_PATH). Empty =
    # served at root (local dev). Port: PORT (PM2/SHA convention) then SWITCHBOARD_PORT.
    base_path = (creds.resolve("APP_BASE_PATH", secret=False) or "").rstrip("/")
    port = _get_int(creds, "PORT", 0) or _get_int(creds, "SWITCHBOARD_PORT", 8080)
    settings = Settings(
        creds=creds,
        env=creds.resolve("SWITCHBOARD_ENV", secret=False) or "local",
        port=port,
        base_path=base_path,
        dry_run_default=_get_bool(creds, "SWITCHBOARD_DRY_RUN_DEFAULT", True),
        kill_switch=_get_bool(creds, "SWITCHBOARD_KILL_SWITCH", False),
        brand_keys=brand_keys,
        models=models,
        caps=caps,
        auth=auth,
        artifacts=artifacts,
        trends=trends,
        endpoints=endpoints,
    )
    log.info(
        "Settings loaded: env=%s brands=%s dry_run_default=%s kill_switch=%s credentials_present=%s",
        settings.env,
        list(settings.brand_keys),
        settings.dry_run_default,
        settings.kill_switch,
        sorted(k for k, v in creds.describe().items() if v),
    )
    return settings
