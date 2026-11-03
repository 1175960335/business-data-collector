"""SQLite persistence: schema, migrations and queries.

Task book 004, stage C: "SQLite 保存任务、阶段、provider 子任务、原始
observation、规范化实体、字段事实、错误和检查点。保存配置指纹、版本和运行时
间；同配置可续跑，配置变化不能误用旧检查点。"

Tables
------
runs                one row per orchestration run, keyed by config fingerprint
stages              per-run stage state (discover / ingest / export)
provider_tasks      one row per Go invocation
observations        raw provider payloads, de-duplicated by content hash
entities            normalised businesses, globally unique by stable key
entity_observations links an entity to every observation that mentioned it
field_facts         one row per (entity, field, value, source) with evidence
errors              structured failures, attached to a run/stage/task
checkpoints         stage-level resume markers

Schema generation lives in ``PRAGMA user_version``. Opening a database written
by a newer generation fails loudly instead of silently mis-reading columns.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sqlite3
import time
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit

#: 026 P3.5: how long a transient refusal to open the database is retried for.
#: Five attempts with growing pauses keep the worst case near a second, which a
#: person does not notice but a stopped run's leftovers easily clear.
_OPEN_ATTEMPTS = 5
_OPEN_PAUSE_SECONDS = 0.15

from .errors import SchemaError
from .util import utc_now_iso

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint       TEXT    NOT NULL,
    config_json       TEXT    NOT NULL,
    config_source     TEXT,
    collector_version TEXT    NOT NULL,
    schema_version    INTEGER NOT NULL,
    go_binary         TEXT,
    go_version        TEXT,
    status            TEXT    NOT NULL,
    target_count      INTEGER NOT NULL,
    started_at        TEXT    NOT NULL,
    finished_at       TEXT,
    note              TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_fingerprint ON runs(fingerprint, id);

CREATE TABLE IF NOT EXISTS stages (
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    stage       TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    attempt     INTEGER NOT NULL DEFAULT 1,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT,
    PRIMARY KEY (run_id, stage)
);

CREATE TABLE IF NOT EXISTS provider_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    provider    TEXT    NOT NULL,
    stage       TEXT    NOT NULL,
    label       TEXT    NOT NULL,
    query       TEXT,
    argv_json   TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    exit_code   INTEGER,
    raw_path    TEXT,
    stdout_path TEXT,
    stderr_path TEXT,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT,
    attempt     INTEGER NOT NULL DEFAULT 1,
    UNIQUE (run_id, label)
);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON provider_tasks(run_id, status);

CREATE TABLE IF NOT EXISTS observations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    provider       TEXT    NOT NULL,
    content_hash   TEXT    NOT NULL,
    mode           TEXT,
    query          TEXT,
    source_url     TEXT,
    scraped_at     TEXT,
    raw_json       TEXT    NOT NULL,
    record_schema  TEXT,
    missing_json   TEXT,
    unknown_json   TEXT,
    raw_path       TEXT,
    raw_line       INTEGER,
    first_run_id   INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    first_seen_at  TEXT    NOT NULL,
    last_seen_at   TEXT    NOT NULL,
    seen_count     INTEGER NOT NULL DEFAULT 1,
    UNIQUE (provider, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_obs_query ON observations(provider, query);

CREATE TABLE IF NOT EXISTS entities (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_key    TEXT    NOT NULL UNIQUE,
    id_kind       TEXT    NOT NULL,
    name          TEXT,
    name_norm     TEXT,
    address       TEXT,
    address_norm  TEXT,
    latitude      REAL,
    longitude     REAL,
    phone_norm    TEXT,
    website       TEXT,
    domain        TEXT,
    country       TEXT,
    first_seen_at TEXT    NOT NULL,
    last_seen_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_domain ON entities(domain);
CREATE INDEX IF NOT EXISTS idx_entities_name_norm ON entities(name_norm);

CREATE TABLE IF NOT EXISTS entity_observations (
    entity_id      INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    run_id         INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    query          TEXT,
    linked_at      TEXT    NOT NULL,
    PRIMARY KEY (entity_id, observation_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_eo_run ON entity_observations(run_id);

CREATE TABLE IF NOT EXISTS field_facts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id         INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    observation_id    INTEGER REFERENCES observations(id) ON DELETE SET NULL,
    run_id            INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    field             TEXT    NOT NULL,
    raw_value         TEXT,
    normalized_value  TEXT,
    provider          TEXT    NOT NULL,
    source_url        TEXT,
    query_context     TEXT,
    scraped_at        TEXT,
    evidence_snippet  TEXT,
    evidence_ref      TEXT,
    extractor_version TEXT    NOT NULL,
    validation_status TEXT    NOT NULL,
    validation_note   TEXT,
    validation_state  TEXT,
    checks_json       TEXT,
    observed_at       TEXT    NOT NULL,
    fact_key          TEXT    NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_facts_entity_field ON field_facts(entity_id, field);

CREATE TABLE IF NOT EXISTS errors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    stage            TEXT,
    provider_task_id INTEGER REFERENCES provider_tasks(id) ON DELETE SET NULL,
    kind             TEXT    NOT NULL,
    message          TEXT    NOT NULL,
    context_json     TEXT,
    occurred_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_errors_run ON errors(run_id);

CREATE TABLE IF NOT EXISTS checkpoints (
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    stage      TEXT    NOT NULL,
    key        TEXT    NOT NULL,
    value_json TEXT,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (run_id, stage, key)
);

-- Stage E: cross-source entity merges. One row per entity that was folded into
-- another, never one row for the survivor, so the canonical entity is simply
-- the target that has no row of its own. ``reason`` names the evidence; the
-- candidate values stay on the merged entity, so nothing is overwritten and a
-- reviewer can always disagree with the decision.
CREATE TABLE IF NOT EXISTS entity_merges (
    canonical_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_id           INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    reason              TEXT    NOT NULL,
    evidence_json       TEXT,
    score               REAL    NOT NULL DEFAULT 0,
    run_id              INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    decided_at          TEXT    NOT NULL,
    PRIMARY KEY (canonical_entity_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_merges_entity ON entity_merges(entity_id);
CREATE INDEX IF NOT EXISTS idx_merges_canonical ON entity_merges(canonical_entity_id);

-- Round 026 P1: candidate official sites. A URL a finder *proposed* is not a
-- fact. It lives here with the finder that produced it, the evidence for the
-- verdict, and the verdict itself, so a reviewer can see what was considered
-- and rejected -- not merely what was accepted. Only a ``confirmed`` candidate
-- also writes a ``website`` field fact; the row stays as its justification.
-- One row per (run, entity, url): re-judging the same URL is an update, never a
-- second opinion silently appended.
CREATE TABLE IF NOT EXISTS site_candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    entity_id     INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    url           TEXT    NOT NULL,
    host          TEXT,
    found_by      TEXT    NOT NULL,
    origin        TEXT,
    status        TEXT    NOT NULL,
    reason        TEXT,
    note          TEXT,
    evidence_json TEXT,
    http_status   INTEGER,
    judged_at     TEXT    NOT NULL,
    UNIQUE (run_id, entity_id, url)
);
CREATE INDEX IF NOT EXISTS idx_site_candidates_run ON site_candidates(run_id, status);
CREATE INDEX IF NOT EXISTS idx_site_candidates_entity ON site_candidates(entity_id);

-- Round 026 P2: what each source is doing right now. Not per run, because the
-- facts are about the *site*, not about one batch: "11467.com answered 429 at
-- 02:14 and asked for 900 s" outlives the run that discovered it, and the next
-- run should open with that on screen rather than rediscovering it by hitting
-- the site again. One row per source.
CREATE TABLE IF NOT EXISTS source_status (
    source          TEXT PRIMARY KEY,
    network_mode    TEXT NOT NULL DEFAULT '',
    proxy           TEXT NOT NULL DEFAULT '',
    last_success_at TEXT,
    last_error      TEXT,
    last_error_at   TEXT,
    last_error_kind TEXT,
    cooldown_until  TEXT,
    cooldown_seconds INTEGER,
    updated_at      TEXT NOT NULL
);
"""

