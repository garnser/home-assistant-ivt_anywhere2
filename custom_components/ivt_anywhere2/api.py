from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Callable, Awaitable
from urllib.parse import urlencode

from aiohttp import ClientSession

from .const import (
    CLIENT_ID,
    OAUTH_TOKEN_URL,
    POINTT_BASE,
)

_LOGGER = logging.getLogger(__name__)


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
        self._refresh_token_seed = (refresh_token or "").strip()
        self._on_refresh_token = on_refresh_token
        self._lock = asyncio.Lock()

        _LOGGER.debug(
            "PointtApi init: refresh_token len=%s hint=%s",
            len(self._refresh_token_seed),
            self._hint(self._refresh_token_seed),
        )

        if not self._refresh_token_seed:
            raise ValueError("refresh_token is empty after stripping")

    # ---------------------------------------------------------------------

    async def _ensure_tokens(self) -> Tokens:
        async with self._lock:
            if self._tokens and not self._tokens.expired():
                _LOGGER.debug("Access token still valid")
                return self._tokens

            rt = (
                self._tokens.refresh_token
                if self._tokens
                else self._refresh_token_seed
            ).strip()

            _LOGGER.debug(
                "Refreshing token using refresh_token len=%s hint=%s",
                len(rt),
                self._hint(rt),
            )

            form = {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": rt,
            }

            body = urlencode(form)

            # Log body safely (redacted)
            _LOGGER.debug(
                "POST %s body=%s",
                OAUTH_TOKEN_URL,
                body.replace(rt, "***REDACTED***"),
            )

            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            }

            async with self._session.post(
                OAUTH_TOKEN_URL,
                data=body,
                headers=headers,
            ) as resp:
                text = await resp.text()
                _LOGGER.debug(
                    "Token endpoint response status=%s body=%s",
                    resp.status,
                    text,
                )

                if resp.status >= 400:
                    raise RuntimeError(
                        f"Token refresh failed: HTTP {resp.status}: {text}"
                    )

                js = await resp.json()

            access = js["access_token"]
            refresh = (js.get("refresh_token") or rt).strip()
            expires_in = float(js.get("expires_in", 3600))

            if refresh != self._refresh_token_seed:
                _LOGGER.debug(
                    "Refresh token rotated old=%s new=%s",
                    self._hint(self._refresh_token_seed),
                    self._hint(refresh),
                )
                self._refresh_token_seed = refresh
                if self._on_refresh_token is not None:
                    await self._on_refresh_token(refresh)
            else:
                _LOGGER.debug("Refresh token NOT rotated")

            self._tokens = Tokens(
                access_token=access,
                refresh_token=refresh,
                expires_at=time.time() + expires_in,
            )

            _LOGGER.debug(
                "New access token expires in %ss",
                expires_in,
            )

            return self._tokens

    async def _headers(self) -> Dict[str, str]:
        t = await self._ensure_tokens()
        return {
            "Authorization": f"Bearer {t.access_token}",
            "Accept": "application/json",
        }

    async def get_gateways(self) -> List[Dict[str, Any]]:
        url = f"{POINTT_BASE}/gateways/"
        _LOGGER.debug("GET %s", url)
        async with self._session.get(url, headers=await self._headers()) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def bulk(self, gateway_id: str, resource_paths: List[str]) -> Any:
        url = f"{POINTT_BASE}/bulk"
        body = [{"gatewayId": gateway_id, "resourcePaths": resource_paths}]
        _LOGGER.debug("POST %s body=%s", url, body)
        async with self._session.post(
            url,
            headers=await self._headers(),
            json=body,
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    def current_refresh_token(self) -> str:
        """Return the most recent refresh token (after any rotation)."""
        return self._refresh_token_seed        

    @staticmethod
    def _hint(token: str) -> str:
        if not token:
            return "<empty>"
        if len(token) < 12:
            return token
        return f"{token[:6]}…{token[-6:]}"

