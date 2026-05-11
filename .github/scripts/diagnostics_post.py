#!/usr/bin/env python3
"""Read the diagnostics JSON saved by the workflow, evaluate anomalies,
and open OR update a single tracker issue on GitHub. Idempotent —
the same issue is updated each run so the operator's notifications
aren't spammed."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

TRACKER_TITLE = '[bot-diagnostics] Anomaly tracker'
TRACKER_LABEL = 'bot-diagnostics'
RESPONSE_PATH = Path('response.json')


def run_gh(args: list[str], check: bool = True) -> str:
    """Wrap `gh` calls so we get useful errors."""
    result = subprocess.run(['gh', *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f'gh {" ".join(args)} failed: {result.stderr}', file=sys.stderr)
        sys.exit(1)
    return result.stdout


def ensure_label(repo: str) -> bool:
    """Create the tracker label if it doesn't exist. Returns True on success
    so the caller knows whether to pass --label to `issue create`. Failures
    are non-fatal — we'd rather file an unlabeled tracker than no tracker."""
    # Check by name. `gh api repos/X/labels/<name>` returns 200 if exists.
    result = subprocess.run(
        ['gh', 'api', f'repos/{repo}/labels/{TRACKER_LABEL}'],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True
    # Create. POST /repos/{owner}/{repo}/labels with name + color.
    create = subprocess.run(
        ['gh', 'api', '--method', 'POST', f'repos/{repo}/labels',
         '-f', f'name={TRACKER_LABEL}',
         '-f', 'color=d73a4a',
         '-f', 'description=Auto-filed by the diagnostics cron when anomalies fire.'],
        capture_output=True, text=True,
    )
    if create.returncode == 0:
        return True
    print(f'Label create failed (will file unlabeled): {create.stderr.strip()}', file=sys.stderr)
    return False


def find_open_tracker(repo: str) -> int | None:
    """Locate the tracker issue by exact title match. We search the whole
    open-issue list rather than filtering by label so it still works when
    the label couldn't be created (e.g., insufficient permissions on the
    repo's default GITHUB_TOKEN)."""
    out = run_gh([
        'issue', 'list', '--repo', repo,
        '--state', 'open', '--limit', '100', '--json', 'number,title',
    ])
    try:
        rows = json.loads(out or '[]')
    except json.JSONDecodeError:
        return None
    for row in rows:
        if row.get('title') == TRACKER_TITLE:
            return int(row['number'])
    return None


def short_anomaly_lines(anomalies: list[dict]) -> list[str]:
    out: list[str] = []
    sev_order = {'critical': 0, 'warn': 1, 'info': 2}
    for a in sorted(anomalies, key=lambda x: sev_order.get(x.get('severity', 'info'), 9)):
        out.append(f'- **[{a.get("severity", "info").upper()}] {a.get("rule", "?")}** — {a.get("detail", "")}')
    return out


def render_body(payload: dict, status: int) -> str:
    ts = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
    lines: list[str] = [
        f'_Last scan: **{ts}** • diagnostics endpoint returned HTTP **{status}**_',
        '',
        '## Anomalies',
        '',
    ]
    anomalies = payload.get('anomalies') or []
    if not anomalies:
        lines.append('_No anomalies in this scan — closing the tracker._')
    else:
        lines.extend(short_anomaly_lines(anomalies))
    lines += ['', '## Cycle health', '']
    ch = payload.get('cycle_health') or {}
    lines.append(f'- last event: `{ch.get("last_event_ts")}` ({ch.get("seconds_since_last_event", "?")}s ago)')
    lines.append(f'- errors in window: `{ch.get("error_count", "?")}` · warns: `{ch.get("warn_count", "?")}`')
    lines.append(f'- last event msg: `{(ch.get("last_event_msg") or "")[:200]}`')
    lines += ['', '## Positions', '']
    pos = payload.get('positions') or {}
    lines.append(f'- by status: `{pos.get("by_status", {})}`')
    if pos.get('naked'):
        lines.append('- naked spot positions:')
        for n in pos['naked']:
            lines.append(f'  - {n.get("symbol")} qty {n.get("quantity"):.6f} ~${n.get("notional_est"):.2f}, age {n.get("age_minutes"):.1f}m')
    lines += ['', '## Rejections (grouped)', '']
    lines.append(f'- total in window: `{payload.get("rejections_total", "?")}`')
    rej = payload.get('rejections_grouped') or {}
    for key, cats in rej.items():
        cats_str = ', '.join(f'{c}={n}' for c, n in sorted(cats.items(), key=lambda x: -x[1])[:8])
        lines.append(f'- `{key}`: {cats_str}')
    lines += ['', '## Recent events (WARN/ERROR)', '', '```']
    for e in (payload.get('recent_events') or [])[:30]:
        lines.append(f'{e.get("ts", "")} [{e.get("level", "")}/{e.get("exchange", "")}] {e.get("msg", "")[:240]}')
    lines.append('```')
    lines += ['', '## Full payload', '', '<details><summary>Click to expand</summary>', '', '```json',
              json.dumps(payload, indent=2, default=str)[:60000], '```', '', '</details>']
    return '\n'.join(lines)


def main() -> int:
    repo = os.environ['REPO']
    try:
        status = int(os.environ.get('STATUS') or '0')
    except ValueError:
        status = 0
    if not RESPONSE_PATH.exists():
        print('No response.json — workflow fetch failed.', file=sys.stderr)
        payload: dict = {'anomalies': [{'severity': 'critical', 'rule': 'workflow_fetch_failed', 'detail': 'No response from /api/diagnostics'}]}
    elif status >= 400:
        # The endpoint returned non-2xx. Treat as critical anomaly.
        try:
            raw = RESPONSE_PATH.read_text()[:1000]
        except Exception:
            raw = '(unreadable)'
        payload = {
            'anomalies': [{
                'severity': 'critical',
                'rule': 'endpoint_returned_error',
                'detail': f'/api/diagnostics returned HTTP {status}: {raw[:400]}',
            }],
        }
    else:
        try:
            payload = json.loads(RESPONSE_PATH.read_text())
        except json.JSONDecodeError as e:
            payload = {'anomalies': [{'severity': 'critical', 'rule': 'invalid_json', 'detail': str(e)[:200]}]}

    has_anomalies = bool(payload.get('anomalies'))
    label_ok = ensure_label(repo)
    existing = find_open_tracker(repo)
    body = render_body(payload, status)

    if has_anomalies:
        if existing:
            # Update body + add a fresh comment so each cron run is timestamped.
            run_gh(['issue', 'edit', str(existing), '--repo', repo, '--body', body])
            comment_body = f'Diagnostics cron @ {datetime.utcnow().isoformat(timespec="seconds")}Z — see issue body for current state.'
            run_gh(['issue', 'comment', str(existing), '--repo', repo, '--body', comment_body])
            print(f'Updated tracker issue #{existing}')
        else:
            create_args = ['issue', 'create', '--repo', repo, '--title', TRACKER_TITLE, '--body', body]
            if label_ok:
                create_args += ['--label', TRACKER_LABEL]
            out = run_gh(create_args)
            print(f'Opened tracker issue: {out.strip()}')
    else:
        # No anomalies. Close the tracker if open; otherwise nothing to do.
        if existing:
            run_gh(['issue', 'edit', str(existing), '--repo', repo, '--body', body])
            run_gh(['issue', 'close', str(existing), '--repo', repo, '--comment', f'All clear at {datetime.utcnow().isoformat(timespec="seconds")}Z.'])
            print(f'Closed tracker issue #{existing} (all clear)')
        else:
            print('No anomalies and no open tracker — nothing to do.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