#: Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` does
#: not touch a table that already exists, so an older database would keep the
#: old shape and a later INSERT naming the new column would fail. These are
#: applied with ``ALTER TABLE ... ADD COLUMN`` instead, which is safe to run on
#: every open and needs no schema-generation bump: existing columns keep their
#: meaning, so a database written by an older collector still reads correctly.
ADDITIVE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "field_facts": (
        # Machine-readable validation state, e.g. "mx_present", "null_mx",
        # "dns_timeout". The task book requires these to be stored separately
        # rather than folded into a free-text note.
        ("validation_state", "TEXT"),
        # The evidence behind a verdict (MX hosts, E.164, address components).
        ("checks_json", "TEXT"),
    ),
    "entities": (
        # The coordinate reference system the stored latitude/longitude are in
        # ("WGS84", "GCJ-02", "BD-09"). Coordinates are meaningless without it:
        # a Chinese map provider's GCJ-02 point is 300-500 m from the same
        # place in WGS84, so the merge stage must not compare across systems.
        ("crs", "TEXT"),
    ),
    # Round 026 P0: provenance. ``mode`` alone could not say whether a record
    # came from a page this run fetched or from a file the operator supplied,
    # and the source *page* was nowhere near the facts. These columns keep the
    # acquisition method and the page the facts were read from, so the reader
    # view can cite the real page and an imported row cannot read as a visit.
    "observations": (
        # "live" | "cache" | "import" — see sources.base.ACQUIRED_VIA.
        ("acquired_via", "TEXT"),
        # The page the facts were actually read from (the listing page for a
        # listing row), which is what evidence should cite.
        ("source_page_url", "TEXT"),
        # The entity's own page, when the source only links to it. Not evidence
        # unless ``detail_fetched`` says this run opened it.
        ("detail_url", "TEXT"),
        ("detail_fetched", "INTEGER"),
        # The originating site, when it differs from the provider name.
        ("origin", "TEXT"),
        # Identifier of the immutable artifact a row came from.
        ("snapshot_id", "TEXT"),
        # When the source page was captured, per the operator; NULL when unknown.
        ("observed_at", "TEXT"),
        # The local transport (file) an import was read from, kept apart from the
        # source page so a path is never mistaken for a page.
        ("input_path", "TEXT"),
    ),
}


def _apply_additive_columns(conn: sqlite3.Connection) -> None:
    for table, columns in ADDITIVE_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def connect(path: str | pathlib.Path) -> sqlite3.Connection:
    """Open (and create) the database, applying pragmas and the schema.

    026 P3.5: this is a *shared* database — the control screen, the run it
    started and any terminal command all open it, and WAL mode lets them write
    at once. There is one window where opening it can still fail transiently:
    right after a run is killed (``taskkill`` on 「停止」, or closing the console
    window) a new opener may be told the database is locked, or even that it is
    read-only, while the file itself is perfectly writable. That was observed
    once, in exactly that window, and it lasted for less than one request.

    Giving up on the first refusal would turn a half-second hiccup into a
    crashed page, so a refusal is retried briefly. A real permission problem is
    not hidden by this: it fails every time and the last error is raised.
    """
    file = pathlib.Path(path)
    if file.parent and str(file.parent) not in ("", "."):
        file.parent.mkdir(parents=True, exist_ok=True)

    last: Exception | None = None
    for attempt in range(_OPEN_ATTEMPTS):
        try:
            conn = sqlite3.connect(str(file))
        except sqlite3.OperationalError as exc:  # pragma: no cover - rare
            last = exc
            time.sleep(_OPEN_PAUSE_SECONDS * (attempt + 1))
            continue
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            ensure_schema(conn)
        except sqlite3.OperationalError as exc:
            # The transient case lives here: the pragmas succeed, then the first
            # real write is refused. Anything else (a schema from the future)
            # propagates untouched.
            conn.close()
            last = exc
            time.sleep(_OPEN_PAUSE_SECONDS * (attempt + 1))
            continue
        except Exception:
            # A rejected database must not leave a half-open handle behind: on
            # Windows that keeps the file locked and the error unactionable.
            conn.close()
            raise
        return conn
    assert last is not None
    raise last


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------

#: Outer retry for a refused write. ``PRAGMA busy_timeout`` (set in ``connect``)
#: already makes SQLite wait ten seconds, which covers the ordinary case: two
#: connections wanting to write at once. 052 found the case it does not cover --
#: the window ``connect``'s docstring documents, right after a run is killed
#: (``taskkill`` from 「停止」), where a write was still told "database is locked"
#: after ten seconds. Waiting longer is the whole remedy, because the file is
#: writable and the other holder is gone.
_WRITE_ATTEMPTS = 16
_WRITE_PAUSE_SECONDS = 5.0


def _is_locked(exc: BaseException) -> bool:
    message = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in message or "busy" in message
    )


