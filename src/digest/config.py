"""Settings and config-file loaders.

Two kinds of configuration:

- ``Settings``: environment variables (and a local, git-ignored ``.env``), spec section 17.
- ``Config``: the YAML files in ``config/``, validated with Pydantic and cross-checked
  against each other at startup (registry vs. blocklist, category hints, ...).
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

UsagePolicy = Literal["open_license", "facts_only", "link_only"]
FeedKind = Literal["rss", "atom", "json_api", "data_api"]
Role = Literal["extract", "synthesize", "verify"]


class ConfigError(Exception):
    """A config file is missing, malformed or inconsistent with another one."""


# --------------------------------------------------------------------------- environment


class Settings(BaseSettings):
    """Environment variables. Every field is documented in ``.env.example``."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore"
    )

    product_name: str = "Digest"
    site_url: str | None = None
    site_noindex: bool = False  # shadow week: keep search engines out
    contact_email: str | None = None  # shown on the contact, privacy and corrections pages
    edition_lang: Literal["en", "sv"] = "en"
    tz: str = "Europe/Stockholm"
    config_dir: Path = Path("config")

    summary_mode: Literal["llm", "links"] = "llm"
    fallback_to_links: bool = False  # D28: no links cards next to AI summaries
    template_stories: bool = True
    cluster_method: Literal["embeddings", "tfidf"] = "embeddings"
    summary_words_min: int = Field(default=60, ge=5)
    summary_words_max: int = Field(default=120, ge=5)

    state_store: Literal["github_release", "local_file"] = "github_release"
    state_path: Path = Path(".state/digest.sqlite")
    state_release_tag: str = "pipeline-state"
    state_upload: bool = False
    content_repo: str | None = None
    content_repo_token: SecretStr | None = None

    llm_providers_file: Path | None = None
    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    cloudflare_ai_token: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    llm_daily_budget_usd: float = Field(default=0.0, ge=0)
    use_strong_model_for_top: bool = False
    llm_max_input_tokens: int = Field(default=5000, gt=0)

    embed_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embed_cache_dir: Path = Path.home() / ".cache" / "fastembed"
    content_checkout: Path = Path("../digest-content")  # local clone, for clustering labels

    run_time_budget_minutes: float = Field(default=11, gt=0)
    fulltext_retention_hours: int = Field(default=72, gt=0)
    item_retention_days: int = Field(default=30, gt=0)

    static_host: Literal["cloudflare_pages", "cloudflare_workers", "github_pages"] = (
        "cloudflare_pages"
    )
    min_minutes_between_deploys: int = Field(default=55, ge=0)  # hourly updates (D23)
    cloudflare_api_token: SecretStr | None = None
    cloudflare_account_id: str | None = None
    cf_pages_project: str | None = None

    healthcheck_url: SecretStr | None = None
    bot_contact_url: str | None = None
    status_file: Path = Path("status/last_run.json")

    @field_validator("tz")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown time zone {v!r}") from e
        return v

    @field_validator("content_repo")
    @classmethod
    def _owner_slash_name(cls, v: str | None) -> str | None:
        if v is not None and (v.count("/") != 1 or not all(v.split("/"))):
            raise ValueError("CONTENT_REPO must look like 'owner/name'")
        return v

    @model_validator(mode="after")
    def _word_range(self) -> Self:
        if self.summary_words_min > self.summary_words_max:
            raise ValueError("SUMMARY_WORDS_MIN must not exceed SUMMARY_WORDS_MAX")
        return self

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def shows_links(self) -> bool:
        """Whether the site shows links cards: in links mode, or as the llm fallback."""
        return self.summary_mode == "links" or self.fallback_to_links

    @property
    def providers_path(self) -> Path:
        return self.llm_providers_file or self.config_dir / "llm_providers.yaml"

    @property
    def contact_url(self) -> str:
        if self.bot_contact_url:
            return self.bot_contact_url
        return f"{self.site_url.rstrip('/')}/bot" if self.site_url else ""

    @property
    def bot_name(self) -> str:
        """Robots.txt token: ``DigestBot`` for the default product name."""
        return re.sub(r"[^A-Za-z0-9]", "", self.product_name) + "Bot"

    @property
    def user_agent(self) -> str:
        """``DigestBot/1.0 (+https://<site>/bot)``. Raises ``ConfigError`` without a contact URL."""
        if not self.contact_url:
            raise ConfigError("set SITE_URL or BOT_CONTACT_URL: the user agent needs a contact URL")
        return f"{self.bot_name}/1.0 (+{self.contact_url})"


