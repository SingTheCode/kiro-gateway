# -*- coding: utf-8 -*-

"""
Unit tests for CodexAuthManager (ChatGPT OAuth for /v1/responses).
No real network requests.
"""

import base64
import json
import time
from unittest.mock import AsyncMock, Mock

import pytest

from kiro.auth_codex import CodexAuthManager, _jwt_exp


def _mock_client(status_code=200, json_data=None):
    """AsyncMock httpx client (real clients are globally blocked in conftest,
    so httpx classes cannot be used as spec here)."""
    response = Mock()
    response.status_code = status_code
    response.json = Mock(return_value=json_data or {})
    response.text = json.dumps(json_data or {})
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    return client


def _make_jwt(exp: int) -> str:
    """Build an unsigned JWT with the given exp claim."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _write_auth_file(tmp_path, access_token, refresh_token="rt-1", account_id="acc-1"):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "id-1",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
        },
        "last_refresh": "2026-01-01T00:00:00.000Z",
    }))
    return auth_file


def test_jwt_exp_parses_claim():
    assert _jwt_exp(_make_jwt(1234567890)) == 1234567890
    assert _jwt_exp("not-a-jwt") is None


def test_load_reads_tokens_and_expiry(tmp_path):
    exp = int(time.time()) + 7200
    auth_file = _write_auth_file(tmp_path, _make_jwt(exp))
    manager = CodexAuthManager(str(auth_file))
    assert manager.account_id == "acc-1"
    assert manager._expires_at == float(exp)


def test_load_raises_without_tokens(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"tokens": {}}))
    with pytest.raises(ValueError):
        CodexAuthManager(str(auth_file))


@pytest.mark.asyncio
async def test_valid_token_returned_without_refresh(tmp_path):
    token = _make_jwt(int(time.time()) + 7200)
    manager = CodexAuthManager(str(_write_auth_file(tmp_path, token)))

    client = _mock_client()
    assert await manager.get_access_token(client) == token
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_expired_token_refreshes_and_rotates(tmp_path):
    old_token = _make_jwt(int(time.time()) - 10)
    auth_file = _write_auth_file(tmp_path, old_token, refresh_token="rt-old")
    manager = CodexAuthManager(str(auth_file))

    new_token = _make_jwt(int(time.time()) + 7200)
    client = _mock_client(200, {
        "access_token": new_token,
        "refresh_token": "rt-new",
        "id_token": "id-new",
    })

    assert await manager.get_access_token(client) == new_token

    # Refresh request uses the Codex CLI flow
    body = client.post.call_args.kwargs["json"]
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "rt-old"

    # Rotation persisted to disk, unknown fields preserved
    saved = json.loads(auth_file.read_text())
    assert saved["tokens"]["access_token"] == new_token
    assert saved["tokens"]["refresh_token"] == "rt-new"
    assert saved["tokens"]["id_token"] == "id-new"
    assert saved["tokens"]["account_id"] == "acc-1"
    assert saved["auth_mode"] == "chatgpt"
    assert saved["last_refresh"] != "2026-01-01T00:00:00.000Z"


@pytest.mark.asyncio
async def test_refresh_failure_raises(tmp_path):
    manager = CodexAuthManager(str(_write_auth_file(tmp_path, _make_jwt(int(time.time()) - 10))))

    client = _mock_client(401, {"error": "invalid_grant"})
    with pytest.raises(RuntimeError):
        await manager.get_access_token(client)
