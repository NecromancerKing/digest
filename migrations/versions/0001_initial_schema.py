"""Initial schema (spec section 7).

Revision ID: 0001
Revises:
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feeds",
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("etag", sa.Text()),
        sa.Column("last_modified", sa.Text()),
        sa.Column("last_polled_at", sa.Text()),
        sa.Column("last_status", sa.Integer()),
        sa.Column("consecutive_errors", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("disabled_reason", sa.Text()),
        sa.CheckConstraint("kind IN ('rss', 'atom', 'json_api', 'data_api')", name=op.f("ck_feeds_kind")),
        sa.PrimaryKeyConstraint("url", name=op.f("pk_feeds")),
    )
    op.create_table(
        "host_policy",
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("robots_txt", sa.Text()),
        sa.Column("robots_fetched_at", sa.Text()),
        sa.Column("tdmrep_json", sa.Text()),
        sa.Column("tdm_checked_at", sa.Text()),
        sa.PrimaryKeyConstraint("host", name=op.f("pk_host_policy")),
    )
    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("feed_url", sa.Text()),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.LargeBinary(), nullable=False),
        sa.Column("guid", sa.Text()),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("feed_summary", sa.Text()),
        sa.Column("published_at", sa.Text()),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.Column("lang", sa.Text()),
        sa.Column("usage_policy", sa.Text(), nullable=False),
        sa.Column("full_text", sa.Text()),
        sa.Column("full_text_purge_at", sa.Text()),
        sa.Column("embedding", sa.LargeBinary()),
        sa.Column("entities", sa.Text()),
        sa.Column("facts", sa.Text()),
        sa.Column("status", sa.Text(), server_default=sa.text("'new'"), nullable=False),
        sa.CheckConstraint(
            "usage_policy IN ('open_license', 'facts_only', 'link_only')",
            name=op.f("ck_items_usage_policy"),
        ),
        sa.CheckConstraint(
            "status IN ('new', 'embedded', 'clustered', 'extracted', 'skipped', 'failed')",
            name=op.f("ck_items_status"),
        ),
        sa.CheckConstraint(
            "usage_policy <> 'link_only' OR (embedding IS NULL AND facts IS NULL)",
            name=op.f("ck_items_link_only_no_derivatives"),
        ),
        sa.ForeignKeyConstraint(["feed_url"], ["feeds.url"], name=op.f("fk_items_feed_url_feeds")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_items")),
        sa.UniqueConstraint("url_hash", name=op.f("uq_items_url_hash")),
    )
    op.create_index("items_published", "items", ["published_at"])
    op.create_index("items_status", "items", ["status"])
    op.create_index(
        "items_source_guid",
        "items",
        ["source_id", "guid"],
        unique=True,
        sqlite_where=sa.text("guid IS NOT NULL"),
        postgresql_where=sa.text("guid IS NOT NULL"),
    )
    op.create_table(
        "clusters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_item_at", sa.Text(), nullable=False),
        sa.Column("category", sa.Text()),
        sa.Column("centroid", sa.LargeBinary()),
        sa.Column("entity_set", sa.Text(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("extraction", sa.Text()),
        sa.Column("item_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("source_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("score", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_open", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("merged_into", sa.Integer()),
        sa.ForeignKeyConstraint(
            ["merged_into"], ["clusters.id"], name=op.f("fk_clusters_merged_into_clusters")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clusters")),
    )
    op.create_index("clusters_open", "clusters", ["is_open", "last_item_at"])
    op.create_table(
        "cluster_items",
        sa.Column("cluster_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("similarity", sa.Float()),
        sa.Column("added_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["cluster_id"], ["clusters.id"], name=op.f("fk_cluster_items_cluster_id_clusters")
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_cluster_items_item_id_items"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("cluster_id", "item_id", name=op.f("pk_cluster_items")),
    )
    op.create_table(
        "stories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cluster_id", sa.Integer(), nullable=False),
        sa.Column("edition_lang", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("current_rev", sa.Integer()),
        sa.Column("rank_score", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_top", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("written_hash", sa.Text()),
        sa.Column("owner_edited", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("first_published_at", sa.Text()),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'needs_review', 'approved', 'published', 'rejected', 'unpublished')",
            name=op.f("ck_stories_status"),
        ),
        sa.ForeignKeyConstraint(
            ["cluster_id"], ["clusters.id"], name=op.f("fk_stories_cluster_id_clusters")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stories")),
        sa.UniqueConstraint("cluster_id", name=op.f("uq_stories_cluster_id")),
        sa.UniqueConstraint("path", name=op.f("uq_stories_path")),
        sa.UniqueConstraint("edition_lang", "slug", name="uq_stories_edition_lang_slug"),
    )
    op.create_table(
        "story_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("story_id", sa.Integer(), nullable=False),
        sa.Column("rev_no", sa.Integer(), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("why_it_matters", sa.Text()),
        sa.Column("claims", sa.Text(), nullable=False),
        sa.Column("sources", sa.Text(), nullable=False),
        sa.Column("verification", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column("prompt_version", sa.Text()),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), server_default=sa.text("'llm'"), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "created_by IN ('pipeline', 'owner')", name=op.f("ck_story_revisions_created_by")
        ),
        sa.CheckConstraint(
            "kind IN ('llm', 'links', 'template', 'owner_blurb')", name=op.f("ck_story_revisions_kind")
        ),
        sa.ForeignKeyConstraint(
            ["story_id"], ["stories.id"], name=op.f("fk_story_revisions_story_id_stories")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_story_revisions")),
        sa.UniqueConstraint("story_id", "rev_no", name="uq_story_revisions_story_id_rev_no"),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text()),
        sa.Column("ok", sa.Integer()),
        sa.Column("stats", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer()),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("cached_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("reasoning_tokens", sa.Integer()),
        sa.Column("cost_micro_usd", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('extract', 'synthesize', 'verify', 'eval')", name=op.f("ck_llm_calls_purpose")
        ),
        sa.CheckConstraint(
            "outcome IN ('ok', 'rate_limited', 'invalid_output', 'safety_block', 'error')",
            name=op.f("ck_llm_calls_outcome"),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_llm_calls_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_index("llm_calls_window", "llm_calls", ["provider", "model", "created_at"])
    op.create_table(
        "kv",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_kv")),
    )


def downgrade() -> None:
    op.drop_table("kv")
    op.drop_index("llm_calls_window", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_table("runs")
    op.drop_table("story_revisions")
    op.drop_table("stories")
    op.drop_table("cluster_items")
    op.drop_index("clusters_open", table_name="clusters")
    op.drop_table("clusters")
    op.drop_index("items_source_guid", table_name="items")
    op.drop_index("items_status", table_name="items")
    op.drop_index("items_published", table_name="items")
    op.drop_table("items")
    op.drop_table("host_policy")
    op.drop_table("feeds")
