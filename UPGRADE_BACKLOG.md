# Upgrade backlog (AutoTrader_Codex)

**Not gating.** AutoWorker self-directs from `MASTER_DIRECTIVES.md`; it
doesn't need entries here to do work. Use this file when you want to:

- Drop a hint so the next pass prioritises something specific.
- Park an item the agent should NOT pick up yet (move to **Hold**).
- Track what's already shipped so the agent avoids re-doing it.

The agent reads this file as work-selection priority #2 (after
anomaly-driven bug fixes, before directive open items). Hints are
treated as first-class work IF they pass §8 of the directives.

---

## Hints (the agent picks these up first)

Format:

```
- <one-line title>
  - dimension: product | tech | security | ux | marketing | feedback
  - notes: <optional context>
```

(empty — drop your hints here)

---

## Hold (do not autoship)

Reasons to park work here:

- The change is policy-sensitive (touches §8).
- The change needs a manual test the operator wants to run first.
- The work needs design discussion before any code.

(empty)

---

## Shipped (the agent archives here)

Format: `<title> — PR #<n> @ <merge SHA> on <date> [dimension]`

Close 3 Tier C TODO rows in docs/SYSTEM.md §18 — PR #62 @ e4b5461 on 2026-05-20 [tech/docs]
Close Tier C gap: /config strategy tab source — PR #65 @ fd00c30 on 2026-05-24 [tech/docs]
Close 2 Tier C gaps: diagnostics sentinel fields + venue dust floors — PR #67 @ f04d18e on 2026-05-24 [tech/docs]
Test coverage: monitoring route 52% → 100% — PR #69 @ 9123dc7 on 2026-05-24 [tech/tests]
Test coverage: anomaly error_burst + close_blocked + safety per-strategy guardrails — PR #72 @ 1056622 on 2026-05-24 [tech/tests]
Test coverage: loop/executor.py 80% → 100% (both-rejected + timeout paths) — PR #74 @ f7ef729 on 2026-05-24 [tech/tests]
