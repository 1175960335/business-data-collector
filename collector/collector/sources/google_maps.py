"""The Google Maps provider: the Go scraper, wrapped in the stage E contract.

This is the source stages A-D already validated. Wrapping it here changes
nothing about how it is invoked: the orchestrator still builds one task per
query, still passes arguments as an array, still writes ``-json-records``
envelopes, and still ingests them exactly as before. The only difference is
that the run now reaches it through :class:`Provider` like any other source, so
its failures, budget and checkpointing are comparable with the others.
"""

from __future__ import annotations

import json
import pathlib
import re
import time

from .base import Provider, ProviderOutcome, ProviderTask, RunContext

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


class GoogleMapsProvider(Provider):
    """Discovery through the local ``google-maps-scraper`` binary.

    ``license`` is the licence of the Go scraper (MIT). The *data* comes from
    Google Maps and stays subject to Google's terms; the collector does not
    acquire any right to redistribute it, and says so rather than leaving the
    operator to assume otherwise.
    """

    name = "google_maps"
    title = "Google Maps (via the local Go scraper)"
    license = "MIT (scraper) — data subject to Google Maps terms"
    #: Google Maps publishes coordinates in WGS84 (EPSG:4326).
    coordinate_system = "WGS84"
    attribution = (
        "Scraper: gosom/google-maps-scraper (MIT). Data is copied from Google "
        "Maps for personal research; redistribution is not covered by this tool."
    )
    requires_account = False
    provenance = ""

    def is_configured(self) -> bool:
        return self.name in (self.cfg.data.get("sources") or [])

    def not_configured_reason(self) -> str:
        return "google_maps is not listed in sources"

    # -- planning ------------------------------------------------------------

    def tasks(self) -> list[ProviderTask]:
        out: list[ProviderTask] = []
        for query in self.cfg.queries:
            out.append(
                ProviderTask(
                    label=f"discover-{_slug(query)}",
                    query=query,
                    targets=(query,),
                )
            )
        return out

    # -- execution -----------------------------------------------------------

    def adapter(self, ctx: RunContext):
        """The orchestrator's adapter, reused so a stub scraper stays in play."""
        existing = ctx.adapters.get(self.name)
        if existing is not None:
            return existing

        from ..goadapter import GoAdapter

        binary = ctx.go_binary or self.cfg.go_binary or _default_binary(ctx, self.cfg)
        # The scraper takes its proxy as a flag, so a per-source ``network``
        # setting is resolved here the same way the HTTP providers resolve it.
        adapter = GoAdapter(
            binary=pathlib.Path(binary),
            proxy=self.cfg.source_proxy(self.name),
            cwd=ctx.cwd,
        )
        ctx.adapters[self.name] = adapter
        return adapter

    def run_task(self, task: ProviderTask, ctx: RunContext) -> ProviderOutcome:
        adapter = self.adapter(ctx)
        adapter.require()

        # The scraper writes the raw envelopes itself; the collector only points
        # -results at a path inside the run directory and hands it the query
        # through a file, so nothing is embedded in the command line.
        raw_path = ctx.raw_path(task.label, task.attempt)
        stdout_path = ctx.log_path(task.label, task.attempt, "stdout")
        stderr_path = ctx.log_path(task.label, task.attempt, "stderr")
        query_path = ctx.run_dir / "queries" / f"{task.label}.txt"
        query_path.parent.mkdir(parents=True, exist_ok=True)
        query_path.write_text(task.query + "\n", encoding="utf-8")
        raw_path.parent.mkdir(parents=True, exist_ok=True)

        argv = adapter.build_argv(
            self.cfg, queries_file=query_path, results_path=raw_path
        )

        started = time.monotonic()
        outcome = adapter.run(
            argv,
            timeout_seconds=ctx.withdraw_task_timeout(self.cfg.timeout_seconds),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        duration = time.monotonic() - started

        if not outcome.ok:
            reason = "timeout" if outcome.timed_out else f"exit code {outcome.exit_code}"
            return ProviderOutcome(
                ok=False,
                raw_path=raw_path,
                error=reason,
                # ``go_timeout`` / ``go_failure`` are the kinds stage C recorded
                # and the existing acceptance expectations assert on. Keeping
                # them means a source-unified run is comparable with an older one.
                error_kind="go_timeout" if outcome.timed_out else "go_failure",
                attempts=1,
                duration_seconds=duration,
                exit_code=outcome.exit_code,
            )

        # A clean exit is not the same as a successful scrape. The scraper
        # returns 0 even when every job failed — an unreachable proxy gives a
        # zero-byte result file and exit code 0, which looks exactly like a
        # legitimate empty search. Treating it as success means the run reports
        # "completed" with one source silently missing, and a later resume skips
        # the task instead of retrying it (observed on a real run). The outcome
        # is in the scraper's own JSON log, so read it rather than guess.
        total_jobs, failed_jobs = _job_statuses(stderr_path)
        if total_jobs and failed_jobs >= total_jobs:
            return ProviderOutcome(
                ok=False,
                raw_path=raw_path,
                error=f"all {total_jobs} job(s) failed (see {stderr_path.name})",
                error_kind="all_jobs_failed",
                attempts=1,
                duration_seconds=duration,
                exit_code=outcome.exit_code,
            )

        return ProviderOutcome(
            ok=True,
            records=_count_lines(raw_path),
            raw_path=raw_path,
            duration_seconds=duration,
        )


def _job_statuses(stderr_path: pathlib.Path) -> tuple[int, int]:
    """``(jobs the scraper reported, how many of them failed)``.

    The scraper logs one JSON object per event on stderr and a finished job
    carries ``"message": "job finished"`` plus a ``status``. Anything else — a
    missing log, a different format, a line that is not JSON — is ignored, so a
    future change to the scraper's logging makes this check abstain (returns
    ``(0, 0)``) rather than mis-report a healthy run as broken.
    """
    total = 0
    failed = 0
    try:
        handle = open(stderr_path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return (0, 0)
    with handle:
        for line in handle:
            line = line.strip()
            if not line or '"job finished"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("message") != "job finished":
                continue
            total += 1
            if str(event.get("status") or "").casefold() == "failed":
                failed += 1
    return (total, failed)


def _count_lines(path: pathlib.Path) -> int:
    if not path.is_file():
        return 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return sum(1 for line in handle if line.strip())


def _slug(text: str, limit: int = 40) -> str:
    cleaned = _SLUG.sub("-", text).strip("-")
    return (cleaned or "query")[:limit]


def _default_binary(ctx: RunContext, cfg) -> pathlib.Path:
    from ..orchestrator import default_go_binary

    return default_go_binary(ctx.cwd or pathlib.Path.cwd())
