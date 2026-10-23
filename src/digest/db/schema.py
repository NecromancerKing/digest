"""SQLAlchemy Core tables for the pipeline state (spec section 7).

Portable types only, so Postgres can replace SQLite later: timestamps are ISO 8601 text in
UTC, JSON columns hold JSON text, vectors are little-endian float32 blobs.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING)

FEED_KINDS = ("rss", "atom", "json_api", "data_api")
USAGE_POLICIES = ("open_license", "facts_only", "link_only")
ITEM_STATUSES = ("new", "embedded", "clustered", "extracted", "skipped", "failed")
STORY_STATUSES = ("draft", "needs_review", "approved", "published", "rejected", "unpublished")
REVISION_AUTHORS = ("pipeline", "owner")
REVISION_KINDS = ("llm", "links", "template", "owner_blurb")
LLM_PURPOSES = ("extract", "synthesize", "verify", "eval")
LLM_OUTCOMES = ("ok", "rate_limited", "invalid_output", "safety_block", "error")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# Runtime state per feed; the registry itself is config/sources.yaml.
feeds = Table(
    "feeds",
    metadata,
    Column("url", Text, primary_key=True),
    Column("source_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("etag", Text),
    Column("last_modified", Text),
    Column("last_polled_at", Text),
    Column("last_status", Integer),
    Column("consecutive_errors", Integer, nullable=False, server_default=text("0")),
    Column("disabled_reason", Text),  # set after repeated errors
    CheckConstraint(_in("kind", FEED_KINDS), name="kind"),
)

# robots.txt and site-wide TDM status per host.
host_policy = Table(
    "host_policy",
    metadata,
    Column("host", Text, primary_key=True),
    Column("robots_txt", Text),
    Column("robots_fetched_at", Text),
    Column("tdmrep_json", Text),
    Column("tdm_checked_at", Text),
)

items = Table(
    "items",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("source_id", Text, nullable=False),
    Column("feed_url", Text, ForeignKey("feeds.url")),
    Column("url", Text, nullable=False),
    Column("url_hash", LargeBinary, nullable=False, unique=True),  # sha256 of canonical URL
    Column("guid", Text),
    Column("title", Text, nullable=False),
    Column("feed_summary", Text),
    Column("published_at", Text),
    Column("fetched_at", Text, nullable=False),
    Column("lang", Text),
    Column("usage_policy", Text, nullable=False),
    Column("full_text", Text),  # NULL unless allowed; purged
    Column("full_text_purge_at", Text),
    Column("embedding", LargeBinary),  # NULL for link_only; dropped when the cluster closes
    Column("entities", Text),  # JSON
    Column("facts", Text),  # JSON, our words
    Column("status", Text, nullable=False, server_default=text("'new'")),
    CheckConstraint(_in("usage_policy", USAGE_POLICIES), name="usage_policy"),
    CheckConstraint(_in("status", ITEM_STATUSES), name="status"),
    CheckConstraint(
        "usage_policy <> 'link_only' OR (embedding IS NULL AND facts IS NULL)",
        name="link_only_no_derivatives",
    ),
    Index("items_published", "published_at"),
    Index("items_status", "status"),
    Index(
        "items_source_guid",
        "source_id",
        "guid",
        unique=True,
        sqlite_where=text("guid IS NOT NULL"),
        postgresql_where=text("guid IS NOT NULL"),
    ),
)

clusters = Table(
    "clusters",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", Text, nullable=False),
    Column("last_item_at", Text, nullable=False),
    Column("category", Text),
    Column("centroid", LargeBinary),  # NULL once closed
    Column("entity_set", Text, nullable=False, server_default=text("'[]'")),
    Column("extraction", Text),  # JSON from stage 5
    Column("item_count", Integer, nullable=False, server_default=text("0")),
    Column("source_count", Integer, nullable=False, server_default=text("0")),
    Column("score", Float, nullable=False, server_default=text("0")),
    Column("is_open", Integer, nullable=False, server_default=text("1")),
    Column("merged_into", Integer, ForeignKey("clusters.id")),
    Index("clusters_open", "is_open", "last_item_at"),
)

cluster_items = Table(
    "cluster_items",
    metadata,
    Column("cluster_id", Integer, ForeignKey("clusters.id"), primary_key=True),
    Column("item_id", Integer, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True),
    Column("similarity", Float),
    Column("added_at", Text, nullable=False),
)

# The pipeline's copy; the content repository is the source of truth.
stories = Table(
    "stories",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("cluster_id", Integer, ForeignKey("clusters.id"), nullable=False, unique=True),
    Column("edition_lang", Text, nullable=False),
    Column("slug", Text, nullable=False),
    Column("path", Text, nullable=False, unique=True),  # stories/YYYY-MM-DD/<slug>.yaml
    Column("category", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'draft'")),
    Column("current_rev", Integer),
    Column("rank_score", Float, nullable=False, server_default=text("0")),
    Column("is_top", Integer, nullable=False, server_default=text("0")),
    Column("written_hash", Text),  # hash of the file as the pipeline last wrote it
    Column("owner_edited", Integer, nullable=False, server_default=text("0")),
    Column("first_published_at", Text),
    Column("updated_at", Text, nullable=False),
    CheckConstraint(_in("status", STORY_STATUSES), name="status"),
    UniqueConstraint("edition_lang", "slug", name="uq_stories_edition_lang_slug"),
)

story_revisions = Table(
    "story_revisions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("story_id", Integer, ForeignKey("stories.id"), nullable=False),
    Column("rev_no", Integer, nullable=False),
    Column("headline", Text, nullable=False),
    Column("summary", Text),  # NULL for links-mode cards
    Column("why_it_matters", Text),
    Column("claims", Text, nullable=False),  # JSON [{text, fact_ids[]}]
    Column("sources", Text, nullable=False),  # JSON [{item_id, source_id, name, url, ...}]
    Column("verification", Text, nullable=False),  # JSON gate results and scores
    Column("provider", Text),
    Column("model", Text),
    Column("prompt_version", Text),
    Column("created_by", Text, nullable=False),
    Column("kind", Text, nullable=False, server_default=text("'llm'")),
    Column("created_at", Text, nullable=False),
    CheckConstraint(_in("created_by", REVISION_AUTHORS), name="created_by"),
    CheckConstraint(_in("kind", REVISION_KINDS), name="kind"),
    UniqueConstraint("story_id", "rev_no", name="uq_story_revisions_story_id_rev_no"),
)

runs = Table(
    "runs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("started_at", Text, nullable=False),
    Column("finished_at", Text),
    Column("ok", Integer),
    Column("stats", Text),  # JSON counts and timings; no third-party text
    Column("error", Text),
)

llm_calls = Table(
    "llm_calls",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", Integer, ForeignKey("runs.id")),
    Column("purpose", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("input_tokens", Integer),
    Column("cached_tokens", Integer),
    Column("output_tokens", Integer),
    Column("reasoning_tokens", Integer),
    Column("cost_micro_usd", Integer, nullable=False, server_default=text("0")),
    Column("outcome", Text, nullable=False),
    Column("latency_ms", Integer),
    Column("created_at", Text, nullable=False),
    CheckConstraint(_in("purpose", LLM_PURPOSES), name="purpose"),
    CheckConstraint(_in("outcome", LLM_OUTCOMES), name="outcome"),
    Index("llm_calls_window", "provider", "model", "created_at"),
)

# Small facts: last snapshot date, last report date, ...
kv = Table(
    "kv",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)
