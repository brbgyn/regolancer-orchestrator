import aiohttp
import os
from typing import Any, Dict, List

LNDG_BASE_URL = "http://localhost:8889"
USERNAME = os.getenv("LNDG_USER")
PASSWORD = os.getenv("LNDG_PASS")
CHANNELS_API_URL = f"{LNDG_BASE_URL}/api/channels/?is_open=true&is_active=true"

EXCLUSION_LIST = set()


async def fetch_all_channels(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    url = CHANNELS_API_URL
    out: List[Dict[str, Any]] = []
    auth = aiohttp.BasicAuth(USERNAME, PASSWORD)

    while url:
        async with session.get(url, auth=auth) as r:
            if r.status != 200:
                raise RuntimeError(f"LNDg API error {r.status}")
            data = await r.json()
            out.extend(data.get("results", []))
            url = data.get("next")

    return out


async def load_channels():
    async with aiohttp.ClientSession() as session:
        raw = await fetch_all_channels(session)

    channels = []

    for ch in raw:
        cid = str(ch.get("chan_id") or "")
        if not cid or cid in EXCLUSION_LIST:
            continue

        cap = int(ch["capacity"])

        local_balance = int(ch["local_balance"])
        pending_out = int(ch.get("pending_outbound") or 0)

        local_effective = local_balance + pending_out

        local_pct = int(local_effective * 100 / cap)

        remote_effective = cap - local_effective

        channels.append({
            "chan_id": cid,
            "pubkey": ch.get("remote_pubkey"),
            "alias": ch.get("alias") or "unknown",

            "capacity": cap,
            "local": local_effective,
            "remote": remote_effective,

            "local_pct": local_pct,
            "ar_out_target": int(ch["ar_out_target"]),
            "ar_in_target": int(ch["ar_in_target"]),
            "ar": bool(ch["auto_rebalance"]),
        })

    return channels
