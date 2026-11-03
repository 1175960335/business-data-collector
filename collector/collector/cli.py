"""Command line interface.

    python -m collector validate-config --config configs/example.yaml
    python -m collector plan            --config configs/example.yaml
    python -m collector run             --config configs/example.yaml [--no-resume]
    python -m collector status          --config configs/example.yaml
    python -m collector export          --config configs/example.yaml --run-id 3
    python -m collector ui              [--base-dir ..] [--port 8757]   # local screen

Exit codes: 0 success, 1 runtime failure, 2 configuration failure.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Sequence

from . import config as config_module
from . import db, export
from .errors import CollectorError, ConfigError
from .orchestrator import Orchestrator
from .version import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collector",
        description="Orchestrate the Go Maps scraper, store evidence in SQLite, export CSV/JSONL.",
    )
    parser.add_argument("--version", action="version", version=f"collector {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, *, config_required: bool = True) -> None:
        if config_required:
            p.add_argument("--config", required=True, help="path to a .yaml/.toml/.json config")
        p.add_argument(
            "--base-dir",
            default=None,
            help="project root used to resolve relative output paths (default: cwd)",
        )
        p.add_argument("--json", action="store_true", help="print machine-readable JSON")

    p_validate = sub.add_parser("validate-config", help="validate a config and print the normalised form")
    add_common(p_validate)

    p_plan = sub.add_parser("plan", help="show what a run would do, without executing it")
    add_common(p_plan)
    p_plan.add_argument("--go-binary", default=None)

    p_run = sub.add_parser("run", help="run the pipeline")
    add_common(p_run)
    p_run.add_argument("--go-binary", default=None, help="override runtime.go_binary")
    p_run.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="refuse to continue an unfinished run with the same configuration",
    )
    p_run.set_defaults(resume=True)

    p_status = sub.add_parser("status", help="show runs recorded in the database")
    add_common(p_status)
    p_status.add_argument("--run-id", type=int, default=None)

    p_export = sub.add_parser("export", help="re-export an existing run")
    add_common(p_export)
    p_export.add_argument("--run-id", type=int, required=True)
    p_export.add_argument("--directory", default=None, help="override the export directory")

    p_ui = sub.add_parser("ui", help="start the small local control screen (default: loopback only)")
    add_common(p_ui, config_required=False)
    p_ui.add_argument("--port", type=int, default=8757)
    p_ui.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; the default keeps the screen reachable only from this machine",
    )
    p_ui.add_argument("--no-browser", action="store_true", help="do not open a browser window")

    return parser


def _load(args) -> config_module.Config:
    return config_module.load(args.config)


def _resolve_base(args) -> pathlib.Path:
    return pathlib.Path(args.base_dir or pathlib.Path.cwd())


def cmd_validate_config(args) -> int:
    cfg = _load(args)
    if args.json:
        print(json.dumps(cfg.data, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"config OK: {args.config}")
        print(f"  fingerprint      {cfg.fingerprint()}")
        print(f"  industry         {cfg.industry}")
        print(f"  query            {cfg.query}")
        print(f"  country          {cfg.country}")
        print(f"  bbox             {cfg.bbox}")
        print(f"  target_count     {cfg.target_count}")
        print(f"  mode             {cfg.mode}")
        print(f"  collect fields   {', '.join(cfg.collect_fields)}")
        print(f"  export fields    {', '.join(cfg.export_fields)}")
        print(f"  fetch flags      {cfg.fetch_flags() or '(none)'}")
        if cfg.enrich_enabled:
            print(
                f"  enrich           enabled: fields={', '.join(cfg.enrich_fields)} "
                f"pages/site={cfg.enrich_pages_per_site} "
                f"budget={cfg.enrich_request_budget or 'unlimited'} fetches / "
                f"{cfg.enrich_time_budget_seconds or 'unlimited'}s"
            )
        else:
            print("  enrich           disabled")
        if cfg.sitediscovery_enabled:
            print(
                f"  sitediscovery    enabled: sources={','.join(cfg.sitediscovery_sources)} "
                f"pages/site={cfg.sitediscovery_pages_per_site} "
                f"budget={cfg.sitediscovery_request_budget or 'unlimited'} fetches / "
                f"{cfg.sitediscovery_seconds_budget or 'unlimited'}s"
            )
        else:
            print("  sitediscovery    disabled")
    return 0


def cmd_plan(args) -> int:
    cfg = _load(args)
    orch = Orchestrator(cfg, base_dir=_resolve_base(args), go_binary=args.go_binary)
    plan = orch.plan()
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(f"fingerprint : {plan['fingerprint']}")
        print(f"database    : {plan['database']}")
        print(f"run dir     : {plan['run_dir']}")
        print(f"go binary   : {plan['go_binary']} (present: {plan['go_binary_present']})")
        print(f"fetch flags : {' '.join(plan['fetch_flags']) or '(none)'}")
        enrich = plan.get("enrich", {})
        if enrich.get("enabled"):
            print(
                f"enrich      : fields={','.join(enrich['fields'])} "
                f"pages/site={enrich['pages_per_site']} "
                f"budget={enrich['request_budget'] or 'unlimited'} fetches / "
                f"{enrich['time_budget_seconds'] or 'unlimited'}s"
            )
        else:
            print("enrich      : disabled")
        discovery = plan.get("sitediscovery", {})
        if discovery.get("enabled"):
            print(
                f"sitediscovery: sources={','.join(discovery['sources'])} "
                f"pages/site={discovery['pages_per_site']} "
                f"budget={discovery['request_budget'] or 'unlimited'} fetches / "
                f"{discovery['time_budget_seconds'] or 'unlimited'}s"
            )
            for finder in discovery.get("finders", []):
                state = "configured" if finder["configured"] else "NOT configured"
                detail = "" if finder["configured"] else f" -- {finder['not_configured_reason']}"
                print(f"  {finder['name']:<11} {state}{detail}")
                if finder.get("note"):
                    print(f"  {'':<11} WARNING: {finder['note']}")
        else:
            print("sitediscovery: disabled")
        for task in plan["tasks"]:
            print(f"\ntask {task['label']} (query={task['query']!r})")
            print("  argv: " + " ".join(task["argv"]))
    return 0


def cmd_run(args) -> int:
    cfg = _load(args)
    orch = Orchestrator(cfg, base_dir=_resolve_base(args), go_binary=args.go_binary)
    result = orch.run(resume=args.resume)

    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"run {result.run_id} [{result.status}]  resumed={result.resumed}")
        for name, status in result.stages.items():
            print(f"  {name:<9} {status}")
        print(f"  collected {result.collected}/{result.target_count} entities")
        if result.shortfall:
            print(
                f"  shortfall {result.shortfall}: the configured sources did not yield "
                "enough distinct businesses (reported, not an error)"
            )
        for name, path in sorted(result.exports.items()):
            print(f"  export {name}: {path}")
        print(f"  database {result.database}")

    return 0 if result.status in ("completed", "partial") else 1


def cmd_status(args) -> int:
    cfg = _load(args)
    orch = Orchestrator(cfg, base_dir=_resolve_base(args))
    conn = db.connect(orch.database_path)
    try:
        if args.run_id is not None:
            row = db.get_run(conn, args.run_id)
            if row is None:
                print(f"run {args.run_id} not found", file=sys.stderr)
                return 1
            payload = {
                "run": dict(row),
                "stages": [dict(r) for r in db.list_stages(conn, args.run_id)],
                "tasks": [dict(r) for r in db.list_provider_tasks(conn, args.run_id)],
                "counts": db.table_counts(conn),
                "entities_in_run": db.count_run_entities(conn, args.run_id),
                "errors": [dict(r) for r in db.list_errors(conn, args.run_id)],
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            else:
                print(f"run {row['id']}: {row['status']}  target={row['target_count']}")
                print(f"  started  {row['started_at']}")
                print(f"  finished {row['finished_at']}")
                print(f"  note     {row['note']}")
                print(f"  entities {payload['entities_in_run']}")
                for task in payload["tasks"]:
                    print(f"  task {task['label']}: {task['status']} exit={task['exit_code']}")
                for err in payload["errors"]:
                    print(f"  error [{err['kind']}] {err['message']}")
            return 0

        runs = db.list_runs(conn)
        counts = db.table_counts(conn)
        if args.json:
            print(json.dumps({"runs": [dict(r) for r in runs], "counts": counts},
                             ensure_ascii=False, indent=2, default=str))
        else:
            print(f"database: {orch.database_path}")
            print(f"fingerprint: {cfg.short_fingerprint()}")
            if not runs:
                print("  (no runs yet)")
            for row in runs:
                marker = "*" if row["fingerprint"] == cfg.fingerprint() else " "
                print(
                    f" {marker} run {row['id']:<3} {row['status']:<11} "
                    f"fp={row['fingerprint'][:12]} target={row['target_count']} "
                    f"started={row['started_at']}"
                )
            print("  counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        return 0
    finally:
        conn.close()


def cmd_export(args) -> int:
    cfg = _load(args)
    orch = Orchestrator(cfg, base_dir=_resolve_base(args))
    conn = db.connect(orch.database_path)
    try:
        row = db.get_run(conn, args.run_id)
        if row is None:
            print(f"run {args.run_id} not found in {orch.database_path}", file=sys.stderr)
            return 1
        directory = (
            pathlib.Path(args.directory)
            if args.directory
            else orch.run_dir(args.run_id) / cfg.export_directory_name
        )
        paths = export.export_run(conn, args.run_id, cfg, directory)
        if args.json:
            print(json.dumps({k: str(v) for k, v in paths.items()}, indent=2))
        else:
            for name, path in sorted(paths.items()):
                print(f"{name}: {path}")
        return 0
    finally:
        conn.close()


def cmd_ui(args) -> int:
    from . import local_ui

    return local_ui.serve(
        base_dir=_resolve_base(args),
        port=args.port,
        host=args.host,
        open_browser=not args.no_browser,
    )


HANDLERS = {
    "validate-config": cmd_validate_config,
    "plan": cmd_plan,
    "run": cmd_run,
    "status": cmd_status,
    "export": cmd_export,
    "ui": cmd_ui,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return HANDLERS[args.command](args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except CollectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
