#!/usr/bin/env python3
"""
Pump.Fun Trade - swaps SOL for a token via PumpPortal local API.

GATES (all must pass or trade aborts, no SOL spent):
1. Confirmation flag: confirmations/confirm_<mint>.json must exist, matching
   mint, sol_amount and slippage, and must have been created within
   CONFIRM_EXPIRY_SEC (5 minutes) of the user's YES (yes_at).
2. Spend limits: size_usd <= MAX_TRADE_USD (100) and
   today_total_usd + size_usd <= MAX_DAY_USD (15000), tracked in
   daily_spend.json with UTC date rollover (resets at 00:00 UTC).
3. Wallet guard: keypair pubkey must equal EXPECTED_WALLET
   (28K5za3Mkt3QopDvZN4TZA8mqnRk2M9wSHUHDkbLaLoy). Any other wallet aborts.

Logs every trade to trades.csv and desk_state_trade_log.json
(token, entry price, size USD, tx hash, PnL basis).
Keypair is loaded from makefun key file but never printed / logged.
"""
import base64, csv, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

import requests
from solders.keypair import Keypair as SoldersKeypair
from solders.transaction import VersionedTransaction
from solders.message import VersionedMessage

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
import rpc_config

TRADES_CSV = BASE / "trades.csv"
CONFIRM_DIR = BASE / "confirmations"
KEYPAIR_PATH = Path("/w/hosts/3/u/6adaa5b6ea93d17e1b6a4572/workspace/makefun/makefun-keypair.json")
TRADE_API = "https://pumpportal.fun/api/trade-local"

CONFIRM_EXPIRY_SEC = 300  # 5 min: flag must be created within 5 min of YES
MAX_TRADE_USD = 100.0     # user-set 2026-11-19
MAX_DAY_USD = 15000.0     # user-set 2026-11-19
EXPECTED_WALLET = "28K5za3Mkt3QopDvZN4TZA8mqnRk2M9wSHUHDkbLaLoy"
DAILY_SPEND_FILE = BASE / "daily_spend.json"
TRADE_LOG_FILE = BASE / "desk_state_trade_log.json"

CONFIRM_DIR.mkdir(exist_ok=True)

def fail(msg):
    print(f"ABORT: {msg}", file=sys.stderr)
    sys.exit(1)

# ---- spend-limit tracking (daily_spend.json with UTC date rollover) ----
def today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def load_daily_spend():
    default = {"date": today_utc(), "total_usd": 0.0, "trades": []}
    if not DAILY_SPEND_FILE.exists():
        return default
    try:
        data = json.loads(DAILY_SPEND_FILE.read_text())
    except Exception:
        return default
    # date rollover: new UTC day resets the total
    if data.get("date") != today_utc():
        return default
    return {
        "date": today_utc(),
        "total_usd": float(data.get("total_usd") or 0.0),
        "trades": data.get("trades") or [],
    }

def save_daily_spend(state):
    DAILY_SPEND_FILE.write_text(json.dumps(state, indent=2))

def check_spend_limit(size_usd, daily=None):
    """Returns (ok: bool, reason: str). Pure, no side effects."""
    state = daily if daily is not None else load_daily_spend()
    spent = float(state.get("total_usd") or 0.0)
    if size_usd > MAX_TRADE_USD:
        return False, f"size ${size_usd:.2f} exceeds max per trade ${MAX_TRADE_USD:.2f}"
    if spent + size_usd > MAX_DAY_USD:
        return False, f"today ${spent:.2f} + ${size_usd:.2f} exceeds max per day ${MAX_DAY_USD:.2f}"
    return True, "ok"

def record_spend(size_usd, mint, tx_id, entry=None, date=None):
    state = load_daily_spend()
    state["total_usd"] = round(float(state["total_usd"]) + float(size_usd), 2)
    state["trades"].append({
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mint": mint,
        "size_usd": float(size_usd),
        "tx": tx_id,
        "entry_price": entry,
    })
    save_daily_spend(state)
    return state

# ---- wallet guard (pure, testable) ----
def check_wallet(pubkey_str):
    return pubkey_str == EXPECTED_WALLET

