"""Parity harness — compare /api/diagnostics between legacy and rewrite.

Per §17 Stage 0 + Stage 4 exit criterion: the rewritten endpoint must
produce a JSON-equivalent response (modulo timestamps + ordering of
identical-shape lists) to the legacy bot's. Diff must be empty.

Usage modes:

  1. Two-URL mode (both versions deployed):
       python scripts/diagnostics_parity.py \\
           --legacy http://prod-v1.3/api/diagnostics?token=$TOKEN \\
           --new    http://staging-v1.5/api/diagnostics?token=$TOKEN

  2. In-process mode (only the legacy is deployed; spin up v1.5 locally
     against a SNAPSHOT of the legacy's SQLite DB):
       python scripts/diagnostics_parity.py \\
           --legacy http://prod-v1.3/api/diagnostics?token=$TOKEN \\
           --in-process --db-snapshot ./prod_bot.db

The in-process mode imports `web.app` with `BOT_WORKER_ENABLED=0` so the
new code serves the diagnostics endpoint without trading. The snapshot
file isn't mutated — the harness opens a read-only copy. Safe to run
against the production DB without disturbing anything.

Exit 0 = parity OK. Exit 1 = drift (structural diff printed). Exit 2 =
fetch error.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from typing import Any
from urllib.request import Request, urlopen


def fetch(url: str) -> dict[str, Any]:
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_in_process(db_snapshot: str | None, token: str, hours: int) -> dict[str, Any]:
    """Boot v1.5 in-process, point it at a read-only copy of the snapshot
    DB, fetch its diagnostics shape. No bot loop, no orders, no mutations.
    """
    if db_snapshot:
        if not os.path.isfile(db_snapshot):
            raise FileNotFoundError(f"snapshot DB not found: {db_snapshot}")
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        shutil.copy2(db_snapshot, tmp.name)
        os.environ["DATABASE_URL"] = f"sqlite:///{tmp.name}"
    os.environ.setdefault("DIAGNOSTICS_TOKEN", token)
    os.environ["BOT_WORKER_ENABLED"] = "0"
    # Reload state.db so it picks up the new DATABASE_URL.
    import importlib
    import state.db as db_mod
    importlib.reload(db_mod)
    import diagnostics.endpoint as ep
    importlib.reload(ep)
    from fastapi.testclient import TestClient
    from web.app import create_app
    with TestClient(create_app()) as client:
        r = client.get(f"/api/diagnostics?token={token}&hours={hours}")
        r.raise_for_status()
        return r.json()


TS_KEYS = {"generated_at_utc", "last_event_ts", "ts", "seconds_since_last_event"}


def normalize(node: Any) -> Any:
    """Recursively normalize a node for diffing:
      - drop wall-clock timestamps (they will always differ)
      - sort lists of dicts by a stable key when present
      - keep numeric values as-is; equality compares exactly
    """
    if isinstance(node, dict):
        return {
            k: normalize(v)
            for k, v in node.items()
            if k not in TS_KEYS
        }
    if isinstance(node, list):
        return [normalize(item) for item in node]
    return node


def diff_keys(a: Any, b: Any, path: str = "") -> list[str]:
    """Return a list of human-readable structural differences."""
    out: list[str] = []
    if type(a) is not type(b):
        out.append(f"{path}: type {type(a).__name__} vs {type(b).__name__}")
        return out
    if isinstance(a, dict):
        ka, kb = set(a.keys()), set(b.keys())
        for k in sorted(ka - kb):
            out.append(f"{path}.{k}: missing in NEW")
        for k in sorted(kb - ka):
            out.append(f"{path}.{k}: missing in LEGACY")
        for k in sorted(ka & kb):
            out.extend(diff_keys(a[k], b[k], f"{path}.{k}"))
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} vs {len(b)}")
        for i, (ai, bi) in enumerate(zip(a, b)):
            out.extend(diff_keys(ai, bi, f"{path}[{i}]"))
    else:
        if a != b:
            out.append(f"{path}: {a!r} vs {b!r}")
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--legacy", required=True, help="URL of legacy /api/diagnostics")
    p.add_argument("--new", help="URL of new /api/diagnostics (omit if --in-process)")
    p.add_argument(
        "--in-process",
        action="store_true",
        help="Spin up v1.5 in-process instead of fetching --new",
    )
    p.add_argument(
        "--db-snapshot",
        help="Path to a SQLite snapshot for in-process mode (read-only copy is used)",
    )
    p.add_argument("--token", default=os.environ.get("DIAGNOSTICS_TOKEN", ""))
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--output", help="Save the structural diff to this JSON file")
    args = p.parse_args()

    if not args.in_process and not args.new:
        print("error: either --new or --in-process is required", file=sys.stderr)
        return 2

    try:
        legacy = fetch(args.legacy)
        if args.in_process:
            new = fetch_in_process(
                db_snapshot=args.db_snapshot, token=args.token, hours=args.hours
            )
        else:
            new = fetch(args.new)
    except Exception as exc:  # noqa: BLE001
        print(f"fetch_error: {exc}", file=sys.stderr)
        return 2

    a = normalize(legacy)
    b = normalize(new)
    deltas = diff_keys(a, b)

    if args.output:
        with open(args.output, "w") as f:
            json.dump({"legacy_url": args.legacy, "diff_count": len(deltas), "diffs": deltas}, f, indent=2)

    if not deltas:
        print("parity: OK")
        return 0
    print(f"parity: DRIFT ({len(deltas)} differences)")
    for d in deltas[:80]:
        print(f"  {d}")
    if len(deltas) > 80:
        print(f"  ... +{len(deltas) - 80} more")
    return 1


if __name__ == "__main__":
    sys.exit(main())
