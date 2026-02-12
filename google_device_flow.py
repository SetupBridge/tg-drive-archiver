# -*- coding: utf-8 -*-
import json
import os
from typing import Dict, Any, Optional, Tuple

import aiohttp

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def load_client_secret(path: str) -> Tuple[str, Optional[str]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    block = data.get("installed") or data.get("web") or {}
    client_id = block.get("client_id")
    client_secret = block.get("client_secret")
    if not client_id:
        raise RuntimeError("client_secret.json missing client_id")
    return client_id, client_secret


async def start_device_flow(
    client_secret_path: str,
    scopes: list[str],
) -> Dict[str, Any]:
    client_id, _ = load_client_secret(client_secret_path)
    payload = {
        "client_id": client_id,
        "scope": " ".join(scopes),
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(DEVICE_CODE_URL, data=payload, timeout=30) as r:
            js = await r.json()
            if r.status >= 400:
                raise RuntimeError(f"device_flow_start_failed: {js}")
            return js


async def poll_device_flow_token(
    client_secret_path: str,
    device_code: str,
    interval: int,
    timeout_sec: int = 180,
) -> Dict[str, Any]:
    client_id, client_secret = load_client_secret(client_secret_path)
    payload = {
        "client_id": client_id,
        "client_secret": client_secret or "",
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }

    waited = 0
    async with aiohttp.ClientSession() as s:
        while waited < timeout_sec:
            async with s.post(TOKEN_URL, data=payload, timeout=30) as r:
                js = await r.json()
                if r.status == 200 and js.get("access_token"):
                    return js

                err = js.get("error")
                if err in ("authorization_pending", "slow_down"):
                    await aiohttp.client_reqrep.asyncio.sleep(interval + (2 if err == "slow_down" else 0))
                    waited += interval
                    continue

                # denied / expired / invalid / etc
                raise RuntimeError(f"device_flow_token_failed: {js}")

    raise RuntimeError("device_flow_timeout")
