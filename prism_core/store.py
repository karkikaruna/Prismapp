"""Local run index (SQLite, stdlib only).

This is an **index, not a blob store** (plan §"Local store"): the heavy
artifacts stay as JSONL/CSV under ``runs/<id>/``. The DB records which runs
exist and - crucially - the per-(model, dataset) ``config_fingerprint`` so the
run-setup screen can answer *"is there already a local result equivalent to what
you're about to run?"* without re-reading every run's files.

Two tables:

* ``runs`` - one row per :func:`prism_core.fingerprint.new_benchmark_run_id`.
* ``run_results`` - one row per (run, dataset) with the fingerprint and the
  headline model/dataset-level metrics for fast listing.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

SCHEMA_VERSION = 1

# Metric columns mirrored from model_dataset_summary for fast listing without
# reopening the run's CSVs. Names match report.build_model_dataset_summary keys.
_RESULT_METRIC_COLUMNS = (
    "n_questions",
    "prompt_response_accuracy",
    "conditional_accuracy",
    "answer_recovery_rate",
    "instruction_compliance_rate",
    "question_majority_accuracy",
    "mean_agreement",
    "mean_prompt_sensitivity",
    "answer_unanimous_rate",
    "prompt_invariant_incorrect_rate",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    benchmark_run_id  TEXT PRIMARY KEY,
    created_utc       TEXT NOT NULL,
    finished_utc      TEXT,
    status            TEXT NOT NULL,
    model             TEXT NOT NULL,
    model_digest      TEXT,
    datasets          TEXT NOT NULL,          -- JSON array of dataset names
    question_count    INTEGER,
    app_version       TEXT,
    protocol_version  TEXT,
    run_dir           TEXT NOT NULL,
    sync_status       TEXT DEFAULT 'pending',
    synced_at         TEXT,
    sync_attempts     INTEGER DEFAULT 0,
    last_sync_error   TEXT
);

CREATE TABLE IF NOT EXISTS run_results (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    benchmark_run_id            TEXT NOT NULL REFERENCES runs(benchmark_run_id) ON DELETE CASCADE,
    dataset                     TEXT NOT NULL,
    model                       TEXT,
    config_fingerprint          TEXT NOT NULL,
    n_questions                 INTEGER,
    prompt_response_accuracy    REAL,
    conditional_accuracy        REAL,
    answer_recovery_rate        REAL,
    instruction_compliance_rate REAL,
    question_majority_accuracy  REAL,
    mean_agreement              REAL,
    mean_prompt_sensitivity     REAL,
    answer_unanimous_rate       REAL,
    prompt_invariant_incorrect_rate REAL,
    UNIQUE (benchmark_run_id, dataset)
);

CREATE INDEX IF NOT EXISTS idx_run_results_fingerprint ON run_results(config_fingerprint);
CREATE INDEX IF NOT EXISTS idx_run_results_run ON run_results(benchmark_run_id);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the run index and ensure the schema exists."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _migrate_add_run_results_model_column(conn)
    _migrate_add_sync_outbox_columns(conn)
    _migrate_add_run_results_unique_constraint(conn)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _migrate_add_sync_outbox_columns(conn: sqlite3.Connection) -> None:
    """Add the Supabase-sync outbox columns (``sync_status``, ``synced_at``,
    ``sync_attempts``, ``last_sync_error``) to ``runs`` for a DB created
    before these existed, and retroactively mark bundled/public seed runs
    as ``exempt`` so they're never queued for cloud sync.

    Same reasoning as ``_migrate_add_run_results_model_column``: the
    ``CREATE TABLE IF NOT EXISTS`` in ``_SCHEMA`` only takes effect for a
    brand-new database, so an existing ``~/.prism/index.db`` needs these
    columns added explicitly.
    """
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(runs)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        for col_name, col_type in (
            ("sync_status", "TEXT DEFAULT 'pending'"),
            ("synced_at", "TEXT"),
            ("sync_attempts", "INTEGER DEFAULT 0"),
            ("last_sync_error", "TEXT"),
        ):
            if col_name not in existing_cols:
                cursor.execute(f"ALTER TABLE runs ADD COLUMN {col_name} {col_type}")

        # Seed and public benchmark datasets are exempt from cloud syncing.
        cursor.execute(
            """
            UPDATE runs
            SET sync_status = 'exempt'
            WHERE (app_version LIKE 'bundled%' OR app_version LIKE 'public%')
              AND (sync_status != 'exempt' OR sync_status IS NULL)
            """
        )
        conn.commit()
    except Exception:
        pass


def _migrate_add_run_results_model_column(conn: sqlite3.Connection) -> None:
    """Add ``run_results.model`` immediately after ``dataset`` for a DB
    created before this column existed.

    ``CREATE TABLE IF NOT EXISTS`` in ``_SCHEMA`` only applies to brand-new
    databases - it's a no-op against an existing ``run_results`` table on
    disk, so anyone with a pre-existing ``~/.prism/index.db`` would never
    get the new column without this. A plain ``ALTER TABLE ... ADD COLUMN``
    would technically add it, but SQLite always appends new columns at the
    *end* of the table regardless of where they're declared - there's no
    "ADD COLUMN ... AFTER" - so that alone would leave ``model`` trailing
    after every metric column instead of sitting next to ``dataset`` the
    way a fresh install gets it. Rebuild the table instead (SQLite's
    standard way to reorder columns): create a new table with the desired
    column order, copy every row across, then drop the old one - the usual
    12-step ALTER TABLE recipe from the SQLite docs, done here as one
    explicit rebuild since we already know the exact before/after shape.
    Backfills ``model`` from the parent ``runs`` row via ``benchmark_run_id``
    so old rows aren't left blank. Existing indexes are recreated at the end
    since dropping the renamed-away old table drops any index still
    attached to it.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(run_results)")}
    if "model" in columns:
        return

    # ``n_questions`` is a count and belongs on INTEGER affinity, matching
    # the original _SCHEMA - every other metric here is a genuine float
    # (a rate/score). Declaring it REAL here (as earlier versions of this
    # migration did) let SQLite return it as a Python float instead of an
    # int, which broke the Supabase sync payload downstream: Postgres's
    # integer parser rejects a literal like "200.0" outright.
    metric_cols_def = ",\n            ".join(
        f"{c} {'INTEGER' if c == 'n_questions' else 'REAL'}"
        for c in _RESULT_METRIC_COLUMNS
    )
    metric_cols_list = ", ".join(_RESULT_METRIC_COLUMNS)

    conn.executescript(
        f"""
        ALTER TABLE run_results RENAME TO run_results_old;

        CREATE TABLE run_results (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            benchmark_run_id            TEXT NOT NULL REFERENCES runs(benchmark_run_id) ON DELETE CASCADE,
            dataset                     TEXT NOT NULL,
            model                       TEXT,
            config_fingerprint          TEXT NOT NULL,
            {metric_cols_def}
        );

        INSERT INTO run_results (
            id, benchmark_run_id, dataset, model, config_fingerprint, {metric_cols_list}
        )
        SELECT
            old.id, old.benchmark_run_id, old.dataset,
            (SELECT runs.model FROM runs WHERE runs.benchmark_run_id = old.benchmark_run_id),
            old.config_fingerprint, {", ".join(f"old.{c}" for c in _RESULT_METRIC_COLUMNS)}
        FROM run_results_old AS old;

        DROP TABLE run_results_old;

        CREATE INDEX IF NOT EXISTS idx_run_results_fingerprint ON run_results(config_fingerprint);
        CREATE INDEX IF NOT EXISTS idx_run_results_run ON run_results(benchmark_run_id);
        """
    )


