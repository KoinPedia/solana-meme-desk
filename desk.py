#!/usr/bin/env python3
"""
Desk coordinator - 4-agent Solana memecoin desk.
Scanner -> Rug Check -> Momentum -> Exit, with coordinator enforcement.

Run modes:
  paper (default): all trades are paper fills, logged to desk_log.csv and paper_state.json
  live: still requires per-trade confirmation flag file confirmations/confirm_<mint>.json.
        Without flag, trade is skipped (logged as GATED). This is the hard gate.

Coordinator owns:
  - 24h run window (start time + 24h, then stops scanning)
  - 15 minute scanner interval
  - one larger position per day flag (persisted in coordinator_state.json)
  - reinvestment: realized paper profits added back to trading capital

Logs every handoff, decision and paper fill to desk_log.csv with timestamps.
Paper fills also logged to trades_paper.csv.

Floors (Scanner):
  liquidity >= $5,000, holders >= 50, 24h volume >= $10,000

Rug Check hard kills:
  mint authority active, freeze authority active, top-10 > 30%,
  LP unlocked (rugcheck risks contains LP unlocked/lock), token-2022 dangers.

Momentum: all 4 criteria must pass (see momentum.py).
Position sizing: regular = 1.5% of capital, stop 30%.
  Larger position = 5% of capital, once per day max.
  size = risk_amount / stop_pct

No real trades in paper mode. No SOL spent here; live mode calls trade.py
which itself enforces the confirmation flag.
"""
import argparse, csv, json, os, sys, time, subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
import momentum as mom
import exit as exit_mod
import scanner as scanner_mod
from scanner import fetch_new_pools, fetch_dex, fetch_rugcheck, fetch_pool_detail

DESK_LOG = BASE / "desk_log.csv"
PAPER_TRADES = BASE / "trades_paper.csv"
COORD_STATE = BASE / "coordinator_state.json"
PAPER_STATE = BASE / "paper_state.json"
CONFIRM_DIR = BASE / "confirmations"
CONFIRM_DIR.mkdir(exist_ok=True)

SCAN_INTERVAL_SEC = 15 * 60  # 900
RUN_WINDOW_HOURS = 24
INITIAL_CAPITAL_USD = 500.0  # paper capital, user adjustable
REGULAR_RISK_PCT = 0.015  # 1.5% of capital
LARGER_RISK_PCT = 0.05    # 5% risk for best setup, once/day (sized at larger tier)
STOP_PCT = 0.30

H = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

LOG_FIELDS = ["timestamp", "agent", "event", "mint", "detail"]

def _now():
    return datetime.now(timezone.utc)

def _ts():
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")

def log(event_agent, event, mint="", detail=""):
    write_header = not DESK_LOG.exists()
    row = {"timestamp": _ts(), "agent": event_agent, "event": event, "mint": mint, "detail": str(detail)[:500]}
    with DESK_LOG.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow(row)
    print(f"[{row['timestamp']}] {event_agent} {event} {mint} {detail}", flush=True)

def _load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default

def _save_json(path, data):
    path.write_text(json.dumps(data, indent=2))

def load_coord():
    return _load_json(COORD_STATE, {
        "run_started_at": None,
        "run_ends_at": None,
        "larger_used_date_utc": None,
        "cycle_count": 0,
        "mode": "paper",
    })

def save_coord(c):
    _save_json(COORD_STATE, c)

def load_paper():
    return _load_json(PAPER_STATE, {
        "capital_usd": INITIAL_CAPITAL_USD,
        "starting_capital_usd": INITIAL_CAPITAL_USD,
        "realized_pnl_usd": 0.0,
        "open_count": 0,
    })

def save_paper(p):
    _save_json(PAPER_STATE, p)

