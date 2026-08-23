"""
PRISM backend adapter.

This is the *only* place the GUI touches prism_core. It wraps the Qt-free
engine (prism_core.ollama / prism_core.benchmark / prism_core.store) with:

  - a fixed on-disk location for the local run index + run outputs
    (~/.prism/index.db, ~/.prism/runs/<run_id>/...) - the same SQLite file
    and JSON/CSV run artifacts can be committed to a repo, synced, or served
    by a companion website; nothing here is GUI-specific.
  - read helpers scoped to a single model (dataset-level metrics from the
    SQLite index, per-prompt-condition + per-question detail read from the
    latest completed run's own summary files on disk).
  - Qt worker threads (PullWorker, BenchmarkWorker) so the engine's plain
    callback-based progress reporting becomes Qt signals for the GUI.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QThread, Signal

from prism_core import config, ollama, seed, store
from prism_core import benchmark as core_benchmark
from prism_core.benchmark import BenchmarkStage
from app.services import supabase_sync, public_results

APP_DIR = Path.home() / ".prism"
DB_PATH = APP_DIR / "index.db"
RUNS_ROOT = APP_DIR / "runs"


def ensure_bundled_seed() -> list[str]:
    """Populate the local index with the shipped results for all four
    validated models the first time the app runs, so the dashboard has data
    with no Ollama pull or inference needed. Safe to call on every launch - a no-op after the first time. Any model can still be re-run for real
    from the Benchmark screen; that just adds a newer completed run."""
    conn = get_conn()
    try:
        return seed.seed_bundled_results(conn, RUNS_ROOT)
    finally:
        conn.close()


def has_public_result(model_tag: str) -> bool:
    """Check if a verified public result exists on GitHub for this model."""
    return public_results.has_published_result(model_tag)


def import_public_result(model_tag: str) -> Optional[str]:
    """Import a verified public result into the local index."""
    conn = get_conn()
    try:
        return public_results.import_public_result_into_local_store(conn, model_tag, RUNS_ROOT)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Catalog / Ollama status
# --------------------------------------------------------------------------

def models_catalog() -> dict[str, dict[str, str]]:
    return config.MODELS


def datasets_catalog() -> dict[str, dict[str, Any]]:
    return config.DATASETS


def ollama_available() -> bool:
    return ollama.is_available()


def ollama_ensure_no_cloud_env() -> bool:
    """If Ollama is already running but missing ``OLLAMA_NO_CLOUD=1``,
    restart it under the corrected environment. Returns True if a restart
    was performed (caller should re-poll availability before proceeding),
    False if nothing needed to change (already correct, or undeterminable
    on this platform - see ``ollama_installer._server_has_no_cloud_env``).

    This mirrors the check ``ollama_installer.ensure_ollama`` already does
    for the "Install/Start Ollama" button path - but that path is only
    ever reached when the button is clicked, which never happens if
    Ollama was *already* answering `/api/tags` at launch (e.g. started
    manually, or left running from a previous session). Without this, a
    plain `ollama serve` with no `OLLAMA_NO_CLOUD` set looks fully
    "CONNECTED" to the app, and pulls then hang indefinitely on "pulling
    manifest" on any network that can't reach Ollama's cloud endpoint -
    exactly the symptom this setting exists to avoid.
    """
    from prism_core import ollama_installer
    if ollama_installer._server_has_no_cloud_env() is False:
        if ollama_installer._kill_stale_server_on_port():
            ollama_installer.start()
            return True
    return False


def ollama_installed_on_device() -> bool:
    from prism_core import ollama_installer
    return ollama_installer.is_installed()


def ollama_stop() -> bool:
    """Best-effort stop of any locally running Ollama server. Called on
    app shutdown so the app never leaves a background process running,
    and the next launch always starts clean via the app's own Start
    Ollama step rather than silently reusing whatever's left over."""
    from prism_core import ollama_installer
    try:
        return ollama_installer.stop()
    except Exception:
        return False


def ollama_auto_install_supported() -> bool:
    from prism_core import ollama_installer
    return ollama_installer.is_platform_supported()


def normalize_tag(tag: str) -> str:
    """Treat a bare tag (``"qwen2.5"``) and its explicit ``:latest`` form
    (``"qwen2.5:latest"``) as the same model - Ollama's own listing is
    inconsistent about which form it reports. This must NOT collapse two
    *different* explicit tags of the same base name (``"qwen2.5:0.5b"``
    vs ``"qwen2.5:3b"`` are different weights on disk, not the same
    model) - only the missing-tag case is ambiguous, an explicit
    non-"latest" tag never is.
    """
    return tag if ":" in tag else f"{tag}:latest"