def _migrate_add_run_results_unique_constraint(conn: sqlite3.Connection) -> None:
    """Add ``UNIQUE (benchmark_run_id, dataset)`` to ``run_results`` for a DB
    created before this constraint existed.

    Without it, ``insert_run_result`` (formerly a plain ``INSERT``, now an
    upsert - see below) could accumulate more than one row for the same
    ``(benchmark_run_id, dataset)`` pair, e.g. across a resumed/rerun
    benchmark. That's mostly invisible locally, but ``supabase_sync.sync_run``
    batches *every* local row for a run into one upsert request keyed on
    ``on_conflict=benchmark_run_id,dataset`` - if two rows in that same
    batch share a conflict key, Postgres rejects the whole request with
    ``21000 ON CONFLICT DO UPDATE command cannot affect row a second time``,
    which previously failed the sync for a run whose inference had actually
    completed successfully.

    SQLite has no ``ALTER TABLE ... ADD CONSTRAINT``, so - as with
    ``_migrate_add_run_results_model_column`` above - this rebuilds the
    table. Any pre-existing duplicate rows are collapsed first (keeping the
    highest ``id``, i.e. the most recent result for that dataset) since a
    ``UNIQUE`` index cannot be created over data that already violates it.
    """
    existing_constraints = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'run_results'"
    ).fetchone()
    if existing_constraints and "UNIQUE" in (existing_constraints[0] or ""):
        return

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(run_results)")}
    if not columns:
        return  # table doesn't exist yet (fresh DB, _SCHEMA already covers it)

    metric_cols_def = ",\n            ".join(
        f"{c} {'INTEGER' if c == 'n_questions' else 'REAL'}"
        for c in _RESULT_METRIC_COLUMNS
    )
    metric_cols_list = ", ".join(_RESULT_METRIC_COLUMNS)

    conn.executescript(
        f"""
        DELETE FROM run_results
        WHERE id NOT IN (
            SELECT MAX(id) FROM run_results GROUP BY benchmark_run_id, dataset
        );

        ALTER TABLE run_results RENAME TO run_results_old;

        CREATE TABLE run_results (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            benchmark_run_id            TEXT NOT NULL REFERENCES runs(benchmark_run_id) ON DELETE CASCADE,
            dataset                     TEXT NOT NULL,
            model                       TEXT,
            config_fingerprint          TEXT NOT NULL,
            {metric_cols_def},
            UNIQUE (benchmark_run_id, dataset)
        );

        INSERT INTO run_results (
            id, benchmark_run_id, dataset, model, config_fingerprint, {metric_cols_list}
        )
        SELECT
            old.id, old.benchmark_run_id, old.dataset, old.model,
            old.config_fingerprint, {", ".join(f"old.{c}" for c in _RESULT_METRIC_COLUMNS)}
        FROM run_results_old AS old;

        DROP TABLE run_results_old;

        CREATE INDEX IF NOT EXISTS idx_run_results_fingerprint ON run_results(config_fingerprint);
        CREATE INDEX IF NOT EXISTS idx_run_results_run ON run_results(benchmark_run_id);
        """
    )
    conn.commit()