# --------------------------------------------------------------------------- YAML models


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


CategoryColor = Literal["blue", "teal", "amber", "violet", "green", "rose"]
CategoryIcon = Literal["pin", "globe", "trend", "flask", "leaf", "trophy"]


class Region(_Strict):
    """A world region for the "Your region" box and region pages (D27)."""

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    slug: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")]
    names: dict[str, str]
    timezones: list[str] = []  # IANA zones; an entry ending in "/" is a prefix
    places: list[str] = []  # names that put a story here: whole words, case-sensitive

    def name(self, lang: str) -> str:
        return self.names.get(lang) or next(iter(self.names.values()), self.id)


class Category(_Strict):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    slug: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")]
    names: dict[str, str]
    weight: float = 0.0
    daily_cap: int = Field(ge=0)
    # For stage 5: what belongs here, and whether it is a topic or a place. A topic wins
    # over a place, so a Riksbank decision is economy, not Sweden.
    kind: Literal["topic", "place"] = "topic"
    hint: str = ""
    # For the website (spec section 10): a palette key, an icon name, and optional shorter
    # names for pills and tiles ("Climate" for "Climate & Environment").
    color: CategoryColor = "blue"
    icon: CategoryIcon = "pin"
    short_names: dict[str, str] = {}
    # Title words that file a story here before stage 5 has (a topic only): word starts,
    # lower case. Catches sport from general feeds such as link-only sources' (D27).
    keywords: list[str] = []

    def matches(self, title: str) -> bool:
        low = title.lower()
        return any(re.search(rf"(?<![^\W_]){re.escape(k)}", low) for k in self.keywords)

    def name(self, lang: str) -> str:
        return self.names.get(lang) or next(iter(self.names.values()), self.id)

    def short_name(self, lang: str) -> str:
        return self.short_names.get(lang) or self.name(lang)


class Blocklist(_Strict):
    domains: list[str]

    @field_validator("domains")
    @classmethod
    def _normalise(cls, v: list[str]) -> list[str]:
        out = [d.strip().lower().lstrip(".") for d in v]
        if any(not d or "/" in d or ":" in d for d in out):
            raise ValueError("blocklist entries must be bare domains such as 'example.com'")
        return out

    def is_blocked(self, host: str) -> bool:
        """True if ``host`` is a listed domain or a subdomain of one."""
        h = host.lower().rstrip(".")
        return any(h == d or h.endswith("." + d) for d in self.domains)


class Feed(_Strict):
    url: HttpUrl
    kind: FeedKind
    category: str | None = None  # overrides the source's first category hint for this feed


class Source(_Strict):
    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]
    name: str
    homepage: HttpUrl
    feeds: list[Feed] = Field(min_length=1)
    lang: str
    country: str
    category_hints: list[str] = []
    usage_policy: UsagePolicy
    display_headlines: bool = False
    fetch_article_pages: bool = False
    poll_minutes: int = Field(default=15, ge=1)
    terms_url: HttpUrl | None = None
    terms_checked_on: dt.date | None = None
    official: bool = False
    template: str | None = None
    extra_hosts: list[str] = []  # other hosts this source's links point to (article pages)
    publisher: str | None = None  # sources with the same publisher are not independent
    enabled: bool = False

    @field_validator("extra_hosts")
    @classmethod
    def _bare_hosts(cls, v: list[str]) -> list[str]:
        if any(not h or "/" in h or ":" in h for h in v):
            raise ValueError("extra_hosts entries must be bare hosts such as 'www.example.com'")
        return v

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if self.usage_policy == "link_only" and self.fetch_article_pages:
            raise ValueError(f"{self.id}: link_only sources may not fetch article pages")
        if self.enabled and self.terms_checked_on is None:
            raise ValueError(f"{self.id}: enabled sources need terms_checked_on (owner review)")
        if self.template and not any(f.kind == "data_api" for f in self.feeds):
            raise ValueError(f"{self.id}: template sources need a data_api feed")
        return self

    @property
    def independence_key(self) -> str:
        """Sources with the same key count as one source (rank: independent sources)."""
        return self.publisher or self.id

    def category_for(self, feed_url: str | None) -> str | None:
        """The category an item from this feed is filed under before stage 5 decides."""
        for f in self.feeds:
            if str(f.url) == feed_url and f.category:
                return f.category
        return self.category_hints[0] if self.category_hints else None

    def hosts(self) -> set[str]:
        """Every host this source may be fetched from (rule L1)."""
        urls = [str(self.homepage), *(str(f.url) for f in self.feeds)]
        return {(urlsplit(u).hostname or "").lower() for u in urls} | {
            h.lower() for h in self.extra_hosts
        }


