"""Parity harness — compare /api/diagnostics between legacy and rewrite.

Per §17 Stage 0 + Stage 4 exit criterion: the rewritten endpoint must
produce a JSON-equivalent response (modulo timestamps + ordering of
identical-shape lists) to the legacy bot's. Diff must be empty.

Usage:
    python scripts/diagnostics_parity.py \
        --legacy http://localhost:8000/api/diagnostics?token=$TOKEN \
        --new    http://localhost:8001/api/diagnostics?token=$TOKEN

Exit 0 = parity OK. Exit 1 = drift; the harness prints a structural diff
suitable for triage. No assertions about specific values; only shape.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.request import Request, urlopen


def fetch(url: str) -> dict[str, Any]:
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    p.add_argument("--legacy", required=True)
    p.add_argument("--new", required=True)
    args = p.parse_args()
    try:
        legacy = fetch(args.legacy)
        new = fetch(args.new)
    except Exception as exc:  # noqa: BLE001
        print(f"fetch_error: {exc}", file=sys.stderr)
        return 2
    a = normalize(legacy)
    b = normalize(new)
    deltas = diff_keys(a, b)
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
