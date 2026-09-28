# Hermes PM V2

Autonomous public-wallet research and **paper-only** copy trading. Python standard library, SQLite on Railway persistent volume. No private keys, signing code, or live order endpoints.

## Run

Set `HERMES_PM_KEY` to a strong secret, `DB_PATH=/data/hermes_pm.sqlite3`, `POLL_SECONDS=20`, and run `python app.py`. Dockerfile is supplied. Deploy one replica only. Mount a persistent volume at `/data`. `PORT` is supplied by Railway. Public `/health`; all data/actions require `X-Hermes-Key`. Auth key stays in browser tab session storage.

## Changes

- Three independent paper ledgers: reference, filtered, adaptive; initial capital 1,000 each. Same observed signals, separate cash and liquidity simulation.
- Proportional buys at 1% source shares, capped per trade; proportional source sells using tracked source inventory; outstanding exit reservations prevent overselling. Paused entries retain exit processing.
- Per-wallet/per-token cost basis and realized P&L, book-depth liquidation valuation, unknown marks shown as unknown (never stale invented equity).
- Cumulative cost limits: 25 per buy, 200 per wallet, 75 per market, 125 per event, 600 total, 20 positions. Daily 5% drawdown entry brake from first complete UTC daily snapshot. Exits still run.
- Reference accepts current prices; filtered/adaptive cap adverse buy-price drift at min(2 cents,5%) and spread at 6 cents. All share conservative risk ceilings and exclusions.
- Orders respect the published minimum share quantity and a 1-USDC buy floor. Partial fills consume depth within each portfolio/cycle; pending sells retry, buys expire in 180 seconds. No queue simulation or future-market impact model. Fee rate/exponent read from Gamma; explicit zero from CLOB accepted. Unknown positive fee schedules block execution.
- Fees modeled as cash-equivalent `qty * rate * (p*(1-p))**exponent`. Does not model exact onchain fee-in-shares rounding, gas, rebates, or latency below polling resolution.
- Settlement requires Gamma `closed=true`, `umaResolutionStatus=resolved`, aligned token IDs/prices with payouts 0/0.5/1 summing to 1. Closed alone is not settlement. Unsupported resolution stays open/unknown.
- Discovery: up to 200 monthly leaderboard candidates; evaluate 40 per hourly round, 50 latest closed positions each. Sample-based exploratory score. Auto-admit up to 10 meeting documented evidence filters; manually added wallets start observation-only. No promise of complete wallet history or profitability.
- Adaptive weights reviewed daily after >=14 days and >=20 markets with exits in the reference ledger. Weight changes <=0.25/day within 0.5–1.25. Starts identical to filtered; no retrospective selection backtest.
- Concurrent public data polling, bounded pagination and 300-second overlap. First position snapshot defines baseline; no historical copy. Periodic reconciliation blocks entries on inventory discrepancies; operator can request a fresh baseline. Snapshots and trade indexing are not atomic, so discrepancies can reflect feed delay as well as transfers/splits/merges.
- Neg-risk/complex markets excluded from buys. Multi-wallet hedges, maker queue capture, and arbitraje not reconstructed.
- Daily consistent SQLite backup on the same volume, last 3 retained. Authenticated external download. Minute equity retained 7 days then hourly, decision logs 90 days. Signals/fills retained; monitor disk before reaching 500 MB.

## Migration

Original V1 tables retained; one pre-V2 backup. Wallets retained. Legacy holdings/cash import into filtered only; if V1 has trades, reference/adaptive begin fresh and should not be compared over mismatched dates. No cash reset on restarts. Migration is one-time transaction guarded by schema version.

## Tests

`python -m unittest discover -s tests -v`

Tests cover copy proportions, isolated wallet inventory, partial exits, cash/cost/fee conservation, exposure concentration, stale signals, price filters, settlement, null valuations, idempotency, migration, and restart persistence. HTTP smoke and live read-only API checks are separate from synthetic execution tests.

## Official API references

https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets
https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user
https://docs.polymarket.com/market-data/market-details
https://docs.polymarket.com/trading/fees

Legacy data routes remain isolated in provider.py for migration to v2 when its contract is tested.
