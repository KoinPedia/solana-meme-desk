# Solana Meme Desk (pumpfun-agent)

> **Risk disclaimer: Trading Solana memecoins is extremely risky. You can lose 100% of your capital. This is research code, not financial advice. Never trade with money you cannot afford to lose. Backtest on paper mode before any live use. Fraud, rug pulls, and illiquidity are the norm in this market.**

Read-only scanner + confirmation-gated trader. Nothing spends SOL without your explicit "yes" in chat. Paper mode is the default.

## Where things live

- `scanner.py` - pulls newest Solana pools (GeckoTerminal), screens with RugCheck + DexScreener, writes `watchlist.json`
- `desk.py` - 4-agent coordinator (Scanner -> Rug Check -> Momentum -> Exit). `--mode paper` (default, no real trades) or `--mode live` (still requires per-trade confirmation flag). `--once` runs one cycle. Enforces floors (liq >=$5k, holders >=50, vol24 >=$10k) and one larger position per day, reinvests paper profits into `paper_state.json` capital ($500 starting).
- `momentum.py` - confirms 4 uptrend criteria via DexScreener/GeckoTerminal OHLCV: volume rising with price, HH/HL, buy/sell > 1, new buyers rising.
- `exit.py` - ladder (25% @2.5x, 25% @5x, 20% @10x) + trailing stop (breakeven at +65%, trail 30% from peak, never widens), persisted to `exit_state.json`.
- `trade.py` - swaps SOL for a token via PumpPortal local API. Only runs if a per-trade confirmation file exists and matches exactly
- `watchlist.json` - ranked watchlist with mints, safety scores, flags (rewritten each scan)
- `trades.csv` - every executed trade + pnl (created on first trade)
- `confirmations/confirm_<mint>.json` - one file per trade, created only after you say yes in chat, consumed (deleted) after execution

## Trigger a scan

```bash
git clone https://github.com/KoinPedia/solana-meme-desk.git
cd solana-meme-desk
python3 scanner.py
```

Env var `PUMP_SCAN_LIMIT` controls how many newest pools to screen (default 20).

## How a confirmed trade flows

1. You say "buy <token> with X SOL" in chat
2. I present an exact preview: token symbol, full mint address, SOL amount, slippage %, safety score/verdict from `watchlist.json`, and flags
3. You reply "yes" in that same turn
4. I create `confirmations/confirm_<mint>.json` with mint, sol_amount, slippage, and timestamp
5. I run `python3 trade.py <mint> <sol_amount> <slippage_pct> buy`
6. `trade.py` verifies the flag matches mint/amount/slippage exactly, loads the make.fun keypair (never printed), requests a signed-ready tx from PumpPortal (`https://pumpportal.fun/api/trade-local`), signs it locally, sends via Solana RPC, logs to `trades.csv`, deletes the flag
7. If you don't say yes, no flag, no spend. If amounts mismatch, abort.

## Modes

The script above is confirmation-gated. On 2026-11-18 the user asked for "Enable auto trading". Auto mode was NOT enabled by default because the wallet balance is unverified and the current safety scores are all HIGH RISK / CRITICAL. To enable auto mode, an explicit opt-in file `confirmations/auto_mode.json` with `{"auto": true, "max_sol_per_trade": 0.05, "max_score": 64}` must be present. Without it, trades always require the per-trade flag. Never use auto mode on an unfunded/unverified wallet.

## Safety model

Score 0-100, higher = riskier. 0-24 SAFE, 25-44 LOW RISK, 45-64 CAUTION, 65-84 HIGH RISK, 85-100 CRITICAL, UNKNOWN if no routable market. Checks liquidity in USD, mint + freeze authority revoked, Token-2022 dangerous extensions, top-holder concentration, RugCheck risks, socials present. Informational only, not financial advice.

## Notes

- Pump.fun's own frontend API now returns Cloudflare 403s to plain HTTP clients, so the scanner uses GeckoTerminal (free, same pool data) for discovery. RugCheck + DexScreener do the screening.
- Rate limits: GeckoTerminal is 30 req/min on the free keyless tier. The scanner retries 429s with backoff.
- Wallet: `trade.py` loads a Solana keypair from the path in the `KEYPAIR_PATH` env var (defaults to `./keypair.json`). Never commit a keypair or seed phrase. Balance is unverified. Do not fund until you confirm the address and amount.
