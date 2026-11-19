#!/usr/bin/env python3
"""
Pump.Fun Trade - swaps SOL for a token via PumpPortal local API.
REQUIRES: confirm_<mint>.json flag file next to this script, created only
after the user says yes in chat. Without it, exits without spending anything.
Logs every trade to trades.csv.
Keypair is loaded from makefun key file but never printed / logged.
"""
import base64, csv, json, os, sys, time
from pathlib import Path

import requests
from solders.keypair import Keypair as SoldersKeypair
from solders.transaction import VersionedTransaction
from solders.message import VersionedMessage

BASE = Path(__file__).parent
TRADES_CSV = BASE / "trades.csv"
CONFIRM_DIR = BASE / "confirmations"
KEYPAIR_PATH = Path(os.environ.get("KEYPAIR_PATH", str(BASE / "keypair.json")))
TRADE_API = "https://pumpportal.fun/api/trade-local"

CONFIRM_DIR.mkdir(exist_ok=True)

def fail(msg):
    print(f"ABORT: {msg}", file=sys.stderr)
    sys.exit(1)

def load_keypair():
    if not KEYPAIR_PATH.exists():
        fail(f"keypair not found at {KEYPAIR_PATH}")
    raw = KEYPAIR_PATH.read_text().strip()
    try:
        arr = json.loads(raw)
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
        fail("usage: trade.py <mint> <sol_amount> <slippage_pct> <buy|sell>")
    mint, sol_amount, slippage, action = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4].lower()

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

    print(f"confirmation verified: {flag.name}")

    keypair = load_keypair()
    pubkey = str(keypair.pubkey())
    print(f"wallet {pubkey[:8]}... (key never printed)")

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
    # RPC send
    import subprocess, json as _json
    rpc = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
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
        "notes": "pumpportal local, confirmed via flag file",
    })

    # consume flag so it cannot be reused
    flag.unlink(missing_ok=True)
    print("confirmation flag consumed")

if __name__ == "__main__":
    main()
