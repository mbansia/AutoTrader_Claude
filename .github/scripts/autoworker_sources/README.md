# Data sources

AutoWorker data sources are small Python modules that the ingest cron
runs each tick. Each module:

- Exposes one function: `collect(config: dict) -> dict`
- Returns a dict with optional keys:
  - `summary` — a one-line string for the tracker issue body
  - `markdown` — a markdown block of detail
  - `anomalies` — a list of `{severity, rule, detail}` dicts that get
    promoted to the snapshot's top-level anomalies list (the agent's
    primary signal surface)
- Reads any secrets it needs from `os.environ` (so the GitHub Actions
  workflow can inject them).

Sources live at `.github/scripts/autoworker_sources/<name>.py` in your
target repo. The ingest script (`autoworker_data_ingest.py`) loads each
one by file path and calls `collect()` with the per-source config
block.

## Built-in sources

- **diagnostics_endpoint** — polls an HTTP endpoint, treats the JSON as
  the snapshot. Carries forward the v0 AutoTrader pattern.
- **github_signals** — open issues (especially labelled `bug`,
  `regression`, `urgent`), recently failed CI runs.
- **social_reddit** — public Reddit search for a project name. No auth.

## Add your own

Drop `mycustom.py` in this directory. Minimal shape:

```python
import os

def collect(config: dict) -> dict:
    threshold = config.get('threshold', 10)
    # ... do work ...
    return {
        'summary': f'Hello — got {n} events',
        'markdown': '- detail line 1\n- detail line 2',
        'anomalies': [
            {'severity': 'warn', 'rule': 'too_many', 'detail': f'{n} > {threshold}'},
        ],
    }
```

Then enable it in `.autoworker/sources.yml`:

```yaml
enabled:
  - mycustom:
      threshold: 25
```

## Common additions

Likely sources to write yourself:

- **social_x** — needs `X_BEARER_TOKEN`. Skeleton in the X API docs;
  not bundled here because the auth lifecycle and rate limits depend on
  the operator's account tier.
- **support_inbox** — IMAP / email-API ingestion of `support@yourdomain`
  with a sentiment / urgency tag.
- **app_store_reviews** — RSS for iOS / Play Store reviews.
- **product_metrics** — your analytics provider (Mixpanel, Amplitude,
  PostHog) summarised to a dashboard delta.

Keep each source narrow. The agent treats `anomalies` as the action
trigger; everything else is context.
