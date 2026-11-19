# Roadmap

Features brainstormed for the Solana Meme Desk, in no particular order. None of these exist yet. PRs for them are welcome, one feature per PR so review stays easy.

## Analytics and reporting

- **PnL analytics dashboard** — a small report (HTML or terminal) that reads `trades.csv`, `trades_paper.csv` and `desk_state_trade_log.json` and shows win rate, average multiple, best and worst fills, and drawdown per day and per token
- **Multi-token portfolio view** — one screen with every open position from `exit_state.json`: entry, current price, remaining percentage, realized plus unrealized PnL, and where the trailing stop sits
- **Gas and fee tracker** — record priority fees and rent for every transaction and show them as a drag line on PnL, so a profitable trade that loses to fees shows up honestly

## Alerts and safety

- **Telegram alerts on ladder fills** — when a rung fills or the trailing stop hits, send a message to a configured chat with token, multiple, percentage sold and PnL
- **Auto-pause on drawdown** — if realized PnL drops below a configured threshold (for example minus 10% of capital in a day), stop opening new paper and live positions until manually resumed
- **Honeypot simulation before buy** — simulate the transfer before approving a trade to detect fee-on-transfer, blacklist or sell-blocking behaviour, and surface anything suspicious in the preview

## Trading smarts

- **Position size auto-scaling** — scale size with recent win rate and current drawdown instead of fixed 1.5% and 5% tiers, within the existing $100 and $15,000 hard limits
- **Jupiter price impact preview** — when quoting a buy, show the estimated price impact for the size so thin-liquidity tokens are rejected before the yes, rather than after a bad fill

## Data and strategy

- **WebSocket streaming from RugCheck paid tier** — replace polling with a push feed so authority and LP changes are caught in seconds, not on the next 15-minute cycle
- **Backtest module** — replay past cycles from stored candles and trade logs to score strategy changes (different ladder percentages, stop widths) before running them live

## Good first issues

The analytics dashboard, portfolio view and gas tracker are read-only over existing logs and are the friendliest starting points. A feature is ready to merge with a README section explaining the new command or output, and a paper-mode test showing it works end to end.