def insert_run(
    conn: sqlite3.Connection,
    *,
    benchmark_run_id: str,
    created_utc: str,
    status: str,
    model: str,
    datasets: Iterable[str],
    run_dir: str,
    model_digest: Optional[str] = None,
    question_count: Optional[int] = None,
    app_version: Optional[str] = None,
    protocol_version: Optional[str] = None,
    finished_utc: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO runs (
            benchmark_run_id, created_utc, finished_utc, status, model,
            model_digest, datasets, question_count, app_version,
            protocol_version, run_dir
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            benchmark_run_id,
            created_utc,
            finished_utc,
            status,
            model,
            model_digest,
            json.dumps(list(datasets)),
            question_count,
            app_version,
            protocol_version,
            run_dir,
        ),
    )
    conn.commit()


def set_run_status(
    conn: sqlite3.Connection,
    benchmark_run_id: str,
    status: str,
    *,
    finished_utc: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE runs SET status = ?, finished_utc = COALESCE(?, finished_utc) "
        "WHERE benchmark_run_id = ?",
        (status, finished_utc, benchmark_run_id),
    )
    conn.commit()


def insert_run_result(
    conn: sqlite3.Connection,
    *,
    benchmark_run_id: str,
    dataset: str,
    config_fingerprint: str,
    metrics: Mapping[str, Any],
    model: Optional[str] = None,
) -> None:
    """Record one (run, dataset) result row, pulling known metric columns.

    ``model`` is denormalized onto this row (in addition to living on the
    parent ``runs`` row) so callers that list/filter run_results directly
    - e.g. a dashboard or export - never need to join back to ``runs`` just
    to know which model a given result belongs to.
    """
    values = [metrics.get(column) for column in _RESULT_METRIC_COLUMNS]
    # Defensive: force n_questions to a real int no matter what type it
    # arrives as (a prior schema-migration bug could have left an existing
    # local DB's column with REAL affinity, which silently turns 200 into
    # 200.0 - a value Postgres's integer parser rejects outright once this
    # row is later synced to Supabase). Every other metric here is a
    # genuine float and is left alone.
    n_idx = _RESULT_METRIC_COLUMNS.index("n_questions")
    if values[n_idx] is not None:
        values[n_idx] = int(float(values[n_idx]))
    columns = ", ".join(_RESULT_METRIC_COLUMNS)
    placeholders = ", ".join("?" for _ in _RESULT_METRIC_COLUMNS)
    # Upsert on (benchmark_run_id, dataset) rather than a plain INSERT: a
    # resumed/rerun benchmark calls this again for a dataset it already has
    # a row for, and a second plain INSERT left two rows for the same pair
    # sitting in the local table with nothing to distinguish "current" from
    # "stale". That silently broke Supabase sync (see
    # _migrate_add_run_results_unique_constraint) once both duplicate rows
    # were batched into the same upsert request.
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in _RESULT_METRIC_COLUMNS)
    conn.execute(
        f"""
        INSERT INTO run_results (
            benchmark_run_id, dataset, model, config_fingerprint, {columns}
        ) VALUES (?, ?, ?, ?, {placeholders})
        ON CONFLICT (benchmark_run_id, dataset) DO UPDATE SET
            model = excluded.model,
            config_fingerprint = excluded.config_fingerprint,
            {update_clause}
        """,
        (benchmark_run_id, dataset, model, config_fingerprint, *values),
    )
    conn.commit()