def installed_model_tags() -> set[str]:
    try:
        names = {m.name for m in ollama.list_models()}
    except ollama.OllamaError:
        return set()
    # Normalize every installed name so a bare "qwen2.5" and an explicit
    # "qwen2.5:latest" from Ollama are recognized as the same entry,
    # without ever widening the match to a *different* explicit tag of
    # the same base name (see _normalize_tag - previously this added the
    # bare base name for every installed model unconditionally, e.g.
    # installing qwen2.5:3b also added bare "qwen2.5" to this set, which
    # then falsely matched qwen2.5:0.5b, qwen2.5:7b, or any other
    # never-pulled qwen2.5 variant as "installed" - letting the app
    # attempt inference against a model that was never actually on disk).
    return {normalize_tag(n) for n in names}


def installed_models() -> list[ollama.OllamaModel]:
    """Every model Ollama currently reports installed on this device, in
    its native (untolerated) tag form - i.e. exactly what would need its
    own row/entry anywhere the app lists "other" models beyond the four
    curated ones. Returns an empty list (never raises) if Ollama can't be
    reached right now."""
    try:
        return ollama.list_models()
    except ollama.OllamaError:
        return []


def is_installed(model_tag: str) -> bool:
    installed = installed_model_tags()
    return normalize_tag(model_tag) in installed


def model_label(model_tag: str) -> str:
    meta = config.MODELS.get(model_tag)
    if meta:
        return meta["label"]
    return model_tag


# --------------------------------------------------------------------------
# RAM checks - advisory warning before pulling a model too big for this
# device, never a hard block (the user can always choose to continue).
# --------------------------------------------------------------------------

_PARAM_COUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b(?![a-zA-Z])", re.IGNORECASE)