def write(conn: sqlite3.Connection, sql: str, parameters=()):
    """Execute one write and commit it, waiting out a stale lock.

    Every write in this module goes through here, on purpose. ``connect``'s
    retry only protects opening the file; a write that arrives while another
    connection still holds the database raised ``OperationalError`` straight at
    the caller. In 052 that happened inside ``enrich``, and the handler which
    would have recorded the failure (``set_stage(..., "failed")``) was refused
    by the same lock -- so the run died without a terminal state at all, which
    is the one outcome the run state machine exists to prevent.

    Retrying for up to ``_WRITE_ATTEMPTS * _WRITE_PAUSE_SECONDS`` (80 s) is not
    papering over a bug: the message is SQLite's for "someone else has it", the
    dead holder releases it, and the queue drains. Anything that is genuinely
    broken raises after the last attempt -- with SQLite's own wording first and
    the shape of the failure named after it (see the final attempt below);
    ``__cause__`` still carries the original exception.

    What waiting out a lock *cannot* fix, and this retry does not pretend to:
    a refusal that arrives immediately every time, which is not a holder at all
    but this connection's own stale read snapshot (``iter_run_entities``).
    """
    for attempt in range(_WRITE_ATTEMPTS):
        try:
            cur = conn.execute(sql, parameters)
            conn.commit()
            return cur
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc):
                raise
            # A write that was refused may still hold the connection in an open
            # transaction; drop it before trying again or every retry re-fails.
            try:
                conn.rollback()
            except sqlite3.Error:  # pragma: no cover - already broken
                pass
            if attempt == _WRITE_ATTEMPTS - 1:
                # 053 spent hours on a bare "database is locked" that was in fact
                # ``SQLITE_BUSY_SNAPSHOT``: the writer held an open read statement
                # (a live cursor it was iterating), so the refusal came instantly,
                # ``busy_timeout`` never engaged, and the retry could not help
                # because the statement was still open. Name that shape here --
                # a verdict that cannot be traced back to a line of code is a
                # rumour. The original is kept as ``__cause__`` for the caller.
                raise sqlite3.OperationalError(
                    f"{exc} (after {_WRITE_ATTEMPTS} attempts over "
                    f"{_WRITE_ATTEMPTS * _WRITE_PAUSE_SECONDS:.0f}s; if this connection is "
                    "iterating a cursor from a SELECT, close that cursor first -- a stale "
                    "read snapshot is refused immediately and no waiting can clear it)"
                ) from exc
            time.sleep(_WRITE_PAUSE_SECONDS)
    raise AssertionError("unreachable")  # pragma: no cover - the loop always returns or raises


def ensure_schema(conn: sqlite3.Connection, generation: int | None = None) -> None:
    """Create missing tables and check the stored generation."""
    from .version import SCHEMA_VERSION

    generation = SCHEMA_VERSION if generation is None else generation

    stored = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if stored > generation:
        raise SchemaError(
            f"database was written by schema generation {stored}, this collector "
            f"understands {generation}; refusing to read it. Use a newer collector "
            "or point output.database at a different file."
        )

    conn.executescript(SCHEMA_SQL)
    _apply_additive_columns(conn)
    write(conn, f"PRAGMA user_version={generation}")


# --- runs -------------------------------------------------------------------


def create_run(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
    config_json: str,
    config_source: str,
    collector_version: str,
    schema_version: int,
    go_binary: str,
    go_version: str | None,
    target_count: int,
    note: str = "",
) -> int:
    cur = write(conn,
        """
        INSERT INTO runs (fingerprint, config_json, config_source, collector_version,
                          schema_version, go_binary, go_version, status, target_count,
                          started_at, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
        """,
        (
            fingerprint,
            config_json,
            config_source,
            collector_version,
            schema_version,
            go_binary,
            go_version,
            target_count,
            utc_now_iso(),
            note,
        ),
    )
    return int(cur.lastrowid)


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def latest_resumable_run(conn: sqlite3.Connection, fingerprint: str) -> sqlite3.Row | None:
    """The most recent run with this fingerprint that is not finished cleanly."""
    return conn.execute(
        """
        SELECT * FROM runs
        WHERE fingerprint = ?
          AND status IN ('running', 'interrupted', 'failed', 'partial')
        ORDER BY id DESC LIMIT 1
        """,
        (fingerprint,),
    ).fetchone()


def latest_run(conn: sqlite3.Connection, fingerprint: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE fingerprint = ? ORDER BY id DESC LIMIT 1",
        (fingerprint,),
    ).fetchone()


def list_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM runs ORDER BY id DESC"))


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    *,
    note: str | None = None,
) -> None:
    write(conn,
        "UPDATE runs SET status = ?, finished_at = ?, note = COALESCE(?, note) WHERE id = ?",
        (status, utc_now_iso(), note, run_id),
    )


def set_run_status(conn: sqlite3.Connection, run_id: int, status: str) -> None:
    write(conn, "UPDATE runs SET status = ? WHERE id = ?", (status, run_id))


# --- stages -----------------------------------------------------------------


def set_stage(
    conn: sqlite3.Connection,
    run_id: int,
    stage: str,
    status: str,
    *,
    attempt: int | None = None,
    error: str | None = None,
    touch_started: bool = False,
    touch_finished: bool = False,
) -> None:
    row = conn.execute(
        "SELECT attempt FROM stages WHERE run_id = ? AND stage = ?", (run_id, stage)
    ).fetchone()

    if row is None:
        # Through ``write``, like every other write here: this branch is the
        # first time a stage row exists, and in 052 the very first stage
        # recording of a run could arrive inside the locked window -- the row
        # does not exist yet, so there is no UPDATE branch to fall back to.
        write(conn,
            "INSERT INTO stages (run_id, stage, status, attempt, started_at, finished_at, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                stage,
                status,
                attempt or 1,
                utc_now_iso() if touch_started or status == "running" else None,
                utc_now_iso() if touch_finished else None,
                error,
            ),
        )
    else:
        write(conn,
            """
            UPDATE stages
               SET status = ?,
                   attempt = COALESCE(?, attempt),
                   started_at = CASE WHEN ? THEN ? ELSE started_at END,
                   finished_at = CASE WHEN ? THEN ? ELSE finished_at END,
                   error = ?
             WHERE run_id = ? AND stage = ?
            """,
            (
                status,
                attempt,
                int(touch_started or status == "running"),
                utc_now_iso(),
                int(touch_finished),
                utc_now_iso(),
                error,
                run_id,
                stage,
            ),
        )


def get_stage(conn: sqlite3.Connection, run_id: int, stage: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM stages WHERE run_id = ? AND stage = ?", (run_id, stage)
    ).fetchone()


def list_stages(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM stages WHERE run_id = ? ORDER BY stage", (run_id,))
    )


# --- provider tasks ---------------------------------------------------------


