"""Diagnostics endpoint source.

Polls an HTTP endpoint that returns JSON. The endpoint is expected to
either return a snapshot directly (with optional top-level `anomalies`)
or any other JSON the operator wants surfaced raw.

Config (in .autoworker/sources.yml):

    diagnostics_endpoint:
      endpoint: /api/diagnostics
      hours_param: hours        # query-string knob the endpoint reads (optional)

Secrets used (from env):
  BOT_URL              — base URL (e.g. https://service.example.com)
  DIAGNOSTICS_TOKEN    — auth token, sent as `?token=`
  HOURS                — lookback window injected by the workflow
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def collect(config: dict) -> dict:
    base = os.environ.get('BOT_URL', '').rstrip('/')
    token = os.environ.get('DIAGNOSTICS_TOKEN', '')
    hours = os.environ.get('HOURS', '24')
    endpoint = config.get('endpoint', '/api/diagnostics')
    hours_param = config.get('hours_param', 'hours')

    if not base:
        return {
            'summary': 'BOT_URL not set — diagnostics_endpoint skipped.',
            'anomalies': [{
                'severity': 'critical',
                'rule': 'diagnostics_unconfigured',
                'detail': 'Set BOT_URL secret on the repo.',
            }],
        }

    url = f'{base}{endpoint}?token={token}&{hours_param}={hours}'
    req = urllib.request.Request(url, headers={'User-Agent': 'autoworker-ingest/1'})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            status = resp.status
            raw = resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        return {
            'summary': f'Endpoint returned HTTP {e.code}.',
            'anomalies': [{
                'severity': 'critical',
                'rule': 'endpoint_returned_error',
                'detail': f'HTTP {e.code} from {endpoint}',
            }],
        }
    except (urllib.error.URLError, TimeoutError) as e:
        return {
            'summary': f'Endpoint unreachable: {e}.',
            'anomalies': [{
                'severity': 'critical',
                'rule': 'endpoint_unreachable',
                'detail': str(e)[:200],
            }],
        }

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return {
            'summary': f'Invalid JSON from endpoint: {e}.',
            'anomalies': [{
                'severity': 'critical',
                'rule': 'invalid_json',
                'detail': str(e)[:200],
            }],
        }

    anomalies = payload.get('anomalies') if isinstance(payload, dict) else []
    summary_bits: list[str] = [f'HTTP {status}']
    if isinstance(payload, dict):
        ch = payload.get('cycle_health') or {}
        if ch:
            summary_bits.append(
                f'errors {ch.get("error_count", "?")} · '
                f'warns {ch.get("warn_count", "?")}'
            )
    return {
        'summary': ' · '.join(summary_bits),
        'markdown': '```json\n' + json.dumps(payload, indent=2, default=str)[:4000] + '\n```',
        'anomalies': anomalies or [],
    }
