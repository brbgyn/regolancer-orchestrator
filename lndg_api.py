import aiohttp
import os
from typing import Any, Dict, List, Tuple

# =========================
# CONFIG
# =========================

DEFAULT_LNDG_BASE_URL = "http://localhost:8889"
EXCLUSION_LIST = set()

# =========================
# INTERNAL HELPERS
# =========================

def _get_lndg_config() -> Tuple[str, aiohttp.BasicAuth]:
    base_url = os.getenv("LNDG_BASE_URL", DEFAULT_LNDG_BASE_URL).rstrip("/")

    username = os.getenv("LNDG_USER")
    password = os.getenv("LNDG_PASS")

    if not username:
        raise RuntimeError("Missing required env var: LNDG_USER")
    if not password:
        raise RuntimeError("Missing required env var: LNDG_PASS")

    return base_url, aiohttp.BasicAuth(username, password)

# =========================
# API CALLS
# =========================

async def fetch_all_channels(
    session: aiohttp.ClientSession,
    base_url: str,
    auth: aiohttp.BasicAuth,
) -> List[Dict[str, Any]]:
    """
    Fetch all open and active channels from LNDg (paginated)
    """
    url = f"{base_url}/api/channels/?is_open=true&is_active=true"
    out: List[Dict[str, Any]] = []

    while url:
        async with session.get(url, auth=auth) as r:
            if r.status != 200:
                text = await r.text()
                raise RuntimeError(f"LNDg API error {r.status}: {text}")

            data = await r.json()
            out.extend(data.get("results", []))
            url = data.get("next")

    return out

# =========================
# PUBLIC API
# =========================

async def load_channels() -> List[Dict[str, Any]]:
    """
    Load channels from LNDg and normalize fields for the orchestrator
    """
    base_url, auth = _get_lndg_config()

    async with aiohttp.ClientSession() as session:
        raw = await fetch_all_channels(session, base_url, auth)

    channels: List[Dict[str, Any]] = []

    for ch in raw:
        cid = str(ch.get("chan_id") or "")
        if not cid or cid in EXCLUSION_LIST:
            continue

        try:
            cap = int(ch["capacity"])
            local_balance = int(ch["local_balance"])
            pending_out = int(ch.get("pending_outbound") or 0)
        except (KeyError, ValueError):
            continue

        local_effective = local_balance + pending_out
        local_pct = int(local_effective * 100 / cap) if cap > 0 else 0
        remote_effective = cap - local_effective

        channels.append({
            "chan_id": cid,
            "pubkey": ch.get("remote_pubkey"),
            "alias": ch.get("alias") or "unknown",

            "capacity": cap,
            "local": local_effective,
            "remote": remote_effective,

            "local_pct": local_pct,
            "ar_out_target": int(ch.get("ar_out_target") or 0),
            "ar_in_target": int(ch.get("ar_in_target") or 0),
            "ar": bool(ch.get("auto_rebalance")),
        })

    return channels
