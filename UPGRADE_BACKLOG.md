# Upgrade suggestions (operator hint box)

**Not gating.** The autopilot loop in `MONITOR_RUNBOOK.md` self-directs;
it doesn't require entries here. Use this file to:

- Drop a hint when you want the loop to prioritise something specific.
- Mark items the loop should NOT pick up (move to "Hold").
- Record what shipped so the loop avoids re-doing it.

The loop reads this file as priority #2 (after anomaly-driven bug fixes,
before §18 Tier C polish). Hints are treated as first-class work IF
they pass the never-autoship policy check in the runbook.

---

## Hints (loop will pick these up first)

Format:
```
- <one-line title>
  - notes: <optional context>
```

(empty — drop your hints here)

---

## Hold (do not autoship)

Reasons an operator might park work here:
- The change is policy-sensitive (touches §3.1 math, §4 fields, etc.).
- The change requires a venue test the operator wants to run first.
- The work needs design discussion before code.

(empty)

---

## Shipped (loop archives here)

Format: `<title> — PR #<n> @ <merge SHA> on <date>`

(empty — first autopilot shipments will appear here)