def upsert_provider_task(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    provider: str,
    stage: str,
    label: str,
    query: str,
    argv: list[str],
    status: str = "pending",
    raw_path: str | None = None,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
) -> int:
    row = conn.execute(
        "SELECT id FROM provider_tasks WHERE run_id = ? AND label = ?", (run_id, label)
    ).fetchone()
    if row is not None:
        return int(row["id"])

    cur = write(conn,
        """
        INSERT INTO provider_tasks (run_id, provider, stage, label, query, argv_json,
                                    status, raw_path, stdout_path, stderr_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            provider,
            stage,
            label,
            query,
            json.dumps(argv, ensure_ascii=False),
            status,
            raw_path,
            stdout_path,
            stderr_path,
        ),
    )
    return int(cur.lastrowid)


def update_provider_task(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    status: str,
    exit_code: int | None = None,
    error: str | None = None,
    attempt: int | None = None,
) -> None:
    write(conn,
        """
        UPDATE provider_tasks
           SET status = ?,
               exit_code = COALESCE(?, exit_code),
               error = ?,
               attempt = COALESCE(?, attempt),
               started_at = CASE WHEN ? = 'running' THEN ? ELSE started_at END,
               finished_at = CASE WHEN ? IN ('completed','failed','interrupted','skipped')
                                  THEN ? ELSE finished_at END
         WHERE id = ?
        """,
        (
            status,
            exit_code,
            error,
            attempt,
            status,
            utc_now_iso(),
            status,
            utc_now_iso(),
            task_id,
        ),
    )


def update_provider_task_paths(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    raw_path: str,
    stdout_path: str,
    stderr_path: str,
    argv: list[str],
) -> None:
    """Point an existing task at the artifacts of its next attempt.

    A retried task writes to new files so the artifacts of the failed attempt
    stay on disk as evidence.
    """
    write(conn,
        """
        UPDATE provider_tasks
           SET raw_path = ?, stdout_path = ?, stderr_path = ?, argv_json = ?
         WHERE id = ?
        """,
        (raw_path, stdout_path, stderr_path, json.dumps(argv, ensure_ascii=False), task_id),
    )


def get_provider_task(conn: sqlite3.Connection, run_id: int, label: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM provider_tasks WHERE run_id = ? AND label = ?", (run_id, label)
    ).fetchone()


def list_provider_tasks(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM provider_tasks WHERE run_id = ? ORDER BY id", (run_id,)
        )
    )


def recover_orphan_tasks(conn: sqlite3.Connection, run_id: int) -> int:
    """Mark tasks left 'running' by a killed process as interrupted.

    A task row can only be 'running' if the orchestrator that owned it died,
    because the orchestrator sets a terminal status before exiting.
    """
    cur = write(conn,
        """
        UPDATE provider_tasks
           SET status = 'interrupted',
               finished_at = ?,
               error = COALESCE(error, 'orchestrator process ended while this task was running')
         WHERE run_id = ? AND status = 'running'
        """,
        (utc_now_iso(), run_id),
    )
    return cur.rowcount


# --- observations -----------------------------------------------------------


def upsert_observation(
    conn: sqlite3.Connection,
    *,
    provider: str,
    payload: Any,
    raw_json: str,
    content_hash: str,
    mode: str | None,
    query: str | None,
    source_url: str | None,
    scraped_at: str | None,
    record_schema: str | None,
    missing: list[str] | None,
    unknown: list[str] | None,
    raw_path: str | None,
    raw_line: int | None,
    run_id: int,
    acquired_via: str | None = None,
    source_page_url: str | None = None,
    detail_url: str | None = None,
    detail_fetched: bool | None = None,
    origin: str | None = None,
    snapshot_id: str | None = None,
    observed_at: str | None = None,
    input_path: str | None = None,
) -> tuple[int, bool]:
    """Insert or refresh an observation. Returns (id, created).

    The provenance keyword arguments (round 026 P0) are optional, so a caller
    that predates them — or a test that only cares about the original columns —
    still works.
    """
    now = utc_now_iso()
    provenance = (
        acquired_via,
        source_page_url,
        detail_url,
        None if detail_fetched is None else int(bool(detail_fetched)),
        origin,
        snapshot_id,
        observed_at,
        input_path,
    )
    row = conn.execute(
        "SELECT id FROM observations WHERE provider = ? AND content_hash = ?",
        (provider, content_hash),
    ).fetchone()

    if row is not None:
        write(conn,
            """
            UPDATE observations
               SET last_seen_at = ?, seen_count = seen_count + 1,
                   raw_path = COALESCE(?, raw_path),
                   raw_line = COALESCE(?, raw_line),
                   acquired_via = COALESCE(?, acquired_via),
                   source_page_url = COALESCE(?, source_page_url),
                   detail_url = COALESCE(?, detail_url),
                   detail_fetched = COALESCE(?, detail_fetched),
                   origin = COALESCE(?, origin),
                   snapshot_id = COALESCE(?, snapshot_id),
                   observed_at = COALESCE(?, observed_at),
                   input_path = COALESCE(?, input_path)
             WHERE id = ?
            """,
            (now, raw_path, raw_line, *provenance, row["id"]),
        )
        return int(row["id"]), False

    cur = write(conn,
        """
        INSERT INTO observations (provider, content_hash, mode, query, source_url,
                                  scraped_at, raw_json, record_schema, missing_json,
                                  unknown_json, raw_path, raw_line, first_run_id,
                                  first_seen_at, last_seen_at, seen_count,
                                  acquired_via, source_page_url, detail_url,
                                  detail_fetched, origin, snapshot_id, observed_at,
                                  input_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            provider,
            content_hash,
            mode,
            query,
            source_url,
            scraped_at,
            raw_json,
            record_schema,
            json.dumps(missing or [], ensure_ascii=False),
            json.dumps(unknown or [], ensure_ascii=False),
            raw_path,
            raw_line,
            run_id,
            now,
            now,
            *provenance,
        ),
    )
    return int(cur.lastrowid), True


def entity_provenance(conn: sqlite3.Connection, entity_id: int) -> dict[str, Any]:
    """How one entity's facts were obtained, aggregated over its observations.

    Returns the distinct acquisition methods and page URLs behind the entity, so
    the reader view can say *where* a record was read and *whether* the site was
    visited, instead of inferring a page from an identifier. Nothing is guessed:
    a missing value stays missing.
    """
    rows = conn.execute(
        """
        SELECT o.acquired_via, o.source_page_url, o.detail_url, o.detail_fetched,
               o.origin, o.snapshot_id, o.observed_at, o.input_path, o.source_url
          FROM observations o
          JOIN entity_observations eo ON eo.observation_id = o.id
         WHERE eo.entity_id = ?
        """,
        (entity_id,),
    ).fetchall()

    def distinct(key: str) -> list[str]:
        seen: list[str] = []
        for row in rows:
            value = row[key] if key in row.keys() else None
            if isinstance(value, str) and value.strip() and value.strip() not in seen:
                seen.append(value.strip())
        return seen

    return {
        "acquired_via": distinct("acquired_via"),
        "source_page_urls": distinct("source_page_url"),
        "detail_urls": distinct("detail_url"),
        "detail_fetched": any(bool(row["detail_fetched"]) for row in rows),
        "origins": distinct("origin"),
        "snapshot_ids": distinct("snapshot_id"),
        "observed_ats": distinct("observed_at"),
        "input_paths": distinct("input_path"),
        # The transport that used to double as the evidence URL; kept so a
        # caller can tell a page from a path without re-reading the schema.
        "source_urls": distinct("source_url"),
    }


