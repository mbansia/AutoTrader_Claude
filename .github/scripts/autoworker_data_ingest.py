#!/usr/bin/env python3
"""AutoWorker data ingest.

Read `.autoworker/sources.yml`, run each enabled source, aggregate the
results into a single snapshot, and update the persistent tracker
issue:

- Body = full latest snapshot (anomalies + per-source summaries +
  raw payload, collapsed).
- Comment = one-line heartbeat per run (fires the webhook that wakes
  scheduled agent sessions).

Built-in sources:

- `diagnostics_endpoint` — polls an HTTP endpoint, treats the JSON as
  the snapshot. Carries forward the v0 AutoTrader pattern.
- `github_signals`       — open issues (especially `bug` / `regression`
  labels), recently failed CI runs, dependabot alerts.
- `social_x`             — search X mentions of the project handle.
  Opt in: needs `X_BEARER_TOKEN`.
- `social_reddit`        — public search on r/all for the project name.
  No auth needed.

Sources are configured in `.autoworker/sources.yml`:

    enabled:
      - diagnostics_endpoint:
          endpoint: /api/diagnostics
      - github_signals:
          labels: [bug, regression, urgent]
      - social_reddit:
          query: "{{PROJECT_NAME}}"

The agent reads the tracker, not the raw sources, so the ingest is the
narrow contract between data and decision-making.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:
    print('Missing pyyaml — workflow should pip install it.', file=sys.stderr)
    sys.exit(1)

TRACKER_TITLE = '[autoworker] Tracker'
TRACKER_LABEL = 'autoworker-tracker'
CONFIG_PATH = Path('.autoworker/sources.yml')
SOURCES_DIR = Path('.github/scripts/autoworker_sources')


def run_gh(args: list[str], check: bool = True) -> str:
    result = subprocess.run(['gh', *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f'gh {" ".join(args)} failed: {result.stderr}', file=sys.stderr)
        sys.exit(1)
    return result.stdout


def ensure_label(repo: str) -> bool:
    if subprocess.run(
        ['gh', 'api', f'repos/{repo}/labels/{TRACKER_LABEL}'],
        capture_output=True, text=True,
    ).returncode == 0:
        return True
    create = subprocess.run(
        ['gh', 'api', '--method', 'POST', f'repos/{repo}/labels',
         '-f', f'name={TRACKER_LABEL}',
         '-f', 'color=0e8a16',
         '-f', 'description=Auto-managed by AutoWorker data ingest.'],
        capture_output=True, text=True,
    )
    if create.returncode == 0:
        return True
    print(f'Label create failed (will file unlabeled): {create.stderr.strip()}', file=sys.stderr)
    return False


def find_tracker(repo: str) -> int | None:
    for state in ('open', 'closed'):
        out = run_gh([
            'issue', 'list', '--repo', repo,
            '--state', state, '--limit', '100', '--json', 'number,title',
        ])
        try:
            for row in json.loads(out or '[]'):
                if row.get('title') == TRACKER_TITLE:
                    return int(row['number'])
        except json.JSONDecodeError:
            continue
    return None


def load_config() -> list[dict[str, Any]]:
    if not CONFIG_PATH.exists():
        return []
    data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    return data.get('enabled', []) or []


def collect_all(enabled: list[dict[str, Any]]) -> dict[str, Any]:
    """Dispatch each enabled source to its module. A source module lives
    at `.github/scripts/autoworker_sources/<name>.py` and exposes
    `collect(config: dict) -> dict`. Failures in one source do not
    abort the others — they surface as anomalies."""
    snapshot: dict[str, Any] = {'sources': {}, 'anomalies': []}
    for entry in enabled:
        # entry shape: { source_name: { ...config... } } or just "source_name"
        if isinstance(entry, str):
            name, cfg = entry, {}
        elif isinstance(entry, dict) and len(entry) == 1:
            name, cfg = next(iter(entry.items()))
            cfg = cfg or {}
        else:
            snapshot['anomalies'].append({
                'severity': 'warn',
                'rule': 'malformed_source_config',
                'detail': f'{entry!r}',
            })
            continue

        module_path = SOURCES_DIR / f'{name}.py'
        if not module_path.exists():
            snapshot['anomalies'].append({
                'severity': 'warn',
                'rule': 'unknown_source',
                'detail': f'No collector at {module_path}',
            })
            continue

        try:
            ns: dict[str, Any] = {}
            exec(compile(module_path.read_text(), str(module_path), 'exec'), ns)
            collect_fn = ns.get('collect')
            if not callable(collect_fn):
                raise RuntimeError('source module has no callable `collect`')
            result = collect_fn(cfg) or {}
        except Exception as e:  # noqa: BLE001 — surface as anomaly, do not crash
            snapshot['anomalies'].append({
                'severity': 'warn',
                'rule': 'source_failed',
                'detail': f'{name}: {e}',
            })
            continue

        snapshot['sources'][name] = result
        for a in result.get('anomalies', []):
            snapshot['anomalies'].append(a)
    return snapshot


def render_body(snapshot: dict[str, Any]) -> str:
    ts = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
    anomalies = snapshot.get('anomalies') or []
    lines = [
        f'_Last scan: **{ts}**_',
        '',
        '## Status',
        '',
        '✅ **All clear** — no anomalies detected.' if not anomalies
        else f'⚠️ **{len(anomalies)} anomaly/anomalies** in latest scan:',
        '',
    ]
    if anomalies:
        sev_order = {'critical': 0, 'warn': 1, 'info': 2}
        for a in sorted(anomalies, key=lambda x: sev_order.get(x.get('severity', 'info'), 9)):
            lines.append(
                f'- **[{a.get("severity", "info").upper()}] {a.get("rule", "?")}** — '
                f'{a.get("detail", "")}'
            )
        lines.append('')

    for name, payload in (snapshot.get('sources') or {}).items():
        lines.append(f'## {name}')
        lines.append('')
        summary = payload.get('summary')
        if summary:
            lines.append(str(summary))
            lines.append('')
        # Source can ship a markdown block of detail in `markdown`.
        md = payload.get('markdown')
        if md:
            lines.append(md)
            lines.append('')

    lines += [
        '## Full snapshot',
        '',
        '<details><summary>Click to expand</summary>',
        '', '```json',
        json.dumps(snapshot, indent=2, default=str)[:15000],
        '```', '',
        '</details>',
    ]
    return '\n'.join(lines)


def render_heartbeat(snapshot: dict[str, Any]) -> str:
    ts = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
    anomalies = snapshot.get('anomalies') or []
    if not anomalies:
        head = '✅ **AutoWorker heartbeat — all clear**'
    else:
        sev_critical = sum(1 for a in anomalies if a.get('severity') == 'critical')
        sev_warn = sum(1 for a in anomalies if a.get('severity') == 'warn')
        head = f'⚠️ **AutoWorker heartbeat — {sev_critical} critical, {sev_warn} warn**'
    lines = [f'{head} @ {ts}']
    if anomalies:
        lines.append('- top:')
        for a in anomalies[:3]:
            lines.append(
                f'  - **[{a.get("severity", "?").upper()}]** '
                f'{a.get("rule", "?")} — {a.get("detail", "")[:160]}'
            )
    lines.append('')
    lines.append('_See the issue body above for the full snapshot._')
    return '\n'.join(lines)


def main() -> int:
    repo = os.environ['REPO']

    enabled = load_config()
    if not enabled:
        snapshot = {
            'sources': {},
            'anomalies': [{
                'severity': 'warn',
                'rule': 'no_sources_configured',
                'detail': 'No enabled sources in .autoworker/sources.yml. '
                          'The agent will run blind.',
            }],
        }
    else:
        snapshot = collect_all(enabled)

    label_ok = ensure_label(repo)
    existing = find_tracker(repo)
    body = render_body(snapshot)
    comment = render_heartbeat(snapshot)

    if existing is None:
        create_args = ['issue', 'create', '--repo', repo,
                       '--title', TRACKER_TITLE, '--body', body]
        if label_ok:
            create_args += ['--label', TRACKER_LABEL]
        url = run_gh(create_args).strip()
        print(f'Opened persistent tracker: {url}')
        return 0

    # Comment first (fires the webhook even if the body edit times out
    # on a large payload), then update the body.
    run_gh(['issue', 'reopen', str(existing), '--repo', repo], check=False)
    run_gh(['issue', 'comment', str(existing), '--repo', repo, '--body', comment])
    edit = subprocess.run(
        ['gh', 'issue', 'edit', str(existing), '--repo', repo, '--body', body],
        capture_output=True, text=True,
    )
    if edit.returncode != 0:
        print(
            f'Body edit failed (comment already posted, agent is woken): '
            f'{edit.stderr.strip()[:400]}',
            file=sys.stderr,
        )
    print(f'Heartbeat posted on tracker issue #{existing}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
