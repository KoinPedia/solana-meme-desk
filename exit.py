#!/usr/bin/env python3
"""
Exit - ladder + trailing stop state machine per open position.
Persisted to exit_state.json so restarts resume cleanly.

Ladder (pre-committed before trade opens):
  20-30% at 2-3x, 20-30% at 5x, optional 20% at 10x, remainder trailing stop.
  Stop moves to breakeven at +50-80%, then trails. Stop never widens.
  If price hits initial stop first, exit full position.

Paper mode: fills are paper only, recorded with pnl_usd.
Live mode: fills still require per-trade confirmation flag file (checked by caller desk.py).
This module never signs or sends anything itself.
"""
import json, time, os
from pathlib import Path

BASE = Path(__file__).parent
STATE_FILE = BASE / "exit_state.json"

DEFAULT_LADDER = [
    {"multiple": 2.5, "pct": 25},   # 2-3x tier: sell 25%
    {"multiple": 5.0, "pct": 25},   # 5x tier: sell 25%
    {"multiple": 10.0, "pct": 20},  # optional runner tier: 20%
    # remainder (30%) rides trailing stop
]

def _load():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {"positions": {}}
    return {"positions": {}}

def _save(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def open_position(mint, entry_price, size_usd, stop_pct=0.30, ladder=None, mode="paper", capital_usd=None):
    """Create a tracked position. Returns position dict."""
    state = _load()
    ladder = ladder or DEFAULT_LADDER
    stop_price = entry_price * (1 - stop_pct)
    pos = {
        "mint": mint,
        "entry_price": entry_price,
        "size_usd": size_usd,
        "stop_pct": stop_pct,
        "stop_price": stop_price,
        "current_stop": stop_price,
        "ladder": ladder,
        "ladder_idx": 0,
        "remaining_pct": 100.0,
        "realized_pnl_usd": 0.0,
        "opened_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": mode,
        "status": "open",
        "capital_usd": capital_usd,
        "history": [],
    }
    state["positions"][mint] = pos
    _save(state)
    return pos

def get_positions(state=None):
    state = _load() if state is None else state
    return state.get("positions", {})

def check_position(pos, current_price):
    """Evaluate ladder + stop for one position at current_price.
    Returns list of fill dicts (possibly empty). mutates pos in place (fill applied)."""
    fills = []
    if pos.get("status") != "open":
        return fills
    entry = float(pos["entry_price"])
    mult = current_price / entry if entry else 0
    remaining = float(pos["remaining_pct"])

    # Trailing stop: move to breakeven at +50-80%, then trail never widening.
    # Breakeven trigger at +65% (midpoint of 50-80).
    if mult >= 1.65:
        breakeven = entry
        if pos["current_stop"] < breakeven:
            pos["current_stop"] = breakeven
    # Trail: keep stop at 30% below peak (peak approximated by current_price high so far)
    peak = max(float(pos.get("peak_price", entry)), current_price)
    pos["peak_price"] = peak
    if mult >= 1.65:
        trail_stop = peak * 0.70  # 30% trailing from peak
        if trail_stop > pos["current_stop"]:
            pos["current_stop"] = trail_stop

    # Stop hit (initial or trailed) -> exit remainder
    if current_price <= float(pos["current_stop"]):
        # full exit of remaining_pct
        qty_usd = float(pos["size_usd"]) * (remaining / 100.0)
        # pnl on remaining: (current/entry - 1) * qty_usd
        pnl = (mult - 1) * qty_usd
        fill = {
            "mint": pos["mint"], "kind": "stop",
            "price": current_price, "multiple": mult,
            "pct_sold": remaining, "pnl_usd": round(pnl, 2),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": pos.get("mode", "paper"),
        }
        fills.append(fill)
        pos["realized_pnl_usd"] = round(float(pos["realized_pnl_usd"]) + pnl, 2)
        pos["remaining_pct"] = 0.0
        pos["status"] = "closed"
        pos["history"].append(fill)
        return fills

    # Ladder fills
    ladder = pos["ladder"]
    idx = int(pos["ladder_idx"])
    while idx < len(ladder):
        rung = ladder[idx]
        if mult >= float(rung["multiple"]) and remaining > 0:
            pct = min(float(rung["pct"]), remaining)
            qty_usd = float(pos["size_usd"]) * (pct / 100.0)
            pnl = (mult - 1) * qty_usd
            fill = {
                "mint": pos["mint"], "kind": "ladder",
                "rung_multiple": rung["multiple"],
                "price": current_price, "multiple": mult,
                "pct_sold": pct, "pnl_usd": round(pnl, 2),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "mode": pos.get("mode", "paper"),
            }
            fills.append(fill)
            pos["realized_pnl_usd"] = round(float(pos["realized_pnl_usd"]) + pnl, 2)
            pos["remaining_pct"] = round(remaining - pct, 2)
            remaining = pos["remaining_pct"]
            pos["history"].append(fill)
            idx += 1
            pos["ladder_idx"] = idx
        else:
            break

    if pos["remaining_pct"] <= 0:
        pos["status"] = "closed"

    return fills

def update_all(price_map):
    """price_map: {mint: current_price}. Returns list of fills."""
    state = _load()
    all_fills = []
    for mint, pos in state.get("positions", {}).items():
        if pos.get("status") != "open":
            continue
        price = price_map.get(mint)
        if price is None:
            continue
        fills = check_position(pos, float(price))
        all_fills.extend(fills)
    _save(state)
    return all_fills
