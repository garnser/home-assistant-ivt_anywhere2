from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Awaitable, Optional

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
    def __init__(
        self,
        session: ClientSession,
        refresh_token: str,
        on_refresh_token: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self._session = session
        self._tokens: Optional[Tokens] = None
        self._refresh_token_seed = refresh_token
        self._on_refresh_token = on_refresh_token
        self._lock = asyncio.Lock()

    async def _ensure_tokens(self) -> Tokens:
        async with self._lock:
            if self._tokens and not self._tokens.expired():
                return self._tokens

            rt = self._tokens.refresh_token if self._tokens else self._refresh_token_seed

            data = {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": rt,
                "redirect_uri": REDIRECT_URI,
            }

            headers = {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            }

            async with self._session.post(OAUTH_TOKEN_URL, data=data, headers=headers) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    # Show the real reason (usually invalid_grant)
                    raise RuntimeError(f"Token refresh failed: HTTP {resp.status}: {text}")
                js = await resp.json()

            access = js["access_token"]
            refresh = js.get("refresh_token", rt)
            expires_in = float(js.get("expires_in", 3600))

            # Persist rotation back to HA if token changed
            if refresh != self._refresh_token_seed:
                self._refresh_token_seed = refresh
                if self._on_refresh_token is not None:
                    await self._on_refresh_token(refresh)

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
