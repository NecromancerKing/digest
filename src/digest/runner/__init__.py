"""``digest run --once``: one pipeline run (spec section 5, run model).

Download the state file, run every due stage in order under the time budget, upload the
state at checkpoints, exit. A run that fails midway uploads nothing more, so the last
checkpoint stays the newest good state.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import Engine, select

from digest import db
from digest.config import Config, Settings
from digest.db.schema import kv, runs
from digest.housekeeping import prune, purge_full_text
from digest.ingest import make_fetcher
from digest.ingest.stage import DataContext, run_ingest
from digest.links.stage import run_links
from digest.nlp.embed import Embedder, FastEmbedder
from digest.nlp.stages import run_cluster, run_embed, run_maintenance, run_rank
from digest.state import StateStore, make_state_store
from digest.timeutil import iso, utcnow

log = logging.getLogger(__name__)


class TimeBudget:
    """Seconds left before the run stops starting new work (``RUN_TIME_BUDGET_MINUTES``)."""

    def __init__(self, minutes: float, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._deadline = monotonic() + minutes * 60

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._monotonic())

    def exhausted(self) -> bool:
        return self.remaining() <= 0


@dataclass
class RunResult:
    ok: bool
    stats: dict[str, Any] = field(default_factory=lambda: {})
    error_type: str | None = None


def get_kv(engine: Engine, key: str) -> str | None:
    with engine.connect() as conn:
        return conn.execute(select(kv.c.value).where(kv.c.key == key)).scalar_one_or_none()


def set_kv(engine: Engine, key: str, value: str) -> None:
    with engine.begin() as conn:
        if not conn.execute(kv.update().where(kv.c.key == key).values(value=value)).rowcount:
            conn.execute(kv.insert().values(key=key, value=value))


def write_status_file(path: Path, day: dt.date, payload: dict[str, Any]) -> bool:
    """Rewrite the keep-alive status file once per local day. True if it changed.

    The file is committed to the public repository, so it holds counts only.
    """
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(current, dict) and current.get("day") == day.isoformat():  # pyright: ignore[reportUnknownMemberType]
            return False
    except (OSError, ValueError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"day": day.isoformat(), **payload}
    path.write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return True


class Run:
    def __init__(
        self,
        settings: Settings,
        config: Config,
        *,
        store: StateStore | None = None,
        http_client: httpx.Client | None = None,
        now: Callable[[], dt.datetime] = utcnow,
        budget: TimeBudget | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.settings = settings
        self._embedder = embedder
        self.config = config
        self.budget = budget or TimeBudget(settings.run_time_budget_minutes)
        self._store = store
        self.http_client = http_client
        self.now = now
        self.stats: dict[str, Any] = {"timings_ms": {}}
        # Local commands never upload to GitHub; only pipeline.yml sets STATE_UPLOAD.
        self.upload = settings.state_store == "local_file" or settings.state_upload

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = FastEmbedder(self.settings.embed_model, self.settings.embed_cache_dir)
        return self._embedder

    @property
    def store(self) -> StateStore:
        if self._store is None:
            self._store = make_state_store(self.settings, self.config.settings)
        return self._store

    def _timed(self, name: str, fn: Callable[[], Any]) -> Any:
        t0 = time.monotonic()
        try:
            return fn()
        finally:
            self.stats["timings_ms"][name] = round((time.monotonic() - t0) * 1000)

    def _checkpoint(self, name: str) -> None:
        if not self.upload:
            log.info("checkpoint_skipped", extra={"stage": name, "reason": "uploads_disabled"})
            return
        file = self._timed(f"upload_{name}", lambda: self.store.push(self.settings.state_path))
        self.stats.setdefault("checkpoints", 0)
        self.stats["checkpoints"] += 1
        log.info("checkpoint_uploaded", extra={"stage": name, "file": file})

    def _pull(self) -> Engine:
        path = self.settings.state_path
        result = self._timed("download", lambda: self.store.pull(path))
        if result.fell_back:
            log.warning("state_fallback", extra={"rejected": len(result.rejected)})
        if result.source is None:
            log.info("state_fresh_start")
        self.stats["state"] = {"fresh": result.source is None, "rejected": len(result.rejected)}
        db.migrate(path)
        return db.make_engine(path)

    def _snapshot_if_due(self, engine: Engine) -> None:
        local = self.now().astimezone(self.settings.zone)
        if local.time() < self.config.settings.schedule.snapshot_after or not self.upload:
            return
        today = local.date().isoformat()
        if get_kv(engine, "last_snapshot_day") == today:
            return
        name = self._timed(
            "snapshot", lambda: self.store.push_snapshot(self.settings.state_path, local.date())
        )
        set_kv(engine, "last_snapshot_day", today)
        log.info("snapshot_uploaded", extra={"file": name})

    def execute(self) -> RunResult:
        started = self.now()
        engine: Engine | None = None
        run_id: int | None = None
        if not self.config.enabled_sources:
            # Nothing can change: don't touch the state (and don't need its token yet).
            log.info("run_skipped", extra={"reason": "no_enabled_sources"})
            result = RunResult(ok=True, stats={"skipped": 1})
            self._write_status(started, result)
            return result
        try:
            engine = self._pull()
            with engine.begin() as conn:
                run_id = conn.execute(
                    runs.insert().values(started_at=iso(started)).returning(runs.c.id)
                ).scalar_one()
            log.info(
                "run_started", extra={"run_id": run_id, "state_store": self.settings.state_store}
            )

            self._timed("housekeeping", lambda: self._housekeeping(engine))
            self._timed("ingest", lambda: self._ingest(engine))
            self._checkpoint("ingest")
            self._group(engine)
            self._checkpoint("group")
            self._stories(engine)

            self._snapshot_if_due(engine)

            self.stats["state"]["size_bytes"] = self.settings.state_path.stat().st_size
            with engine.begin() as conn:
                conn.execute(
                    runs.update()
                    .where(runs.c.id == run_id)
                    .values(finished_at=iso(self.now()), ok=1, stats=json.dumps(self.stats))
                )
            self._checkpoint("end")
            log.info("run_finished", extra={"run_id": run_id, **_flat_counts(self.stats)})
            result = RunResult(ok=True, stats=self.stats)
        except Exception as e:
            log.error(
                "run_failed",
                extra={
                    "run_id": run_id,
                    "error_type": type(e).__name__,
                    "http_status": getattr(e, "status", None),
                },
            )
            result = RunResult(ok=False, stats=self.stats, error_type=type(e).__name__)
        finally:
            if engine is not None:
                engine.dispose()
        self._write_status(started, result)
        return result

    def _write_status(self, started: dt.datetime, result: RunResult) -> None:
        write_status_file(
            self.settings.status_file,
            started.astimezone(self.settings.zone).date(),
            {
                "run_started_at": iso(started),
                "ok": result.ok,
                "error_type": result.error_type,
                "counts": _flat_counts(result.stats),
            },
        )

    def _housekeeping(self, engine: Engine) -> None:
        now = self.now()
        purged = purge_full_text(engine, now, self.settings.fulltext_retention_hours)
        retention = self.config.settings.retention
        self.stats["housekeeping"] = {
            "full_text_purged": purged,
            **prune(
                engine,
                now,
                item_days=self.settings.item_retention_days,
                runs_days=retention.runs_days,
                llm_calls_days=retention.llm_calls_days,
            ),
        }

    def _group(self, engine: Engine) -> None:
        """Stages 2-4: embed, cluster (with maintenance when due), rank and select."""
        if self.settings.cluster_method != "embeddings":
            log.warning("stage_skipped", extra={"stage": "cluster", "reason": "tfidf_in_phase_3"})
            return
        if self.budget.remaining() < 60:
            log.info("stage_skipped", extra={"stage": "embed", "reason": "time_budget"})
            return
        self.stats["embed"] = self._timed(
            "embed", lambda: run_embed(engine, self.embedder, self.config, self.budget)
        )
        self.stats["cluster"] = self._timed(
            "cluster", lambda: run_cluster(engine, self.config, self.now())
        )
        every = dt.timedelta(hours=self.config.settings.cluster.maintenance_every_hours)
        last = get_kv(engine, "last_cluster_maintenance")
        if last is None or self.now() - dt.datetime.fromisoformat(last) >= every:
            self.stats["maintenance"] = self._timed(
                "maintenance", lambda: run_maintenance(engine, self.config, self.now())
            )
            set_kv(engine, "last_cluster_maintenance", iso(self.now()))
        self.stats["rank"] = self._timed(
            "rank", lambda: run_rank(engine, self.config, self.settings.zone, self.now())
        )

    def _stories(self, engine: Engine) -> None:
        """Stories for selected clusters: links cards, template stories (and the fallback)."""
        self.stats["links"] = self._timed(
            "links",
            lambda: run_links(engine, self.config, self.settings, self.now(), self.settings.zone),
        )

    def _ingest(self, engine: Engine) -> None:
        sources = self.config.enabled_sources
        fetcher = make_fetcher(self.settings, self.config, engine, sources, client=self.http_client)
        try:
            ingest = run_ingest(
                engine,
                self.config,
                fetcher,
                self.budget,
                retention_hours=self.settings.fulltext_retention_hours,
                now=self.now,
                data=DataContext(
                    lang=self.settings.edition_lang,
                    zone=self.settings.zone,
                    enabled=self.settings.template_stories,
                ),
            )
        finally:
            if self.http_client is None:
                fetcher.close()
        self.stats["ingest"] = ingest.as_dict()
        self.stats["fetch"] = fetcher.stats.as_dict()


def _flat_counts(stats: dict[str, Any]) -> dict[str, int | float]:
    """``{"ingest": {"items_new": 3}}`` -> ``{"ingest_items_new": 3}``; numbers only."""
    out: dict[str, int | float] = {}
    for group, values in stats.items():
        if isinstance(values, dict):
            for k, v in values.items():  # pyright: ignore[reportUnknownVariableType]
                if isinstance(v, int | float) and not isinstance(v, bool):
                    out[f"{group}_{k}"] = v
        elif isinstance(values, int | float) and not isinstance(values, bool):
            out[group] = values
    return out


def run_once(settings: Settings, config: Config, **kwargs: Any) -> RunResult:
    return Run(settings, config, **kwargs).execute()
