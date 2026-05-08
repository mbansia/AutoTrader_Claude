# Yield optimisation — archived

This subsystem was active in the bot through commit
`1268ff6` (2026-05-08). It was removed because the operator's KuCoin
sub-account cannot enable Auto-Subscribe / margin auto-lend (the toggle
isn't propagated from the master account), and Binance's old
sapiPostPortfolioMint endpoint was retired without a ccxt-exposed
replacement. Net effect: the active yield code spent rate-limit budget
moving money in/out of earn surfaces with no actual yield being booked.

The simpler shape — USDT just sits in PM (Binance) or UTA (KuCoin) as
collateral — is what the bot does today. Adding yield back is a
non-core enhancement we'll revisit later.

## What was removed

- **`StrategyConfig` columns:** `earn_enabled`,
  `earn_idle_threshold_usdt`, `earn_paper_apr`, `binance_max_bfusd_pct`,
  `kucoin_auto_lend_enabled`, `earn_subscribe_spot_assets`. The DB
  columns stay (in-place schema migrations only ever add) but the
  Python model no longer maps them.
- **`EarnState` model + `earn_state` table:** model removed; existing
  rows are unreferenced. The table is left in the DB so historical
  data isn't lost.
- **Bot:** `_earn_sweep_for_venue`, `get_earn_state`,
  `get_all_earn_states`, `_accrue_paper_yield`,
  `_refresh_live_earn_balance`, `_enforce_venue_yield_settings`, the
  per-candidate just-in-time earn redeem, the post-trade earn sweep,
  the `position_closed` / `deposit_detected` event subscribers.
- **Exchange:** `flexible_earn_apr`, `earn_balance_usdt`,
  `earn_balance`, `earn_subscribe`, `earn_subscribe_asset`,
  `earn_redeem`, `earn_redeem_asset`, `earn_product_id`,
  `lent_usdt_active`, `toggle_auto_lend`, `combined_apy`,
  `spot_earn_apr`, the BFUSD subscribe-cooldown machinery and APR /
  product-ID caches.
- **UI:** `Yielding collateral` dashboard card, `Yield routing` config
  card, `Paper Earn APR` field, `+ Earn APR (%)` candidate column,
  the BFUSD debug probes on `/monitoring`, the safety-page yield rows.

## How to revive

`git log --follow --diff-filter=D` for any of the removed
identifiers (e.g. `git log -S 'EarnState' --all`) finds the deletion
commit; `git show <sha>:app/bot.py` etc. gives the original code. The
docs in `archive/yield/docs/` are the design docs as they stood at
removal time — they're the right starting point if/when the toggles
become available again.

## Re-enabling preconditions

Before re-introducing this subsystem, verify:

1. **KuCoin margin auto-lend** is callable from a sub-account API key
   (the user can enable it once via the master, or KuCoin's API
   re-exposes the toggle). Without this the toggle stays a status
   reporter only.
2. **Binance BFUSD** has a current ccxt-exposed mint endpoint, or we
   wrap the raw HTTP. The Aug 2025 migration to Simple Earn flexible
   meant `sapiPostPortfolioMint` returned `-21015 deprecated`.
3. **Interest-history endpoints** are wired so cumulative yield is
   measured from real interest payments, not synthesised from balance
   deltas (which over-counted every internal transfer).

Until those land, the simpler "collateral only" shape is correct.
