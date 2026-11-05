import base64
import hashlib
import hmac
import time

import pytest
from fastapi import HTTPException

from app.auth import verified_learner_id
from app.config import Settings
from app.routes import SearchRequest


def signed_cookie(learner_id: str, secret: str, expires_at: int) -> str:
    payload = f"{learner_id}.{expires_at}".encode()
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(), payload, hashlib.sha256).digest())
    return f"{learner_id}.{expires_at}.{signature.rstrip(b'=').decode()}"


def test_python_verifies_existing_node_session_signature() -> None:
    secret = "synthetic-session-secret-that-is-long-enough"
    settings = Settings(_env_file=None, session_secret=secret)
    token = signed_cookie("learner-123", secret, int(time.time()) + 600)

    assert verified_learner_id(token, settings) == "learner-123"


@pytest.mark.parametrize(
    "token",
    [
        None,
        "forged.9999999999.invalid",
        "learner.1.invalid",
    ],
)
def test_invalid_or_expired_session_is_rejected(token: str | None) -> None:
    settings = Settings(_env_file=None, session_secret="synthetic-session-secret-that-is-long-enough")

    with pytest.raises(HTTPException) as error:
        verified_learner_id(token, settings)

    assert error.value.status_code == 401


def test_missing_shared_secret_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    settings = Settings(_env_file=None, session_secret=None)
    monkeypatch.setattr("app.auth.local_session_secret_path", lambda: tmp_path / "missing-session-secret")

    with pytest.raises(HTTPException) as error:
        verified_learner_id("learner.9999999999.signature", settings)

    assert error.value.status_code == 503
    assert error.value.detail["code"] == "auth_not_configured"


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "   "},
        {"query": "x", "top_k": 0},
        {"query": "x", "top_k": 21},
        {"query": "x", "account_id": "forged-account"},
        {"query": "x" * 2001},
    ],
)
def test_search_request_rejects_malformed_and_untrusted_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SearchRequest.model_validate(payload)
