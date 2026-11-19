#!/usr/bin/env python3
"""Shared Helius Solana RPC config for the desk scripts.

Loads HELIUS_RPC_URL from .env next to this file (never hard-codes a key),
and falls back to the public mainnet RPC when unset. Also loads BIRDEYE_API_KEY
so one import centrally populates env for the other modules.

Usage:
    import rpc_config
    rpc_url = rpc_config.get_rpc_url()
    session = rpc_config.rpc_post({"jsonrpc":"2.0","id":1,"method":"getSlot","params":[]})
"""
import json
import os
from pathlib import Path

import requests

BASE = Path(__file__).parent
ENV_FILE = BASE / ".env"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def _load_env_file():
    """Load .env without requiring python-dotenv (manual parse, no override)."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env_file()

# Try python-dotenv too for users who prefer it; setdefault semantics above
# already loaded the file, so this is a no-op for missing vars only.
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(ENV_FILE, override=False)
except Exception:
    pass


def get_rpc_url():
    """Return Helius RPC URL from env, or public fallback. Never logs the key."""
    return os.environ.get("HELIUS_RPC_URL") or os.environ.get("SOLANA_RPC") or PUBLIC_RPC


def _flux_key_from_env():
    """Parse the Flux RPC key out of FLUX_RPC_URL (never printed)."""
    from urllib.parse import parse_qs, urlparse
    flux = os.environ.get("FLUX_RPC_URL") or ""
    try:
        q = parse_qs(urlparse(flux).query)
        for k in ("key", "api_key", "apikey"):
            if q.get(k):
                return q[k][0]
    except Exception:
        pass
    return None


def get_rugcheck_api_key():
    """RugCheck X-API-KEY candidate: dedicated RUGCHECK_API_KEY wins, else the
    Flux RPC key from FLUX_RPC_URL. Returns None when unset. Never logged."""
    return os.environ.get("RUGCHECK_API_KEY") or _flux_key_from_env()


def is_helius():
    return "helius" in get_rpc_url()


def rpc_post(payload, timeout=15):
    """POST a JSON-RPC payload to the configured RPC. Returns parsed JSON."""
    r = requests.post(get_rpc_url(), json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def rpc_call(method, params=None, timeout=15):
    """Convenience wrapper: returns the `result` field or raises on RPC error.
    NOTE: result for getBalance/getTokenSupply IS the {'context','value'} object,
    not unwrapped value; helpers below extract .value explicitly."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    j = rpc_post(payload, timeout=timeout)
    if isinstance(j, dict) and j.get("error"):
        raise RuntimeError(f"RPC error: {j['error']}")
    return (j or {}).get("result")


def _value_of(result):
    """Extract .value from a {'context':..., 'value': X} RPC result, else result."""
    if isinstance(result, dict) and "value" in result:
        return result["value"]
    return result


def get_balance_lamports(pubkey, timeout=15):
    """Return lamports balance for a wallet pubkey (int) or None on failure."""
    try:
        return int(_value_of(rpc_call("getBalance", [pubkey], timeout=timeout)) or 0)
    except Exception:
        return None


def get_account_info(pubkey, timeout=15):
    """getAccountInfo with parsed encoding. Returns value dict or None."""
    try:
        return rpc_call(
            "getAccountInfo",
            [pubkey, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            timeout=timeout,
        )
    except Exception:
        return None


def get_token_supply(mint, timeout=15):
    """Return token supply value dict {amount, decimals, uiAmount, uiAmountString} or None."""
    try:
        return _value_of(rpc_call("getTokenSupply", [mint], timeout=timeout))
    except Exception:
        return None


def get_mint_authorities(mint, timeout=15):
    """Return (mint_authority, freeze_authority) from parsed mint account.

    Authorities are None when revoked. Returns (None, None) on failure/unknown
    so callers can distinguish? Callers should treat unknown as None only when
    account parsed successfully; on failure this returns None overall.
    """
    info = get_account_info(mint, timeout=timeout)
    if not info:
        return None
    try:
        parsed = ((info.get("data") or {}).get("parsed") or {}).get("info") or {}
        return parsed.get("mintAuthority"), parsed.get("freezeAuthority")
    except Exception:
        return None


if __name__ == "__main__":
    url = get_rpc_url()
    print(f"rpc host: {url.split('/')[2] if '://' in url else url}")
    print(f"helius: {is_helius()}")
    try:
        slot = rpc_call("getSlot", timeout=15)
        print(f"slot: {slot}")
    except Exception as e:
        print(f"slot check failed: {e}")
