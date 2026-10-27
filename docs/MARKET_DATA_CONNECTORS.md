# Multi-provider market data connectors

The connector job is read-only. It never creates trade signals, changes strategy thresholds, signs transactions, or submits orders.

## Providers

- **DexScreener:** public pair snapshots for up to five mints discovered from the existing tracked-wallet state.
- **Birdeye:** optional recent token-trade feed. Configure `BIRDEYE_API_KEY`.
- **Dune:** optional saved-query execution. Configure `DUNE_API_KEY` and `DUNE_SOLANA_TRADES_QUERY_ID`. The saved query should target Solana DEX trade data (for example, `dex_solana.trades`) and return the fields needed for analysis. Dune rows are retained as source records and are not automatically treated as verified wallet PnL.
- **Helius:** the existing wallet transaction parser remains in `whale_copy_paper.py`; the connector report only shows whether Helius is configured, to avoid double-counting its swap events.
- **Nansen:** not yet called by code. `NANSEN_API_KEY` is detected only as a credential-presence marker. Before enabling an adapter, verify the account's API access, exact endpoint, authentication contract, and response schema against the current official docs. A key alone is not a working integration.

## Configure GitHub Actions

In the repository, open **Settings → Secrets and variables → Actions** and add the required secrets there. Do not paste API keys into chat, commit them, or put them in source files.

Required for existing Helius ingestion:
- `HELIUS_API_KEY` or `HELIUS_RPC_URL`

Optional:
- `BIRDEYE_API_KEY`
- `DUNE_API_KEY`
- `DUNE_SOLANA_TRADES_QUERY_ID`
- `NANSEN_API_KEY` (presence is reported only; no Nansen request is made yet)

If optional credentials are absent, the report marks that provider as `not_configured`; it does not claim the provider observed zero trades. Individual provider failures are isolated and reported rather than silently treated as empty evidence.

## Report

The scheduled trading-cycle workflow writes `/tmp/market-data-connectors-report.json`, adds a concise provider-status section to the Actions summary, and includes the provider report in the generated dashboard research data. The raw temporary file is not committed as a persistent historical dataset.

The first version is deliberately bounded to five mints and twenty recent Birdeye trades per mint. It is a collection layer, not yet a complete multi-provider historical warehouse. Before any strategy consumes these records, add provider-specific schema validation, deduplication by chain + signature + event index, timestamp normalization, and explicit coverage/retention metadata.

## Verification limits

CI tests mock provider responses and verify normalization and failure isolation; they do not prove that external credentials are valid or that a paid provider plan exposes a given endpoint. Real provider connectivity is only verified in a scheduled run with credentials configured.