def link_entity_observation(
    conn: sqlite3.Connection,
    *,
    entity_id: int,
    observation_id: int,
    run_id: int,
    query: str | None,
) -> None:
    write(conn,
        """
        INSERT OR IGNORE INTO entity_observations
            (entity_id, observation_id, run_id, query, linked_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (entity_id, observation_id, run_id, query, utc_now_iso()),
    )


# --- entities ---------------------------------------------------------------


def upsert_entity(
    conn: sqlite3.Connection,
    *,
    stable_key: str,
    id_kind: str,
    name: str | None,
    name_norm: str | None,
    address: str | None,
    address_norm: str | None,
    latitude: float | None,
    longitude: float | None,
    phone_norm: str | None,
    website: str | None,
    domain: str | None,
    country: str | None,
    crs: str | None = None,
) -> int:
    """Insert the entity or refresh the fields that are missing/empty.

    Existing non-empty values are never overwritten by an empty one, so a later
    thinner observation cannot erase evidence an earlier one supplied.
    """
    now = utc_now_iso()
    row = conn.execute(
        "SELECT id FROM entities WHERE stable_key = ?", (stable_key,)
    ).fetchone()

    if row is not None:
        write(conn,
            """
            UPDATE entities
               SET last_seen_at = ?,
                   name        = COALESCE(NULLIF(?, ''), name),
                   name_norm   = COALESCE(NULLIF(?, ''), name_norm),
                   address     = COALESCE(NULLIF(?, ''), address),
                   address_norm= COALESCE(NULLIF(?, ''), address_norm),
                   latitude    = COALESCE(?, latitude),
                   longitude   = COALESCE(?, longitude),
                   phone_norm  = COALESCE(NULLIF(?, ''), phone_norm),
                   website     = COALESCE(NULLIF(?, ''), website),
                   domain      = COALESCE(NULLIF(?, ''), domain),
                   country     = COALESCE(NULLIF(?, ''), country),
                   crs         = COALESCE(NULLIF(?, ''), crs)
             WHERE id = ?
            """,
            (
                now,
                name,
                name_norm,
                address,
                address_norm,
                latitude,
                longitude,
                phone_norm,
                website,
                domain,
                country,
                crs,
                row["id"],
            ),
        )
        return int(row["id"])

    cur = write(conn,
        """
        INSERT INTO entities (stable_key, id_kind, name, name_norm, address, address_norm,
                              latitude, longitude, phone_norm, website, domain, country, crs,
                              first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stable_key,
            id_kind,
            name,
            name_norm,
            address,
            address_norm,
            latitude,
            longitude,
            phone_norm,
            website,
            domain,
            country,
            crs,
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def fill_entity_website(conn: sqlite3.Connection, entity_id: int, url: str) -> bool:
    """Set an entity's website only when the listing left it empty.

    Discovery exists to fill a blank, never to overrule a value the source
    already stated: a listed site is the operator's data, and a discovered one
    is an inference. Returns True when the column was filled.
    """
    cur = write(conn,
        """
        UPDATE entities
           SET website = ?, domain = ?, last_seen_at = ?
         WHERE id = ? AND (website IS NULL OR TRIM(website) = '')
        """,
        (url, _domain_of(url), utc_now_iso(), entity_id),
    )
    return cur.rowcount > 0


def _domain_of(url: str) -> str | None:
    """The registrable domain of a URL, or None when it has no host."""
    from .webfetch import registrable_domain  # local: keeps db import-light

    text = str(url or "").strip()
    if not text:
        return None
    host = urlsplit(text if "//" in text else "//" + text).hostname or ""
    return registrable_domain(host) or None


def count_run_entities(conn: sqlite3.Connection, run_id: int) -> int:
    """Distinct entities seen by this run, i.e. the collected-so-far count."""
    return int(
        conn.execute(
            "SELECT COUNT(DISTINCT entity_id) AS n FROM entity_observations WHERE run_id = ?",
            (run_id,),
        ).fetchone()["n"]
    )


def count_run_stores(conn: sqlite3.Connection, run_id: int) -> int:
    """Distinct *stores* seen by this run: entities after cross-source merges.

    Two sources describing one business produce two entities and one store.
    Counting entities would inflate the result by exactly the number of
    duplicates that were merged, which is the opposite of what de-duplication is
    for.
    """
    return int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT COALESCE(m.canonical_entity_id, e.id)) AS n
              FROM entity_observations eo
              JOIN entities e ON e.id = eo.entity_id
              LEFT JOIN entity_merges m ON m.entity_id = e.id
             WHERE eo.run_id = ?
            """,
            (run_id,),
        ).fetchone()["n"]
    )


def iter_run_entities(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    """Every entity a run saw, as a list -- never as a live cursor.

    Materialising is load-bearing, not an optimisation. 053 found the shape
    it prevents: a caller iterating the cursor this function used to return
    while its own loop body wrote through ``write`` -- which is exactly what
    ``enrich_run`` does.  In WAL mode a connection that holds an open read
    statement holds a read snapshot; once any other connection commits
    (the screen does, on every poll), that snapshot is stale, and the
    connection's next write is refused *immediately* with
    ``SQLITE_BUSY_SNAPSHOT``, reported as "database is locked".  ``busy_timeout``
    cannot help it -- the error is raised without waiting -- and neither can
    ``write``'s retry: a rollback does not close a statement that is still
    being iterated, so every retry re-fails until the attempts run out.  The
    enrich stage then dies after ~80 s of retries with nothing written,
    which is how a healthy run got a ``failed`` status in 053's trace.

    Returning a snapshot makes the failure structurally impossible: the read
    is finished before the caller can write.  ``merge`` and ``sitediscovery``
    already wrapped this call in ``list(...)`` for the same reason, each in
    their own code, which is why enrich was the last place to be bitten.
    """
    return list(
        conn.execute(
            """
        SELECT e.*, MIN(eo.query) AS query
          FROM entities e
          JOIN entity_observations eo ON eo.entity_id = e.id
         WHERE eo.run_id = ?
         GROUP BY e.id
         ORDER BY e.id
        """,
            (run_id,),
        ).fetchall()
    )


# --- field facts ------------------------------------------------------------


def insert_fact(conn: sqlite3.Connection, **values: Any) -> bool:
    """Insert a field fact. Returns False when the identical fact already exists.

    ``fact_key`` is a hash of everything that makes the fact distinct, so a
    repeated scrape of unchanged data adds nothing while a changed value becomes
    a new row (evidence is never overwritten).

    ``validation_state`` and ``checks_json`` (added in stage D) default to NULL,
    so a caller that predates them — or a test that only cares about the
    original columns — still works.
    """
    values.setdefault("validation_state", None)
    values.setdefault("checks_json", None)
    cur = write(conn,
        """
        INSERT OR IGNORE INTO field_facts
            (entity_id, observation_id, run_id, field, raw_value, normalized_value,
             provider, source_url, query_context, scraped_at, evidence_snippet,
             evidence_ref, extractor_version, validation_status, validation_note,
             validation_state, checks_json, observed_at, fact_key)
        VALUES (:entity_id, :observation_id, :run_id, :field, :raw_value,
                :normalized_value, :provider, :source_url, :query_context, :scraped_at,
                :evidence_snippet, :evidence_ref, :extractor_version,
                :validation_status, :validation_note, :validation_state, :checks_json,
                :observed_at, :fact_key)
        """,
        values,
    )
    return cur.rowcount > 0


def latest_facts(conn: sqlite3.Connection, run_id: int) -> dict[tuple[int, str], sqlite3.Row]:
    """Latest fact per (entity, field) for the entities this run collected.

    Facts are looked up per *entity*, not per fact row's run id. A repeated run
    re-observes the same businesses, and its export must show what is known about
    them even though the fact rows were written by the earlier run. Each fact
    still records the run that wrote it.

    "Latest" is by ``observed_at``; ties are broken by row id so the result is
    deterministic.
    """
    rows = conn.execute(
        """
        SELECT f.*
          FROM field_facts f
          JOIN (
                SELECT entity_id, field, MAX(observed_at || '#' || printf('%08d', id)) AS pick
                  FROM field_facts
                 WHERE entity_id IN (SELECT entity_id FROM entity_observations WHERE run_id = ?)
                 GROUP BY entity_id, field
               ) newest
            ON newest.entity_id = f.entity_id
           AND newest.field = f.field
           AND newest.pick = f.observed_at || '#' || printf('%08d', f.id)
        """,
        (run_id,),
    )
    return {(int(r["entity_id"]), str(r["field"])): r for r in rows}


def iter_run_facts(conn: sqlite3.Connection, run_id: int) -> Iterator[sqlite3.Row]:
    """Every fact known about the entities this run collected.

    A live cursor, unlike ``iter_run_entities``: a run's facts are the one
    result set large enough that materialising it for a streaming export is
    a real cost. The consequence is on the caller -- iterate this *only*
    over a loop that does not write. A loop that writes while this cursor
    is open has its writes refused immediately and permanently (see
    ``iter_run_entities`` for the mechanism, 053 for the outage it caused).
    A writing caller must materialise: ``list(db.iter_run_facts(...))``.
    """
    return conn.execute(
        """
        SELECT f.* FROM field_facts f
         WHERE f.entity_id IN (SELECT entity_id FROM entity_observations WHERE run_id = ?)
         ORDER BY f.entity_id, f.field, f.id
        """,
        (run_id,),
    )


# --- errors -----------------------------------------------------------------


def insert_error(
    conn: sqlite3.Connection,
    *,
    run_id: int | None,
    stage: str | None,
    kind: str,
    message: str,
    context: Any = None,
    provider_task_id: int | None = None,
) -> int:
    cur = write(conn,
        """
        INSERT INTO errors (run_id, stage, provider_task_id, kind, message,
                            context_json, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            stage,
            provider_task_id,
            kind,
            message,
            json.dumps(context, ensure_ascii=False) if context is not None else None,
            utc_now_iso(),
        ),
    )
    return int(cur.lastrowid)


