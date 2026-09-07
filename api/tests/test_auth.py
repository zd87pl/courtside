import pytest
from fastapi import HTTPException

from courtside_api import auth


def test_keys_are_prefixed_unique_and_stored_only_as_a_hash():
    key, prefix, digest = auth.generate_key()
    assert key.startswith(auth.KEY_PREFIX)
    assert prefix == key[: auth.PREFIX_LEN] and len(prefix) < len(key)
    assert digest == auth.hash_key(key)
    # The stored material must not contain the secret.
    assert key not in digest and key not in prefix
    assert auth.generate_key()[0] != key


def test_hash_is_stable_and_ignores_surrounding_whitespace():
    key, _, digest = auth.generate_key()
    assert auth.hash_key(f"  {key}\n") == digest


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "Bearer   ", "cs_live_x"])
def test_malformed_authorization_headers_are_401(header):
    with pytest.raises(HTTPException) as e:
        auth._bearer(header)
    assert e.value.status_code == 401
    assert e.value.headers["WWW-Authenticate"] == "Bearer"


def test_bearer_token_is_extracted_case_insensitively():
    assert auth._bearer("bearer cs_live_abc") == "cs_live_abc"
    assert auth._bearer("Bearer  cs_live_abc  ") == "cs_live_abc"


def test_admin_gate_rejects_missing_and_wrong_tokens():
    for bad in (None, "", "wrong", "test-admin-token "):
        with pytest.raises(HTTPException) as e:
            auth.require_admin(bad)
        assert e.value.status_code == 403


def test_admin_gate_accepts_the_configured_token():
    auth.require_admin("test-admin-token")     # does not raise