class FetchSettings(_Strict):
    min_seconds_between_requests_per_host: float = 5
    timeout_seconds: float = 10
    min_poll_minutes: int = 15
    poll_grace_minutes: int = 5
    robots_cache_hours: int = 24
    tdm_cache_hours: int = 24
    disable_feed_after_errors: int = 10
    disabled_feed_probe_hours: int = 24
    backoff_max_hours: int = 24
    max_redirects: int = 5
    max_response_bytes: int = 5_000_000
    max_entry_age_hours: int = 72
    title_max_chars: int = 300
    feed_summary_max_chars: int = 2000
    article_text_max_chars: int = 3000
    max_articles_per_run: int = 60
    drop_query_params: list[str] = ["utm_*", "fbclid", "gclid"]


class ClusterSettings(_Strict):
    max_embed_per_run: int = 3000
    candidate_window_hours: int = 48
    candidate_top_k: int = 10
    cosine_weight: float = 0.75
    entity_weight: float = 0.25
    join_threshold: float = 0.78
    same_source_threshold: float = 0.88
    link_only_trigram_threshold: float = 0.35
    link_only_max_hours_apart: int = 24  # also: unmatched link_only items are retried this long
    entity_set_max: int = 40
    merge_cosine: float = 0.90
    merge_entity_jaccard: float = 0.4
    maintenance_every_hours: int = 2
    close_after_idle_hours: int = 36
    # CLUSTER_METHOD=tfidf: its vectors score lower than embeddings, so its own thresholds.
    tfidf_dim: int = Field(default=1024, ge=64)
    tfidf_join_threshold: float = 0.125
    tfidf_same_source_threshold: float = 0.5
    tfidf_merge_cosine: float = 0.5
    # D14: merge open clusters whose stage-5 event lines (English) match; tuned on live data.
    event_merge_cosine: float = 0.85
    tfidf_event_merge_cosine: float = 0.6
    # Fold open clusters into a live story's cluster about the same event (stage 4).
    story_merge_cosine: float = 0.87
    tfidf_story_merge_cosine: float = 0.5
    # Stage 5 folds a cluster into the live story the model names only at least this alike.
    same_event_min_cosine: float = 0.40
    tfidf_same_event_min_cosine: float = 0.05


class RankSettings(_Strict):
    source_count_weight: float = 1.0
    official_bonus: float = 0.5
    recency_weight: float = 0.4
    recency_half_life_hours: float = 8
    single_source_penalty: float = 0.6
    min_independent_sources: int = 2
    top_stories: int = 5
    top_stories_max_per_category: int = 2
    today_min_stories: int = 10
    today_max_stories: int = 12
    today_max_per_category: int = 3
    today_window_hours: int = 24
    # A selection still without a story after this long stops holding its category's slot.
    unwritten_slot_hours: float = Field(default=4, gt=0)


class SynthesisSettings(_Strict):
    update_min_minutes: int = 30
    max_retries: int = 2
    max_input_tokens: int | None = None  # below LLM_MAX_INPUT_TOKENS to save daily quota
    # Reports read for a new story in all, and per update run (newest, sources in turns).
    max_reports_read: int = Field(default=12, gt=0)
    why_it_matters_max_words: int = 25
    headline_max_chars: int = 70
    key_facts_max: int = 5
    key_fact_max_words: int = 30  # G1 limit; the prompt asks for about 15
    owner_blurb_max_words: int = 60


