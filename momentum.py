#!/usr/bin/env python3
"""
Momentum - confirms uptrend for tokens that passed Rug Check.
Checks all 4 criteria. Primary source is Birdeye (user-supplied endpoints);
DexScreener / GeckoTerminal remain as fallback on Birdeye errors or nulls.

Criteria (all must pass):
  1. 15m volume rising with price
  2. Higher highs and higher lows (HH/HL structure)
  3. Buy volume > sell volume over recent window (buy/sell ratio > 1)
  4. New unique buyers increasing

Birdeye endpoints (base https://public-api.birdeye.so, header X-API-KEY
from BIRDEYE_API_KEY in .env, chain via x-chain: solana):
  - GET /defi/v3/token/market-data?address=<token>  (current price, fdv, mcap, holders, liquidity)
  - GET /defi/history_price?address=<token>&address_type=token&type=15m&time_from=&time_to=
        (15m price series; Birdeye requires the full param set, address alone 400s)
  - GET /defi/ohlcv?address=<token>&type=15m&time_from=&time_to=  (15m OHLCV candles for checks 1+2)
  - GET /defi/v3/token/txs?address=<token>&limit=&tx_type=swap
        (trades feed for checks 3+4; user-named /defi/v3/token/trades 404s, /txs is the live route)

Fallbacks (unchanged):
  - DexScreener: https://api.dexscreener.com/latest/dex/tokens/{mint}
  - GeckoTerminal OHLCV minute/hour by pool

Read-only. No trading.
"""
import json, math, os, sys, time
from pathlib import Path

import requests

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
import rpc_config

H = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

GECKO_OHLCV_MIN = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute?aggregate=15&limit={limit}"
DEX_API = "https://api.dexscreener.com/latest/dex/tokens/{mint}"

BIRDEYE_BASE = "https://public-api.birdeye.so"
BIRDEYE_OHLCV_WINDOW_MIN = 6 * 60  # 6h of 15m candles (24 buckets)
BIRDEYE_TRADES_LIMIT = 100


def _get(url, timeout=12):
    r = requests.get(url, headers=H, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ---------------- Birdeye (primary) ----------------

def _birdeye_headers():
    key = os.environ.get("BIRDEYE_API_KEY") or ""
    return {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "X-API-KEY": key,
        "x-chain": "solana",
    }


def _birdeye_get(path, params, timeout=15, retries=2):
    """GET a Birdeye path with params. Returns the `data` dict or None on any
    failure (including success=false). Retries once on 429. Never logs the key."""
    url = BIRDEYE_BASE + path
    headers = _birdeye_headers()
    if not headers["X-API-KEY"]:
        return None
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 429 and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code != 200:
                return None
            j = r.json()
            if not j.get("success"):
                return None
            return j.get("data") or {}
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
                continue
            return None
    return None


def fetch_market_data(mint):
    """Current metrics from Birdeye market-data. {} on error/null."""
    data = _birdeye_get("/defi/v3/token/market-data", {"address": mint})
    return data or {}


def fetch_history_price(mint, window_min=BIRDEYE_OHLCV_WINDOW_MIN):
    """15m price series from Birdeye history_price.
    Returns [{'unixTime': int, 'value': float}] sorted by time, [] on error."""
    now = int(time.time())
    data = _birdeye_get("/defi/history_price", {
        "address": mint,
        "address_type": "token",
        "type": "15m",
        "time_from": now - window_min * 60,
        "time_to": now,
    })
    if not data:
        return []
    items = data.get("items") or []
    out = []
    for it in items:
        try:
            out.append({"unixTime": int(it.get("unixTime") or 0), "value": float(it.get("value") or 0)})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["unixTime"])
    return out


def fetch_birdeye_ohlcv(mint, window_min=BIRDEYE_OHLCV_WINDOW_MIN):
    """15m OHLCV candles from Birdeye /defi/ohlcv in the shared candle shape
    [{ts, open, high, low, close, volume}]. [] on error."""
    now = int(time.time())
    data = _birdeye_get("/defi/ohlcv", {
        "address": mint,
        "type": "15m",
        "time_from": now - window_min * 60,
        "time_to": now,
    })
    if not data:
        return []
    items = data.get("items") or []
    candles = []
    for it in items:
        try:
            candles.append({
                "ts": int(it.get("unixTime") or 0),
                "open": float(it.get("o") or 0),
                "high": float(it.get("h") or 0),
                "low": float(it.get("l") or 0),
                "close": float(it.get("c") or 0),
                "volume": float(it.get("v") or 0),
            })
        except (TypeError, ValueError):
            continue
    candles.sort(key=lambda c: c["ts"])
    return candles


