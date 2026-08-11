# -*- coding: utf-8 -*-

"""
Unit tests for the /v1/responses proxy route.
No real network requests — upstream client is mocked on app.state.
"""

import json
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kiro.config import PROXY_API_KEY
from kiro.routes_responses import router


def _make_app(codex_auth=None, http_client=None):
    app = FastAPI()
    app.include_router(router)
    app.state.codex_auth = codex_auth
    app.state.http_client = http_client
    return app


AUTH = {"Authorization": f"Bearer {PROXY_API_KEY}"}


def test_missing_api_key_returns_401():
    client = TestClient(_make_app())
    response = client.post("/v1/responses", json={})
    assert response.status_code == 401


def test_unconfigured_returns_503():
    client = TestClient(_make_app(codex_auth=None))
    response = client.post("/v1/responses", json={}, headers=AUTH)
    assert response.status_code == 503
    assert "CODEX_AUTH_FILE" in response.json()["detail"]


def _upstream_mock(status_code=200, content=b"", headers=None):
    """Mock httpx client whose send() returns a streaming-style response."""
    upstream = Mock()
    upstream.status_code = status_code
    upstream.headers = headers or {"content-type": "text/event-stream"}
    upstream.aread = AsyncMock(return_value=content)
    upstream.aclose = AsyncMock()

    async def aiter_raw():
        yield content

    upstream.aiter_raw = aiter_raw
    http_client = Mock()
    http_client.build_request = Mock()
    http_client.send = AsyncMock(return_value=upstream)
    return http_client


def _codex_auth_mock():
    codex_auth = Mock()
    codex_auth.account_id = "acc-1"
    codex_auth.get_access_token = AsyncMock(return_value="tok-1")
    return codex_auth


def test_proxies_body_and_headers_unchanged():
    http_client = _upstream_mock(200, b'data: {"type":"response.completed"}\n\n')
    app = _make_app(codex_auth=_codex_auth_mock(), http_client=http_client)
    client = TestClient(app)

    body = {"model": "gpt-5", "input": [], "store": False}
    response = client.post("/v1/responses", json=body, headers=AUTH)

    assert response.status_code == 200
    assert b"response.completed" in response.content

    # Body passed through untouched, headers swapped for OAuth credentials
    _, kwargs = http_client.build_request.call_args
    args, _ = http_client.build_request.call_args
    assert json.loads(kwargs["content"]) == body
    sent_headers = kwargs["headers"]
    assert sent_headers["authorization"] == "Bearer tok-1"
    assert sent_headers["ChatGPT-Account-Id"] == "acc-1"
    assert "originator" in sent_headers
    assert "session_id" in sent_headers


def test_upstream_error_passed_through():
    http_client = _upstream_mock(
        429, b'{"error":"rate_limit"}', headers={"content-type": "application/json"}
    )
    app = _make_app(codex_auth=_codex_auth_mock(), http_client=http_client)
    client = TestClient(app)

    response = client.post("/v1/responses", json={}, headers=AUTH)
    assert response.status_code == 429
    assert response.json() == {"error": "rate_limit"}
