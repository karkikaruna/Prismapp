"""Bundled-results seeding.

PRISM ships pre-computed research results for all four validated models
(Llama-3.2 3B, Gemma-3 4B, Phi-4-mini, Mistral-7B) across both datasets - see :mod:`prism_core.resources` (``seed_results/``). A first-time install
should show a populated dashboard for every model immediately, with no
Ollama pull and no inference required.

This module materializes that bundled data into the exact same shape a real
:mod:`prism_core.benchmark` run produces - one ``runs/<benchmark_run_id>/``
directory per model (raw_responses/parsed/scored/summary) plus matching rows
in the local SQLite index - so the GUI cannot tell a seeded run from a run
the user triggered themselves. Nothing here is special-cased downstream:
``backend.has_data`` / ``model_dataset_rows`` / the dashboard all just see a
``status == "completed"`` run.

Re-running a model from the Benchmark screen behaves normally afterwards - a fresh run is inserted and becomes that model's new "latest completed run"
(``store.list_runs`` is newest-first), so the seeded data is simply
superseded, never deleted.
"""
from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from prism_core import config, fingerprint, paths, resources, store

SEED_DIR: Path = resources.DATA_DIR / "seed_results"
SEED_MARKER_KEY = "seeded_bundled_results_v1"


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def already_seeded(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (SEED_MARKER_KEY,)
    ).fetchone()
    return row is not None


def _mark_seeded(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (SEED_MARKER_KEY, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _copy_model_slice(model_tag: str, dest: paths.RunPaths) -> None:
    """Copy only this model's raw/parsed/scored/summary files out of the
    shared bundled results tree into the run's own directory."""
    dest.ensure()
    safe = paths.safe_model(model_tag)
    for dataset in config.DATASETS:
        for stage_dir, dest_dir in (
            ("raw_responses", dest.raw_dir),
            ("parsed", dest.parsed_dir),
            ("scored", dest.scored_dir),
        ):
            src = SEED_DIR / stage_dir / f"{dataset}__{safe}.jsonl"
            if src.exists():
                shutil.copy2(src, dest_dir / src.name)
        metrics_src = SEED_DIR / "summary" / f"{dataset}__{safe}_question_metrics.jsonl"
        if metrics_src.exists():
            shutil.copy2(metrics_src, dest.summary_dir / metrics_src.name)

    # model_dataset_summary.csv / model_prompt_summary.csv are shared across
    # models in the research export - copy them as-is so report.py's globbing
    # and the raw-CSV reads in backend.py keep working unchanged.
    for name in ("model_dataset_summary.csv", "model_prompt_summary.csv"):
        src = SEED_DIR / "summary" / name
        if src.exists():
            shutil.copy2(src, dest.summary_dir / name)


def seed_bundled_results(conn: sqlite3.Connection, runs_root: Path) -> list[str]:
    """Populate the index with one completed run per bundled model.

    Idempotent: a ``meta`` marker prevents re-seeding on every launch, and
    each seeded run is a normal row a user's own re-run can supersede.
    Returns the list of model tags that were seeded (empty if already done
    or if no bundled data is present, e.g. a stripped-down build).
    """
    if already_seeded(conn) or not SEED_DIR.exists():
        return []

    dataset_summary = _read_csv(SEED_DIR / "summary" / "model_dataset_summary.csv")
    if not dataset_summary:
        _mark_seeded(conn)
        return []

    seeded: list[str] = []
    base_now = datetime.now(timezone.utc)

    for i, model_tag in enumerate(config.MODELS):
        safe = paths.safe_model(model_tag)
        rows_for_model = [r for r in dataset_summary if paths.safe_model(r["model"]) == safe]
        if not rows_for_model:
            continue  # this model wasn't part of the bundled export

        run_id = fingerprint.new_benchmark_run_id()
        run_dir = paths.RunPaths.for_run(runs_root, run_id)
        _copy_model_slice(model_tag, run_dir)

        datasets_present = sorted({r["dataset"] for r in rows_for_model})
        question_count = int(float(rows_for_model[0].get("n_questions", 0) or 0))

        # Every seeded run previously got the *exact same* timestamp (one
        # `now` computed before the loop and reused for all four models),
        # which meant "most recent completed run" - what
        # store.list_runs()'s ORDER BY created_utc DESC, and anything
        # downstream that reads its first row, relies on to mean anything
        # - was actually a tie across all four models. The tie-break then
        # came down to unspecified SQL/SQLite ordering behavior, which in
        # practice resolved to the same model every time (Phi-4-mini) -
        # i.e. an apparent "default" nobody chose. Stagger each seeded
        # run's timestamp by a second per model (in stable config.MODELS
        # order) so there's always a well-defined, deterministic answer to
        # "which run is newest" instead of an accidental one.
        run_time = (base_now + timedelta(seconds=i)).isoformat()

        store.insert_run(
            conn,
            benchmark_run_id=run_id,
            created_utc=run_time,
            status="completed",
            model=model_tag,
            datasets=datasets_present,
            run_dir=str(run_dir.root),
            model_digest=None,
            question_count=question_count,
            app_version="bundled-seed-1.0",
            protocol_version=config.PROTOCOL_VERSION,
            finished_utc=run_time,
        )

        for row in rows_for_model:
            metrics = {k: (float(v) if _is_float(v) else v) for k, v in row.items()}
            # Seeded rows aren't tied to a live model digest, so the
            # equivalence fingerprint is scoped to "this bundled export" - # it still lets the Run screen detect "you already have this"
            # for these four validated models without an Ollama call.
            fp = f"bundled::{model_tag}::{row['dataset']}::{config.DATASET_VERSION}"
            store.insert_run_result(
                conn,
                benchmark_run_id=run_id,
                dataset=row["dataset"],
                config_fingerprint=fp,
                metrics=metrics,
                model=model_tag,
            )

        seeded.append(model_tag)

    _mark_seeded(conn)
    return seeded


def _is_float(value: str) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def seeded_report_pdfs() -> list[Path]:
    """Any pre-rendered PDF reports shipped with the bundled results."""
    reports_dir = SEED_DIR / "reports"
    if not reports_dir.exists():
        return []
    return sorted(reports_dir.glob("*.pdf"))