# ---- confirmation expiry (pure, testable) ----
def check_confirmation_expiry(yes_at, now=None):
    """yes_at: epoch seconds (float/int) or ISO string. Valid if within
    CONFIRM_EXPIRY_SEC of now. Missing or unparsable -> invalid."""
    if yes_at is None:
        return False, "missing yes_at; flag must be created within 5 minutes of YES"
    try:
        if isinstance(yes_at, str):
            dt = datetime.fromisoformat(yes_at.replace("Z", "+00:00"))
            y_ts = dt.timestamp()
        else:
            y_ts = float(yes_at)
    except Exception:
        return False, f"unparsable yes_at: {yes_at!r}"
    now_ts = float(now) if now is not None else time.time()
    age = now_ts - y_ts
    if age < 0:
        return False, f"yes_at is in the future ({age:.0f}s ahead); refusing"
    if age > CONFIRM_EXPIRY_SEC:
        return False, f"YES expired {age:.0f}s ago; trade aborts (5-min window)"
    return True, "ok"

# ---- desk_state_trade_log.json ----
def append_desk_log(entry):
    log = []
    if TRADE_LOG_FILE.exists():
        try:
            log = json.loads(TRADE_LOG_FILE.read_text())
            if not isinstance(log, list):
                log = [log]
        except Exception:
            log = []
    log.append(entry)
    TRADE_LOG_FILE.write_text(json.dumps(log, indent=2))
    print(f"log -> {TRADE_LOG_FILE}")

def load_keypair():
    if not KEYPAIR_PATH.exists():
        fail(f"keypair not found at {KEYPAIR_PATH}")
    raw = KEYPAIR_PATH.read_text().strip()
    try:
        arr = json.loads(raw)
        if isinstance(arr, dict):  # {"secretKey": [64 ints], "version": ...}
            arr = arr["secretKey"]
        secret = bytes(arr)
    except Exception:
        # may be base64 or raw
        try:
            secret = base64.b64decode(raw)
        except Exception:
            fail("could not read keypair file")
    if len(secret) != 64:
        fail("keypair is not 64 bytes")
    return SoldersKeypair.from_bytes(secret)

def log_trade(row):
    TRADES_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not TRADES_CSV.exists()
    with TRADES_CSV.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time","action","mint","sol_in","pnl_sol","tx","notes"])
        if write_header:
            w.writeheader()
        w.writerow(row)
    print(f"logged -> {TRADES_CSV}")

