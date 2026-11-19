#!/usr/bin/env python3
"""
Momentum - confirms uptrend for tokens that passed Rug Check.
Checks all 4 criteria using DexScreener / GeckoTerminal price+volume series.

Criteria (all must pass):
  1. 15m volume rising with price
  2. Higher highs and higher lows (HH/HL structure)
  3. Buy volume > sell volume over recent window (buy/sell ratio > 1)
  4. New unique buyers increasing

Data sources:
  - DexScreener: https://api.dexscreener.com/latest/dex/tokens/{mint}
  - GeckoTerminal OHLCV: https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/hour?limit=...
  - GeckoTerminal 15m: https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute?limit=...

Read-only. No trading.
"""
import json, math, os, sys, time
from pathlib import Path

import requests

BASE = Path(__file__).parent
H = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

GECKO_OHLCV_MIN = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute?aggregate=15&limit={limit}"
DEX_API = "https://api.dexscreener.com/latest/dex/tokens/{mint}"


def _get(url, timeout=12):
    r = requests.get(url, headers=H, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_ohlcv(pool_addr, limit=20):
    """Return list of candles: [{ts, open, high, low, close, volume}]."""
    url = GECKO_OHLCV_MIN.format(pool=pool_addr, limit=limit)
    try:
        j = _get(url)
    except Exception:
        # fallback to hourly
        try:
            j = _get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool_addr}/ohlcv/hour?limit={limit}")
        except Exception:
            return []
    # GeckoTerminal ohlcv response shape varies; try common keys
    data = j.get("data", {}) or {}
    attrs = data.get("attributes", {}) or {}
    # Try ohlcv_lists
    lists = attrs.get("ohlcv_lists") or []
    if lists:
        raw = lists[0]
        candles = []
        for row in raw:
            # row: [ts, open, high, low, close, volume]
            if len(row) >= 6:
                candles.append({
                    "ts": row[0], "open": float(row[1]), "high": float(row[2]),
                    "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
                })
        return candles
    # Fallback: included ohlcv
    included = j.get("included", []) or []
    for item in included:
        a = item.get("attributes", {}) or {}
        lst = a.get("ohlcv_lists") or a.get("ohlcv_list") or []
        if lst:
            raw = lst[0] if lst and isinstance(lst[0], list) else lst
            candles = []
            for row in raw:
                if isinstance(row, (list, tuple)) and len(row) >= 6:
                    candles.append({
                        "ts": row[0], "open": float(row[1]), "high": float(row[2]),
                        "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
                    })
            return candles
    return []


def fetch_dex_txns(mint):
    """Get buy/sell txn counts from DexScreener."""
    try:
        j = _get(DEX_API.format(mint=mint), timeout=10)
        pairs = j.get("pairs") or []
        if not pairs:
            return {}
        p = pairs[0]
        txns = p.get("txns") or {}
        # txns: {"h1": {"buys": n, "sells": n}, "h6": {...}, "h24": {...}}
        return {
            "txns": txns,
            "priceUsd": p.get("priceUsd"),
            "volume": p.get("volume") or {},
            "priceChange": p.get("priceChange") or {},
        }
    except Exception:
        return {}


def check_volume_rising_with_price(candles):
    """Criterion 1: recent volume rising paired with price rising (not spike + collapse)."""
    if len(candles) < 4:
        return None, "insufficient candles"
    recent = candles[-3:]
    earlier = candles[-6:-3] if len(candles) >= 6 else candles[:-3]
    if not earlier:
        return None, "not enough history"
    def avg_vol(cs): return sum(c["volume"] for c in cs) / max(len(cs), 1)
    def price_move(cs): return (cs[-1]["close"] - cs[0]["open"]) if cs else 0
    vol_up = avg_vol(recent) > avg_vol(earlier)
    price_up = price_move(recent) > 0
    # reject spike-then-collapse: last close below first close of recent window
    collapse = recent[-1]["close"] < recent[0]["open"]
    passed = vol_up and price_up and not collapse
    detail = f"vol_recent={avg_vol(recent):.0f} vs vol_earlier={avg_vol(earlier):.0f}, price_up={price_up}, collapse={collapse}"
    return passed, detail


def check_hh_hl(candles):
    """Criterion 2: higher highs and higher lows."""
    if len(candles) < 4:
        return None, "insufficient candles"
    first_half = candles[: len(candles)//2]
    second_half = candles[len(candles)//2:]
    hh = max(c["high"] for c in second_half) > max(c["high"] for c in first_half)
    hl = min(c["low"] for c in second_half) > min(c["low"] for c in first_half)
    detail = f"hh={hh} hl={hl}"
    return (hh and hl), detail


def check_buy_sell_ratio(mint):
    """Criterion 3: buy volume > sell volume (buys > sells in txns)."""
    info = fetch_dex_txns(mint)
    txns = info.get("txns") or {}
    # prefer h1, then h6
    window = txns.get("h1") or txns.get("h6") or {}
    buys = window.get("buys") or 0
    sells = window.get("sells") or 0
    if buys == 0 and sells == 0:
        return None, "no txn data"
    ratio = buys / max(sells, 1)
    passed = ratio > 1
    detail = f"buys={buys} sells={sells} ratio={ratio:.2f}"
    return passed, detail


def check_new_buyers(dex_info):
    """Criterion 4: new unique buyers increasing.
    Proxy: txns + volume trend, and buyers in h1 > 0 with positive priceChange h1.
    Full unique-wallet data requires paid source; document proxy used."""
    info = dex_info if isinstance(dex_info, dict) else fetch_dex_txns("")
    # Use priceChange.h1 > 0 and txns.h1.buys > 0 as proxy signal
    pc = (info.get("priceChange") or {}) if info else {}
    txns = (info.get("txns") or {}) if info else {}
    h1_buys = ((txns.get("h1") or {}).get("buys") or 0)
    h1_change = pc.get("h1")
    if h1_buys == 0:
        return None, "no recent buy txns"
    if h1_change is None:
        return None, "no price change data"
    passed = float(h1_change) > 0 and h1_buys > 0
    detail = f"h1_buys={h1_buys} h1_change={h1_change}% (proxy: price up + buys present)"
    return passed, detail


def assess(mint, pool_addr):
    """Run all 4 checks. Returns dict with passed(bool) and per-criterion results."""
    candles = fetch_ohlcv(pool_addr) if pool_addr else []
    dex_info = fetch_dex_txns(mint)

    c1, d1 = check_volume_rising_with_price(candles) if candles else (None, "no ohlcv")
    c2, d2 = check_hh_hl(candles) if candles else (None, "no ohlcv")
    c3, d3 = check_buy_sell_ratio(mint)
    c4, d4 = check_new_buyers(dex_info)

    checks = {
        "volume_rising_with_price": {"pass": c1, "detail": d1},
        "higher_highs_lows": {"pass": c2, "detail": d2},
        "buy_sell_ratio_gt1": {"pass": c3, "detail": d3},
        "new_buyers_rising": {"pass": c4, "detail": d4},
    }
    # Require all four True. None (unknown) fails closed.
    passed = all(v["pass"] is True for v in checks.values())
    return {"passed": passed, "checks": checks, "candle_count": len(candles), "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: momentum.py <mint> <pool_addr>")
        sys.exit(1)
    result = assess(sys.argv[1], sys.argv[2])
    print(json.dumps(result, indent=2))