class GateSettings(_Strict):
    # Gates whose failure stops a story (retry, then drop). Others are still recorded in
    # the revision's verification, except G3, whose grader is then not called at all.
    enforce: list[Literal["g1", "g2", "g3"]] = ["g1", "g2", "g3"]
    originality_shared_run_words: int = 8
    originality_headline_jaccard: float = 0.5
    originality_cross_lang_cosine: float = 0.90
    duplicate_cosine: float = 0.90
    duplicate_window_hours: int = 24


class RetentionSettings(_Strict):
    runs_days: int = 90
    llm_calls_days: int = 90


class StateSettings(_Strict):
    keep_state_files: int = Field(default=3, ge=2)
    keep_daily_snapshots: int = 14
    keep_weekly_snapshots: int = 8
    warn_size_mb: int = 100


class PublishSettings(_Strict):
    rebuild_if_older_than_hours: float = 3
    glance_most_covered: int = Field(default=5, ge=0)
    glance_developing_max: int = Field(default=3, ge=0)
    developing_max_age_hours: float = Field(default=6, gt=0)
    section_rows: int = Field(default=3, ge=0)
    category_page_hours: int = Field(default=48, gt=0)
    region_box_stories: int = Field(default=5, gt=0)  # "Your region" box on the front page
    briefing_seconds_per_story: int = Field(default=16, gt=0)


class ScheduleSettings(_Strict):
    snapshot_after: dt.time = dt.time(3, 0)
    daily_report_after: dt.time = dt.time(22, 0)
    morning_refresh_by: dt.time = dt.time(6, 45)
    morning_alert_at: dt.time = dt.time(6, 50)
    robots_recheck_weekday: int = Field(default=0, ge=0, le=6)
    restore_test_day_of_month: int = Field(default=1, ge=1, le=28)


class TemplateSettings(_Strict):
    smhi_min_warning_level: Literal["yellow", "orange", "red"] = "yellow"
    smhi_exclude_events: list[str] = ["WIND_SEA", "LOW_SEA_LEVEL"]
    riksbank_policy_rate_change: bool = True
    scb_new_release: bool = True


class AppSettings(_Strict):
    """``config/settings.yaml``."""

    fetch: FetchSettings = FetchSettings()
    cluster: ClusterSettings = ClusterSettings()
    rank: RankSettings = RankSettings()
    synthesis: SynthesisSettings = SynthesisSettings()
    gates: GateSettings = GateSettings()
    retention: RetentionSettings = RetentionSettings()
    state: StateSettings = StateSettings()
    publish: PublishSettings = PublishSettings()
    schedule: ScheduleSettings = ScheduleSettings()
    templates: TemplateSettings = TemplateSettings()


class ModelLimits(_Strict):
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    tpd: int | None = None


class ModelSpec(_Strict):
    id: str
    quality: int = 0
    roles: list[Role] = Field(min_length=1)
    reasoning: Literal["none", "minimal", "low", "medium", "high"] | None = None
    limits: ModelLimits = ModelLimits()
    strong: bool = False
    # json_schema: the provider enforces our schema; json_object: valid JSON only, and the
    # schema is checked afterwards (models without structured-output support).
    json_mode: Literal["json_schema", "json_object"] = "json_schema"
    extract_ok: bool = True  # passed the extraction eval; extract prefers the weakest that did
    price_input_per_mtok: float = Field(default=0.0, ge=0)  # USD; 0 on free tiers
    price_output_per_mtok: float = Field(default=0.0, ge=0)


class Provider(_Strict):
    id: str
    adapter: Literal["openai_compatible", "anthropic"]
    enabled: bool = True
    base_url: HttpUrl | None = None
    api_key_env: str
    trains_on_inputs: bool | Literal["varies"] | None = None
    paid: bool = False
    # Shared by all the provider's models, on top of each model's own (OpenRouter's free
    # requests a day count across every ":free" model).
    limits: ModelLimits = ModelLimits()
    timeout_seconds: float = Field(
        default=60, gt=0
    )  # one request; a timeout moves to the next model
    models: list[ModelSpec] = []

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if self.adapter == "anthropic" and not self.paid:
            raise ValueError(f"{self.id}: the anthropic adapter is paid; set paid: true")
        ids = [m.id for m in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.id}: duplicate model ids")
        return self


