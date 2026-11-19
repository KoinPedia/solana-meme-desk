#!/usr/bin/env python3
"""
Pump.Fun Scanner - GeckoTerminal new pools -> RugCheck + DexScreener screen.
Writes ranked watchlist to watchlist.json next to this file.
Read-only: no trading, no signing, no key access.
"""
import json, os, sys, time
from pathlib import Path

import requests

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
WATCHLIST = BASE / "watchlist.json"
GECKO_NEW = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
GECKO_POOL = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}?include=base_token,quote_token,dex"
BIRDEYE_NEW_LISTING = "https://public-api.birdeye.so/defi/v2/tokens/new_listing?sort_by=creation_time&sort_type=desc&meme_platform_enabled=true"
DEX_API = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
RUGCHECK_API = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
H = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

# Helius RPC (from .env HELIUS_RPC_URL, fallback public) for on-chain checks:
# token supply + mint/freeze authority cross-check against RugCheck report.
import rpc_config
from rpc_config import get_mint_authorities, get_token_supply, get_account_info, rpc_call, is_helius

LIMIT = int(os.environ.get("PUMP_SCAN_LIMIT", "20"))

BURN_ADDRESS = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

def fetch_onchain(mint):
    """On-chain rug checks via Helius RPC (fail-soft: returns {} on RPC error).

    Checks wired:
      1. getAccountInfo on mint -> mintAuthority/freezeAuthority from parsed info.
         (Spec wording 'owner must be null' refers to the parsed mintAuthority
         field being null, i.e. revoked; the account owner is the token program.)
      2. getTokenSupply on mint -> supply decimals/uiAmount.
      3. LP/burn: tries getTokenAccountsByOwner on the burn address for this mint
         (light, dataSlice-free token account list). LP mint is not known from
         the pool feed, so the authoritative LP-locked/unlocked signal stays
         with the RugCheck API (see fetch_rugcheck risks). getProgramAccounts
         on the token program is NOT used: too heavy / rate-limited on free tier.
    """
    out = {"source": "Helius" if is_helius() else "public RPC"}
    try:
        auth = get_mint_authorities(mint, timeout=8)
        supply = get_token_supply(mint, timeout=8)
        if auth is not None:
            mint_auth, freeze_auth = auth
            out["mintAuthority"] = mint_auth
            out["freezeAuthority"] = freeze_auth
            out["mintRevoked"] = mint_auth is None
            out["freezeRevoked"] = freeze_auth is None
        if supply is not None:
            # supply is the unwrapped value dict {amount, decimals, uiAmount, uiAmountString}
            if isinstance(supply, dict):
                out["supply"] = supply.get("uiAmountString")
                out["supplyRaw"] = supply.get("amount")
                out["decimals"] = supply.get("decimals")
        # LP/burn attempt: token accounts of burn address holding this mint
        # (proves some supply was burned; NOT proof LP is burned, LP mint differs)
        # NOTE: Helius/Pub RPC rejects {mint} filter with encoding jsonParsed on
        # getTokenAccountsByOwner (-32602), so we list by programId and filter
        # client-side. This can be 8k+ accounts on the dead burn address; kept
        # only as burn evidence. LP lock status stays with RugCheck API.
        try:
            burned = rpc_call(
                "getTokenAccountsByOwner",
                [BURN_ADDRESS, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
                timeout=10,
            )
            vals = rpc_config._value_of(burned) or []
            hits = 0
            for v in vals:
                info = ((v.get("account") or {}).get("data") or {}).get("parsed", {}).get("info") or {}
                if info.get("mint") == mint:
                    hits += 1
            out["burnedTokenAccounts"] = hits
        except Exception:
            out["burnedTokenAccounts"] = None
            out["lpSource"] = "RugCheck API fallback"
    except Exception:
        return {}
    return out

def fetch_lp_burn_check(mint):
    """LP/burn check: tries Helius getTokenAccountsByOwner on burn address.

    Returns dict with burn evidence + explicit fallback note. LP lock status
    itself comes from RugCheck risks (LP unlocked), not from this call,
    because the LP mint is not in the scanner feed.
    """
    onchain = fetch_onchain(mint)
    return {
        "burnedTokenAccounts": onchain.get("burnedTokenAccounts"),
        "lpSource": onchain.get("lpSource", "RugCheck API fallback"),
        "note": "getProgramAccounts skipped (heavy/rate-limited); RugCheck API is LP source",
    }

def slug(mint):
    return "solana_" + mint

def fetch_pump_ohlcv_new_buyers(mint):
    """New unique buyers proxy in last 15 min via PumpPortal/CoinGecko bars: not available free.
    Falls back to DexScreener txn buys h1 as proxy count. Returns int or None when unknown."""
    try:
        r = requests.get(DEX_API.format(mint=mint), headers=H, timeout=8)
        pairs = (r.json() or {}).get("pairs") or []
        if not pairs:
            return None
        txns = (pairs[0].get("txns") or {}).get("h1") or {}
        buys = txns.get("buys")
        return int(buys) if buys is not None else None
    except Exception:
        return None

def fetch_birdeye_new_listing():
    """Birdeye GET /defi/v2/tokens/new_listing with x-chain: solana and meme_platform_enabled=true.
    Requires X-API-KEY / X-SOL-API-KEY. Without a key returns 401. Raises on failure so caller falls back."""
    api_key = os.environ.get("BIRDEYE_API_KEY") or os.environ.get("X_SOL_API_KEY")
    headers = dict(H); headers["x-chain"] = "solana"
    if api_key:
        headers["X-API-KEY"] = api_key; headers["X-SOL-API-KEY"] = api_key
    r = requests.get(BIRDEYE_NEW_LISTING, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()

def fetch_new_pools():
    """Try Birdeye as primary (needs API key), fall back to GeckoTerminal free feed.
    Returns (pools, source_label) where source_label is 'Birdeye' or 'GeckoTerminal'."""
    try:
        j = fetch_birdeye_new_listing()
        items = ((j.get("data") or {}).get("items") or []) if isinstance(j, dict) else []
        if items is None:
            items = []
        if isinstance(j, dict) and isinstance(j.get("data"), list):
            items = j.get("data")
        pools = []
        for it in items:
            addr = it.get("address") or it.get("mint") or it.get("token_address") or ""
            pools.append({
                "id": "solana_" + addr, "attributes": {"name": it.get("name") or it.get("symbol") or addr[:8],
                    "reserve_in_usd": it.get("liquidity") or it.get("liquidity_usd"),
                    "base_token_price_usd": it.get("price") or it.get("price_usd")},
                "relationships": {"base_token": {"data": {"id": "solana_" + addr}}, "dex": {"data": {"id": it.get("exchange") or "birdeye"}}},
            })
        if pools:
            return pools, "Birdeye"
        # Birdeye reachable but empty: fall through to GeckoTerminal
    except Exception:
        pass
    # Fallback: GeckoTerminal (free, no key). Current behavior when Birdeye needs a key we do not have.
    for i in range(5):
        r = requests.get(GECKO_NEW, headers=H, timeout=15)
        if r.status_code == 429:
            time.sleep(5 * (i + 1))
            continue
        r.raise_for_status()
        return r.json().get("data", [])
    raise RuntimeError("geckoterminal 429 after retries")

def fetch_pool_detail(pool_addr):
    r = requests.get(GECKO_POOL.format(pool=pool_addr), headers=H, timeout=15)
    j = r.json()
    included = {i["id"]: i for i in j.get("included", [])}
    return j.get("data", {}), included

def fetch_dex(mint):
    try:
        r = requests.get(DEX_API.format(mint=mint), headers=H, timeout=10)
        pairs = r.json().get("pairs") or []
        if not pairs:
            return {}
        p = pairs[0]
        return {
            "priceUsd": p.get("priceUsd"),
            "liquidityUsd": (p.get("liquidity") or {}).get("usd"),
            "volume24h": (p.get("volume") or {}).get("h24"),
            "marketCap": p.get("marketCap") or p.get("fdv"),
            "pairCount": len(pairs),
            "url": p.get("url"),
        }
    except Exception:
        return {}

def fetch_rugcheck(mint):
    """RugCheck report for a mint.

    Primary: rugcheck_api.get_report() -> /v1/tokens/{mint}/report/summary
    (score_normalised, risks, lpLockedPct). Fallback: legacy
    /v1/tokens/{mint}/report scraping path already inside rugcheck_api.
    Returns the normalized dict; {} if both paths fail.

    Authoritative mint/freeze authority stays Helius (rpc_config) in the
    rug check stage; these values are only for scoring/display.
    """
    try:
        from rugcheck_api import get_report as _api_report
        out = _api_report(mint, timeout=10)
        if out:
            return out
    except Exception:
        pass
    # Legacy direct /report call as last resort (pre-API scraping path)
    try:
        r = requests.get(RUGCHECK_API.format(mint=mint), headers=H, timeout=10)
        j = r.json()
        token = j.get("token") or {}
        ext = j.get("token_extensions") or {}
        return {
            "score_normalised": j.get("score_normalised"),
            "risks": [x.get("name") for x in (j.get("risks") or [])],
            "lpLockedPct": j.get("lpLockedPct"),
            "totalHolders": j.get("totalHolders"),
            "mintAuthority": token.get("mintAuthority"),
            "freezeAuthority": token.get("freezeAuthority"),
            "creator": j.get("creator"),
            "creatorBalance": j.get("creatorBalance"),
            "topHolders": (j.get("topHolders") or [])[:10],
            "tokenProgram": j.get("tokenProgram"),
            "hasTransferHook": bool(ext.get("transferHook")),
            "hasPermanentDelegate": bool(ext.get("permanentDelegate")),
            "hasPausable": bool(ext.get("pausableConfig")),
            "hasNonTransferable": bool(ext.get("nonTransferable")),
        }
    except Exception:
        return {}

def risk_score(pool_attrs, base, dex, rug, onchain=None):
    """0-100 higher=riskier. Informational only, matches installed safety-check scale.
    onchain: optional fetch_onchain() dict; its mint/freeze authority reading is
    authoritative and overrides RugCheck when present (cross-check flags drift)."""
    onchain = onchain or {}
    score = 50
    flags, positives = [], []

    liq = dex.get("liquidityUsd") or pool_attrs.get("reserve_in_usd")
    if liq is not None:
        try:
            liq = float(liq)
        except (TypeError, ValueError):
            liq = None
    if liq is None:
        flags.append("unknown liquidity"); score += 10
    elif liq < 5000:
        flags.append(f"very low liquidity ${liq:,.0f}"); score += 30
    elif liq < 20000:
        flags.append(f"low liquidity ${liq:,.0f}"); score += 15
    else:
        positives.append(f"liquidity ${liq:,.0f}")

    # authorities: on-chain (Helius) wins over RugCheck when present
    oc_mint = onchain.get("mintAuthority", "__missing__")
    oc_freeze = onchain.get("freezeAuthority", "__missing__")
    if oc_mint != "__missing__":
        if oc_mint is None:
            positives.append("mint revoked (on-chain Helius)")
        else:
            flags.append("mint authority active (on-chain Helius)"); score += 20
        if rug.get("mintAuthority") is not None and rug.get("mintAuthority") != oc_mint:
            flags.append("mint authority drift: Helius vs RugCheck"); score += 10
    else:
        if rug.get("mintAuthority") is None and rug:
            positives.append("mint revoked")
        elif rug.get("mintAuthority"):
            flags.append("mint authority active"); score += 20

    if oc_freeze != "__missing__":
        if oc_freeze is None:
            positives.append("freeze revoked (on-chain Helius)")
        else:
            flags.append("freeze authority active (on-chain Helius)"); score += 20
        if rug.get("freezeAuthority") is not None and rug.get("freezeAuthority") != oc_freeze:
            flags.append("freeze authority drift: Helius vs RugCheck"); score += 10
    else:
        if rug.get("freezeAuthority") is None and rug:
            positives.append("freeze revoked")
        elif rug.get("freezeAuthority"):
            flags.append("freeze authority active"); score += 20

    # token-2022 dangers
    for k, label in (("hasTransferHook", "transfer hook"), ("hasPermanentDelegate", "permanent delegate"),
                     ("hasPausable", "pausable"), ("hasNonTransferable", "non-transferable")):
        if rug.get(k):
            flags.append(f"token2022 {label}"); score += 20

    # concentration from rugcheck top holders
    top = rug.get("topHolders") or []
    if top:
        pct = sum(x.get("pct", 0) for x in top[:10])
        if pct > 50:
            flags.append(f"top-heavy concentration {pct:.1f}%"); score += 25
        elif pct > 30:
            flags.append(f"top holders {pct:.1f}%"); score += 10
        else:
            positives.append(f"top10 {pct:.1f}%")
    else:
        flags.append("unknown concentration"); score += 10

    # rug risks
    for r in (rug.get("risks") or []):
        flags.append(f"rugcheck: {r}"); score += 8
    if rug.get("score_normalised") is not None and rug["score_normalised"] > 5000:
        flags.append("rugcheck high score"); score += 20

    # bundled buys: rugcheck flags large holder bundling
    # no dedicated field, rely on top-holder concentration + risks list

    # socials
    socials = [k for k in ("twitter", "telegram", "website") if base.get(k)]
    if len(socials) >= 1:
        positives.append(f"social: {','.join(socials)}")
    else:
        flags.append("missing socials"); score += 5

    score = max(0, min(100, score))
    verdict = "SAFE" if score <= 24 else "LOW RISK" if score <= 44 else "CAUTION" if score <= 64 else "HIGH RISK" if score <= 84 else "CRITICAL"
    if not dex and not rug:
        verdict = "UNKNOWN"
    return {"score": score, "verdict": verdict, "flags": flags, "positives": positives}

def main():
    print(f"fetching {LIMIT} newest solana pools...", flush=True)
    try:
        pools = fetch_new_pools()
    except Exception as e:
        print(f"geckoterminal fetch failed: {e}", file=sys.stderr)
        sys.exit(1)
    # scanner.py standalone: result may be (pools, source) or pools list
    if isinstance(pools, tuple) and len(pools) == 2:
        pools, _feed_src = pools
    else:
        _feed_src = "GeckoTerminal"
    print(f"feed={_feed_src}", flush=True)

    screened = []
    for p in pools[:LIMIT]:
        pool_addr = p["id"].removeprefix("solana_")
        attrs = p.get("attributes") or {}
        name = attrs.get("name", "?")
        print(f"screening {name} pool {pool_addr[:8]}...", flush=True)
        # use base_token id straight from new_pools relationships; detail call hits rate limits fast
        base_rel = ((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}
        mint = base_rel.get("id", "").removeprefix("solana_")
        dexrel = ((p.get("relationships") or {}).get("dex") or {}).get("data") or {}
        dexname = dexrel.get("id")
        if not mint:
            continue

        dex = fetch_dex(mint)
        rug = fetch_rugcheck(mint)
        onchain = fetch_onchain(mint)
        rs = risk_score(attrs, {}, dex, rug, onchain)

        entry = {
            "name": attrs.get("name"),
            "symbol": attrs.get("name", "").split("/")[0].strip(),
            "mint": mint,
            "pool": pool_addr,
            "dex": dexname,
            "priceUsd": dex.get("priceUsd") or attrs.get("base_token_price_usd"),
            "liquidityUsd": dex.get("liquidityUsd") or attrs.get("reserve_in_usd"),
            "volume24h": dex.get("volume24h"),
            "marketCap": dex.get("marketCap"),
            "dexUrl": dex.get("url"),
            "createdAt": attrs.get("pool_created_at"),
            "creator": rug.get("creator"),
            "totalHolders": rug.get("totalHolders"),
            "mintAuthority": rug.get("mintAuthority"),
            "freezeAuthority": rug.get("freezeAuthority"),
            "onchain": onchain or None,
            "tokenSupply": (onchain or {}).get("supply"),
            "lpBurn": fetch_lp_burn_check(mint) if False else None,  # avoid double RPC; burn via onchain.burnedTokenAccounts
            "rugcheck": rug,
            "safetyScore": rs["score"],
            "verdict": rs["verdict"],
            "flags": rs["flags"],
            "positives": rs["positives"],
            "scannedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        screened.append(entry)
        time.sleep(0.4)

    screened.sort(key=lambda x: x["safetyScore"])
    out = {"generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "count": len(screened), "tokens": screened}
    WATCHLIST.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {WATCHLIST} with {len(screened)} tokens")
    for t in screened[:5]:
        print(f"- {t['symbol']} [{t['verdict']} score {t['safetyScore']}] mint={t['mint']}")

if __name__ == "__main__":
    main()
