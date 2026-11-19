# Tutorial

Beginner walkthrough for the Solana Meme Desk. Everything here runs in paper mode first, which costs nothing and spends nothing.

## 1. Your first scan

```bash
pip install requests solders
cp .env.example .env
# fill in your keys in .env (Birdeye and Helius recommended, both optional: the desk falls back to free endpoints)
python3 scanner.py
```

That screens the newest 20 pools and prints the top 5, lowest risk first, like:

```
feed=Birdeye
- MOONCAT [LOW RISK score 31] mint=XYZ...
```

The full result lands in `watchlist.json`. Open it and look at one entry:

- `mint` — the token address you would trade
- `safetyScore` and `verdict` — the risk summary (lower score is safer)
- `flags` — what went wrong, for example `mint authority active`
- `positives` — what went right, for example `mint authority revoked`
- `rugcheck` — the raw RugCheck payload, including risks, holders and LP lock percentage
- `onchain` — what Helius itself says about authorities and supply

## 2. Reading the rug check verdict

The score runs 0-100, higher means riskier:

- 0-24 SAFE
- 25-44 LOW RISK
- 45-64 CAUTION
- 65-84 HIGH RISK
- 85-100 CRITICAL
- UNKNOWN when there is no routable market to score

Read the flags like a checklist of things that can hurt you:

- `mint authority active` — the creator can still print more supply. This is a hard kill in the desk.
- `freeze authority active` — the creator can freeze your wallet. Also a hard kill.
- `top-heavy concentration 42.3%` — a few wallets hold a huge share. Over 30% in the top 10 is a hard kill.
- `rugcheck: LP unlocked` — liquidity can be pulled instantly. Hard kill.
- `token2022 transferHook` or `permanentDelegate` — the token can block, tax or move your transfers. Hard kill.
- `missing socials` — a small penalty, not a kill, but it means less accountability for the creator.

`ONCHAIN authority drift` deserves a special look: it means what Helius sees on chain disagrees with what RugCheck reports. Trust the chain, treat it as the more dangerous version of the flag.

If you want the raw RugCheck report for one token, straight from the API:

```bash
python3 rugcheck_api.py <mint>
```

## 3. The Approve YES/NO flow

The desk never trades on its own initiative. Here is the exact flow for a live trade:

1. You ask to buy a specific token with a specific SOL amount.
2. The assistant shows you a preview: token symbol, full mint, SOL amount, slippage, safety score and verdict, and the flags from `watchlist.json`.
3. You reply `yes` in that same turn, or `no`.
4. On `yes`, the assistant creates `confirmations/confirm_<mint>.json` with the mint, amount, slippage and a `yes_at` timestamp.
5. The assistant runs:

```bash
python3 trade.py <mint> <sol_amount> <slippage_pct> buy
```

6. `trade.py` checks, in order: the flag exists, the mint/amount/slippage match exactly, `yes_at` is inside 5 minutes, the $100 per-trade and $15,000 daily limits hold, and the keypair belongs to the desk wallet. Any failure aborts before signing.
7. Only then does it build the transaction via PumpPortal, sign locally, send via RPC, write to `trades.csv`, and delete the flag so it cannot be reused.

On `no`, no flag is ever created, and nothing happens. If you are late, the flag expires after 5 minutes and the trade aborts with "YES expired, trade aborts". That is deliberate: a yes should never be replayed hours later.

## 4. How exits fire

Every open position gets a pre-committed exit plan the moment it opens, stored in `exit_state.json`:

- Ladder: at 2.5x entry price sell 25%, at 5x sell another 25%, at 10x sell another 20%
- The remaining 30% rides a trailing stop: at +65% the stop moves to breakeven, then it trails 30% below the highest price seen and never widens
- If price falls to the stop first (initially 30% below entry), the whole remaining position exits

Each time the desk runs a cycle, `exit.py` compares the current price to the entry and records any ladder fills or a stop fill with realized PnL in USD. Paper fills stay paper. If a live fill ever needs to happen, it must pass the same confirmation flag, so an exit can never fire without your involvement either.

## 5. Running one full paper cycle

```bash
python3 desk.py --mode paper --once
```

This is the loop the coordinator runs every 15 minutes: scan candidates, apply the $5k liquidity / 50 holder / $10k volume floors, rug check, momentum, paper open, exit update. Watch what happened in `desk_log.csv`, one row per agent decision:

```bash
cat desk_log.csv
```

## 6. Checking momentum on a token you like

```bash
python3 momentum.py <mint>
```

All four checks must be True for the token to clear. If one shows `null`, the data was insufficient and the check fails closed. That conservatism is the point: no data, no trade.
