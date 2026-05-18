# Upgrade backlog

A curated list of safe-to-autoship work items. The monitor loop picks ONE
item per pass that's marked `status: ready`, implements it with tests, and
ships a PR per the [`MONITOR_RUNBOOK.md`](MONITOR_RUNBOOK.md) upgrade flow.

**Operator owns this file.** Add items to authorise autoship. Remove
items to revoke. The loop does NOT invent new items — if nothing here is
`ready`, the pass is monitor-only.

---

## Never autoship (policy — these require operator decision)

These categories are NEVER touched by the loop, regardless of what looks
like an "improvement":

- **§3.1 math** — gates, basis, APY, fees, deferral, exit triggers. Policy.
- **§4 active fields + defaults** — entry/exit thresholds, sizing %,
  basis-dislocation, stop-loss, sub-target, depeg guard.
- **§7.1.1 frozen schema** — additive migrations only; never rename, never
  drop, never reshape an existing column.
- **§8.1 frozen `/api/diagnostics` JSON contract.**
- **Venue gateways' order placement paths** — `place_market_fok` and
  rollback logic. Bugfixes only with explicit operator authorisation.
- **`docs/SYSTEM.md`** — the spec is binding. Only update in the SAME PR
  as a behaviour change that the operator authorised.
- **Credentials, env-var names, auth paths.**

If a backlog item turns out to require touching any of the above, the loop
must STOP and ask the operator. Do not work around the boundary.

---

## Ready (autoship-eligible)

Format:
```
- [ ] <one-line title>
  - id: <slug>
  - status: ready | blocked | in-progress | done
  - risk: low | medium                   # high → not autoship
  - files: <expected paths>
  - acceptance: <test/check to pass>
  - notes: <optional context>
```

Initial seed below. Operator: edit freely. The loop respects this list as
the source of truth.

- [ ] Add UI test for transactions-page symbol filter
  - id: tx-filter-test
  - status: ready
  - risk: low
  - files: tests/test_ui.py
  - acceptance: pytest -k transactions passes; new test covers filter=ETH path

- [ ] Add a `Last 24h funding accrued` KPI to dashboard
  - id: kpi-funding-24h
  - status: ready
  - risk: low
  - files: web/routes/dashboard.py, web/templates/dashboard.html, tests/test_ui.py
  - acceptance: KPI card renders; sum equals sum(trade.fee where venue='futures' and ts in last 24h)*-1 (placeholder formula; loop should DOC its choice; if formula isn't obviously correct, mark blocked and ask)
  - notes: this is a DISPLAY change. If the loop sees it touching strategy math, STOP.

- [ ] Add `view_cookie_max_age_days` to global config + form
  - id: view-cookie-config
  - status: ready
  - risk: low
  - files: state/models.py, state/db.py, web/view_mode.py, web/routes/config_routes.py, web/templates/config.html, tests/test_ui.py
  - acceptance: cookie max-age comes from DB config; existing default (30d) preserved

- [ ] Surface `expected_account_id` mismatch as a §8.2 anomaly rule
  - id: anomaly-account-id-drift
  - status: ready
  - risk: low
  - files: diagnostics/anomalies.py, diagnostics/endpoint.py, tests/test_diagnostics.py
  - acceptance: when a registered gateway's actual id != expected, anomaly `account_id_drift` fires (warn). Test included.

- [ ] Resolve §18 Tier C: vocabulary drift sweep
  - id: vocab-asset-currency
  - status: blocked
  - risk: low
  - files: docs/SYSTEM.md (multiple)
  - notes: doc-only PR. Operator must approve naming choice ("asset" vs "currency") before this becomes ready.

---

## Blocked / awaiting policy decision

(Move items here when they need operator input.)

---

## Done (loop archives here on merge)

(The loop moves items here with PR number + merge SHA when shipping.)
