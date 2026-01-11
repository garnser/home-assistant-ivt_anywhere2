from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from aiohttp import ClientSession

from .const import (
    CLIENT_ID,
    OAUTH_TOKEN_URL,
    POINTT_BASE,
    REDIRECT_URI,
)

@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds

    def expired(self) -> bool:
        return time.time() >= (self.expires_at - 60)


class PointtApi:
    def __init__(self, session: ClientSession, refresh_token: str) -> None:
        self._session = session
        self._tokens: Optional[Tokens] = None
        self._refresh_token_seed = refresh_token
        self._lock = asyncio.Lock()

    async def _ensure_tokens(self) -> Tokens:
        async with self._lock:
            if self._tokens and not self._tokens.expired():
                return self._tokens

            # Refresh using either stored refresh token (if we already refreshed once)
            rt = self._tokens.refresh_token if self._tokens else self._refresh_token_seed

            data = {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": rt,
                "redirect_uri": REDIRECT_URI,
            }

            async with self._session.post(OAUTH_TOKEN_URL, data=data) as resp:
                text = await resp.text()
                resp.raise_for_status()
                js = await resp.json()

            access = js["access_token"]
            refresh = js.get("refresh_token", rt)
            expires_in = float(js.get("expires_in", 3600))
            self._tokens = Tokens(access, refresh, time.time() + expires_in)
            return self._tokens

    async def _headers(self) -> Dict[str, str]:
        t = await self._ensure_tokens()
        return {
            "Authorization": f"Bearer {t.access_token}",
            "Accept": "application/json",
        }

    async def get_gateways(self) -> List[Dict[str, Any]]:
        url = f"{POINTT_BASE}/gateways/"
        async with self._session.get(url, headers=await self._headers()) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def bulk(self, gateway_id: str, resource_paths: List[str]) -> Any:
        """
        IMPORTANT: this backend expects resourcePaths as LIST OF STRINGS.
        """
        url = f"{POINTT_BASE}/bulk"
        body = [{"gatewayId": gateway_id, "resourcePaths": resource_paths}]
        async with self._session.post(url, headers=await self._headers(), json=body) as resp:
            resp.raise_for_status()
            return await resp.json()