def list_errors(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM errors WHERE run_id = ? ORDER BY id", (run_id,)))


# --- checkpoints ------------------------------------------------------------


def set_checkpoint(
    conn: sqlite3.Connection, run_id: int, stage: str, key: str, value: Any
) -> None:
    write(conn,
        """
        INSERT INTO checkpoints (run_id, stage, key, value_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(run_id, stage, key)
        DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
        """,
        (run_id, stage, key, json.dumps(value, ensure_ascii=False, sort_keys=True), utc_now_iso()),
    )


def get_checkpoint(
    conn: sqlite3.Connection, run_id: int, stage: str, key: str
) -> Any | None:
    row = conn.execute(
        "SELECT value_json FROM checkpoints WHERE run_id = ? AND stage = ? AND key = ?",
        (run_id, stage, key),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["value_json"])


def list_checkpoints(conn: sqlite3.Connection, run_id: int, stage: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM checkpoints WHERE run_id = ? AND stage = ? ORDER BY key",
            (run_id, stage),
        )
    )


# --- cross-source entity merges (stage E) ------------------------------------


def upsert_merge(
    conn: sqlite3.Connection,
    *,
    canonical_entity_id: int,
    entity_id: int,
    reason: str,
    evidence: dict | None = None,
    score: float = 0.0,
    run_id: int | None = None,
) -> bool:
    """Record that one entity is the same store as another.

    Returns False when the identical decision already exists, which is what
    makes a re-run of the merge stage idempotent. The candidate values are never
    touched: the merged entity keeps every fact it had, so the merge can be
    undone by deleting the row rather than by recovering lost data.
    """
    already = conn.execute(
        "SELECT 1 FROM entity_merges WHERE canonical_entity_id = ? AND entity_id = ?",
        (canonical_entity_id, entity_id),
    ).fetchone()
    write(conn,
        """
        INSERT INTO entity_merges (canonical_entity_id, entity_id, reason,
                                   evidence_json, score, run_id, decided_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_entity_id, entity_id) DO UPDATE
            SET reason = excluded.reason,
                evidence_json = excluded.evidence_json,
                score = excluded.score,
                run_id = COALESCE(excluded.run_id, entity_merges.run_id),
                decided_at = excluded.decided_at
        """,
        (
            canonical_entity_id,
            entity_id,
            reason,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True) if evidence else None,
            float(score),
            run_id,
            utc_now_iso(),
        ),
    )
    # ``cursor.rowcount`` counts the rows *written*, and ON CONFLICT DO UPDATE
    # reports an updated row as well — so a repeated decision would look like a
    # new merge and inflate ``MergeStats.merged``. Whether this was a new
    # decision is answered by looking before writing, not after.
    return already is None


