# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
ChatGPT OAuth token manager for the /v1/responses proxy route.

Owns the Codex CLI credential file (~/.codex/auth.json):
- Loads access/refresh tokens written by `codex login`
- Determines expiry from the access token JWT `exp` claim
- Refreshes via https://auth.openai.com/oauth/token (same flow as Codex CLI)
- Persists rotated refresh tokens back to the file

Reference: openai/codex codex-rs/login/src/auth/manager.rs
(CLIENT_ID, REFRESH_TOKEN_URL, persist_tokens rotation behavior).
"""

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

# Constants from codex-rs/login/src/auth/manager.rs
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REFRESH_URL = "https://auth.openai.com/oauth/token"

# Refresh slightly before actual expiry to avoid mid-request 401s
EXPIRY_MARGIN_SECONDS = 300


def _jwt_exp(token: str) -> Optional[int]:
    """Extract the `exp` claim from a JWT without verifying the signature."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


class CodexAuthManager:
    """
    Manages ChatGPT OAuth credentials from a Codex CLI auth.json file.

    The gateway owns the file: refreshes update it in place, including
    refresh token rotation (mirrors persist_tokens in Codex CLI).
    """

    def __init__(self, auth_file: str):
        self._auth_file = Path(auth_file).expanduser()
        self._lock = asyncio.Lock()
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._account_id: Optional[str] = None
        self._expires_at: float = 0.0
        self._load()

    @property
    def account_id(self) -> Optional[str]:
        return self._account_id

    def _load(self) -> None:
        """Load tokens from auth.json. Raises on missing/invalid file."""
        data = json.loads(self._auth_file.read_text(encoding="utf-8"))
        tokens = data.get("tokens") or {}
        self._access_token = tokens.get("access_token")
        self._refresh_token = tokens.get("refresh_token")
        self._account_id = tokens.get("account_id")
        if not self._access_token or not self._refresh_token:
            raise ValueError(
                f"No ChatGPT OAuth tokens in {self._auth_file}. "
                "Run `codex login` on this machine first."
            )
        exp = _jwt_exp(self._access_token)
        self._expires_at = float(exp) if exp else 0.0
        logger.info(
            f"Codex auth loaded from {self._auth_file} "
            f"(account_id={'set' if self._account_id else 'missing'}, "
            f"expires_at={self._expires_at})"
        )

    def _save(self, access_token: str, refresh_token: Optional[str], id_token: Optional[str]) -> None:
        """
        Persist refreshed tokens back to auth.json, preserving unknown fields.

        Mirrors Codex CLI persist_tokens: only overwrite fields present in
        the refresh response; always bump last_refresh.
        """
        data = json.loads(self._auth_file.read_text(encoding="utf-8"))
        tokens = data.setdefault("tokens", {})
        tokens["access_token"] = access_token
        if refresh_token:
            tokens["refresh_token"] = refresh_token
        if id_token:
            tokens["id_token"] = id_token
        data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        tmp = self._auth_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._auth_file)

    async def get_access_token(self, http_client: httpx.AsyncClient) -> str:
        """Return a valid access token, refreshing if expired or near expiry."""
        if self._access_token and time.time() < self._expires_at - EXPIRY_MARGIN_SECONDS:
            return self._access_token

        async with self._lock:
            # Re-check after acquiring the lock (another request may have refreshed)
            if self._access_token and time.time() < self._expires_at - EXPIRY_MARGIN_SECONDS:
                return self._access_token

            logger.info("Refreshing ChatGPT OAuth access token")
            response = await http_client.post(
                CODEX_REFRESH_URL,
                json={
                    "client_id": CODEX_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
                headers={"Content-Type": "application/json"},
            )
            if response.status_code != 200:
                body = response.text[:500]
                logger.error(f"ChatGPT token refresh failed: {response.status_code}: {body}")
                raise RuntimeError(
                    f"ChatGPT token refresh failed ({response.status_code}). "
                    "If the refresh token expired, run `codex login` again."
                )

            payload = response.json()
            access_token = payload.get("access_token")
            if not access_token:
                raise RuntimeError("ChatGPT token refresh response missing access_token")

            new_refresh = payload.get("refresh_token")
            self._access_token = access_token
            if new_refresh:
                self._refresh_token = new_refresh
            exp = _jwt_exp(access_token)
            self._expires_at = float(exp) if exp else time.time() + 3600

            self._save(access_token, new_refresh, payload.get("id_token"))
            logger.info(f"ChatGPT access token refreshed (expires_at={self._expires_at})")
            return self._access_token
