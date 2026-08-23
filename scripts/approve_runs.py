"""
approve_runs
============

Maintainer-only review/approval step. This is the human gate between the
shared Supabase mirror (every device writes here automatically with the
anon key) and the public repo (which only ever receives approved=true
rows via the public repo's own sync workflow, see the "public repo setup"
section of README.md).

Never used by the desktop app. Reads/writes with the service_role key,
which must only ever exist as a maintainer-side env var or CI secret -
never committed, never shipped in the packaged app.

Usage:

    export PRISM_SUPABASE_URL="https://xxxxxxxx.supabase.co"
    export PRISM_SUPABASE_SECRET_KEY="sk_..."

    # List everything awaiting review
    python scripts/approve_runs.py list

    # Approve one run by id
    python scripts/approve_runs.py approve <benchmark_run_id> --by "yourname"

    # Approve everything currently pending (use with care)
    python scripts/approve_runs.py approve-all --by "yourname"

    # Reject / permanently exclude a run (deletes it from the mirror so it
    # never resurfaces in `list` - the device that submitted it can still
    # re-sync a corrected version later under the same run id)
    python scripts/approve_runs.py reject <benchmark_run_id>
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import requests


def _client():
    url = os.environ.get("PRISM_SUPABASE_URL", "").rstrip("/")
    key = os.environ.get(
        "PRISM_SUPABASE_SECRET_KEY",
        os.environ.get("PRISM_SUPABASE_SERVICE_ROLE_KEY", ""),  # legacy fallback
    )
    if not url or not key:
        print(
            "Missing credentials. Set PRISM_SUPABASE_URL and "
            "PRISM_SUPABASE_SECRET_KEY first.",
            file=sys.stderr,
        )
        sys.exit(1)
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    return url, headers


def cmd_list(_args: argparse.Namespace) -> int:
    url, headers = _client()
    resp = requests.get(
        f"{url}/rest/v1/runs",
        headers=headers,
        params={
            "select": "benchmark_run_id,model,datasets,created_utc,device_id,approved",
            "approved": "eq.false",
            "status": "eq.completed",
            "order": "created_utc.desc",
        },
        timeout=30,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        print("Nothing pending review.")
        return 0
    print(f"{len(rows)} run(s) awaiting approval:\n")
    for r in rows:
        print(f"  {r['benchmark_run_id']}  model={r['model']}  "
              f"datasets={r['datasets']}  created={r['created_utc']}  "
              f"device={r.get('device_id')}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    url, headers = _client()
    payload = {
        "approved": True,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "approved_by": args.by or "unknown",
    }
    resp = requests.patch(
        f"{url}/rest/v1/runs",
        headers={**headers, "Prefer": "return=representation"},
        params={"benchmark_run_id": f"eq.{args.run_id}"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    updated = resp.json()
    if not updated:
        print(f"No run found with id {args.run_id!r}.", file=sys.stderr)
        return 1
    print(f"Approved {args.run_id}.")
    return 0


def cmd_approve_all(args: argparse.Namespace) -> int:
    url, headers = _client()
    payload = {
        "approved": True,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "approved_by": args.by or "unknown",
    }
    resp = requests.patch(
        f"{url}/rest/v1/runs",
        headers={**headers, "Prefer": "return=representation"},
        params={"approved": "eq.false", "status": "eq.completed"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    updated = resp.json()
    print(f"Approved {len(updated)} run(s).")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    url, headers = _client()
    resp = requests.delete(
        f"{url}/rest/v1/runs",
        headers=headers,
        params={"benchmark_run_id": f"eq.{args.run_id}"},
        timeout=30,
    )
    resp.raise_for_status()
    print(f"Removed {args.run_id} from the mirror (run_results cascade-deleted).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)

    p_approve = sub.add_parser("approve")
    p_approve.add_argument("run_id")
    p_approve.add_argument("--by", default=os.environ.get("USER", ""))
    p_approve.set_defaults(func=cmd_approve)

    p_approve_all = sub.add_parser("approve-all")
    p_approve_all.add_argument("--by", default=os.environ.get("USER", ""))
    p_approve_all.set_defaults(func=cmd_approve_all)

    p_reject = sub.add_parser("reject")
    p_reject.add_argument("run_id")
    p_reject.set_defaults(func=cmd_reject)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())