def iter_merges(conn: sqlite3.Connection, run_id: int | None = None) -> Iterator[sqlite3.Row]:
    sql = (
        "SELECT m.*, e.stable_key AS merged_stable_key "
        "FROM entity_merges m JOIN entities e ON e.id = m.entity_id"
    )
    params: tuple[Any, ...] = ()
    if run_id is not None:
        sql += " WHERE m.run_id = ?"
        params = (run_id,)
    sql += " ORDER BY m.canonical_entity_id, m.entity_id"
    return conn.execute(sql, params)


def count_merges(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COUNT(*) AS n FROM entity_merges").fetchone()["n"]
    )


def canonical_map(conn: sqlite3.Connection) -> dict[int, int]:
    """entity_id -> the entity it was merged into (identity when unmerged)."""
    return {
        int(row["entity_id"]): int(row["canonical_entity_id"])
        for row in conn.execute("SELECT entity_id, canonical_entity_id FROM entity_merges")
    }


def merge_members(conn: sqlite3.Connection, canonical_entity_id: int) -> list[int]:
    """Every entity folded into ``canonical_entity_id``, canonical included."""
    ids = [int(canonical_entity_id)]
    ids.extend(
        int(row["entity_id"])
        for row in conn.execute(
            "SELECT entity_id FROM entity_merges WHERE canonical_entity_id = ? ORDER BY entity_id",
            (canonical_entity_id,),
        )
    )
    return ids


# --- candidate sites (round 026 P1) -----------------------------------------


#: Verdicts a candidate may carry. ``pending`` is a first-class outcome: a page
#: whose name agrees but which supplies no independent signal is a lead for a
#: human, and storing it as a rejection would hide work still to be done.
SITE_CANDIDATE_STATUSES = ("confirmed", "pending", "rejected", "error")


def upsert_site_candidate(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    entity_id: int,
    url: str,
    host: str | None = None,
    found_by: str = "",
    origin: str | None = None,
    status: str = "pending",
    reason: str | None = None,
    note: str | None = None,
    evidence: Any = None,
    http_status: int | None = None,
) -> bool:
    """Record one candidate URL and the verdict on it.

    Returns True when a row was written (first judgement, or a re-judgement that
    actually changed something), False when the identical row was already there.
    The callers use that to avoid re-binding a website and re-incrementing a
    counter on a resumed run.
    """
    if status not in SITE_CANDIDATE_STATUSES:
        raise ValueError(
            f"unknown site candidate status {status!r}; expected one of {SITE_CANDIDATE_STATUSES}"
        )
    payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True) if evidence else None
    cur = write(conn,
        """
        INSERT INTO site_candidates
            (run_id, entity_id, url, host, found_by, origin, status, reason, note,
             evidence_json, http_status, judged_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, entity_id, url) DO UPDATE SET
            host          = excluded.host,
            found_by      = excluded.found_by,
            origin        = excluded.origin,
            status        = excluded.status,
            reason        = excluded.reason,
            note          = excluded.note,
            evidence_json = excluded.evidence_json,
            http_status   = excluded.http_status,
            judged_at     = excluded.judged_at
         WHERE site_candidates.status      IS NOT excluded.status
            OR site_candidates.reason      IS NOT excluded.reason
            OR site_candidates.note        IS NOT excluded.note
            OR site_candidates.http_status IS NOT excluded.http_status
            OR site_candidates.evidence_json IS NOT excluded.evidence_json
        """,
        (
            run_id,
            entity_id,
            url,
            host,
            found_by,
            origin,
            status,
            reason,
            note,
            payload,
            http_status,
            utc_now_iso(),
        ),
    )
    # rowcount is 0 in exactly one case: the row already existed and the
    # ``WHERE`` above found nothing different, so a resumed run neither
    # re-counts a verdict nor re-binds the same website.
    return cur.rowcount > 0


def recent_site_verdict(
    conn: sqlite3.Connection,
    *,
    entity_id: int,
    url: str,
    newer_than: str,
) -> sqlite3.Row | None:
    """A verdict on this (business, url) judged at or after ``newer_than``.

    Round 034, first batch, item 4: the request de-duplication has to state its
    scope and its expiry. Within a run it is the checkpoint, and never expires.
    Across runs this is the opt-in half: only a **confirmed** verdict is reused
    (a rejection or a pending is re-judged, because a site can appear or change
    its mind about what it says about itself), and only while it is newer than
    the caller's cut-off.

    Scoped to ``entity_id`` rather than the URL alone on purpose: the same URL
    proposed for two different businesses is two different questions.
    """
    row = conn.execute(
        """
        SELECT * FROM site_candidates
         WHERE entity_id = ? AND url = ? AND status = 'confirmed'
           AND judged_at >= ?
         ORDER BY judged_at DESC LIMIT 1
        """,
        (int(entity_id), str(url), str(newer_than)),
    ).fetchone()
    return row


def has_fact(
    conn: sqlite3.Connection,
    *,
    entity_id: int,
    field: str,
    provider: str,
    raw_value: str,
) -> bool:
    """Whether this exact fact is already on file for the entity.

    042: the way a re-judgement tells "the listing named this site" apart from
    "this stage confirmed it earlier". Both leave a website on the entity, but
    only the second is a verdict of ours that a rules or evidence change has to
    be able to revisit.
    """
    row = conn.execute(
        "SELECT 1 FROM field_facts WHERE entity_id = ? AND field = ? "
        "AND provider = ? AND raw_value = ? LIMIT 1",
        (int(entity_id), str(field), str(provider), str(raw_value)),
    ).fetchone()
    return row is not None


def site_candidate_row(
    conn: sqlite3.Connection, run_id: int, entity_id: int, url: str
) -> sqlite3.Row | None:
    """The one verdict row for this (run, business, url), if there is one.

    042: the way a pre-042 checkpoint is interpreted. Those stored the bare
    value ``True`` -- "a previous pass judged this" without the verdict -- and
    this row is where that verdict actually lives, so it is read back instead of
    being guessed at.
    """
    return conn.execute(
        "SELECT * FROM site_candidates WHERE run_id = ? AND entity_id = ? AND url = ?",
        (int(run_id), int(entity_id), str(url)),
    ).fetchone()


