# Contributing

## Setup

```bash
pip install requests solders
cp .env.example .env
python3 scanner.py
```

Paper mode requires no keys at all. Birdeye and Helius keys make scans richer but the free fallbacks keep everything working.

## Ground rules

- Read-only first: scanner, rug check and momentum changes must never sign, send, or touch the keypair
- The confirmation gate, the $100 per-trade and $15,000 daily limits, and the desk-wallet guard in `trade.py` are not negotiable and must never be bypassed, even by a flag. A change to their values must be a deliberate, reviewed commit that says so
- Never commit `.env`, any key, or a keypair. `.gitignore` covers `.env`; check `git status` before every commit
- One feature per PR, with a README section for anything user-facing

## Testing safely

- Run `python3 desk.py --mode paper --once` to exercise the full loop with no SOL spent
- `rpc_config.py`, `rugcheck_api.py` and `momentum.py` can be run directly with any mainnet mint to see their JSON output
- Pure helpers in `trade.py` (`check_spend_limit`, `check_wallet`, `check_confirmation_expiry`) are designed to be tested without the network: pass crafted inputs and assert on the returned reason

## Code layout

- `scanner.py` — discovery and scoring, writes `watchlist.json`
- `rugcheck_api.py` — RugCheck client, no side effects
- `rpc_config.py` — shared RPC and `.env` loading
- `momentum.py` — four-criterion assessment, prints JSON
- `exit.py` — ladder and trailing-stop state machine, import-only
- `trade.py` — the only script that signs; fully gated
- `desk.py` — coordinator that wires the rest together

## Pushing

Work on a branch, keep commits small, and describe what changed and how you tested it. Documentation PRs are welcome too: if a script gains a flag or env var, update README.md and `.env.example` in the same PR.