def main():
    if len(sys.argv) < 5:
        fail("usage: trade.py <mint> <sol_amount> <slippage_pct> <buy|sell> [sol_usd_price] [entry_price_usd]")
    mint, sol_amount = sys.argv[1], float(sys.argv[2])

    # Optional: SOL price + token entry price for USD size / desk_state log.
    # From env or trailing args; size_usd = sol_amount * sol_usd_price.
    # Defaults: SOL price from env SOL_USD_PRICE if present, else 150.0.
    sol_usd_price = float(os.environ.get("SOL_USD_PRICE") or (sys.argv[5] if len(sys.argv) > 5 else 150.0))
    entry_price = float(sys.argv[6]) if len(sys.argv) > 6 else None
    size_usd_from_sol = sol_amount * sol_usd_price  # for buys; sells are exits
    slippage, action = float(sys.argv[3]), sys.argv[4].lower()

    if action not in ("buy", "sell"):
        fail("action must be buy or sell")

    # HARD GATE: per-trade confirmation flag
    flag = CONFIRM_DIR / f"confirm_{mint}.json"
    if not flag.exists():
        fail(f"no confirmation flag found ({flag.name}). The agent only trades after you say yes in chat and the flag is created. Refusing to spend.")

    conf = json.loads(flag.read_text())
    if conf.get("mint") != mint:
        fail("confirmation mint mismatch")
    if abs(float(conf.get("sol_amount", 0)) - sol_amount) > 1e-9:
        fail("confirmation amount mismatch")
    if abs(float(conf.get("slippage", 0)) - slippage) > 1e-9:
        fail("confirmation slippage mismatch")

    # 5-minute expiry on the flag: yes_at must be within CONFIRM_EXPIRY_SEC of now
    yes_at = conf.get("yes_at", conf.get("yesAt", conf.get("created_at", conf.get("requested_at"))))
    ok, reason = check_confirmation_expiry(yes_at)
    if not ok:
        fail(f"confirmation expired/invalid ({reason}). Trade aborts.")

    print(f"confirmation verified: {flag.name}")

    # Spend limits: size_usd for buys; sells are exits (no new spend, skip)
    # size_usd can come from flag (size_usd) if present, else sol_amount * sol_usd_price
    if action == "buy":
        size_usd = float(conf.get("size_usd") or size_usd_from_sol)
        ok, reason = check_spend_limit(size_usd)
        if not ok:
            fail(f"spend limit hit: {reason}")
    else:
        size_usd = float(conf.get("size_usd", 0) or 0)

    keypair = load_keypair()
    pubkey = str(keypair.pubkey())
    # Wallet guard: desk wallet only
    if not check_wallet(pubkey):
        fail(f"wallet guard: keypair pubkey {pubkey[:8]}... is not desk wallet {EXPECTED_WALLET[:8]}... Refusing to sign.")
    print(f"wallet {pubkey[:8]}... (key never printed)")

    # Balance check via Helius RPC (pre-trade, fails closed on insufficient SOL)
    from rpc_config import get_balance_lamports, is_helius
    bal = get_balance_lamports(pubkey, timeout=15)
    if bal is not None:
        sol_bal = bal / 1e9
        print(f"balance: {sol_bal:.4f} SOL (via {'Helius' if is_helius() else 'public'} RPC)")
        if sol_bal < sol_amount + 0.001 + 0.0005:  # amount + rent buffer + priority fee
            fail(f"insufficient balance: {sol_bal:.4f} SOL < {sol_amount + 0.0015:.4f} SOL needed")
    else:
        print("balance check: RPC unreachable, proceeding (sendTransaction will fail if bad)")

    payload = {
        "publicKey": pubkey,
        "action": action,
        "mint": mint,
        "denominatedInSol": "true",
        "amount": sol_amount,
        "slippage": slippage,
        "priorityFee": 0.0005,
    }

    print("building tx via pumpportal local api...")
    r = requests.post(TRADE_API, json=payload, headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=20)
    if r.status_code != 200:
        fail(f"trade-local returned {r.status_code}: {r.text[:500]}")

    data = r.json()
    if data.get("error"):
        fail(f"trade-local error: {data['error']}")

    tx_b64 = data.get("transaction") or data.get("txBase64") or data.get("tx")
    if not tx_b64:
        fail(f"no transaction in response: {str(data)[:400]}")

    tx_bytes = base64.b64decode(tx_b64)
    tx = VersionedTransaction.from_bytes(tx_bytes)
    tx.sign([keypair]) if hasattr(tx, "sign") else None
    # solders VersionedTransaction.sign exists in 0.29; if not, sign manually
    try:
        signed = tx.sign([keypair])
    except Exception:
        signed = VersionedTransaction(tx.message, [keypair])


    # serialize
    signed_bytes = bytes(signed) if isinstance(signed, (bytes, bytearray)) else signed.to_bytes() if hasattr(signed, "to_bytes") else bytes(signed)

    print("sending tx...")
    from rpc_config import get_rpc_url, is_helius
    rpc = get_rpc_url()
    print(f"rpc: {'Helius' if is_helius() else 'public'}")
    resp = requests.post(rpc, json={"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":[base64.b64encode(signed_bytes).decode(), {"encoding":"base64", "preflightCommitment":"confirmed"}]}, timeout=30)
    j = resp.json()
    txid = j.get("result") or j.get("error", {}).get("message", "unknown")

    print(f"tx: {txid}")

    log_trade({
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": action,
        "mint": mint,
        "sol_in": sol_amount,
        "pnl_sol": "",
        "tx": txid,
        "notes": "pumpportal local, confirmed via flag file, sent via Helius RPC",
    })

    if action == "buy":
        record_spend(size_usd, mint, txid, entry=entry_price)

    append_desk_log({
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": action,
        "mint": mint,
        "entry_price": entry_price if entry_price is not None else conf.get("entry_price"),
        "size_usd": size_usd,
        "sol_in": sol_amount,
        "tx": txid,
        "wallet": pubkey,
        "pnl_basis": {
            "size_usd": size_usd,
            "pnl_usd": None,
            "pnl_sol": None,
            "notes": "pnl computed by Exit module on realized fills; this entry is the open basis",
        },
    })

    # consume flag so it cannot be reused
    flag.unlink(missing_ok=True)
    print("confirmation flag consumed")

if __name__ == "__main__":
    main()
