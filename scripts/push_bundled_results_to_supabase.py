from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from prism_core import config, seed, store
from app.services import backend


def _post_with_retry(url: str, headers: dict, params: dict, payload, *, retries: int = 2):
    """POST with a longer timeout and one retry - Supabase's REST endpoint
    can be slow to respond on a cold connection, and a plain read-timeout
    on the first attempt shouldn't be treated as a permanent failure the
    way a 4xx/5xx response is. Kept short (2 attempts, 30s each) so a
    genuinely unreachable run fails in under a minute instead of hanging."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return requests.post(
                url, headers=headers, params=params,
                data=json.dumps(payload), timeout=30,
            )
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(2)
    raise last_exc


def main() -> int:
    url = config.SUPABASE_URL
    service_key = config.SUPABASE_SERVICE_ROLE_KEY

    if not url or not service_key:
        print(
            "Missing credentials. Set PRISM_SUPABASE_URL and "
            "PRISM_SUPABASE_SECRET_KEY as environment variables first, e.g.\n\n"
            '  export PRISM_SUPABASE_URL="https://xxxxxxxx.supabase.co"\n'
            '  export PRISM_SUPABASE_SECRET_KEY="sk_..."\n'
            "  python scripts/push_bundled_results_to_supabase.py",
            file=sys.stderr,
        )
        return 1

    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    # Make sure the bundled seed is materialized locally, then push every
    # completed run currently in the index (bundled + any real local runs).
    backend.ensure_bundled_seed()
    conn = backend.get_conn()
    try:
        runs = [r for r in store.list_runs(conn) if r["status"] == "completed"]
        runs_pushed, results_pushed, errors = 0, 0, []

        for run in runs:
            print(f"Pushing {run['benchmark_run_id']} ({run['model']})...", flush=True)
            run_payload = dict(run)
            run_payload["datasets"] = json.loads(run_payload["datasets"])
            # This script publishes the maintainer's own bundled/validated
            # results directly (service_role key bypasses RLS), so mark
            # them pre-approved rather than landing in the same pending
            # queue as anon-key device submissions.
            run_payload["approved"] = True
            run_payload["approved_by"] = "maintainer-seed"
            run_payload["approved_at"] = datetime.now(timezone.utc).isoformat()
            try:
                resp = _post_with_retry(
                    f"{url}/rest/v1/runs", headers,
                    {"on_conflict": "benchmark_run_id"}, run_payload,
                )
                if resp.status_code not in (200, 201, 204):
                    errors.append(f"{run['benchmark_run_id']}: runs {resp.status_code} {resp.text[:150]}")
                    continue
            except requests.RequestException as exc:
                errors.append(f"{run['benchmark_run_id']}: {exc}")
                continue

            results = [dict(r) for r in store.get_run_results(conn, run["benchmark_run_id"])]
            # Local SQLite has its own autoincrement "id" column; Postgres'
            # run_results.id is GENERATED ALWAYS AS IDENTITY and rejects any
            # supplied value (428C9) rather than silently ignoring it - drop
            # it here and let Postgres assign its own id on insert.
            for r in results:
                r.pop("id", None)
            if results:
                try:
                    resp = _post_with_retry(
                        f"{url}/rest/v1/run_results", headers,
                        {"on_conflict": "benchmark_run_id,dataset"}, results,
                    )
                    if resp.status_code in (200, 201, 204):
                        results_pushed += len(results)
                    else:
                        errors.append(f"{run['benchmark_run_id']}: run_results {resp.status_code} {resp.text[:150]}")
                except requests.RequestException as exc:
                    errors.append(f"{run['benchmark_run_id']}: {exc}")
            runs_pushed += 1
    finally:
        conn.close()

    print(f"Pushed {runs_pushed} run(s), {results_pushed} result row(s).")
    if errors:
        print(f"{len(errors)} error(s):", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())