def iter_site_candidates(
    conn: sqlite3.Connection, run_id: int, *, status: str | None = None
) -> list[sqlite3.Row]:
    """The run's candidate sites, as a list. ``iter_run_entities`` explains why
    this returns a snapshot rather than a cursor: a caller that writes while
    iterating a live cursor has its writes refused immediately (053)."""
    sql = "SELECT * FROM site_candidates WHERE run_id = ?"
    params: list[Any] = [run_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY entity_id, url"
    return conn.execute(sql, params).fetchall()


def site_candidate_counts(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    """Counts for the run summary: candidates and each verdict.

    Returned as a dict (not rows) so the caller can report a zero for a verdict
    that never occurred, instead of quietly omitting it and reading as if the
    category did not exist.
    """
    counts = {
        "candidates": 0,
        "confirmed": 0,
        "pending": 0,
        "rejected": 0,
        "errors": 0,
    }
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM site_candidates WHERE run_id = ? GROUP BY status",
        (run_id,),
    ):
        status = str(row["status"])
        counts["candidates"] += int(row["n"])
        bucket = "errors" if status == "error" else status
        if bucket in counts:
            counts[bucket] = int(row["n"])
    return counts


def site_candidate_finders(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    """How many candidates each finder proposed, and how they turned out.

    A finder that proposed nothing is absent from the table, so the report
    distinguishes "this finder found none" from "this finder was not asked" by
    comparing against :data:`collector.sitediscovery.FINDER_NAMES`.
    """
    rows = conn.execute(
        """
        SELECT found_by,
               COUNT(*) AS candidates,
               SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed,
               SUM(CASE WHEN status = 'pending'   THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN status = 'rejected'  THEN 1 ELSE 0 END) AS rejected,
               SUM(CASE WHEN status = 'error'     THEN 1 ELSE 0 END) AS errors
          FROM site_candidates
         WHERE run_id = ?
         GROUP BY found_by
         ORDER BY found_by
        """,
        (run_id,),
    )
    return [
        {
            "finder": str(row["found_by"]),
            "candidates": int(row["candidates"] or 0),
            "confirmed": int(row["confirmed"] or 0),
            "pending": int(row["pending"] or 0),
            "rejected": int(row["rejected"] or 0),
            "errors": int(row["errors"] or 0),
        }
        for row in rows
    ]


# --- per-source status (round 026 P2) ---------------------------------------


def note_source_success(
    conn: sqlite3.Connection,
    source: str,
    *,
    network_mode: str = "",
    proxy: str = "",
    at: str | None = None,
) -> None:
    """Record that a source answered. Clears its cooldown, if any.

    The facts live per *source*, not per run: the cooldown a rate limit earned
    is about the site, and the next run should open knowing it rather than
    rediscovering it by sending another request into the same wall.
    """
    stamp = at or utc_now_iso()
    write(conn,
        """
        INSERT INTO source_status
            (source, network_mode, proxy, last_success_at, cooldown_until,
             cooldown_seconds, updated_at)
        VALUES (?, ?, ?, ?, NULL, NULL, ?)
        ON CONFLICT(source) DO UPDATE SET
            network_mode  = excluded.network_mode,
            proxy         = CASE WHEN excluded.proxy <> '' THEN excluded.proxy
                                 ELSE source_status.proxy END,
            last_success_at = excluded.last_success_at,
            cooldown_until  = NULL,
            cooldown_seconds = NULL,
            updated_at      = excluded.updated_at
        """,
        (source, network_mode, proxy, stamp, stamp),
    )


def note_source_failure(
    conn: sqlite3.Connection,
    source: str,
    *,
    error: str,
    error_kind: str = "",
    retry_after_seconds: int | None = None,
    network_mode: str = "",
    proxy: str = "",
    at: str | None = None,
) -> str | None:
    """Record a source failure; return the cooldown deadline it set, if any.

    A ``Retry-After`` is honoured as a cooldown and the deadline is *stored*, so
    the screen can show "冷却到 03:07" instead of a vague "recently failed". The
    site's own number is never extended, and a request is never made just to
    refresh the status: what the site said is what is recorded.
    """
    stamp = at or utc_now_iso()
    cooldown_until: str | None = None
    cooldown_seconds: int | None = None
    if retry_after_seconds and retry_after_seconds > 0:
        cooldown_seconds = int(retry_after_seconds)
        cooldown_until = _shift_iso(stamp, cooldown_seconds)
    write(conn,
        """
        INSERT INTO source_status
            (source, network_mode, proxy, last_error, last_error_at, last_error_kind,
             cooldown_until, cooldown_seconds, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            -- A blank network mode means "not known for this failure": keep what
            -- the successful call recorded rather than overwriting a known fact
            -- with an empty one.
            network_mode  = CASE WHEN excluded.network_mode <> '' THEN excluded.network_mode
                                 ELSE source_status.network_mode END,
            proxy         = CASE WHEN excluded.proxy <> '' THEN excluded.proxy
                                 ELSE source_status.proxy END,
            last_error    = excluded.last_error,
            last_error_at = excluded.last_error_at,
            last_error_kind = excluded.last_error_kind,
            -- Only a rate limit sets a cooldown. A 404 must not make the source
            -- look unavailable.
            cooldown_until = CASE WHEN excluded.cooldown_until IS NOT NULL
                                  THEN excluded.cooldown_until
                                  ELSE source_status.cooldown_until END,
            cooldown_seconds = CASE WHEN excluded.cooldown_seconds IS NOT NULL
                                    THEN excluded.cooldown_seconds
                                    ELSE source_status.cooldown_seconds END,
            updated_at    = excluded.updated_at
        """,
        (
            source,
            network_mode,
            proxy,
            error,
            stamp,
            error_kind,
            cooldown_until,
            cooldown_seconds,
            stamp,
        ),
    )
    return cooldown_until


def get_source_status(conn: sqlite3.Connection, source: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM source_status WHERE source = ?", (source,)).fetchone()


def list_source_status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every source's status, with the cooldown resolved against *now*.

    ``cooling_down`` is computed here rather than stored, because a stored
    boolean would be wrong the moment the clock moved past the deadline, and a
    screen that shows "冷却中" for a cooldown that ended an hour ago is worse
    than showing nothing.
    """
    now = utc_now_iso()
    out: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM source_status ORDER BY source"):
        entry = {key: row[key] for key in row.keys()}
        deadline = entry.get("cooldown_until")
        entry["cooling_down"] = bool(deadline) and str(deadline) > now
        out.append(entry)
    return out


def _shift_iso(stamp: str, seconds: int) -> str:
    """``stamp`` + ``seconds``, in the same ISO-8601 shape ``utc_now_iso`` uses."""
    try:
        base = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - utc_now_iso always produces this shape
        base = datetime.datetime.now(datetime.timezone.utc)
    return (base + datetime.timedelta(seconds=seconds)).isoformat()


# --- diagnostics ------------------------------------------------------------


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "runs",
        "stages",
        "provider_tasks",
        "observations",
        "entities",
        "entity_observations",
        "field_facts",
        "errors",
        "checkpoints",
        "site_candidates",
        "source_status",
    ]
    out: dict[str, int] = {}
    for table in tables:
        out[table] = int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
    return out


def iter_rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> Iterator[sqlite3.Row]:
    return conn.execute(sql, tuple(params))
