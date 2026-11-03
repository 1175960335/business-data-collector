"""Stage orchestration with checkpoints, resume and a stop target.

Task book 004, stage C:
  "同配置可续跑，配置变化不能误用旧检查点"
  "target_count 是停止目标，不保证来源一定足够；不够时报告缺口"
  "验收：一次配置驱动 Go → 原始文件 → SQLite → 基础导出；中断编排进程后能恢复；
   重复执行不重复实体、不丢新证据。"

Run directory layout::

    <output.directory>/<fingerprint12>/run-<id>/
        raw/       discover-<label>-a<attempt>.jsonl
        logs/      <label>-a<attempt>.stdout.log / .stderr.log
        queries/   <label>.txt
        export/    entities.csv / entities.jsonl / field_facts.jsonl / errors.jsonl

The database lives at ``<output.directory>/<output.database>`` and is shared by
every run, so entity identity survives across runs.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from . import db, export, ingest
from .config import NEVER_LIVE_PROVIDERS, Config
from .errors import CollectorError, ResumeError
from .goadapter import GoAdapter
from .util import utc_now_iso
from .version import EXTRACTOR_VERSION, SCHEMA_VERSION, __version__

STAGES = ("discover", "ingest", "merge", "sitediscovery", "enrich", "export")

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def slug(text: str, limit: int = 40) -> str:
    cleaned = _SLUG.sub("-", text).strip("-")
    return (cleaned or "query")[:limit]


@dataclass
class TaskPlan:
    label: str
    query: str
    argv: list[str]
    raw_path: pathlib.Path
    stdout_path: pathlib.Path
    stderr_path: pathlib.Path
    queries_path: pathlib.Path
    attempt: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "query": self.query,
            "attempt": self.attempt,
            "argv": self.argv,
            "raw_path": str(self.raw_path),
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
        }


@dataclass
class RunResult:
    run_id: int
    fingerprint: str
    status: str
    target_count: int
    collected: int
    shortfall: int
    resumed: bool
    stages: dict[str, str] = field(default_factory=dict)
    exports: dict[str, str] = field(default_factory=dict)
    run_dir: str = ""
    database: str = ""
    notes: list[str] = field(default_factory=list)
    ingest_stats: dict[str, Any] = field(default_factory=dict)
    merge_stats: dict[str, Any] = field(default_factory=dict)
    enrich_stats: dict[str, Any] = field(default_factory=dict)
    sitediscovery_stats: dict[str, Any] = field(default_factory=dict)
    #: Why collection stopped, as a closed code plus the reader's sentence
    #: (round 026 P2.4). ``target_count`` is a *collection* target, so a run
    #: that stopped early and one that hit its target are different outcomes and
    #: must not both read as "finished".
    stop_reason: str = ""
    stop_note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "target_count": self.target_count,
            "collected": self.collected,
            "shortfall": self.shortfall,
            "resumed": self.resumed,
            "stages": self.stages,
            "exports": self.exports,
            "run_dir": self.run_dir,
            "database": self.database,
            "notes": self.notes,
            "ingest_stats": self.ingest_stats,
            "merge_stats": self.merge_stats,
            "enrich_stats": self.enrich_stats,
            "sitediscovery_stats": self.sitediscovery_stats,
            "stop_reason": self.stop_reason,
            "stop_note": self.stop_note,
        }


class Orchestrator:
    """Drives one configuration from the scraper to exported files."""

    def __init__(
        self,
        cfg: Config,
        *,
        base_dir: str | pathlib.Path | None = None,
        go_binary: str | pathlib.Path | None = None,
        adapter: GoAdapter | None = None,
        enricher: Any | None = None,
        discoverer: Any | None = None,
        providers: list[Any] | None = None,
    ):
        self.cfg = cfg
        self.base_dir = pathlib.Path(base_dir or os.getcwd())

        binary = go_binary or cfg.go_binary or self._default_binary()
        self.adapter = adapter or GoAdapter(binary=pathlib.Path(binary), proxy=cfg.proxy)

        # Imported here rather than at module level: the merge stage needs
        # libphonenumber, and a run that never merges should not pay for it.
        from .sources import build_providers

        self._providers = providers if providers is not None else build_providers(cfg)
        self._adapter_seeded = False

        # One cache for the run, shared by the two stages that read business
        # pages (045.3). Discovery reads a site's contact page to decide
        # ownership; enrichment would otherwise read the same page again a
        # minute later. A hit costs no request, so the run's budget goes
        # further without either stage reaching a different conclusion.
        from .webfetch import PageCache

        self._page_cache = PageCache()

        # Imported lazily so a run that does not enrich does not pay for the
        # DNS and libphonenumber imports.
        if enricher is not None:
            self._enricher = enricher
        else:
            from .enrich import Enricher

            self._enricher = Enricher(cfg, page_cache=self._page_cache)

        # Candidate-site discovery. Lazily imported for the same reason as the
        # enricher: a run that does not discover must not pay for the HTML
        # extraction imports. Its own stage sits between merge and enrich, so a
        # site it confirms is one the enricher then visits.
        if discoverer is not None:
            self._discoverer = discoverer
        else:
            from .sitediscovery import SiteDiscoverer

            self._discoverer = SiteDiscoverer(cfg, page_cache=self._page_cache)

    @property
    def providers(self) -> list[Any]:
        """The discovery sources this run uses, in planning order.

        A source the configuration lists but cannot run (no endpoint, no file, no
        directory entries) is not silently dropped: it is reported by
        :meth:`sources_manifest` as not configured, so "配了却不生效" can never be
        mistaken for success.
        """
        return list(self._providers)

    def sources_manifest(self) -> list[dict[str, Any]]:
        """Every provider's licence, attribution and configuration state."""
        from .sources import manifest

        return manifest(self.cfg)

    @property
    def enricher(self):
        return self._enricher

    @property
    def discoverer(self):
        return self._discoverer

    # -- paths ---------------------------------------------------------------

    def _default_binary(self) -> pathlib.Path:
        return self.base_dir / "google-maps-scraper" / "bin" / "google-maps-scraper.exe"

    @property
    def output_dir(self) -> pathlib.Path:
        path = pathlib.Path(self.cfg.output_directory)
        return path if path.is_absolute() else self.base_dir / path

    @property
    def database_path(self) -> pathlib.Path:
        path = pathlib.Path(self.cfg.database_name)
        return path if path.is_absolute() else self.output_dir / path

    def run_dir(self, run_id: int) -> pathlib.Path:
        return self.output_dir / self.cfg.short_fingerprint() / f"run-{run_id}"

    def _sub(self, run_id: int, name: str) -> pathlib.Path:
        path = pathlib.Path(name)
        base = self.run_dir(run_id)
        return path if path.is_absolute() else base / path

    # -- planning ------------------------------------------------------------

    def plan(self, run_id: int = 0, *, conn=None) -> dict[str, Any]:
        """Describe what a run would do, without executing anything.

        The task list comes from the providers, in planning order, so a
        multi-source run shows every source's work rather than only the Go
        scraper's. Google's argv is filled in here because it is the only
        provider whose task is an external command, and reviewing that command
        line is how a run gets audited.
        """
        tasks: list[dict[str, Any]] = []
        for provider in self.providers:
            for task in provider.tasks():
                entry = task.as_dict()
                if provider.name == "google_maps":
                    entry["argv"] = self._task_plan(run_id, task.query, attempt=1).argv
                tasks.append(entry)

        return {
            "fingerprint": self.cfg.fingerprint(),
            "short_fingerprint": self.cfg.short_fingerprint(),
            "collector_version": __version__,
            "extractor_version": EXTRACTOR_VERSION,
            "schema_version": SCHEMA_VERSION,
            "database": str(self.database_path),
            "run_dir": str(self.run_dir(run_id)),
            "go_binary": str(self.adapter.binary),
            "go_binary_present": self.adapter.exists(),
            "fetch_flags": self.cfg.fetch_flags(),
            "collect_fields": self.cfg.collect_fields,
            "export_fields": self.cfg.export_fields,
            "sources": self.sources_manifest(),
            "enrich": {
                "enabled": self.cfg.enrich_enabled,
                "fields": self.cfg.enrich_fields,
                "pages_per_site": self.cfg.enrich_pages_per_site,
                "request_budget": self.cfg.enrich_request_budget,
                "time_budget_seconds": self.cfg.enrich_time_budget_seconds,
            },
            # Every finder is listed, configured or not: a plan that showed only
            # the usable ones would hide that no search service is available,
            # which is exactly the difference between "found nothing" and "could
            # not look" (task book 026 P1.1).
            "sitediscovery": {
                "enabled": self.cfg.sitediscovery_enabled,
                "sources": self.cfg.sitediscovery_sources,
                "pages_per_site": self.cfg.sitediscovery_pages_per_site,
                "request_budget": self.cfg.sitediscovery_request_budget,
                "time_budget_seconds": self.cfg.sitediscovery_seconds_budget,
                "finders": self.discoverer.manifests(),
            },
            "tasks": tasks,
        }

    def _task_plan(self, run_id: int, query: str, *, attempt: int) -> TaskPlan:
        label = f"discover-{slug(query)}"
        raw_dir = self._sub(run_id, self.cfg.raw_directory_name)
        log_dir = self._sub(run_id, "logs")
        query_dir = self._sub(run_id, "queries")

        raw_path = raw_dir / f"{label}-a{attempt}.jsonl"
        queries_path = query_dir / f"{label}.txt"

        argv = self.adapter.build_argv(self.cfg, queries_file=queries_path, results_path=raw_path)

        return TaskPlan(
            label=label,
            query=query,
            argv=argv,
            raw_path=raw_path,
            stdout_path=log_dir / f"{label}-a{attempt}.stdout.log",
            stderr_path=log_dir / f"{label}-a{attempt}.stderr.log",
            queries_path=queries_path,
            attempt=attempt,
        )

    # -- run -----------------------------------------------------------------

    def run(self, *, resume: bool = True, conn=None) -> RunResult:
        owns_conn = conn is None
        conn = conn or db.connect(self.database_path)

        try:
            return self._run(conn, resume=resume)
        finally:
            if owns_conn:
                conn.close()

    def _run(self, conn, *, resume: bool) -> RunResult:
        fingerprint = self.cfg.fingerprint()
        existing = db.latest_resumable_run(conn, fingerprint)

        if existing is not None and not resume:
            raise ResumeError(
                f"run {existing['id']} for this configuration is unfinished "
                f"(status={existing['status']}). Re-run with resume enabled, or point "
                "output.directory somewhere else."
            )

        resumed = existing is not None
        if resumed:
            run_id = int(existing["id"])
            recovered = db.recover_orphan_tasks(conn, run_id)
            db.set_run_status(conn, run_id, "running")
            notes = [f"resumed run {run_id}"]
            if recovered:
                notes.append(f"recovered {recovered} task(s) left running by a previous process")
        else:
            run_id = db.create_run(
                conn,
                fingerprint=fingerprint,
                config_json=self.cfg.canonical_json(),
                config_source=self.cfg.source,
                collector_version=__version__,
                schema_version=SCHEMA_VERSION,
                go_binary=str(self.adapter.binary),
                go_version=self.adapter.version(),
                target_count=self.cfg.target_count,
            )
            notes = [f"new run {run_id}"]

        run_dir = self.run_dir(run_id)
        result = RunResult(
            run_id=run_id,
            fingerprint=fingerprint,
            status="running",
            target_count=self.cfg.target_count,
            collected=0,
            shortfall=0,
            resumed=resumed,
            run_dir=str(run_dir),
            database=str(self.database_path),
            notes=notes,
        )

        try:
            result.stages["discover"] = self._stage_discover(conn, run_id)
            result.stages["ingest"] = self._stage_ingest(conn, run_id, result)
            result.stages["merge"] = self._stage_merge(conn, run_id)
            result.stages["sitediscovery"] = self._stage_sitediscovery(conn, run_id, result)
            result.stages["enrich"] = self._stage_enrich(conn, run_id, result)
            result.stages["export"] = self._stage_export(conn, run_id, result)
        except KeyboardInterrupt:
            db.set_run_status(conn, run_id, "interrupted")
            raise
        except CollectorError as exc:
            # The traceback travels with the error row. 052's "database is
            # locked" run ended with exactly that sentence and nothing else: no
            # stage, no statement, no file.  A one-line verdict that cannot be
            # traced back to a line of code is not an answer, it is a rumour.
            db.insert_error(
                conn, run_id=run_id, stage="orchestrator", kind=type(exc).__name__, message=str(exc),
                context={"traceback": traceback.format_exception(exc)},
            )
            db.finish_run(conn, run_id, "failed", note=str(exc))
            result.status = "failed"
            result.notes.append(str(exc))
            result.collected = db.count_run_stores(conn, run_id)
            result.shortfall = max(0, self.cfg.target_count - result.collected)
            return result

        result.collected = db.count_run_stores(conn, run_id)
        result.shortfall = max(0, self.cfg.target_count - result.collected)
        result.status = self._final_status(conn, run_id, result)
        result.stop_reason, result.stop_note = self._stop_reason(conn, run_id, result)

        note_parts = [f"collected {result.collected}/{result.target_count} entities"]
        if result.shortfall:
            note_parts.append(
                f"shortfall {result.shortfall}: the sources did not yield enough distinct businesses"
            )
        if result.stop_note:
            note_parts.append(result.stop_note)
        db.set_checkpoint(
            conn,
            run_id,
            "run",
            "summary",
            {
                "collected": result.collected,
                "target_count": result.target_count,
                "shortfall": result.shortfall,
                "status": result.status,
                "stop_reason": result.stop_reason,
                "stop_note": result.stop_note,
                "finished_at": utc_now_iso(),
            },
        )
        db.finish_run(conn, run_id, result.status, note="; ".join(note_parts))
        result.notes.extend(note_parts)
        return result

    #: Reader-facing sentences for :attr:`RunResult.stop_reason`. Kept next to
    #: the codes so a new code cannot be added without a sentence.
    STOP_NOTES = {
        "target_reached": "已达到目标条数",
        "sources_exhausted": "来源已给不出更多不重复的商家，未达到目标条数",
        "source_cooling_down": "来源被限速（冷却中），本批到此为止",
        "budget_exhausted": "请求或时间预算用尽",
        "source_failed": "来源采集失败",
        "no_tasks": "没有可执行的任务（来源未配置或未计划）",
        "failed": "运行失败，详见错误记录",
    }

    def _stop_reason(self, conn, run_id: int, result: RunResult) -> tuple[str, str]:
        """Why collection ended, and the sentence that says it.

        ``target_count`` caps *collection*, and the counters that matter to a
        reader are the match verdicts, so "it stopped" needs a reason: the
        target, the sources running dry, a cooldown, or a failure. Without this
        a run that hit a 429 on page 2 and a run that genuinely ran out of
        businesses both just show a smaller number (task book 026 P2.4).
        """
        tasks = db.list_provider_tasks(conn, run_id)
        if not tasks:
            return "no_tasks", self.STOP_NOTES["no_tasks"]
        statuses = [str(row["status"]) for row in tasks]
        if result.status == "failed":
            return "failed", self.STOP_NOTES["failed"]
        if result.shortfall == 0:
            return "target_reached", self.STOP_NOTES["target_reached"]
        if any(status == "failed" for status in statuses):
            return "source_failed", self.STOP_NOTES["source_failed"]
        for entry in db.list_source_status(conn):
            if entry.get("cooling_down") and entry.get("source") in self.cfg.sources_list:
                return "source_cooling_down", (
                    f"{self.STOP_NOTES['source_cooling_down']}：{entry['source']} "
                    f"冷却到 {entry.get('cooldown_until')}"
                )
        reason = self._budget_reason(conn, run_id)
        if reason:
            return "budget_exhausted", f"{self.STOP_NOTES['budget_exhausted']}（{reason}）"
        return "sources_exhausted", self.STOP_NOTES["sources_exhausted"]

    def _budget_reason(self, conn, run_id: int) -> str:
        """The run budget's own reason, when it is what stopped the run."""
        rows = conn.execute(
            "SELECT error FROM provider_tasks WHERE run_id = ? AND status = 'skipped'",
            (run_id,),
        ).fetchall()
        for row in rows:
            error = str(row["error"] or "")
            if "budget" in error:
                return error
        return ""

    def _final_status(self, conn, run_id: int, result: RunResult) -> str:
        tasks = db.list_provider_tasks(conn, run_id)
        if not tasks:
            return "failed"
        statuses = [row["status"] for row in tasks]
        # A task skipped because the stop target was reached is not a failure.
        if all(status in ("completed", "skipped") for status in statuses):
            return "completed"
        if any(status == "completed" for status in statuses):
            return "partial"
        return "failed"

    # -- stages --------------------------------------------------------------

    def _stage_done(self, conn, run_id: int, stage: str) -> bool:
        row = db.get_stage(conn, run_id, stage)
        return row is not None and row["status"] == "completed"

    def _stage_discover(self, conn, run_id: int) -> str:
        if self._stage_done(conn, run_id, "discover"):
            return "skipped (already completed)"

        db.set_stage(conn, run_id, "discover", "running", touch_started=True)

        started_at = time.monotonic()
        started_tasks = 0
        failed = 0
        skipped = 0
        failed_sources: list[str] = []
        completed_sources: list[str] = []
        notes: list[str] = []

        try:
            for provider in self.providers:
                available, reason = provider.availability()
                if not available:
                    # A listed source that cannot run is recorded, never
                    # silently skipped: pretending it ran would inflate the
                    # result and hide a configuration mistake.
                    skipped += 1
                    notes.append(f"{provider.name}: {reason}")
                    continue

                tasks = provider.tasks()
                if not tasks:
                    skipped += 1
                    notes.append(f"{provider.name}: no search task could be planned")
                    continue

                source_failed = 0
                source_tasks = 0
                for task in tasks:
                    if db.count_run_stores(conn, run_id) >= self.cfg.target_count:
                        # target_count is a stop target: once it is met there is
                        # no reason to spend more requests, and the remaining
                        # tasks are recorded as skipped rather than silently
                        # dropped.
                        self._skip_remaining(
                            conn, run_id, provider.name, tasks, task, "target_count reached"
                        )
                        return self._finish_discover(
                            conn,
                            run_id,
                            started_tasks,
                            failed,
                            skipped,
                            completed_sources,
                            failed_sources,
                            notes,
                            tail=(
                                f"target_count {self.cfg.target_count} reached; "
                                f"{self._remaining_tasks(tasks, task)} task(s) not run"
                            ),
                        )

                    exhausted = self._budget_exhausted(started_tasks, started_at)
                    if exhausted:
                        self._skip_remaining(
                            conn, run_id, provider.name, tasks, task, exhausted
                        )
                        return self._finish_discover(
                            conn,
                            run_id,
                            started_tasks,
                            failed,
                            skipped,
                            completed_sources,
                            failed_sources,
                            notes,
                            tail=f"{exhausted}; {self._remaining_tasks(tasks, task)} task(s) not run",
                        )

                    ok, _reason_text = self._run_provider_task(
                        conn, run_id, provider, task, started_at
                    )
                    started_tasks += 1
                    source_tasks += 1
                    if not ok:
                        failed += 1
                        source_failed += 1

                if source_failed:
                    # One source failing must not abort the run: the task book
                    # requires the other sources' partial results to survive.
                    failed_sources.append(f"{provider.name} ({source_failed}/{source_tasks})")
                else:
                    completed_sources.append(provider.name)

        except CollectorError as exc:
            db.set_stage(conn, run_id, "discover", "failed", error=str(exc), touch_finished=True)
            raise

        return self._finish_discover(
            conn, run_id, started_tasks, failed, skipped, completed_sources,
            failed_sources, notes,
        )

    def _finish_discover(
        self,
        conn,
        run_id: int,
        started_tasks: int,
        failed: int,
        skipped: int,
        completed_sources: list[str],
        failed_sources: list[str],
        notes: list[str],
        tail: str = "",
    ) -> str:
        """Write the discover stage's terminal status and describe what happened.

        Every exit path goes through here, so the stage is never left at
        ``running``: a run that stopped after discovery has to be resumable,
        and a stage stuck at ``running`` is skipped by the next run instead of
        retried.
        """
        if failed:
            # Not "completed": a later resume must retry the failed task rather
            # than skip the stage. The stage status records the failure, the run
            # keeps whatever the successful tasks produced.
            db.set_stage(
                conn,
                run_id,
                "discover",
                "failed",
                error=f"{failed} task(s) failed",
                touch_finished=True,
            )
        else:
            db.set_stage(conn, run_id, "discover", "completed", touch_finished=True)

        return self._discover_report(
            started_tasks, failed, skipped, completed_sources, failed_sources, notes, tail=tail
        )

    def _discover_report(
        self,
        started_tasks: int,
        failed: int,
        skipped: int,
        completed_sources: list[str],
        failed_sources: list[str],
        notes: list[str],
        tail: str = "",
    ) -> str:
        """The human-readable summary of the discover stage."""
        parts: list[str] = []
        if failed:
            parts.append(f"{failed} failed task(s)")
        if skipped:
            parts.append(f"{skipped} unavailable source(s)")
        if notes:
            parts.append("; ".join(notes))
        if tail:
            parts.append(tail)
        elif completed_sources:
            parts.append("sources: " + ", ".join(completed_sources))
        if failed_sources:
            parts.append("failed sources: " + ", ".join(failed_sources))
        if not parts:
            return "completed"
        return "completed with " + " | ".join(parts)

    def _remaining_tasks(self, tasks: list, from_task) -> int:
        return len(tasks) - tasks.index(from_task)

    def _run_context(self, run_id: int, run_dir: pathlib.Path):
        """The shared facilities every provider of this run works through.

        The budget, the domain rate limiter and the retry policy are created once
        per stage and handed to every provider, so "所有来源共享预算、重试退避、
        域级限速" is a property of the run rather than of any one source.
        """
        from .sources.base import DomainRateLimiter, RetryPolicy, RunBudget, RunContext

        budget = RunBudget(
            max_requests=self.cfg.request_budget, max_seconds=self.cfg.time_budget_seconds
        )
        limiter = DomainRateLimiter(min_interval_seconds=self._domain_interval())
        retry = RetryPolicy()
        return RunContext(
            run_id=run_id,
            run_dir=run_dir,
            budget=budget,
            rate_limiter=limiter,
            retry=retry,
            proxy=self.cfg.proxy,
            timeout_seconds=self.cfg.timeout_seconds,
            min_interval_seconds=self._domain_interval(),
            adapters=self._seeded_adapters(),
            go_binary=self.adapter.binary,
            cwd=self.base_dir,
            # Per-source routing (``sources_map.<name>.network``). A source that
            # did not configure it is absent here and keeps the run-wide proxy.
            networks=self.cfg.source_networks(),
        )

    def _domain_interval(self) -> float:
        settings = (self.cfg.sources_map or {}).get("_politeness") or {}
        try:
            return max(0.0, float(settings.get("min_interval_seconds", 1.0)))
        except (AttributeError, TypeError, ValueError):
            return 1.0

    def _seeded_adapters(self) -> dict[str, Any]:
        """The Go adapter, shared with the Google Maps provider.

        Seeding it (rather than letting the provider build one) is what keeps a
        run started with a stub scraper using that stub — which is how the whole
        test suite exercises discovery without touching the network.
        """
        from .sources import PROVIDER_CLASSES

        adapters: dict[str, Any] = {}
        google = PROVIDER_CLASSES.get("google_maps")
        if google is not None:
            adapters[google.name] = self.adapter
        return adapters

    def _run_provider_task(self, conn, run_id: int, provider, task, started_at: float) -> tuple[bool, str]:
        """Run one task of one provider, recording it in provider_tasks."""
        existing = db.get_provider_task(conn, run_id, task.label)
        if existing is not None and existing["status"] == "completed":
            return True, ""

        attempt = int(existing["attempt"]) + 1 if existing is not None else task.attempt
        ctx = self._run_context(run_id, self.run_dir(run_id))

        # The attempt number decides the raw file name, so a retry writes a new
        # file and the previous attempt survives as evidence. Without this the
        # second attempt would overwrite the first.
        task.attempt = attempt

        # Google's argv is worth recording: it is the only source whose task is
        # an external command, and reviewing it is how a run is audited.
        argv = self._argv_for(provider, task, attempt)

        task_id = db.upsert_provider_task(
            conn,
            run_id=run_id,
            provider=provider.name,
            stage="discover",
            label=task.label,
            query=task.query,
            argv=argv,
            status="running",
            raw_path=str(ctx.raw_path(task.label, attempt)),
            stdout_path=str(ctx.log_path(task.label, attempt, "stdout")),
            stderr_path=str(ctx.log_path(task.label, attempt, "stderr")),
        )
        db.update_provider_task_paths(
            conn,
            task_id,
            raw_path=str(ctx.raw_path(task.label, attempt)),
            stdout_path=str(ctx.log_path(task.label, attempt, "stdout")),
            stderr_path=str(ctx.log_path(task.label, attempt, "stderr")),
            argv=argv,
        )
        db.update_provider_task(conn, task_id, status="running", attempt=attempt)

        outcome = provider.run_task(task, ctx)

        if outcome.skipped_reason:
            db.update_provider_task(
                conn, task_id, status="skipped", error=outcome.skipped_reason, attempt=attempt
            )
            return False, outcome.skipped_reason

        if not outcome.ok:
            db.update_provider_task(
                conn,
                task_id,
                status="failed",
                error=outcome.error,
                attempt=attempt,
                exit_code=outcome.exit_code,
            )
            # Round 026 P2: what the source is doing right now, kept where the
            # next run can see it. A rate limit earns a cooldown from the site's
            # own ``Retry-After``; anything else is recorded as a failure with
            # no cooldown, because a 404 must not make a source look unavailable.
            network = self.cfg.source_network(provider.name)
            db.note_source_failure(
                conn,
                provider.name,
                error=outcome.error,
                error_kind=outcome.error_kind or "provider_failed",
                retry_after_seconds=outcome.retry_after_seconds,
                network_mode=str(network.get("mode") or ""),
                proxy=str(network.get("proxy") or ""),
            )
            # The message carries the query, not just the label: the label is a
            # slug, and "a in Berlin" is what an operator recognises from their
            # own config file when deciding what to re-run.
            message = f"{provider.name} failed for task {task.label!r}"
            if task.query:
                message += f" (query: {task.query!r})"
            message += f": {outcome.error}"
            db.insert_error(
                conn,
                run_id=run_id,
                stage="discover",
                provider_task_id=task_id,
                kind=outcome.error_kind or "provider_failed",
                message=message,
                context={
                    "provider": provider.name,
                    "label": task.label,
                    "query": task.query,
                    "targets": list(task.targets),
                    "raw_path": str(outcome.raw_path) if outcome.raw_path else None,
                    "exit_code": outcome.exit_code,
                    "retry_after_seconds": outcome.retry_after_seconds,
                },
            )
            return False, outcome.error

        db.update_provider_task(conn, task_id, status="completed", attempt=attempt)
        # A completed task is the site answering. Recorded so the screen can say
        # "上次成功 02:31" instead of leaving the reader to guess, and so a
        # cooldown earned earlier is cleared.
        network = self.cfg.source_network(provider.name)
        db.note_source_success(
            conn,
            provider.name,
            network_mode=str(network.get("mode") or "inherit"),
            proxy=str(network.get("proxy") or ""),
        )
        # Ingest straight away so the stop target is evaluated against real
        # numbers rather than an empty table. The ingest stage re-runs this as
        # a catch-up pass and finds nothing new.
        if outcome.raw_path is not None:
            # 061: the Go scraper's envelopes state no ``acquired_via``, so its
            # rows -- fetched over the network seconds ago -- were published as
            # "获取方式：未知". The method is not guessed inside ingest (026 P0);
            # it is stated here, where the provider that wrote the file is known.
            ingested = ingest.ingest_one(
                conn,
                outcome.raw_path,
                run_id=run_id,
                cfg=self.cfg,
                default_acquired_via=_acquired_via_for(provider.name),
            )
            # Records can land after the merge has already run (this is how a
            # retried source's output arrives), so the merge has to be reopened.
            self._invalidate_stale_merge(conn, run_id, ingested)
        return True, ""

    def _invalidate_stale_merge(self, conn, run_id: int, ingested) -> None:
        """Reopen the merge stage when records arrived after it last ran.

        The merge folds duplicates among the entities it can see. Records can
        arrive later than that: discovery ingests each task's output as it lands,
        and a source that failed on the first attempt is retried on the next, so
        its records show up after the merge stage already reported "completed".
        Trusting that mark means the new records are never compared with the old
        ones and cross-source duplicates are silently missed — observed on a
        real run, where a retried Google task added 20 entities after the merge
        had run over 139 OSM ones and the run reported zero cross-source merges.

        ``merge_run`` is idempotent (``upsert_merge``), so running it again only
        costs the comparison. Nothing is reopened when the incoming batch added
        nothing, so an ordinary resume does not re-merge for no reason.
        """
        if ingested is None:
            return
        if not (ingested.observations_new > 0 or ingested.entities_new > 0):
            return
        if self._stage_done(conn, run_id, "merge"):
            db.set_stage(conn, run_id, "merge", "pending")

    def _argv_for(self, provider, task, attempt: int) -> list[str]:
        """The command line for a task, for the providers that have one."""
        if provider.name != "google_maps":
            return []
        try:
            plan = self._task_plan(0, task.query, attempt=attempt)
            return plan.argv
        except Exception:  # noqa: BLE001 - planning must not break a run
            return []

    def _budget_exhausted(self, tasks_started: int, started_at: float) -> str | None:
        """The reason discovery must not start another task, or None.

        ``runtime.budget`` is enforced, not just recorded: a request budget caps
        the number of provider tasks, and a time budget caps both the total wall
        clock and the timeout of the next task.
        """
        request_budget = self.cfg.request_budget
        if request_budget and tasks_started >= request_budget:
            return f"request budget exhausted ({request_budget} task(s))"

        time_budget = self.cfg.time_budget_seconds
        if time_budget and (time.monotonic() - started_at) >= time_budget:
            return f"time budget exhausted ({time_budget}s)"

        return None

    def _task_timeout(self, started_at: float) -> int:
        """The timeout for the next task, capped by the remaining time budget."""
        time_budget = self.cfg.time_budget_seconds
        if not time_budget:
            return self.cfg.timeout_seconds
        remaining = time_budget - (time.monotonic() - started_at)
        # At least a second, so the child is not killed before it starts.
        return max(1, min(self.cfg.timeout_seconds, int(remaining)))

    def _skip_remaining(self, conn, run_id: int, provider: str, tasks: list, from_task, reason: str) -> None:
        """Record the tasks that the stop target or a budget made unnecessary."""
        start = tasks.index(from_task)
        for task in tasks[start:]:
            if db.get_provider_task(conn, run_id, task.label) is not None:
                continue
            db.upsert_provider_task(
                conn,
                run_id=run_id,
                provider=provider,
                stage="discover",
                label=task.label,
                query=task.query,
                argv=[],
                status="skipped",
            )
            row = db.get_provider_task(conn, run_id, task.label)
            if row is not None:
                db.update_provider_task(
                    conn, int(row["id"]), status="skipped", error=reason
                )

    # -- merge (stage E) ------------------------------------------------------

    def _stage_merge(self, conn, run_id: int) -> str:
        """Fold cross-source duplicates into one store.

        The stage is skipped rather than failed when there is nothing to merge,
        because a single-source run legitimately has no duplicates — calling
        that skipped is the difference between "nothing to do" and "it broke".

        A failure inside the stage aborts the run: unlike one unreachable
        website (stage D), a merge that half-applied would silently change which
        entities the export shows, which is not something a later stage can
        recover from.
        """
        if self._stage_done(conn, run_id, "merge"):
            return "skipped (already completed)"

        db.set_stage(conn, run_id, "merge", "running", touch_started=True)
        try:
            from .merge import merge_run

            stats = merge_run(conn, run_id, self.cfg)
        except Exception as exc:  # noqa: BLE001 - recorded, then surfaced as a stage failure
            db.set_stage(conn, run_id, "merge", "failed", error=str(exc), touch_finished=True)
            raise CollectorError(f"cross-source merge failed: {exc}") from exc

        db.set_checkpoint(
            conn,
            run_id,
            "merge",
            "stats",
            {
                "entities": stats.entities,
                "pairs_considered": stats.pairs_considered,
                "merged": stats.merged,
                "refused_branch": stats.refused_branch,
                "refused_insufficient": stats.refused_insufficient,
                # 056: the kept-apart pairs whose name *and* phone agree. They are
                # the ones a reader would otherwise see as a duplicate with no
                # explanation, so the pairs themselves travel to the export.
                "branch_pairs": list(stats.branch_pairs),
                "branch_pairs_total": stats.branch_pairs_total,
                "notes": list(stats.notes),
                "finished_at": utc_now_iso(),
            },
        )
        db.set_stage(conn, run_id, "merge", "completed", touch_finished=True)
        return (
            f"completed ({stats.entities} entity(ies) -> {stats.merged} merge(s); "
            f"{stats.refused_branch} branch(es) kept separate)"
        )

    def _stage_ingest(self, conn, run_id: int, result: RunResult) -> str:
        if self._stage_done(conn, run_id, "ingest"):
            return "skipped (already completed)"

        db.set_stage(conn, run_id, "ingest", "running", touch_started=True)
        raw_dir = self._sub(run_id, self.cfg.raw_directory_name)

        try:
            ingested = ingest.ingest_directory(conn, raw_dir, run_id=run_id, cfg=self.cfg)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised as a stage failure
            db.set_stage(conn, run_id, "ingest", "failed", error=str(exc), touch_finished=True)
            raise

        # Discovery already ingested each task's output, so this pass is usually
        # empty. The checkpoint holds the run-wide totals, which is what a report
        # should quote.
        totals = ingest.run_totals(conn, run_id)
        db.set_stage(conn, run_id, "ingest", "completed", touch_finished=True)
        result.ingest_stats = totals

        # New observations invalidate an earlier merge (see the helper).
        self._invalidate_stale_merge(conn, run_id, ingested)

        return (
            f"completed (run total: {totals.get('observations_new', 0)} new / "
            f"{totals.get('observations_repeated', 0)} repeated observations, "
            f"{totals.get('entities_new', 0)} new entities, "
            f"{totals.get('facts_new', 0)} facts, "
            f"{totals.get('missing_identity', 0)} unattributable)"
        )

    def _stage_export(self, conn, run_id: int, result: RunResult) -> str:
        db.set_stage(conn, run_id, "export", "running", touch_started=True)
        directory = self._sub(run_id, self.cfg.export_directory_name)
        paths = export.export_run(conn, run_id, self.cfg, directory)
        db.set_stage(conn, run_id, "export", "completed", touch_finished=True)

        result.exports = {name: str(path) for name, path in paths.items()}
        return "completed (" + ", ".join(sorted(result.exports)) + ")"

    def _stage_sitediscovery(self, conn, run_id: int, result: RunResult) -> str:
        """Find a business's own site when the listing did not name one.

        Sits after merge and before enrich: after merge so the run's cross-source
        picture exists before a site is attributed, and before enrich so a site
        this stage confirms is one the enricher immediately visits.

        A *candidate* is not a fact. Only a page whose own name and an
        independent signal (the same phone or address) both agree writes a
        ``website`` fact; everything else is stored with its verdict for a
        reviewer. Nothing here updates ``entities.website`` when the listing
        already stated one.

        A crash inside the stage is recorded and does not abort the run, for the
        same reason as enrichment: one unreachable candidate site must not cost
        the whole batch.
        """
        if not self.cfg.sitediscovery_enabled:
            db.set_stage(conn, run_id, "sitediscovery", "skipped", touch_finished=True)
            return "skipped (sitediscovery.enabled is false)"

        if self._stage_done(conn, run_id, "sitediscovery"):
            return "skipped (already completed)"

        db.set_stage(conn, run_id, "sitediscovery", "running", touch_started=True)
        try:
            stats = self.discoverer.discover_run(conn, run_id)
        except Exception as exc:  # noqa: BLE001 - recorded, then surfaced as a stage failure
            db.set_stage(
                conn, run_id, "sitediscovery", "failed", error=str(exc), touch_finished=True
            )
            raise CollectorError(f"candidate-site discovery failed: {exc}") from exc

        result.sitediscovery_stats = stats.as_dict()
        result.sitediscovery_stats.update(self.discoverer.summary(conn, run_id))
        db.set_stage(conn, run_id, "sitediscovery", "completed", touch_finished=True)

        parts = [
            f"{stats.candidates_found} candidate(s) from {len(stats.finders)} finder(s)",
            f"{stats.confirmed} confirmed",
            f"{stats.pending} pending",
            f"{stats.rejected} rejected",
            f"{stats.pages_fetched} page(s) read, {stats.pages_failed} failed",
        ]
        if stats.candidates_skipped_budget:
            parts.append(f"{stats.candidates_skipped_budget} left for a later run ({stats.budget_reason})")
        if stats.unavailable_finders:
            # Reported rather than left implicit: "found nothing" and "could not
            # look" are different answers and the task book forbids conflating them.
            parts.append("unavailable: " + "; ".join(stats.unavailable_finders))
        if stats.finder_notes:
            parts.append("also: " + "; ".join(stats.finder_notes))
        return "completed (" + ", ".join(parts) + ")"

    def _stage_enrich(self, conn, run_id: int, result: RunResult) -> str:
        """Visit the businesses' own websites (stage D).

        Disabled by configuration, the stage records that and returns: a run
        that never asked for enrichment must not look like one that failed at
        it. A failure inside the stage is recorded per entity by the enricher
        and does not abort the run, because losing a whole batch over one
        unreachable website is exactly what the task book forbids.
        """
        if not self.cfg.enrich_enabled:
            db.set_stage(conn, run_id, "enrich", "skipped", touch_finished=True)
            return "skipped (enrich.enabled is false)"

        if self._stage_done(conn, run_id, "enrich"):
            return "skipped (already completed)"

        db.set_stage(conn, run_id, "enrich", "running", touch_started=True)
        try:
            stats = self.enricher.enrich_run(conn, run_id)
        except Exception as exc:  # noqa: BLE001 - recorded, then surfaced as a stage failure
            db.set_stage(conn, run_id, "enrich", "failed", error=str(exc), touch_finished=True)
            raise CollectorError(f"enrichment failed: {exc}") from exc

        result.enrich_stats = stats.as_dict()
        db.set_stage(conn, run_id, "enrich", "completed", touch_finished=True)
        return (
            f"completed ({stats.sites_visited}/{stats.entities_with_website} sites, "
            f"{stats.pages_fetched} page(s) read, {stats.pages_failed} failed, "
            f"{stats.facts_written} fact(s), {stats.identity_conflicts} conflict(s), "
            f"{stats.boundary_rejections} refused)"
        )

    # -- resume helper -------------------------------------------------------

    def resume_run(self, conn, run_id: int) -> int:
        """Resume a specific run, refusing when the configuration changed."""
        row = db.get_run(conn, run_id)
        if row is None:
            raise ResumeError(f"run {run_id} not found")
        if row["fingerprint"] != self.cfg.fingerprint():
            raise ResumeError(
                f"run {run_id} was created with a different configuration "
                f"(fingerprint {row['fingerprint'][:12]} != {self.cfg.short_fingerprint()}); "
                "reusing its checkpoints would mix incompatible evidence"
            )
        return run_id


def _acquired_via_for(provider_name: str) -> str:
    """How a provider's rows were obtained, for envelopes that do not say.

    061: the Go scraper writes ``context`` with no provenance at all, so its
    rows reached the workbook as "获取方式：未知" even though this run had just
    fetched them. Ingest refuses to guess that from the payload -- a raw file
    handed over from an earlier run is not live -- but the orchestrator knows
    which provider produced the file, and a provider here either reads a file
    the operator supplied or goes to the network. Stating it at the call site
    keeps the "never guess" rule intact while removing the contradiction.
    """
    return "import" if provider_name in NEVER_LIVE_PROVIDERS else "live"


def default_go_binary(base_dir: str | pathlib.Path) -> pathlib.Path:
    return pathlib.Path(base_dir) / "google-maps-scraper" / "bin" / "google-maps-scraper.exe"


def env_summary() -> dict[str, str]:
    """A few environment facts worth recording next to a run."""
    return {
        "platform": os.name,
        "python": sys.version.split()[0],
    }
