#!/usr/bin/env python3
"""RugCheck API client for the desk.

Endpoint (verified): GET https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary
Returns JSON with score, score_normalised, risks[], lpLockedPct, totalHolders,
topHolders, token{ mintAuthority, freezeAuthority }, etc. Keyless works for
light use; when limits bite, we try the Flux RPC key from .env
(FLUX_RPC_URL ?key=...) as an X-API-KEY header or query param. A dedicated
RUGCHECK_API_KEY env var takes precedence over the Flux key if present.

Auth modes tried (in order):
  1. keyless (no auth)
  2. X-API-KEY header with the Flux key
  3. ?api_key=<flux_key> query param
  4. ?key=<flux_key> query param
The first mode returning HTTP 200 with a JSON body wins; the winning mode is
recorded in the returned dict as _auth_mode so scans can report it.

Failure: get_summary/get_report return {} (or the fallback dict) rather than
raising; scanner.fetch_rugcheck also falls back to the legacy /report
endpoint (the existing scraping path) when this module returns nothing.

Key safety: keys are read from env/.env via rpc_config and never printed.
"""
import os
import requests

try:
    import rpc_config  # noqa: F401  (ensures .env is loaded)
    from rpc_config import get_rugcheck_api_key
except Exception:  # pragma: no cover
    get_rugcheck_api_key = lambda: os.environ.get("RUGCHECK_API_KEY")  # noqa

BASE_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
LEGACY_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
H = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

LAST_AUTH_MODE = "unknown"


def _risks_to_names(risks):
    """API may return risks as strings or dicts ({name: ...}); normalize to names."""
    out = []
    for x in risks or []:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            out.append(x.get("name") or x.get("description") or str(x))
        else:
            out.append(str(x))
    return out


def _normalize(j, auth_mode):
    token = {}
    if isinstance(j, dict):
        token = j.get("token") or {}
        if not token and "mintAuthority" in j:
            token = j
    ext = (j or {}).get("token_extensions") or {}
    return {
        "score": (j or {}).get("score"),
        "score_normalised": (j or {}).get("score_normalised"),
        "risks": _risks_to_names((j or {}).get("risks") or []),
        "lpLockedPct": (j or {}).get("lpLockedPct"),
        "lpLocked": (j or {}).get("lpLocked"),
        "totalHolders": (j or {}).get("totalHolders"),
        "mintAuthority": token.get("mintAuthority"),
        "freezeAuthority": token.get("freezeAuthority"),
        "creator": (j or {}).get("creator"),
        "creatorBalance": (j or {}).get("creatorBalance"),
        "topHolders": ((j or {}).get("topHolders") or [])[:10],
        "tokenProgram": (j or {}).get("tokenProgram"),
        "hasTransferHook": bool(ext.get("transferHook")),
        "hasPermanentDelegate": bool(ext.get("permanentDelegate")),
        "hasPausable": bool(ext.get("pausableConfig")),
        "hasNonTransferable": bool(ext.get("nonTransferable")),
        "_source": "rugcheck_api_summary",
        "_auth_mode": auth_mode,
    }


def _attempt(mint, mode, api_key, timeout):
    url = BASE_URL.format(mint=mint)
    headers = dict(H)
    params = {}
    if mode == "x-apikey" and api_key:
        headers["X-API-KEY"] = api_key
    elif mode == "query_api_key" and api_key:
        params["api_key"] = api_key
    elif mode == "query_key" and api_key:
        params["key"] = api_key
    r = requests.get(url, headers=headers, params=params, timeout=timeout)
    return r


def get_summary(mint, timeout=10):
    """GET /report/summary for mint. Returns normalized dict; {} on failure."""
    global LAST_AUTH_MODE
    if not mint:
        return {}
    api_key = None
    try:
        api_key = get_rugcheck_api_key()
    except Exception:
        api_key = os.environ.get("RUGCHECK_API_KEY")
    modes = ["keyless", "x-apikey", "query_api_key", "query_key"]
    for mode in modes:
        if mode != "keyless" and not api_key:
            continue
        try:
            r = _attempt(mint, mode, api_key, timeout)
            if r.status_code == 200:
                try:
                    j = r.json()
                except Exception:
                    continue
                if isinstance(j, dict) and (j.get("score_normalised") is not None or j.get("risks") is not None or j.get("totalHolders") is not None):
                    LAST_AUTH_MODE = mode
                    return _normalize(j, mode)
                # 200 but empty body: try next mode
                continue
            # Non-200: try next mode (429/401 etc.), not a hard fail
            continue
        except Exception:
            continue
    LAST_AUTH_MODE = "all_failed"
    return {}


# Legacy /report fallback (the pre-API scraping path used by the desk).
def _legacy_report(mint, timeout=10):
    try:
        r = requests.get(LEGACY_URL.format(mint=mint), headers=H, timeout=timeout)
        if r.status_code != 200:
            return {}
        j = r.json()
        token = j.get("token") or {}
        ext = j.get("token_extensions") or {}
        return {
            "score": j.get("score"),
            "score_normalised": j.get("score_normalised"),
            "risks": _risks_to_names(j.get("risks") or []),
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
            "_source": "rugcheck_legacy_report",
            "_auth_mode": "legacy",
        }
    except Exception:
        return {}


def get_report(mint, timeout=10):
    """Primary entry point. Summary API first, legacy /report as fallback."""
    summary = get_summary(mint, timeout=timeout)
    if summary:
        return summary
    return _legacy_report(mint, timeout=timeout)


if __name__ == "__main__":
    import sys, json
    mint = sys.argv[1] if len(sys.argv) > 1 else "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC
    print(json.dumps(get_report(mint), indent=2))