def fetch_token_trades(mint, limit=BIRDEYE_TRADES_LIMIT):
    """Recent trades from Birdeye /defi/v3/token/txs (the live route; the
    user-named /defi/v3/token/trades 404s). Returns [{'side','owner',
    'block_unix_time','volume_usd'}], [] on error."""
    data = _birdeye_get("/defi/v3/token/txs", {
        "address": mint, "limit": limit, "tx_type": "swap",
    })
    if not data:
        return []
    items = data.get("items") or []
    out = []
    for it in items:
        try:
            out.append({
                "side": (it.get("side") or it.get("tx_type") or "").lower(),
                "owner": it.get("owner") or "",
                "block_unix_time": int(it.get("block_unix_time") or 0),
                "volume_usd": float(it.get("volume_usd") or 0),
            })
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda t: t["block_unix_time"])
    return out


# ---------------- Fallbacks: DexScreener / GeckoTerminal ----------------

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


# ---------------- Criterion checks (source-agnostic) ----------------

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


def check_hh_hl(candles, price_series=None):
    """Criterion 2: higher highs and higher lows.
    Uses OHLCV high/low when available; falls back to the history_price
    point series (value = close proxy) when candles are missing."""
    if len(candles) >= 4:
        first_half = candles[: len(candles)//2]
        second_half = candles[len(candles)//2:]
        hh = max(c["high"] for c in second_half) > max(c["high"] for c in first_half)
        hl = min(c["low"] for c in second_half) > min(c["low"] for c in first_half)
        detail = f"hh={hh} hl={hl}"
        return (hh and hl), detail
    if price_series and len(price_series) >= 4:
        vals = [p["value"] for p in price_series]
        first_half = vals[: len(vals)//2]
        second_half = vals[len(vals)//2:]
        hh = max(second_half) > max(first_half)
        hl = min(second_half) > min(first_half)
        detail = f"hh={hh} hl={hl} (history_price points)"
        return (hh and hl), detail
    return None, "insufficient candles"


def check_buy_sell_ratio(mint):
    """Criterion 3: buy volume > sell volume.
    Primary: Birdeye trades (side counts + volume_usd sums).
    Fallback: DexScreener txn counts."""
    trades = fetch_token_trades(mint)
    if trades:
        buy_vol = sum(t["volume_usd"] for t in trades if t["side"] == "buy")
        sell_vol = sum(t["volume_usd"] for t in trades if t["side"] == "sell")
        buys = sum(1 for t in trades if t["side"] == "buy")
        sells = sum(1 for t in trades if t["side"] == "sell")
        if buys + sells == 0:
            return None, "no buy/sell trades"
        vol_ratio = buy_vol / sell_vol if sell_vol > 0 else float("inf")
        cnt_ratio = buys / max(sells, 1)
        passed = (vol_ratio > 1) or (cnt_ratio > 1)
        # volume ratio is the stricter read; require it when sell volume exists
        passed = vol_ratio > 1 if sell_vol > 0 else cnt_ratio > 1
        detail = (f"birdeye trades={len(trades)} buys={buys} sells={sells} "
                  f"buy_vol_usd={buy_vol:.2f} sell_vol_usd={sell_vol:.2f} "
                  f"vol_ratio={vol_ratio:.2f} cnt_ratio={cnt_ratio:.2f}")
        return passed, detail
    # Fallback: DexScreener
    info = fetch_dex_txns(mint)
    txns = info.get("txns") or {}
    # prefer h1, then h6
    window = txns.get("h1") or txns.get("h6") or {}
    buys = window.get("buys") or 0
    sells = window.get("sells") or 0
    if buys == 0 and sells == 0:
        return None, "no txn data (birdeye + dexscreener both empty)"
    ratio = buys / max(sells, 1)
    passed = ratio > 1
    detail = f"dexscreener fallback buys={buys} sells={sells} ratio={ratio:.2f}"
    return passed, detail


def _unique_buyers_in_window(trades, lo, hi):
    return {t["owner"] for t in trades if t["side"] == "buy" and t["owner"] and lo <= t["block_unix_time"] <= hi}


def check_new_buyers(mint, dex_info=None, trades=None):
    """Criterion 4: new unique buyers increasing.
    Primary: Birdeye trades: unique buy-side owners in the recent half of the
    trade list vs the earlier half.
    Fallback: DexScreener proxy (price up + buys present)."""
    if trades is None:
        trades = fetch_token_trades(mint)
    if trades:
        buy_times = sorted(t["block_unix_time"] for t in trades if t["side"] == "buy" and t["owner"])
        total_unique = len(set(t["owner"] for t in trades if t["side"] == "buy" and t["owner"]))
        if len(buy_times) >= 4 and total_unique >= 2:
            mid = buy_times[len(buy_times) // 2]
            earlier = _unique_buyers_in_window(trades, 0, mid)
            recent = _unique_buyers_in_window(trades, mid + 1, 10**15)
            passed = len(recent) > len(earlier)
            detail = (f"birdeye unique_buyers recent={len(recent)} earlier={len(earlier)} "
                      f"total={total_unique} trades={len(trades)}")
            return passed, detail
        return None, f"birdeye trades too few for new-buyer read (trades={len(trades)}, unique_buyers={total_unique})"
    # Fallback: DexScreener proxy
    info = dex_info if isinstance(dex_info, dict) else fetch_dex_txns(mint)
    if not info:
        return None, "no buyer data (birdeye + dexscreener both empty)"
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


def get_rpc_slot():
    """Current slot via Helius RPC (used for staleness checks on pool data)."""
    try:
        return rpc_config.rpc_call("getSlot", timeout=8)
    except Exception:
        return None


def assess(mint, pool_addr=None):
    """Run all 4 checks. Birdeye primary; DexScreener/GeckoTerminal fallback.
    Returns dict with passed(bool) and per-criterion results.
    pool_addr is optional: only used by the GeckoTerminal fallback."""
    slot = get_rpc_slot()

    birdeye_md = fetch_market_data(mint)
    price_series = fetch_history_price(mint)
    candles = fetch_birdeye_ohlcv(mint)
    candle_source = "birdeye_ohlcv"
    if len(candles) < 4 and pool_addr:
        gecko = fetch_ohlcv(pool_addr)
        if gecko:
            candles = gecko
            candle_source = "geckoterminal_fallback"

    # Trades fetched once, shared by checks 3 and 4 for efficiency
    trades = fetch_token_trades(mint)
    dex_info = {}

    c1, d1 = check_volume_rising_with_price(candles) if candles else (None, "no ohlcv (birdeye + gecko empty)")
    c2, d2 = check_hh_hl(candles, price_series) if (candles or price_series) else (None, "no ohlcv / history_price")

    if trades:
        # recompute check 3 from the already-fetched trades
        buy_vol = sum(t["volume_usd"] for t in trades if t["side"] == "buy")
        sell_vol = sum(t["volume_usd"] for t in trades if t["side"] == "sell")
        buys = sum(1 for t in trades if t["side"] == "buy")
        sells = sum(1 for t in trades if t["side"] == "sell")
        if buys + sells == 0:
            c3, d3 = None, "no buy/sell trades"
        else:
            vol_ratio = buy_vol / sell_vol if sell_vol > 0 else float("inf")
            cnt_ratio = buys / max(sells, 1)
            c3 = (vol_ratio > 1) if sell_vol > 0 else (cnt_ratio > 1)
            d3 = (f"birdeye trades={len(trades)} buys={buys} sells={sells} "
                  f"buy_vol_usd={buy_vol:.2f} sell_vol_usd={sell_vol:.2f} "
                  f"vol_ratio={vol_ratio:.2f} cnt_ratio={cnt_ratio:.2f}")
    else:
        c3, d3 = check_buy_sell_ratio(mint)  # will try birdeye again then dexscreener

    c4, d4 = check_new_buyers(mint, dex_info, trades)

    checks = {
        "volume_rising_with_price": {"pass": c1, "detail": d1},
        "higher_highs_lows": {"pass": c2, "detail": d2},
        "buy_sell_ratio_gt1": {"pass": c3, "detail": d3},
        "new_buyers_rising": {"pass": c4, "detail": d4},
    }
    # Require all four True. None (unknown) fails closed.
    passed = all(v["pass"] is True for v in checks.values())
    return {"passed": passed, "checks": checks, "candle_count": len(candles),
            "candle_source": candle_source,
            "slot": slot,
            "rpc": "Helius" if rpc_config.is_helius() else "public",
            "birdeye": {
                "price_usd": birdeye_md.get("price"),
                "fdv": birdeye_md.get("fdv"),
                "market_cap": birdeye_md.get("market_cap"),
                "holders": birdeye_md.get("holder"),
                "liquidity": birdeye_md.get("liquidity"),
                "history_price_points": len(price_series),
                "trades_fetched": len(trades),
            },
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: momentum.py <mint> [pool_addr]")
        sys.exit(1)
    result = assess(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(json.dumps(result, indent=2))