# ---- Scanner stage ----
def scan_candidates(limit=20):
    log("Scanner", "scan_start", detail=f"limit={limit}")
    try:
        result = fetch_new_pools()
        if isinstance(result, tuple) and len(result) == 2:
            pools, feed_source = result
        else:
            pools = result
            feed_source = "GeckoTerminal"
    except Exception as e:
        log("Scanner", "scan_error", detail=str(e)[:200])
        return []
    log("Scanner", "feed", detail=f"source={feed_source} count={len(pools)}")
    candidates = []
    for p in pools[:limit]:
        pool_addr = p["id"].removeprefix("solana_")
        attrs = p.get("attributes") or {}
        base_rel = ((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}
        mint = base_rel.get("id", "").removeprefix("solana_")
        if not mint:
            continue
        dex = fetch_dex(mint)
        liq = dex.get("liquidityUsd") or attrs.get("reserve_in_usd")
        vol24 = dex.get("volume24h")
        try:
            liq_f = float(liq) if liq else 0
            vol_f = float(vol24) if vol24 else 0
        except Exception:
            liq_f, vol_f = 0, 0

        rug = fetch_rugcheck(mint)
        holders = rug.get("totalHolders")
        try:
            holders_i = int(holders) if holders is not None else 0
        except Exception:
            holders_i = 0

        _top = (rug.get("topHolders") or [])
        _top10 = round(sum(float(x.get("pct", 0)) for x in _top[:10]), 2) if _top else None
        try:
            from scanner import fetch_pump_ohlcv_new_buyers as _nb
            _new_buyers = _nb(mint)
        except Exception:
            _new_buyers = None
        cand = {
            "mint": mint, "pool": pool_addr, "name": attrs.get("name"),
            "liquidityUsd": liq_f, "volume24h": vol_f,
            "holders": holders_i, "priceUsd": dex.get("priceUsd"),
            "top10_pct": _top10, "new_buyers_15m": _new_buyers,
            "dex": dex, "rug": rug, "attrs": attrs,
            "detected_at": _ts(),
        }
        # Floors
        if liq_f < 5000 or holders_i < 50 or vol_f < 10000:
            log("Scanner", "floor_drop", mint,
                f"liq=${liq_f:,.0f} holders={holders_i} vol24=${vol_f:,.0f}")
            continue
        log("Scanner", "candidate_pass", mint,
            f"feed={feed_source} liq=${liq_f:,.0f} holders={holders_i} top10={cand['top10_pct']}% new_buyers_15m={cand['new_buyers_15m']} vol24=${vol_f:,.0f}")
        candidates.append(cand)
        time.sleep(0.3)
    return candidates

# ---- Rug Check stage ----
def rugcheck(cand):
    mint = cand["mint"]
    rug = cand["rug"] or {}
    kills = []
    evidence = {}
    # Helius on-chain mint/freeze authorities are AUTHORITATIVE. RugCheck
    # supplies score_normalised, risks[] and lpLockedPct from the API only.
    ma, fa = None, None
    authority_source = "rugcheck"
    try:
        import rpc_config
        auth = rpc_config.get_mint_authorities(mint, timeout=8)
        if auth is not None:
            ma, fa = auth
            authority_source = "helius_onchain"
    except Exception:
        pass
    if authority_source == "rugcheck":
        ma = rug.get("mintAuthority")
        fa = rug.get("freezeAuthority")
    evidence["mint_authority"] = ma
    evidence["freeze_authority"] = fa
    evidence["authority_source"] = authority_source
    evidence["rugcheck_source"] = rug.get("_source", "unknown")
    evidence["rugcheck_auth"] = rug.get("_auth_mode", "unknown")
    evidence["rugcheck_score"] = rug.get("score_normalised")
    evidence["rugcheck_score_raw"] = rug.get("score")
    evidence["lpLockedPct"] = rug.get("lpLockedPct")
    if ma:
        kills.append("mint authority active")
    if fa:
        kills.append("freeze authority active")
    # top-10 concentration
    top = rug.get("topHolders") or []
    top10 = sum(float(x.get("pct", 0)) for x in top[:10]) if top else None
    evidence["top10_pct"] = round(top10, 2) if top10 is not None else None
    if top10 is not None and top10 > 30:
        kills.append(f"top10 {top10:.1f}% > 30%")
    # LP unlocked
    risks = [str(x).lower() for x in (rug.get("risks") or [])]
    lp_risk = any(("lp" in r and ("unlocked" in r or "lock" in r)) or "lp unlocked" in r for r in risks)
    evidence["lp_unlocked"] = lp_risk
    evidence["risks"] = rug.get("risks") or []
    if lp_risk:
        kills.append("LP unlocked")
    # token2022 dangers
    for k in ("hasTransferHook", "hasPermanentDelegate", "hasPausable", "hasNonTransferable"):
        if rug.get(k):
            kills.append(f"token2022 {k}")
    evidence["rugcheck_score"] = rug.get("score_normalised")
    evidence["holders"] = rug.get("totalHolders")
    # Soft warning: top-10 20-30% elevated (pass but flag in log/handoff)
    if top10 is not None and 20 <= top10 <= 30:
        evidence["soft_warning"] = f"top10 {top10:.1f}% elevated (20-30%)"
    # Token age < 1h AND no known team: needs created_at + team info. Neither available free without key.
    # rugcheck creator is a deployment tx, not a team. Treat as: no socials/team via pool attrs, age from attrs pool_created_at.
    _attrs = cand.get("attrs") or {}
    _created = _attrs.get("pool_created_at")
    _age_h = None
    if _created:
        try:
            from datetime import datetime, timezone as _tz
            _ts_created = datetime.fromisoformat(str(_created).replace("Z", "+00:00"))
            _age_h = (_now() - _ts_created).total_seconds() / 3600
        except Exception:
            _age_h = None
    # Known team heuristic: socials present (twitter/telegram/website) in attrs or rug creator balance nonzero + risks none.
    # We have no team registry, so consider socials missing => no known team.
    _socials = [k for k in ("twitter", "telegram", "website") if _attrs.get(k)]
    _has_team = len(_socials) > 0
    evidence["token_age_h"] = round(_age_h, 2) if _age_h is not None else None
    evidence["has_known_team"] = _has_team

    # Hard kill: age < 1h AND no known team
    if _age_h is not None and _age_h < 1 and not _has_team:
        kills.append("RUGGABLE, DO NOT PASS: age < 1h and no known team")

    if kills:
        log("RugCheck", "RUGGABLE_DROP", mint, "; ".join(kills))
        return False, {"verdict": "RUGGABLE", "kills": kills, "evidence": evidence}
    log("RugCheck", "CLEAN_PASS", mint, f"mint_status={'REVOKED' if not ma else 'ACTIVE'} freeze_status={'REVOKED' if not fa else 'ACTIVE'} lp_lock={'LOCKED' if not lp_risk else 'UNLOCKED'} top10={top10}% age_unknown team_unknown")
    return True, {"verdict": "CLEAN", "evidence": evidence}

# ---- Momentum stage ----
def momentum_check(cand):
    mint = cand["mint"]
    pool = cand["pool"]
    result = mom.assess(mint, pool)
    if result["passed"]:
        log("Momentum", "uptrend_confirmed", mint, json.dumps(result["checks"])[:300])
    else:
        log("Momentum", "uptrend_fail", mint, json.dumps(result["checks"])[:300])
    return result["passed"], result

# ---- Exit update ----
def update_exits():
    positions = exit_mod.get_positions()
    open_mints = [m for m, p in positions.items() if p.get("status") == "open"]
    if not open_mints:
        return []
    price_map = {}
    for mint in open_mints:
        dex = fetch_dex(mint)
        price = dex.get("priceUsd")
        if price:
            try:
                price_map[mint] = float(price)
            except Exception:
                pass
        time.sleep(0.2)
    if not price_map:
        return []
    fills = exit_mod.update_all(price_map)
    for fill in fills:
        log("Exit", f"fill_{fill['kind']}", fill["mint"],
            f"mult={fill.get('multiple')} pnl_usd={fill.get('pnl_usd')} pct_sold={fill.get('pct_sold')}")
        if fill.get("mode") == "paper":
            # log to paper trades
            write_header = not PAPER_TRADES.exists()
            with PAPER_TRADES.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["timestamp", "mint", "kind", "multiple", "pct_sold", "pnl_usd", "mode"])
                if write_header:
                    w.writeheader()
                w.writerow({"timestamp": fill["timestamp"], "mint": fill["mint"], "kind": fill["kind"],
                            "multiple": fill.get("multiple"), "pct_sold": fill.get("pct_sold"),
                            "pnl_usd": fill.get("pnl_usd"), "mode": "paper"})
            # reinvest
            paper = load_paper()
            pnl = float(fill.get("pnl_usd") or 0)
            paper["realized_pnl_usd"] = round(float(paper["realized_pnl_usd"]) + pnl, 2)
            paper["capital_usd"] = round(float(paper["capital_usd"]) + max(pnl, 0), 2)
            save_paper(paper)
            log("Coordinator", "reinvest", fill["mint"], f"capital now ${paper['capital_usd']:.2f} pnl {pnl:+.2f}")
    return fills

