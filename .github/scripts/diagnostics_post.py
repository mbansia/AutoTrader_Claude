#!/usr/bin/env python3
"""Read the diagnostics JSON saved by the workflow, summarise the run,
and post to a single persistent tracker issue so the monitor chat
gets a webhook event EVERY run, anomalies or not.

Design (heartbeat model, post-2026-05-11):
  * One persistent open issue titled `[bot-diagnostics] Tracker`.
  * Created on first run if it doesn't exist.
  * Body is always the latest full state (anomalies + cycle health +
    positions + rejections + recent events + full JSON).
  * Each cron run posts a one-line comment with status — anomalies if
    any, "all clear" otherwise. This is what fires the webhook to
    monitor-chat subscribers.

Why a comment EVERY run (not just on anomalies):
  the original design (open issue on anomalies, close on all-clear)
  meant the monitor chat got nothing on healthy cycles and couldn't
  even confirm the workflow was still running. The heartbeat model
  trades a small amount of comment-noise for unambiguous "the cron
  ran and here's what it found" visibility.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

TRACKER_TITLE = '[bot-diagnostics] Tracker'
TRACKER_LABEL = 'bot-diagnostics'
RESPONSE_PATH = Path('response.json')


def run_gh(args: list[str], check: bool = True) -> str:
    result = subprocess.run(['gh', *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f'gh {" ".join(args)} failed: {result.stderr}', file=sys.stderr)
        sys.exit(1)
    return result.stdout


def ensure_label(repo: str) -> bool:
    result = subprocess.run(
        ['gh', 'api', f'repos/{repo}/labels/{TRACKER_LABEL}'],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True
    create = subprocess.run(
        ['gh', 'api', '--method', 'POST', f'repos/{repo}/labels',
         '-f', f'name={TRACKER_LABEL}',
         '-f', 'color=d73a4a',
         '-f', 'description=Auto-managed by the diagnostics cron heartbeat.'],
        capture_output=True, text=True,
    )
    if create.returncode == 0:
        return True
    print(f'Label create failed (will file unlabeled): {create.stderr.strip()}', file=sys.stderr)
    return False


def find_tracker(repo: str) -> int | None:
    """Locate the tracker issue by title. Heartbeat model keeps it open
    indefinitely, but search both states in case someone manually closed
    it — we'll reopen below."""
    for state in ('open', 'closed'):
        out = run_gh([
            'issue', 'list', '--repo', repo,
            '--state', state, '--limit', '100', '--json', 'number,title,state',
        ])
        try:
            rows = json.loads(out or '[]')
        except json.JSONDecodeError:
            continue
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
    """Full state for the issue body. Updated on every run regardless of
    whether anomalies are present, so the latest state is always one
    click away on the issue."""
    ts = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
    anomalies = payload.get('anomalies') or []
    lines: list[str] = [
        f'_Last scan: **{ts}** • endpoint returned HTTP **{status}**_',
        '',
        '## Status',
        '',
        '✅ **All clear** — no anomalies detected.' if not anomalies else f'⚠️ **{len(anomalies)} anomaly/anomalies** in latest scan:',
        '',
    ]
    if anomalies:
        lines.extend(short_anomaly_lines(anomalies))
        lines.append('')

    lines += ['## Cycle health', '']
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

    lines += ['', '## Recent trades', '']
    lines.append(f'- count in window: `{payload.get("recent_trades_count", 0)}`')

    lines += ['', '## Rejections (grouped)', '']
    lines.append(f'- total in window: `{payload.get("rejections_total", "?")}`')
    rej = payload.get('rejections_grouped') or {}
    for key, cats in rej.items():
        cats_str = ', '.join(f'{c}={n}' for c, n in sorted(cats.items(), key=lambda x: -x[1])[:8])
        lines.append(f'- `{key}`: {cats_str}')

    lines += ['', '## Recent events (WARN/ERROR)', '', '```']
    for e in (payload.get('recent_events') or [])[:10]:
        lines.append(f'{e.get("ts", "")} [{e.get("level", "")}/{e.get("exchange", "")}] {e.get("msg", "")[:240]}')
    lines.append('```')

    lines += [
        '', '## Full payload', '',
        '<details><summary>Click to expand</summary>', '', '```json',
        json.dumps(payload, indent=2, default=str)[:15000],
        '```', '', '</details>',
    ]
    return '\n'.join(lines)


