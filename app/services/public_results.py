"""
Public Results Fetcher & Client Service.

Fetches maintainer-approved benchmark results directly from the public GitHub
repository (Nabin-16/Reliability-test-result-model-versions) instead of querying
Supabase directly.

Features:
  - Fast, CDN-backed public model discovery via GitHub raw endpoints.
  - Local caching in ~/.prism/cache/ for offline resilience.
  - Seamless hydration into the local SQLite store so users can view verified
    benchmarks on the Dashboard without running local inference.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from prism_core import config, fingerprint, paths, store

GITHUB_RAW_BASE = (
    "https://raw.githubusercontent.com/Nabin-16/Reliability-test-result-model-versions/main/results"
)
CACHE_DIR = Path.home() / ".prism" / "cache"
INDEX_CACHE_FILE = CACHE_DIR / "public_index.json"
REQUEST_TIMEOUT = 5

# In-process memoization of the public index. Without this, every call to
# has_published_result() (in turn called once per ModelRow built on the
# startup screen - one per catalog model, plus again in main_window.py and
# run_screen.py) did its own blocking `requests.get` to GitHub. Building N
# model rows meant N sequential blocking network round-trips on the GUI
# thread just to draw the launch screen - the on-disk cache only ever
# helped after a network failure, never on the (common) success path. This
# cache makes only the *first* call in a process do real network I/O; every
# later call within the TTL is instant and in-memory.
_INDEX_MEMO: list[dict[str, Any]] | None = None
_INDEX_MEMO_AT: float = 0.0
_INDEX_MEMO_TTL_SECONDS = 300.0


def safe_model_filename(model_name: str) -> str:
    """Matches the public repo file naming convention."""
    clean = model_name.replace(":", "_").replace("/", "_").replace("-", "_")
    if clean.endswith("_latest"):
        clean = clean[:-7]
    return clean


def normalize_model_tag(tag: str) -> str:
    """Normalize model tag for lookup (e.g. 'phi4-mini' -> 'phi4-mini:latest')."""
    tag = tag.strip().lower()
    return tag


def fetch_public_index(force_refresh: bool = False) -> list[dict[str, Any]]:
    """Fetch the index catalog of published model results from GitHub.

    Memoized in-process for _INDEX_MEMO_TTL_SECONDS so repeated calls in the
    same run (e.g. one per model row on the startup screen) reuse the first
    result instead of each doing their own blocking network round-trip.
    Falls back to the on-disk cache, then to an empty list, if the network
    call fails.
    """
    global _INDEX_MEMO, _INDEX_MEMO_AT

    now = time.monotonic()
    if (
        not force_refresh
        and _INDEX_MEMO is not None
        and (now - _INDEX_MEMO_AT) < _INDEX_MEMO_TTL_SECONDS
    ):
        return _INDEX_MEMO

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not force_refresh and INDEX_CACHE_FILE.exists():
        # Check cache age if desired, or return cached on offline fallback
        pass

    try:
        resp = requests.get(
            f"{GITHUB_RAW_BASE}/index.json",
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                INDEX_CACHE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
                _INDEX_MEMO, _INDEX_MEMO_AT = data, now
                return data
    except Exception:
        pass

    # Offline / network failure fallback
    if INDEX_CACHE_FILE.exists():
        try:
            data = json.loads(INDEX_CACHE_FILE.read_text(encoding="utf-8"))
            _INDEX_MEMO, _INDEX_MEMO_AT = data, now
            return data
        except Exception:
            return []
    return []


def get_published_models_map() -> dict[str, dict[str, Any]]:
    """Return a mapping of normalized model tags to their catalog entries."""
    index = fetch_public_index()
    out = {}
    for entry in index:
        tag = entry.get("model")
        if tag:
            out[tag] = entry
            # Also map without :latest
            if ":latest" in tag:
                out[tag.replace(":latest", "")] = entry
            else:
                out[f"{tag}:latest"] = entry
    return out


def has_published_result(model_tag: str) -> bool:
    """Check if an approved public result exists on GitHub for this model tag."""
    models_map = get_published_models_map()
    return model_tag in models_map or normalize_model_tag(model_tag) in models_map


def fetch_model_result(model_tag: str) -> Optional[dict[str, Any]]:
    """Download the detailed public benchmark result JSON for a model."""
    safe_name = safe_model_filename(model_tag)
    model_cache = CACHE_DIR / f"{safe_name}.json"

    try:
        resp = requests.get(
            f"{GITHUB_RAW_BASE}/models/{safe_name}.json",
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            doc = resp.json()
            model_cache.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            return doc
    except Exception:
        pass

    if model_cache.exists():
        try:
            return json.loads(model_cache.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def import_public_result_into_local_store(
    conn: sqlite3.Connection,
    model_tag: str,
    runs_root: Path,
) -> Optional[str]:
    """Import a published GitHub benchmark result into the local SQLite store.

    Creates a completed run entry so the Dashboard screen, KPI tiles, and charts
    can immediately render the verified public result without needing local inference.
    Returns the benchmark_run_id if imported/existing, or None on failure.
    """
    doc = fetch_model_result(model_tag)
    if not doc:
        return None

    datasets_data = doc.get("datasets", {})
    if not datasets_data:
        return None

    run_id = doc.get("benchmark_run_id") or fingerprint.new_benchmark_run_id()
    created_utc = doc.get("created_utc") or datetime.now(timezone.utc).isoformat()
    protocol_version = doc.get("protocol_version") or "1.0"
    model_digest = doc.get("model_digest")
    dataset_names = sorted(datasets_data.keys())

    run_dir = paths.RunPaths.for_run(runs_root, run_id)
    run_dir.ensure()

    # Check if this run is already in the local DB
    existing = store.get_run(conn, run_id)
    if not existing:
        store.insert_run(
            conn,
            benchmark_run_id=run_id,
            created_utc=created_utc,
            finished_utc=created_utc,
            status="completed",
            model=model_tag,
            datasets=dataset_names,
            run_dir=str(run_dir.root),
            model_digest=model_digest,
            question_count=200,
            app_version="public-github-1.0",
            protocol_version=protocol_version,
        )

        for dataset_name, metrics in datasets_data.items():
            fp = f"github-public::{model_tag}::{dataset_name}::{config.DATASET_VERSION}"
            # Format metrics dictionary to match store column expectations
            metric_payload = {
                "n_questions": metrics.get("n_questions", 200),
                "prompt_response_accuracy": metrics.get("accuracy"),
                "conditional_accuracy": metrics.get("conditional_accuracy"),
                "answer_recovery_rate": metrics.get("answer_recovery_rate"),
                "instruction_compliance_rate": metrics.get("instruction_compliance_rate"),
                "question_majority_accuracy": metrics.get("question_majority_accuracy"),
                "mean_agreement": metrics.get("agreement"),
                "mean_prompt_sensitivity": metrics.get("prompt_sensitivity"),
                "answer_unanimous_rate": metrics.get("answer_unanimous_rate"),
                "prompt_invariant_incorrect_rate": metrics.get("prompt_invariant_incorrect_rate"),
            }
            store.insert_run_result(
                conn,
                benchmark_run_id=run_id,
                dataset=dataset_name,
                config_fingerprint=fp,
                metrics=metric_payload,
                model=model_tag,
            )

    return run_id