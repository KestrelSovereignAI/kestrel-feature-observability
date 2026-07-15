"""Recursive denylist redaction (Q2)."""

from __future__ import annotations

from kestrel_feature_observability.fleet.redaction import (
    REDACTED,
    is_secret_key,
    redact_metadata,
)


def test_secret_keys_matched():
    for key in (
        "api_key",
        "apiKey",
        "secret",
        "password",
        "passwd",
        "token",
        "access_token",
        "authorization",
        "credential",
        "private_key",
    ):
        assert is_secret_key(key), key


def test_non_secret_keys_pass():
    for key in ("agent_name", "duration_ms", "count", "ok", "status"):
        assert not is_secret_key(key)


def test_redacts_recursively_through_dicts_and_lists():
    meta = {
        "api_key": "sk-live-123",
        "ok": 1,
        "nested": {"password": "hunter2", "keep": True},
        "items": [{"secret": "x"}, {"value": 5}],
    }
    out = redact_metadata(meta)
    assert out["api_key"] == REDACTED
    assert out["ok"] == 1
    assert out["nested"]["password"] == REDACTED
    assert out["nested"]["keep"] is True
    assert out["items"][0]["secret"] == REDACTED
    assert out["items"][1]["value"] == 5


def test_redaction_does_not_mutate_input():
    meta = {"token": "abc"}
    redact_metadata(meta)
    assert meta["token"] == "abc"


def test_scalars_pass_through():
    assert redact_metadata(5) == 5
    assert redact_metadata("plain") == "plain"
    assert redact_metadata(None) is None