def system_ram_gb() -> Optional[float]:
    """Total physical RAM on this device, in GiB. Returns ``None`` if it
    can't be determined (e.g. ``psutil`` isn't installed) - callers should
    treat that as "unknown" and skip the warning rather than assume a
    failure."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        return psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        return None


def estimate_model_ram_gb(model_tag: str) -> Optional[float]:
    """Recommended RAM to run ``model_tag``, in GiB.

    Uses the curated figure in :data:`config.MODELS` for the four
    validated models. For any other tag (custom/cloud-saved models), it
    falls back to parsing a parameter count out of the tag itself (e.g.
    ``qwen2.5:14b`` -> 14) and estimates ~2x that in GB, which is the rule
    of thumb Ollama itself uses for RAM sizing. Returns ``None`` when no
    estimate can be made (e.g. a tag with no discernible parameter count),
    in which case callers should skip the RAM check entirely rather than
    warn on a guess.
    """
    meta = config.MODELS.get(model_tag)
    if meta and "min_ram_gb" in meta:
        return float(meta["min_ram_gb"])

    match = _PARAM_COUNT_RE.search(model_tag)
    if not match:
        return None
    try:
        params_b = float(match.group(1))
    except ValueError:
        return None
    if params_b <= 0:
        return None
    return max(4.0, round(params_b * 2))


def check_ram_for_model(model_tag: str) -> Optional[tuple[float, float]]:
    """Returns ``(required_gb, available_gb)`` if this device likely has
    too little RAM to run ``model_tag``, or ``None`` if the model should be
    fine (or there isn't enough information to say either way)."""
    required = estimate_model_ram_gb(model_tag)
    available = system_ram_gb()
    if required is None or available is None:
        return None
    if available < required:
        return required, available
    return None


# A device with less than this fraction of a model's recommended RAM isn't
# just "may run slowly" territory - the model is very unlikely to load at
# all (Ollama will typically fail outright or the OS will refuse the
# allocation). Below this threshold the app hard-blocks instead of asking
# "continue anyway?".
_HARD_INCOMPATIBLE_RATIO = 0.5


def model_compatibility_error(model_tag: str) -> Optional[str]:
    """Returns a ready-to-display error message if ``model_tag`` is so far
    beyond this device's capability that pulling/running it should be
    blocked outright (not just warned about), or ``None`` if it's fine or
    merely marginal (see :func:`check_ram_for_model` for the soft warning)."""
    required = estimate_model_ram_gb(model_tag)
    available = system_ram_gb()
    if required is None or available is None:
        return None
    if available >= required * _HARD_INCOMPATIBLE_RATIO:
        return None
    return (
        f"\u201c{model_tag}\u201d cannot run on this device.\n\n"
        f"It recommends about {required:.0f} GB of RAM to load, but this "
        f"device only has about {available:.1f} GB total. This is too far "
        "below the requirement for Ollama to load the model at all - "
        "downloading it would just fail partway through inference.\n\n"
        "Choose a smaller model instead."
    )


# --------------------------------------------------------------------------
# Local run index (SQLite)
# --------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    return store.connect(DB_PATH)


def _latest_completed_run(conn: sqlite3.Connection, model_tag: str) -> Optional[sqlite3.Row]:
    for row in store.list_runs(conn):  # newest first
        if row["model"] == model_tag and row["status"] == "completed":
            return row
    return None


def has_data(conn: sqlite3.Connection, model_tag: str) -> bool:
    return _latest_completed_run(conn, model_tag) is not None


def models_with_data(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Every model with at least one completed run -> its most recent run row."""
    out: dict[str, sqlite3.Row] = {}
    for row in store.list_runs(conn):
        if row["status"] == "completed" and row["model"] not in out:
            out[row["model"]] = row
    return out


def model_dataset_rows(conn: sqlite3.Connection, model_tag: str) -> list[dict[str, Any]]:
    """Dataset-level aggregate metrics for a model's latest completed run."""
    run = _latest_completed_run(conn, model_tag)
    if run is None:
        return []
    rows = store.get_run_results(conn, run["benchmark_run_id"])
    out = []
    for r in rows:
        d = dict(r)
        d["model"] = model_tag
        out.append(d)
    return out


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def model_prompt_rows(conn: sqlite3.Connection, model_tag: str) -> list[dict[str, Any]]:
    """Per-prompt-condition (P0..P4) breakdown for a model's latest run."""
    run = _latest_completed_run(conn, model_tag)
    if run is None:
        return []
    return _read_csv(Path(run["run_dir"]) / "summary" / "model_prompt_summary.csv")


def model_question_metrics(conn: sqlite3.Connection, model_tag: str) -> list[dict[str, Any]]:
    run = _latest_completed_run(conn, model_tag)
    if run is None:
        return []
    summary_dir = Path(run["run_dir"]) / "summary"
    if not summary_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for f in sorted(summary_dir.glob("*_question_metrics.jsonl")):
        records.extend(_read_jsonl(f))
    return records


def run_info(conn: sqlite3.Connection, model_tag: str) -> Optional[dict[str, Any]]:
    run = _latest_completed_run(conn, model_tag)
    return dict(run) if run is not None else None


# --------------------------------------------------------------------------
# Multi-model comparison (all models with local data at once)
# --------------------------------------------------------------------------

def all_models_dataset_rows(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """{model_tag: [dataset-level rows]} for every model that has a completed
    local run. Powers the all-model comparison tab/PDF, as opposed to the
    single vs. single overlay used elsewhere on the dashboard."""
    out: dict[str, list[dict[str, Any]]] = {}
    for tag in models_with_data(conn):
        out[tag] = model_dataset_rows(conn, tag)
    return out


def all_models_prompt_rows(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for tag in models_with_data(conn):
        out[tag] = model_prompt_rows(conn, tag)
    return out


# --------------------------------------------------------------------------
# Qt worker threads
# --------------------------------------------------------------------------

class PullWorker(QThread):
    """Downloads a model through Ollama, streaming progress."""

    # ``completed``/``total`` are raw byte counts from Ollama's pull
    # progress stream - a model like Mistral-7B reports totals in the
    # billions of bytes, which overflows a Qt-typed ``int`` signal
    # (C++ int32, ~2.1B max) and crashes the pull with an OverflowError.
    # Using ``float`` here has no such limit and loses no precision at
    # these magnitudes.
    progress = Signal(str, float, float)   # status text, completed_bytes, total_bytes
    finished_ok = Signal(bool, str, bool)  # success, message, not_found

    def __init__(self, model_tag: str, parent=None) -> None:
        super().__init__(parent)
        self.model_tag = model_tag
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        def on_progress(p) -> None:
            if self._cancel.is_set():
                raise ollama.PullCancelled()
            self.progress.emit(p.status, float(p.completed or 0), float(p.total or 0))

        try:
            # ``cancel_event`` lets ollama.pull() notice a cancel even while
            # the connection is stalled and no progress line has arrived to
            # trigger on_progress() above - otherwise Stop could appear to
            # do nothing for up to the full stall-timeout window.
            ollama.pull(self.model_tag, progress_callback=on_progress, cancel_event=self._cancel)
            self.finished_ok.emit(True, "", False)
        except ollama.PullCancelled:
            self.finished_ok.emit(False, "Cancelled", False)
        except ollama.ModelNotFoundError as exc:
            # Distinguished from the generic OllamaError branch below so the
            # GUI can show a clean "that model doesn't exist" message
            # instead of surfacing the registry's raw error text.
            self.finished_ok.emit(False, str(exc), True)
        except ollama.OllamaError as exc:
            self.finished_ok.emit(False, str(exc), False)


class OllamaInstallWorker(QThread):
    """Runs ``ollama_installer.ensure_ollama`` (detect -> install -> start)
    on a background thread so the GUI never blocks/freezes on what can be a
    multi-minute download+install. Progress is re-emitted as a Qt signal;
    any :class:`ollama_installer.InstallerError` is turned into a plain
    message string for the caller to show in a blocking error dialog.

    On Linux, the official install script needs sudo - but it runs
    headless with no terminal attached, so it can't prompt for a password
    itself. When (and only when) that's actually necessary, this worker
    emits ``password_needed`` and blocks until the GUI thread calls
    :meth:`supply_password` with what the user typed (or ``None`` if they
    cancelled), so the user is asked right there in the app instead of
    having to go type it in a terminal.
    """

    progress = Signal(str, str, float)   # stage, message, percent (-1 = indeterminate)
    finished_ok = Signal(bool, str)  # ok, error_message ("" on success)
    password_needed = Signal()  # ask the GUI thread to prompt for a sudo password

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pw_event = threading.Event()
        self._pw_value: Optional[str] = None

    def supply_password(self, password: Optional[str]) -> None:
        """Called from the GUI thread once the user has answered the sudo
        password prompt (or cancelled it, with ``password=None``)."""
        self._pw_value = password
        self._pw_event.set()

    def _request_password(self) -> Optional[str]:
        # Runs on this worker's own thread; blocks until the GUI thread
        # (which receives ``password_needed`` via a queued connection)
        # answers via ``supply_password``.
        self._pw_event.clear()
        self.password_needed.emit()
        self._pw_event.wait()
        return self._pw_value

    def run(self) -> None:
        from prism_core import ollama_installer

        def on_progress(p) -> None:
            self.progress.emit(p.stage, p.message, float(p.percent) if p.percent is not None else -1.0)

        try:
            ollama_installer.ensure_ollama(cb=on_progress, sudo_password_provider=self._request_password)
            self.finished_ok.emit(True, "")
        except ollama_installer.InstallerError as exc:
            self.finished_ok.emit(False, str(exc))
        except Exception as exc:  # noqa: BLE001 - never let an install crash the GUI thread
            self.finished_ok.emit(False, f"Unexpected error installing Ollama: {exc}")


class OllamaCheckWorker(QThread):
    """Background version of the startup screen's Ollama reachability check.

    Does the same blocking work ``StartupScreen._check_ollama`` used to do
    inline: ``ollama_available()`` (HTTP, up to a 5s timeout) and, if
    reachable, ``ollama_ensure_no_cloud_env()`` - which can kill and restart
    the Ollama process - followed by a re-check. Left on the GUI thread,
    that combination can freeze the window for several seconds right at
    launch and on every "Refresh" click. Runs on a QThread instead and
    reports only the final boolean; all the status-text/button UI logic
    stays in the screen itself, driven off this result.
    """

    checked = Signal(bool)

    def run(self) -> None:
        try:
            reachable = ollama_available()
            if reachable and ollama_ensure_no_cloud_env():
                reachable = ollama_available()
        except Exception:
            reachable = False
        self.checked.emit(reachable)


class OllamaStatusWorker(QThread):
    """Checks Ollama reachability off the GUI thread.

    ``ollama_available()`` does a blocking HTTP request (up to a 5s timeout
    when Ollama is unreachable). Running that on a QTimer tick on the main
    thread freezes the whole UI for up to 5s every poll whenever Ollama is
    offline or slow to respond - noticeable stutter, worse on low-RAM
    machines where Ollama itself is slower to answer. This worker does the
    same check on a background thread and reports the result via signal.
    """

    status_checked = Signal(bool)

    def run(self) -> None:
        try:
            available = ollama_available()
        except Exception:
            available = False
        self.status_checked.emit(available)


class SyncWorker(QThread):
    """Pushes local run data to Supabase on a worker thread.

    mode="all"       -> sync_all_completed (Settings screen "Sync now")
    mode="single"    -> sync just `benchmark_run_id` (auto-sync after a run)
    """

    finished_ok = Signal(bool, str)  # ok, message

    def __init__(self, mode: str = "all", benchmark_run_id: Optional[str] = None, parent=None) -> None:
        super().__init__(parent)
        self.mode = mode
        self.benchmark_run_id = benchmark_run_id

    def run(self) -> None:
        conn = store.connect(DB_PATH)
        try:
            if self.mode == "single" and self.benchmark_run_id:
                result = supabase_sync.sync_run(conn, self.benchmark_run_id)
            else:
                result = supabase_sync.sync_all_completed(conn)
            self.finished_ok.emit(result.ok, result.message)
        except Exception as exc:  # noqa: BLE001
            self.finished_ok.emit(False, str(exc))
        finally:
            conn.close()


def pending_sync_count() -> int:
    """Number of completed local runs still sitting in the sync outbox
    (never synced, or last attempt failed) - cheap enough to call from the
    GUI thread on demand, e.g. to badge Settings or the status bar."""
    conn = store.connect(DB_PATH)
    try:
        return supabase_sync.count_pending_runs(conn)
    finally:
        conn.close()


class BenchmarkWorker(QThread):
    """Runs prism_core.benchmark.run_benchmark for one model on a worker thread."""

    stage = Signal(str, str, object, int, int)      # stage, message, dataset, index, count
    request_progress = Signal(str, str, int, int, float, float, object)
    # dataset, status, request_number, total, percent, avg_latency_seconds, eta_seconds
    finished_ok = Signal(bool, str, object)         # success, message, BenchmarkResult|None
    fatal_halt = Signal(str)                        # halt_reason - inference paused, app kept alive

    def __init__(
        self,
        model_tag: str,
        datasets: list[str],
        max_questions: Optional[int] = None,
        overwrite: bool = False,
        benchmark_run_id: Optional[str] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.model_tag = model_tag
        self.datasets = datasets
        self.max_questions = max_questions
        self.overwrite = overwrite
        self.benchmark_run_id = benchmark_run_id
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        # sqlite3 connections can't cross threads - open our own here.
        conn = store.connect(DB_PATH)

        def on_stage(s: BenchmarkStage) -> None:
            self.stage.emit(s.stage, s.message, s.dataset, s.dataset_index, s.dataset_count)

        def on_progress(p) -> None:
            self.request_progress.emit(
                p.dataset, p.status, p.request_number, p.total,
                p.percent, p.avg_latency_seconds or 0.0, p.eta_seconds,
            )

        def on_fatal(reason: str) -> None:
            # Called from inside the engine the instant a fatal condition
            # (OOM, lost Ollama connection, a run of consecutive failures) is
            # detected. Everything gathered so far is already saved to disk
            # by the engine before this fires. This only notifies the GUI - # it never raises, so it can never crash this thread or the app.
            self.fatal_halt.emit(reason)

        try:
            result = core_benchmark.run_benchmark(
                model_name=self.model_tag,
                datasets=self.datasets,
                runs_root=RUNS_ROOT,
                conn=conn,
                benchmark_run_id=self.benchmark_run_id,
                max_questions=self.max_questions,
                overwrite=self.overwrite,
                stage_callback=on_stage,
                progress_callback=on_progress,
                cancel_event=self.cancel_event,
                fatal_callback=on_fatal,
            )
            if result.cancelled:
                self.finished_ok.emit(False, "Cancelled.", result)
            elif result.halted:
                self.finished_ok.emit(False, result.halt_reason or "Inference halted.", result)
            else:
                self.finished_ok.emit(True, "", result)
        except Exception as exc:  # noqa: BLE001 - surface any engine error to the GUI, never crash it
            self.finished_ok.emit(False, str(exc), None)
        finally:
            conn.close()