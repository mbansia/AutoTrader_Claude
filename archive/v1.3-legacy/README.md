# v1.3 legacy app — archived

This is the original **v1.3** monolith (`app/`) that ran production until
the cutover to the v1.4/v1.5 spec-conformant rewrite (`core/` `state/`
`gateways/` `loop/` `diagnostics/` `web/`). Per `docs/SYSTEM.md` §17 the
two packages ran in parallel during the cutover window; this directory is
the post-cutover archive of the retired package.

Nothing in the active codebase, the test suite, or CI imports it — it was
already a sealed, hermetic package (no imports from the v1.4 packages, and
the v1.4 packages never imported it). It is preserved here only as a
rollback reference and for historical `git blame`/`git log` continuity.

## What's here

- **`app/`** — the v1.3 FastAPI app + cycle loop. Entry point was
  `uvicorn app.main:app`. Internal imports still read `from app.bot import …`
  (the package name is preserved), so restoration is a single move.
- **`docs/kucoin-integration-plan.md`** — the v1.3-era multi-venue plan.
  Phase 1 (KuCoin) shipped and is a live venue in v1.4; the canonical
  roadmap for the still-planned phases now lives in `docs/SYSTEM.md` §3.2
  (cross-venue) and §3.3 (onchain). The doc's `VenueGateway` protocol
  sketch describes the archived `app/` surface, not the v1.4 `Gateway`
  protocol in `gateways/base.py`.

## How to restore as a fallback

```bash
git mv archive/v1.3-legacy/app app
# then run the v1.3 entry point:
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Rolling back the Coolify cutover is the reverse of §17 Stage 6: restore
`app/` and switch the start command from `uvicorn web.app:app` back to
`uvicorn app.main:app`. The DB schema is shared (additive-only), so v1.3
can read rows v1.4 wrote.
