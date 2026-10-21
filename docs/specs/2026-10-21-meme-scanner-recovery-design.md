# Meme Scanner Recovery Design

Date: 2026-10-21
Status: Proposed; implementation is blocked until this design is approved.

## Goal
Restore reliable scheduled scanning and dashboard updates, then evaluate the strategy using isolated paper trading. Never place real trades.

## Decisions
- **Architecture:** Keep the workflow and service in this repository to minimize setup and recovery time.
- **Historical safety:** Preserve `state.json`, the existing drawdown lock, and all historical trade records unchanged. The existing account is halted at its historical drawdown limit; do not reset or bypass it.
- **Paper account:** Add an independent paper account and ledger (separate state and trade history) with its own virtual starting capital and risk guard. No wallet signing, swaps, or real-order path.
- **Backtesting:** Use existing labeled observations and chronological walk-forward validation first. Do not claim this is an OHLCV candle backtest. Add candle-based testing only after a data provider is selected and verified.
- **Dashboard:** Show legacy account and paper account separately, along with last successful cycle, test status, backtest sample size, paper equity, and risk-lock status. Do not present an unvalidated strategy as profitable.
- **Automation:** Keep scheduled execution and deployment in GitHub Actions. Add checks for stale cycles and deployment failures only where existing workflows do not already cover them.

## Implementation plan
1. Inspect current tests and latest workflow results; reproduce the reported test failure locally or in CI before changing code.
2. Add tests for account isolation, preserved legacy lock, paper ledger reconciliation, and prohibition of live-order execution.
3. Implement the independent paper state/ledger and connect the cycle to paper mode without modifying historical state.
4. Repair only confirmed test, scheduling, or dashboard defects; keep workflow changes minimal.
5. Run the full test suite 10 times, then walk-forward validation, adaptive-learning checks, and a complete paper cycle.
6. Verify the dashboard deployment and inspect the published page and latest Actions run before reporting completion.

## Acceptance criteria
- All tests pass in 10 consecutive runs.
- Walk-forward and adaptive-learning jobs complete and report their actual sample sufficiency; insufficient data is not treated as a failure of execution or as strategy approval.
- The paper account is independent; the historical state and drawdown lock remain unchanged.
- No real trades are possible through the implemented path.
- The latest cycle and dashboard deployment are verified from GitHub Actions.

## Initial observation
The latest inspected workflow run (#689, 2026-10-21 13:56 UTC) completed successfully, including the 10-pass test step, walk-forward report, adaptive update, trading cycle, and dashboard deployment. The uploaded status note appears to describe an earlier run. This does not establish strategy profitability or prove the dashboard is currently fresh.
