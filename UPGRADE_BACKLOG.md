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

(empty — first autopilot shipments will appear here)