# ---- Trade execution (paper or gated live) ----
def open_trade(cand, momentum_result, coord, paper, mode):
    mint = cand["mint"]
    price = float(cand["priceUsd"] or 0) if cand.get("priceUsd") else 0
    if price <= 0:
        log("Momentum", "no_price_skip", mint)
        return
    capital = float(paper["capital_usd"])
    today = _now().strftime("%Y-%m-%d")
    best_available = coord.get("larger_used_date_utc") != today
    # Use larger tier only if momentum is strong (all 4 pass) and flag available; desk design says first strong setup of day uses it.
    use_larger = best_available
    risk_pct = LARGER_RISK_PCT if use_larger else REGULAR_RISK_PCT
    risk_amount = capital * risk_pct
    # size = risk / stop_pct  (so max loss approx = risk amount)
    size_usd = risk_amount / STOP_PCT
    if cap := capital * 0.10:  # never put more than 10% of capital in one position
        size_usd = min(size_usd, cap)

    if mode == "live":
        flag = CONFIRM_DIR / f"confirm_{mint}.json"
        if not flag.exists():
            log("Coordinator", "GATED_skip", mint, "live mode: no confirmation flag, skipping")
            return
        # live: call trade.py which re-verifies flag, logs to trades.csv
        sol_estimate = size_usd / 150.0  # rough SOL price placeholder for dead path only
        log("Coordinator", "live_attempt", mint, f"size_usd={size_usd:.2f} flag present")
        res = subprocess.run([sys.executable, str(BASE / "trade.py"), mint, str(sol_estimate), "10", "buy"],
                             capture_output=True, text=True, timeout=30)
        log("Coordinator", "live_result", mint, f"rc={res.returncode} {res.stdout[:200]} {res.stderr[:200]}")
        # In live, Exit tracks real position; still open paper-side tracking for ladder
        exit_mod.open_position(mint, price, size_usd, stop_pct=STOP_PCT, mode="live")
        return

    # paper
    tier = "larger" if use_larger else "regular"
    exit_mod.open_position(mint, price, size_usd, stop_pct=STOP_PCT, mode="paper")
    if use_larger:
        coord["larger_used_date_utc"] = today
        save_coord(coord)
    log("Momentum", "paper_open", mint,
        f"tier={tier} size_usd=${size_usd:.2f} risk_pct={risk_pct} entry={price} stop={price*(1-STOP_PCT):.8f}")
    # paper fill record
    write_header = not PAPER_TRADES.exists()
    with PAPER_TRADES.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "mint", "kind", "multiple", "pct_sold", "pnl_usd", "mode"])
        if write_header:
            w.writeheader()
        w.writerow({"timestamp": _ts(), "mint": mint, "kind": "open", "multiple": 1.0, "pct_sold": 0,
                    "pnl_usd": 0, "mode": "paper"})
    paper["open_count"] = int(paper.get("open_count", 0)) + 1
    save_paper(paper)