def get_run(conn: sqlite3.Connection, benchmark_run_id: str) -> Optional[sqlite3.Row]:
    cursor = conn.execute(
        "SELECT * FROM runs WHERE benchmark_run_id = ?", (benchmark_run_id,)
    )
    return cursor.fetchone()


def list_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All runs, newest first."""
    cursor = conn.execute("SELECT * FROM runs ORDER BY created_utc DESC")
    return list(cursor.fetchall())


def get_run_results(conn: sqlite3.Connection, benchmark_run_id: str) -> list[sqlite3.Row]:
    cursor = conn.execute(
        "SELECT * FROM run_results WHERE benchmark_run_id = ? ORDER BY dataset",
        (benchmark_run_id,),
    )
    return list(cursor.fetchall())


def find_results_by_fingerprint(
    conn: sqlite3.Connection, config_fingerprint: str
) -> list[sqlite3.Row]:
    """Existing local results equivalent to a given fingerprint (joined to runs).

    Powers the "an equivalent local benchmark already exists" prompt on the
    run-setup screen. Only completed runs are considered.
    """
    cursor = conn.execute(
        """
        SELECT r.*, runs.model, runs.model_digest, runs.created_utc,
               runs.status, runs.run_dir
        FROM run_results AS r
        JOIN runs ON runs.benchmark_run_id = r.benchmark_run_id
        WHERE r.config_fingerprint = ? AND runs.status = 'completed'
        ORDER BY runs.created_utc DESC
        """,
        (config_fingerprint,),
    )
    return list(cursor.fetchall())


def delete_run(conn: sqlite3.Connection, benchmark_run_id: str) -> None:
    """Remove a run and its result rows (cascade)."""
    conn.execute("DELETE FROM runs WHERE benchmark_run_id = ?", (benchmark_run_id,))
    conn.commit()