class LLMProviders(_Strict):
    """``config/llm_providers.yaml``."""

    allow_training_providers: bool = True
    providers: list[Provider]

    @field_validator("providers")
    @classmethod
    def _unique(cls, v: list[Provider]) -> list[Provider]:
        ids = [p.id for p in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate provider ids")
        return v

    def eligible(self) -> list[Provider]:
        """Enabled providers, minus those that may train on inputs when that is not allowed."""
        out = [p for p in self.providers if p.enabled]
        if not self.allow_training_providers:
            out = [p for p in out if p.trains_on_inputs is False]
        return out


# --------------------------------------------------------------------------- loading


class Config(BaseModel):
    """Everything in ``config/``, cross-validated."""

    model_config = ConfigDict(frozen=True)

    categories: list[Category]
    regions: list[Region] = []
    blocklist: Blocklist
    sources: list[Source]
    settings: AppSettings
    llm: LLMProviders

    @property
    def fallback_category(self) -> str:
        """Where stories of a removed category go: the first place category (World)."""
        return next((c.id for c in self.categories if c.kind == "place"), self.categories[0].id)

    @model_validator(mode="after")
    def _cross_checks(self) -> Self:
        errors: list[str] = []
        cat_ids = [c.id for c in self.categories]
        if len(cat_ids) != len(set(cat_ids)):
            errors.append("categories.yaml: duplicate category ids")
        slugs = [c.slug for c in self.categories]
        if len(slugs) != len(set(slugs)):
            errors.append("categories.yaml: duplicate category slugs")
        if len({r.id for r in self.regions}) != len(self.regions):
            errors.append("regions.yaml: duplicate region ids")
        if len({r.slug for r in self.regions}) != len(self.regions):
            errors.append("regions.yaml: duplicate region slugs")
        placed: dict[str, str] = {}
        for r in self.regions:
            for name in r.places:
                if placed.setdefault(name, r.id) != r.id:
                    errors.append(f"regions.yaml: {name!r} is in {placed[name]} and {r.id}")
        src_ids = [s.id for s in self.sources]
        if len(src_ids) != len(set(src_ids)):
            errors.append("sources.yaml: duplicate source ids")
        feed_urls = [str(f.url) for s in self.sources for f in s.feeds]
        if len(feed_urls) != len(set(feed_urls)):
            errors.append("sources.yaml: the same feed URL is listed twice")
        for s in self.sources:
            unknown = (set(s.category_hints) | {f.category for f in s.feeds if f.category}) - set(
                cat_ids
            )
            if unknown:
                errors.append(f"sources.yaml: {s.id}: unknown category hints {sorted(unknown)}")
            if s.template is not None:
                from digest.datastories import known_templates

                if s.template not in known_templates():
                    errors.append(f"sources.yaml: {s.id}: unknown template {s.template!r}")
            blocked = sorted(h for h in s.hosts() if self.blocklist.is_blocked(h))
            if blocked:
                errors.append(f"sources.yaml: {s.id}: blocklisted hosts {blocked}")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @property
    def enabled_sources(self) -> list[Source]:
        return [s for s in self.sources if s.enabled]

    def source(self, source_id: str) -> Source:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise KeyError(source_id)


def _read_yaml(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"missing config file: {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e


def load_config(config_dir: Path, providers_file: Path | None = None) -> Config:
    """Load and validate every file in ``config_dir``. Raises ``ConfigError``."""
    raw = {
        "categories": _read_yaml(config_dir / "categories.yaml") or [],
        "regions": _read_yaml(config_dir / "regions.yaml") or []
        if (config_dir / "regions.yaml").is_file()
        else [],
        "blocklist": _read_yaml(config_dir / "blocklist.yaml") or {"domains": []},
        "sources": _read_yaml(config_dir / "sources.yaml") or [],
        "settings": _read_yaml(config_dir / "settings.yaml") or {},
        "llm": _read_yaml(providers_file or config_dir / "llm_providers.yaml"),
    }
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(str(e)) from e


def load_all(settings: Settings | None = None) -> tuple[Settings, Config]:
    """Environment settings plus the config files they point to."""
    s = settings or Settings()
    return s, load_config(s.config_dir, s.providers_path)