# ---- One cycle ----
def run_cycle(mode, limit=20):
    # Hot-reload scanner modules so Birdeye/fallback + new metrics are picked up without restart
    try:
        import importlib
        import scanner as _sc
        importlib.reload(_sc)
        globals()["fetch_new_pools"] = _sc.fetch_new_pools
        globals()["fetch_dex"] = _sc.fetch_dex
        globals()["fetch_rugcheck"] = _sc.fetch_rugcheck
        globals()["fetch_pool_detail"] = _sc.fetch_pool_detail
        globals()["scanner_mod"] = _sc
    except Exception:
        pass
    coord = load_coord()
    paper = load_paper()
    coord["cycle_count"] = int(coord.get("cycle_count", 0)) + 1
    save_coord(coord)
    log("Coordinator", "cycle_start", detail=f"cycle={coord['cycle_count']} mode={mode} capital=${paper['capital_usd']:.2f}")
    candidates = scan_candidates(limit=limit)
    for cand in candidates:
        cleared, verdict = rugcheck(cand)
        if not cleared:
            continue
        _sw = (verdict.get("evidence") or {}).get("soft_warning")
        if _sw:
            log("RugCheck", "SOFT_WARNING", cand["mint"], _sw + " momentum will still evaluate")
        passed, mom_res = momentum_check(cand)
        if not passed:
            continue
        open_trade(cand, mom_res, coord, paper, mode)
        # refresh coord/paper after open_trade may have updated them
        coord = load_coord(); paper = load_paper()
    fills = update_exits()
    log("Coordinator", "cycle_end", detail=f"open fills this cycle: {len(fills)}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    ap.add_argument("--hours", type=float, default=RUN_WINDOW_HOURS)
    ap.add_argument("--interval", type=float, default=SCAN_INTERVAL_SEC, help="seconds between cycles")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    coord = load_coord()
    if not coord.get("run_started_at"):
        now = _now()
        coord["run_started_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        coord["run_ends_at"] = (now + timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        coord["mode"] = args.mode
        save_coord(coord)
    ends = coord.get("run_ends_at")
    log("Coordinator", "run_start", detail=f"mode={args.mode} ends={ends} interval={args.interval}s")

    if args.once:
        run_cycle(args.mode, limit=args.limit)
        return

    end_time = _now() + timedelta(hours=args.hours)
    # if coord already has an end time in future, respect earliest
    try:
        coord_end = datetime.strptime(ends, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if coord_end < end_time:
            end_time = coord_end
    except Exception:
        pass

    while _now() < end_time:
        try:
            run_cycle(args.mode, limit=args.limit)
        except Exception as e:
            log("Coordinator", "cycle_error", detail=str(e)[:300])
        # sleep until next interval, but cap sleep at remaining time
        remaining = (end_time - _now()).total_seconds()
        if remaining <= 0:
            break
        sleep_for = min(args.interval, remaining)
        log("Coordinator", "sleep", detail=f"{sleep_for:.0f}s")
        time.sleep(sleep_for)
    log("Coordinator", "run_end", detail="24h window complete")

if __name__ == "__main__":
    main()