def render_heartbeat_comment(payload: dict, status: int) -> str:
    """One-line-ish status comment posted EVERY run. This is what fires the
    webhook to the monitor chat. Keep it terse — the issue body has the
    full state."""
    ts = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
    anomalies = payload.get('anomalies') or []
    ch = payload.get('cycle_health') or {}
    pos = payload.get('positions') or {}
    rt = payload.get('recent_trades_count', 0)
    rj = payload.get('rejections_total', 0)
    err = ch.get('error_count', '?')
    warn = ch.get('warn_count', '?')
    by_status = pos.get('by_status', {})
    if not anomalies:
        head = '✅ **Heartbeat — all clear**'
    else:
        sev_critical = sum(1 for a in anomalies if a.get('severity') == 'critical')
        sev_warn = sum(1 for a in anomalies if a.get('severity') == 'warn')
        head = f'⚠️ **Heartbeat — {sev_critical} critical, {sev_warn} warn** anomaly/anomalies'
    lines = [
        f'{head} @ {ts} (HTTP {status})',
        f'- positions: `{by_status}` · trades in window: `{rt}` · rejections: `{rj}` · errors/warns: `{err}/{warn}`',
    ]
    if anomalies:
        lines.append('- top:')
        for a in anomalies[:3]:
            lines.append(f'  - **[{a.get("severity", "?").upper()}]** {a.get("rule", "?")} — {a.get("detail", "")[:160]}')
    lines.append('')
    lines.append('_See the issue body above for the full state including the JSON payload._')
    return '\n'.join(lines)


def main() -> int:
    repo = os.environ['REPO']
    try:
        status = int(os.environ.get('STATUS') or '0')
    except ValueError:
        status = 0

    if not RESPONSE_PATH.exists():
        print('No response.json — workflow fetch failed.', file=sys.stderr)
        payload: dict = {
            'anomalies': [{
                'severity': 'critical',
                'rule': 'workflow_fetch_failed',
                'detail': 'No response from /api/diagnostics — endpoint unreachable or workflow step crashed before saving the body.',
            }],
        }
    elif status >= 400:
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
            payload = {
                'anomalies': [{
                    'severity': 'critical',
                    'rule': 'invalid_json',
                    'detail': str(e)[:200],
                }],
            }

    label_ok = ensure_label(repo)
    existing = find_tracker(repo)
    body = render_body(payload, status)
    comment = render_heartbeat_comment(payload, status)

    if existing is None:
        # First run — create the persistent tracker.
        create_args = ['issue', 'create', '--repo', repo,
                       '--title', TRACKER_TITLE, '--body', body]
        if label_ok:
            create_args += ['--label', TRACKER_LABEL]
        out = run_gh(create_args)
        url = out.strip()
        print(f'Opened persistent tracker: {url}')
        # The issue body already carries the first heartbeat header, so
        # no separate comment on the first run.
        return 0

    # Tracker exists. Order of operations matters: the COMMENT is what fires
    # the webhook to the monitor chat, so it must always land — even if the
    # body edit times out (GitHub's GraphQL mutation has 504'd on us when the
    # body grows past ~30KB with full payload + recent_events spam). Reopen
    # first (no-op if already open) so the comment definitely fires on an
    # open issue, then post the comment, then try the body update last.
    run_gh(['issue', 'reopen', str(existing), '--repo', repo], check=False)
    run_gh(['issue', 'comment', str(existing), '--repo', repo, '--body', comment])
    edit_result = subprocess.run(
        ['gh', 'issue', 'edit', str(existing), '--repo', repo, '--body', body],
        capture_output=True, text=True,
    )
    if edit_result.returncode != 0:
        # Comment already posted, so the webhook fired and the monitor chat
        # has the status. The body just wasn't updated to the latest full
        # snapshot — surface the failure but don't crash the workflow.
        print(
            f'Body edit failed (comment was already posted, monitor chat is woken): '
            f'{edit_result.stderr.strip()[:400]}',
            file=sys.stderr,
        )
    print(f'Heartbeat posted on tracker issue #{existing}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
