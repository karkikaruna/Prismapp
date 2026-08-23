"""
Supabase cloud sync.

PRISM's source of truth is always the local SQLite index (prism_core.store);
this module is a one-way, best-effort *mirror* of that index into a single,
hardcoded Supabase project (see prism_core.config.SUPABASE_URL /
SUPABASE_ANON_KEY) over its auto-generated PostgREST API.

There is no per-user configuration screen: every install of the app syncs
into the same project, which then feeds the single GitHub repo via
.github/workflows/sync-results.yml + scripts/fetch_supabase_results.py.

Design:
  - Uses the *anon* key only, sent as both apikey and Bearer token. This key
    is safe to ship hardcoded in a desktop app as long as Row Level Security
    policies on the `runs` / `run_results` tables restrict the anon role to
    INSERT/UPSERT (see supabase/schema.sql) - it must never be able to read
    other devices' data or perform destructive operations.
  - Upserts (`Prefer: resolution=merge-duplicates`) keyed on the same primary
    keys as the local schema, so re-running sync is idempotent and re-runs
    of a benchmark just overwrite the row.
  - Never raises into the GUI thread's normal flow - callers get a
    SyncResult with .ok / .message and decide what to show the user.
  - If SUPABASE_URL/SUPABASE_ANON_KEY are still the placeholder values in
    config.py, sync is a no-op everywhere (is_configured() is False) - the
    app works fully offline either way.
  - Transport: shells out to the system `curl` binary rather than using the
    `requests`/`urllib3` HTTP stack. On this project's Cloudflare-fronted
    Supabase host, Python's built-in `ssl` module's TLS ClientHello gets
    silently black-holed at the TLS-handshake stage (the connection never
    completes - no rejection, no response, just a hang until the client
    timeout fires), while curl's handshake against the identical host/port
    completes in milliseconds. This is TLS fingerprinting on Cloudflare's
    side, not anything specific to the request content, so no combination
    of headers or `requests`/`urllib3` settings fixes it from within pure
    Python - only a different TLS client implementation does. curl ships
    with every desktop OS this app targets (Linux, macOS, and Windows 10+),
    so shelling out is more portable than adding a compiled TLS-impersonation
    dependency (e.g. curl_cffi) for this alone.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from prism_core import config, store

REQUEST_TIMEOUT = 15

logger = logging.getLogger(__name__)


class _CurlError(Exception):
    """Raised for curl-launch/transport failures (never for HTTP error
    status codes, which callers handle via ``_CurlResponse.status_code``)."""


@dataclass
class _CurlResponse:
    status_code: int
    text: str

    def json(self) -> Any:
        return json.loads(self.text)


def _curl_available() -> bool:
    return shutil.which("curl") is not None


def _curl_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
    json_body: Any = None,
    timeout: int = REQUEST_TIMEOUT,
) -> _CurlResponse:
    """Minimal requests.Response-alike backed by a `curl` subprocess.

    Writes the HTTP status code after a literal separator so it can be
    split from the response body unambiguously (curl's own `-w` output is
    appended to stdout right after the body with no delimiter otherwise).
    """
    if not _curl_available():
        raise _CurlError("curl was not found on PATH - it's required for Supabase sync.")

    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"

    cmd = [
        "curl", "-sS", "--max-time", str(timeout),
        "-X", method,
        "-w", "\n__PRISM_STATUS__%{http_code}",
    ]
    for key, value in headers.items():
        cmd += ["-H", f"{key}: {value}"]
    if json_body is not None:
        cmd += ["--data-binary", json.dumps(json_body)]
    cmd.append(url)

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired as exc:
        raise _CurlError(f"curl timed out: {exc}") from exc
    except OSError as exc:
        raise _CurlError(f"could not launch curl: {exc}") from exc

    if proc.returncode != 0:
        raise _CurlError(f"curl exited {proc.returncode}: {proc.stderr.strip()[:300]}")

    marker = "\n__PRISM_STATUS__"
    idx = proc.stdout.rfind(marker)
    if idx == -1:
        raise _CurlError(f"unexpected curl output (no status marker): {proc.stdout[:200]}")
    body = proc.stdout[:idx]
    status_str = proc.stdout[idx + len(marker):].strip()
    try:
        status_code = int(status_str)
    except ValueError:
        raise _CurlError(f"unexpected curl status output: {status_str!r}") from None

    return _CurlResponse(status_code=status_code, text=body)


@dataclass
class SyncResult:
    ok: bool
    message: str
    runs_synced: int = 0
    results_synced: int = 0
    errors: list[str] = field(default_factory=list)


def is_configured() -> bool:
    url, key = config.SUPABASE_URL, config.SUPABASE_ANON_KEY
    return bool(url and key and not url.startswith("https://YOUR-") and not key.startswith("YOUR-"))


def _headers(*, upsert: bool = False) -> dict[str, str]:
    headers = {
        "apikey": config.SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
        # Some Cloudflare-fronted projects (Supabase's REST host included)
        # silently drop/stall requests carrying the default
        # "python-requests/x.y.z" User-Agent instead of returning a clean
        # rejection - the connection is accepted but no response ever comes
        # back, which surfaces here as a `requests` read timeout even
        # though the identical request from curl (a normal-looking UA)
        # succeeds instantly. Sending a conventional UA avoids that.
        "User-Agent": "PRISM-desktop/1.0 (+https://prism-project.example.com)",
    }
    if upsert:
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    return headers


def test_connection() -> SyncResult:
    """Cheap round-trip: select against `runs` with limit 1 row."""
    if not is_configured():
        return SyncResult(False, "Supabase isn't configured (see prism_core/config.py).")
    try:
        resp = _curl_request(
            "GET",
            f"{config.SUPABASE_URL}/rest/v1/runs",
            headers=_headers(),
            params={"select": "benchmark_run_id", "limit": "1"},
        )
    except _CurlError as exc:
        return SyncResult(False, f"Could not reach Supabase: {exc}")
    if resp.status_code == 200:
        return SyncResult(True, "Connected.")
    if resp.status_code in (401, 403):
        return SyncResult(False, "Rejected - check the anon key and RLS policies.")
    if resp.status_code == 404:
        return SyncResult(False, "Reached Supabase, but the `runs` table doesn't exist yet "
                                  " - run supabase/schema.sql in the SQL editor first.")
    return SyncResult(False, f"Unexpected response: {resp.status_code} {resp.text[:200]}")


def _row_to_run_payload(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    # These are local-only outbox bookkeeping columns (see
    # prism_core.store._migrate_add_sync_outbox_columns) - they exist on
    # the SQLite `runs` row so this device can track its own sync state,
    # but the remote Supabase `runs` table never had them added and
    # doesn't expect them. Forwarding them makes PostgREST reject the
    # whole request with a 400 (unrecognized column), which silently
    # broke every sync until removed here.
    for local_only_col in ("sync_status", "sync_attempts", "last_sync_error"):
        d.pop(local_only_col, None)
    # `synced_at` locally tracks *this device's* last successful sync and is
    # NULL until then; the remote column is `not null default now()` and is
    # meant to be stamped by Supabase itself on receipt, not driven by the
    # local (possibly still-NULL) value - sending an explicit null would
    # override the default and violate the not-null constraint.
    d.pop("synced_at", None)
    # `datasets` is stored as a JSON string locally; Supabase column is jsonb,
    # PostgREST accepts either a JSON-encoded string or a native object here - # decode it so it lands as real jsonb rather than a quoted string.
    try:
        d["datasets"] = json.loads(d["datasets"])
    except (TypeError, ValueError, KeyError):
        pass
    d["device_id"] = _device_id()
    return d


def _row_to_result_payload(row: sqlite3.Row, device_id: str) -> dict[str, Any]:
    d = dict(row)
    # `id` is the local SQLite table's own autoincrement primary key - purely
    # a local bookkeeping detail. Supabase's run_results.id is a separate
    # `GENERATED ALWAYS` identity column managed entirely by Postgres, so
    # sending our local id along (even matching a real remote row) is
    # rejected outright: PostgREST returns 428C9 "cannot insert a
    # non-DEFAULT value into column \"id\"". Drop it and let each side keep
    # its own id; the real dedupe/conflict key for upserts here is
    # (benchmark_run_id, dataset), not id.
    d.pop("id", None)
    # Defensive int coercion: a pre-existing local DB may have gone through
    # a schema migration that (as a bug, since fixed) declared n_questions
    # with REAL affinity, so SQLite could hand this back as e.g. 200.0. The
    # remote column is a genuine integer, and Postgres's int4in() rejects a
    # literal like "200.0" outright (no implicit float->int coercion on
    # text input) - coerce here so already-affected local rows sync cleanly
    # too, without requiring anyone to rebuild their local database.
    if d.get("n_questions") is not None:
        d["n_questions"] = int(float(d["n_questions"]))
    d["device_id"] = device_id
    return d


_device_id_cache: str | None = None


def _device_id() -> str:
    """A stable-ish per-install identifier so multiple people's local PRISM
    installs can sync into the same Supabase project without colliding - purely informational, not a security boundary."""
    global _device_id_cache
    if _device_id_cache is not None:
        return _device_id_cache

    marker = Path.home() / ".prism" / "device_id"
    if marker.exists():
        _device_id_cache = marker.read_text(encoding="utf-8").strip()
    else:
        _device_id_cache = uuid.uuid4().hex
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(_device_id_cache, encoding="utf-8")
    return _device_id_cache


# ---------------------------------------------------------------------------
# Offline outbox: local sync-status bookkeeping on the `runs` row
# ---------------------------------------------------------------------------
# Every run's `sync_status` lives on the local SQLite `runs` row itself
# (see prism_core.store._migrate_add_sync_outbox_columns), so the local
# index doubles as a persistent, offline-tolerant outbox: a run is queued
# the moment it's marked complete, and stays queued (surviving app
# restarts, crashes, or no network at all) until a sync attempt actually
# succeeds. This also lets the Settings screen and any background sweep
# show exactly why a given run hasn't synced yet, instead of only a
# transient toast right after a run finishes.

def _is_exempt_app_version(app_version: str | None) -> bool:
    v = app_version or ""
    return v.startswith("bundled") or v.startswith("public")


def _mark_sync_status(
    conn: sqlite3.Connection,
    benchmark_run_id: str,
    *,
    status: str,
    error: str | None = None,
    bump_attempts: bool = False,
) -> None:
    if status == "synced":
        conn.execute(
            "UPDATE runs SET sync_status = ?, synced_at = datetime('now'), "
            "last_sync_error = NULL WHERE benchmark_run_id = ?",
            (status, benchmark_run_id),
        )
    elif bump_attempts:
        conn.execute(
            "UPDATE runs SET sync_status = ?, "
            "sync_attempts = COALESCE(sync_attempts, 0) + 1, "
            "last_sync_error = ? WHERE benchmark_run_id = ?",
            (status, error, benchmark_run_id),
        )
    else:
        conn.execute(
            "UPDATE runs SET sync_status = ?, last_sync_error = ? "
            "WHERE benchmark_run_id = ?",
            (status, error, benchmark_run_id),
        )
    conn.commit()


def _incomplete_datasets(conn: sqlite3.Connection, benchmark_run_id: str) -> list[str]:
    """Datasets in this run that fell short of the full sample size, e.g.
    ``['sciq (120/200)']``. A sync is refused (marked ``exempt`` with the
    reason recorded) rather than silently uploading a partial run."""
    required = getattr(config, "SAMPLE_SIZE_PER_DATASET", 200)
    incomplete = []
    for r in store.get_run_results(conn, benchmark_run_id):
        n = r["n_questions"] or 0
        if n < required:
            incomplete.append(f"{r['dataset']} ({n}/{required})")
    return incomplete


def count_pending_runs(conn: sqlite3.Connection) -> int:
    """Number of completed, non-exempt runs still waiting to reach
    Supabase - used to badge the Settings screen / status bar."""
    cursor = conn.execute(
        """
        SELECT COUNT(*)
        FROM runs r
        WHERE r.status = 'completed'
          AND COALESCE(r.sync_status, 'pending') IN ('pending', 'failed')
          AND (r.app_version NOT LIKE 'bundled%' AND r.app_version NOT LIKE 'public%'
               OR r.app_version IS NULL)
        """
    )
    row = cursor.fetchone()
    return row[0] if row else 0


def sync_run(conn: sqlite3.Connection, benchmark_run_id: str) -> SyncResult:
    """Push one completed run + its per-dataset result rows to Supabase,
    recording the outcome back onto the local `runs` row's sync-status
    columns so it behaves as a durable outbox rather than fire-and-forget."""
    run = store.get_run(conn, benchmark_run_id)
    if run is None:
        return SyncResult(False, f"No local run {benchmark_run_id!r} to sync.")
    if run["status"] != "completed":
        return SyncResult(False, "Only completed runs are synced.")

    # Seed/public bundled results ship with the app and were never generated
    # by this device - never push them to the shared project.
    if _is_exempt_app_version(run["app_version"]):
        _mark_sync_status(conn, benchmark_run_id, status="exempt")
        return SyncResult(True, "Skipped seed/public run.", 0, 0)

    # Only a genuinely complete run (every dataset ran the full sample) is
    # eligible - a partial run synced now would just need overwriting later,
    # and could misrepresent a device's results in the shared project.
    incomplete = _incomplete_datasets(conn, benchmark_run_id)
    if incomplete:
        msg = f"Partial run ({', '.join(incomplete)}); needs the full sample to sync."
        _mark_sync_status(conn, benchmark_run_id, status="exempt", error=msg)
        return SyncResult(True, f"Skipped partial run: {msg}", 0, 0)

    if not is_configured():
        return SyncResult(False, "Supabase isn't configured.")

    device_id = _device_id()
    errors: list[str] = []

    try:
        resp = _curl_request(
            "POST",
            f"{config.SUPABASE_URL}/rest/v1/runs",
            headers=_headers(upsert=True),
            params={"on_conflict": "benchmark_run_id"},
            json_body=_row_to_run_payload(run),
        )
        if resp.status_code not in (200, 201, 204):
            # A 401/403 whose body names row-level security specifically
            # means the *policy* refused the write, not a transient/network
            # problem - most commonly because this run_id was already
            # approved remotely (approved=true), and the "only touch
            # pending, unapproved rows" UPDATE policy correctly won't let
            # anon re-touch it. That will never succeed on retry, so treat
            # it as a terminal outcome (exempt) instead of leaving it
            # 'failed' - otherwise the outbox retries it every cycle
            # forever, forever re-logging the same rejection.
            if resp.status_code in (401, 403) and "row-level security" in resp.text.lower():
                msg = "Blocked by RLS - likely already approved remotely; won't retry."
                _mark_sync_status(conn, benchmark_run_id, status="exempt", error=msg)
                return SyncResult(True, msg, 0, 0)
            errors.append(f"runs upsert failed: {resp.status_code} {resp.text[:200]}")
    except _CurlError as exc:
        err = f"Network error syncing run: {exc}"
        _mark_sync_status(conn, benchmark_run_id, status="failed", error=err, bump_attempts=True)
        return SyncResult(False, err)

    if errors:
        err = "; ".join(errors)
        _mark_sync_status(conn, benchmark_run_id, status="failed", error=err, bump_attempts=True)
        return SyncResult(False, err, errors=errors)

    results = store.get_run_results(conn, benchmark_run_id)
    results_synced = 0
    if results:
        payload = [_row_to_result_payload(r, device_id) for r in results]
        try:
            resp = _curl_request(
                "POST",
                f"{config.SUPABASE_URL}/rest/v1/run_results",
                headers=_headers(upsert=True),
                params={"on_conflict": "benchmark_run_id,dataset"},
                json_body=payload,
            )
            if resp.status_code in (200, 201, 204):
                results_synced = len(payload)
            else:
                errors.append(f"run_results upsert failed: {resp.status_code} {resp.text[:200]}")
        except _CurlError as exc:
            errors.append(f"Network error syncing results: {exc}")

    if errors:
        err = "; ".join(errors)
        _mark_sync_status(conn, benchmark_run_id, status="failed", error=err, bump_attempts=True)
        return SyncResult(False, err, runs_synced=0, results_synced=results_synced, errors=errors)

    _mark_sync_status(conn, benchmark_run_id, status="synced")
    return SyncResult(True, "Synced.", runs_synced=1, results_synced=results_synced)


def fetch_custom_models() -> list[dict[str, Any]]:
    """Return every "other model" any device has pulled and saved, newest
    first. Best-effort: on any error (not configured, offline, table missing
    because schema.sql hasn't been re-run yet) this just returns an empty
    list rather than raising, since the startup screen's local catalog
    always works without it.
    """
    if not is_configured():
        return []
    try:
        resp = _curl_request(
            "GET",
            f"{config.SUPABASE_URL}/rest/v1/custom_models",
            headers=_headers(),
            params={"select": "model_tag,label,added_at", "order": "added_at.desc"},
        )
    except _CurlError:
        return []
    if resp.status_code != 200:
        return []
    try:
        return resp.json()
    except ValueError:
        return []


def save_custom_model(model_tag: str, label: str | None = None) -> SyncResult:
    """Upsert one "other model" tag so it's remembered next time the
    startup screen's Manage Models panel opens, on this device or any other
    device pointed at the same Supabase project."""
    if not is_configured():
        return SyncResult(False, "Supabase isn't configured.")
    payload = {
        "model_tag": model_tag,
        "label": label or model_tag,
        "device_id": _device_id(),
    }
    try:
        resp = _curl_request(
            "POST",
            f"{config.SUPABASE_URL}/rest/v1/custom_models",
            headers=_headers(upsert=True),
            params={"on_conflict": "model_tag"},
            json_body=payload,
        )
    except _CurlError as exc:
        return SyncResult(False, f"Network error saving model: {exc}")
    if resp.status_code in (200, 201, 204):
        return SyncResult(True, "Saved.")
    return SyncResult(False, f"custom_models upsert failed: {resp.status_code} {resp.text[:200]}")


def sync_all_completed(conn: sqlite3.Connection) -> SyncResult:
    """Flush every outstanding item in the local outbox: completed runs
    still marked ``pending`` or ``failed`` (a previous attempt errored, e.g.
    while offline, and is worth retrying now). Already-``synced`` and
    ``exempt`` runs are skipped so repeat calls (e.g. the background
    auto-sync timer) stay cheap."""
    if not is_configured():
        return SyncResult(False, "Supabase isn't configured.")

    runs_synced = 0
    results_synced = 0
    errors: list[str] = []
    for row in store.list_runs(conn):
        if row["status"] != "completed":
            continue
        if row["sync_status"] in ("synced", "exempt"):
            continue
        result = sync_run(conn, row["benchmark_run_id"])
        if result.ok:
            runs_synced += result.runs_synced
            results_synced += result.results_synced
        else:
            errors.append(f"{row['benchmark_run_id']}: {result.message}")

    if errors:
        return SyncResult(
            False, f"Synced {runs_synced} run(s), {len(errors)} failed.",
            runs_synced=runs_synced, results_synced=results_synced, errors=errors,
        )
    return SyncResult(
        True, f"Synced {runs_synced} run(s) / {results_synced} result row(s).",
        runs_synced=runs_synced, results_synced=results_synced,
    )


def trigger_background_sync(conn_factory: Callable[[], sqlite3.Connection]) -> None:
    """Spawn a detached daemon thread that opens its own connection (SQLite
    connections aren't safe to share across threads) and flushes the
    outbox. Used for the on-launch catch-up sync and the recurring
    auto-sync timer so neither ever blocks the GUI thread."""
    def _worker() -> None:
        try:
            conn = conn_factory()
            try:
                res = sync_all_completed(conn)
                if res.runs_synced:
                    logger.info("[Outbox] %s", res.message)
            finally:
                conn.close()
        except Exception as exc:  # pragma: no cover - best-effort background task
            logger.debug("[Outbox] background flush check: %s", exc)

    threading.Thread(target=_worker, daemon=True, name="prism-supabase-outbox").start()