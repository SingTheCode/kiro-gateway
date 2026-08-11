# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
POST /v1/responses — pass-through proxy to the ChatGPT Codex backend.

No body conversion: clients speak the OpenAI Responses API natively
(Codex CLI, opencode, oh-my-pi). The gateway only:
1. Verifies the local PROXY_API_KEY
2. Swaps auth headers for the ChatGPT OAuth access token
3. Relays the response (SSE or JSON) unchanged

Reference: opencode packages/opencode/src/plugin/openai/codex.ts (fetch proxy).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from loguru import logger
from starlette.background import BackgroundTask

from kiro.config import CODEX_UPSTREAM_URL
from kiro.routes_openai import verify_api_key

router = APIRouter()

# Response headers that must not be forwarded verbatim (hop-by-hop / recomputed)
_SKIP_RESPONSE_HEADERS = {
    "content-length", "content-encoding", "transfer-encoding", "connection",
}


@router.post("/v1/responses", dependencies=[Depends(verify_api_key)])
async def proxy_responses(request: Request):
    """Proxy a Responses API request to chatgpt.com with OAuth credentials."""
    http_client = request.app.state.http_client
    codex_auth = getattr(request.app.state, "codex_auth", None)
    if codex_auth is None:
        raise HTTPException(
            status_code=503,
            detail="/v1/responses is not configured. Set CODEX_AUTH_FILE and run `codex login`.",
        )

    access_token = await codex_auth.get_access_token(http_client)

    body = await request.body()
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
        "accept": "text/event-stream",
        "OpenAI-Beta": "responses=experimental",
        "originator": request.headers.get("originator", "codex_cli_rs"),
        "session_id": request.headers.get("session_id", str(uuid.uuid4())),
    }
    if codex_auth.account_id:
        headers["ChatGPT-Account-Id"] = codex_auth.account_id

    upstream_request = http_client.build_request(
        "POST", CODEX_UPSTREAM_URL, content=body, headers=headers
    )
    upstream = await http_client.send(upstream_request, stream=True)

    if upstream.status_code != 200:
        error_body = await upstream.aread()
        await upstream.aclose()
        logger.warning(f"Codex upstream error {upstream.status_code}: {error_body[:500]!r}")
        return Response(
            content=error_body,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    response_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _SKIP_RESPONSE_HEADERS
    }
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream.aclose),
    )
