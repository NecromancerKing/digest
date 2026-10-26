"""The pipeline's copy of stories and their revisions (spec section 7).

Links cards, template stories and LLM stories all land here; Phase 5 writes them to the
content repository as story files. A revision is added only when its content changed, so
re-running a stage creates nothing new.

Which revision kind wins, highest first: the owner's blurb, an approved LLM story, a
template story, a links card. Stories the owner rejected, unpublished or edited are never
touched again by the pipeline.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, func, select

from digest.db.schema import stories, story_revisions
from digest.timeutil import iso

SLUG_MAX = 60
FROZEN_STATUSES = frozenset({"rejected", "unpublished"})
KIND_PRIORITY = {"links": 0, "template": 1, "llm": 2, "owner_blurb": 3}


def slugify(text: str, max_len: int = SLUG_MAX) -> str:
    """ASCII, lower case, hyphens: ``"Economy · Riksbank"`` -> ``economy-riksbank``."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0] or slug[:max_len]
    return slug or "story"


@dataclass(frozen=True)
class Revision:
    kind: str  # llm | links | template | owner_blurb
    headline: str
    summary: str | None
    sources: list[dict[str, Any]]
    claims: list[dict[str, Any]] = field(default_factory=lambda: [])
    why_it_matters: str | None = None
    key_facts: list[str] = field(default_factory=lambda: [])
    verification: dict[str, Any] = field(default_factory=lambda: {})
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    created_by: str = "pipeline"

    def content_key(self) -> str:
        """What a reader would see; a revision is new only when this changes."""
        return json.dumps(
            [
                self.kind,
                self.headline,
                self.summary,
                self.why_it_matters,
                self.key_facts,
                self.sources,
            ],
            sort_keys=True,
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class StoryRow:
    id: int
    cluster_id: int
    status: str
    owner_edited: bool
    current_rev: int | None
    current_kind: str | None


def find_story(conn: Connection, cluster_id: int) -> StoryRow | None:
    row = conn.execute(select(stories).where(stories.c.cluster_id == cluster_id)).first()
    if row is None:
        return None
    kind = None
    if row.current_rev is not None:
        kind = conn.execute(
            select(story_revisions.c.kind).where(
                story_revisions.c.story_id == row.id,
                story_revisions.c.rev_no == row.current_rev,
            )
        ).scalar_one_or_none()
    return StoryRow(
        id=row.id,
        cluster_id=row.cluster_id,
        status=row.status,
        owner_edited=bool(row.owner_edited),
        current_rev=row.current_rev,
        current_kind=kind,
    )


def is_frozen(story: StoryRow) -> bool:
    """Rejected, unpublished or owner-edited stories are left alone (spec section 12)."""
    return story.status in FROZEN_STATUSES or story.owner_edited


def _unique_slug(conn: Connection, lang: str, base: str) -> str:
    taken = set(
        conn.execute(
            select(stories.c.slug).where(
                stories.c.edition_lang == lang, stories.c.slug.like(f"{base}%")
            )
        ).scalars()
    )
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def create_story(
    conn: Connection,
    *,
    cluster_id: int,
    category: str,
    slug_basis: str,
    lang: str,
    rank_score: float,
    now: dt.datetime,
    zone: ZoneInfo,
) -> StoryRow:
    slug = _unique_slug(conn, lang, slugify(slug_basis))
    day = now.astimezone(zone).date().isoformat()
    story_id = conn.execute(
        stories.insert()
        .values(
            cluster_id=cluster_id,
            edition_lang=lang,
            slug=slug,
            path=f"stories/{day}/{slug}.yaml",
            category=category,
            status="draft",
            rank_score=rank_score,
            updated_at=iso(now),
        )
        .returning(stories.c.id)
    ).scalar_one()
    return StoryRow(story_id, cluster_id, "draft", False, None, None)


def current_revision(conn: Connection, story: StoryRow) -> Revision | None:
    if story.current_rev is None:
        return None
    r = conn.execute(
        select(story_revisions).where(
            story_revisions.c.story_id == story.id,
            story_revisions.c.rev_no == story.current_rev,
        )
    ).one()
    return Revision(
        kind=r.kind,
        headline=r.headline,
        summary=r.summary,
        sources=json.loads(r.sources),
        claims=json.loads(r.claims),
        why_it_matters=r.why_it_matters,
        key_facts=json.loads(r.key_facts) if r.key_facts else [],
        verification=json.loads(r.verification),
        provider=r.provider,
        model=r.model,
        prompt_version=r.prompt_version,
        created_by=r.created_by,
    )


def add_revision(
    conn: Connection,
    story: StoryRow,
    rev: Revision,
    *,
    status: str,
    now: dt.datetime,
    category: str | None = None,
    rank_score: float | None = None,
) -> bool:
    """Add ``rev`` as the story's current revision if its content changed. True if added.

    ``status`` is the pipeline's verdict (approved or needs_review); a published story
    stays published, and frozen stories are never changed.
    """
    if is_frozen(story):
        return False
    current = current_revision(conn, story)
    values: dict[str, Any] = {}
    if category is not None:
        values["category"] = category
    if rank_score is not None:
        values["rank_score"] = rank_score
    if current is not None and current.content_key() == rev.content_key():
        if values:
            conn.execute(stories.update().where(stories.c.id == story.id).values(**values))
        return False
    rev_no = (
        conn.execute(
            select(func.max(story_revisions.c.rev_no)).where(story_revisions.c.story_id == story.id)
        ).scalar_one()
        or 0
    ) + 1
    conn.execute(
        story_revisions.insert().values(
            story_id=story.id,
            rev_no=rev_no,
            headline=rev.headline,
            summary=rev.summary,
            why_it_matters=rev.why_it_matters,
            key_facts=json.dumps(rev.key_facts, ensure_ascii=False) if rev.key_facts else None,
            claims=json.dumps(rev.claims, ensure_ascii=False),
            sources=json.dumps(rev.sources, ensure_ascii=False),
            verification=json.dumps(rev.verification, ensure_ascii=False, sort_keys=True),
            provider=rev.provider,
            model=rev.model,
            prompt_version=rev.prompt_version,
            created_by=rev.created_by,
            kind=rev.kind,
            created_at=iso(now),
        )
    )
    new_status = "published" if story.status == "published" else status
    conn.execute(
        stories.update()
        .where(stories.c.id == story.id)
        .values(current_rev=rev_no, status=new_status, updated_at=iso(now), **values)
    )
    return True


def word_count(text: str) -> int:
    return len(text.split())


def blurb_revision(base: Revision, blurb: str, max_words: int) -> Revision:
    """The owner's blurb on top of a card: shown as the summary, "Written by the editor".

    Raises ``ValueError`` if it is empty or longer than ``max_words``.
    """
    text = " ".join(blurb.split())
    if not text:
        raise ValueError("owner blurb is empty")
    if word_count(text) > max_words:
        raise ValueError(f"owner blurb has {word_count(text)} words; at most {max_words}")
    return Revision(
        kind="owner_blurb",
        headline=base.headline,
        summary=text,
        sources=base.sources,
        verification=base.verification,
        created_by="owner",
    )
