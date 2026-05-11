"""
test_vault_refs.py — Tests for vault reference support in token map (GAP-011).

Covers:
    - Pass: token with $env:MY_VAR, env var set → resolved correctly in payload.
    - Fail: token with $env:MISSING_VAR, var not in env → ValueError.
    - Pass: plain value token → resolved unchanged (no regression).
    - Inspect rule pass: payload contains no $env: or vault: → passes.
    - Inspect rule fail: payload contains $env:UNRESOLVED → ERROR VAULT_REF_UNRESOLVED.
"""

from __future__ import annotations

import os

import pytest

from td_release_packager.token_engine import _resolve_secret_value
from td_release_packager.security_rules import scan_vault_refs


# ---------------------------------------------------------------
# _resolve_secret_value: $env: prefix
# ---------------------------------------------------------------


def test_resolve_env_var_set(monkeypatch):
    """$env:MY_VAR with env var set → resolves to env var value."""
    monkeypatch.setenv("MY_TEST_VAR", "supersecret")
    result = _resolve_secret_value("$env:MY_TEST_VAR", "DB_PASSWORD")
    assert result == "supersecret"


def test_resolve_env_var_missing(monkeypatch):
    """$env:MISSING_VAR with var not set → raises ValueError."""
    monkeypatch.delenv("MISSING_VAULT_VAR_009", raising=False)
    with pytest.raises(ValueError, match="MISSING_VAULT_VAR_009"):
        _resolve_secret_value("$env:MISSING_VAULT_VAR_009", "DB_PASSWORD")


def test_resolve_plain_value():
    """Plain value (no prefix) → returned unchanged."""
    assert _resolve_secret_value("my_plain_value", "TOKEN") == "my_plain_value"


def test_resolve_empty_plain():
    """Empty value → returned unchanged."""
    assert _resolve_secret_value("", "TOKEN") == ""


# ---------------------------------------------------------------
# VAULT_REF_UNRESOLVED inspect rule
# ---------------------------------------------------------------


def test_vault_refs_pass_clean():
    """Payload with no $env: or vault: → no findings."""
    content = "INSERT INTO D.T VALUES ('hello', 42);\n"
    issues = scan_vault_refs("ddl/D.T.dml", content, "/fake/D.T.dml")
    assert issues == []


def test_vault_refs_fail_env_ref():
    """Payload contains $env:UNRESOLVED → ERROR VAULT_REF_UNRESOLVED."""
    content = "SET v_pw = '$env:TD_PROD_PASSWORD';\n"
    issues = scan_vault_refs("ddl/Proc.spl", content, "/fake/Proc.spl")
    assert len(issues) == 1
    assert issues[0].severity == "ERROR"
    assert "VAULT_REF_UNRESOLVED" in issues[0].message
    assert issues[0].line == 1


def test_vault_refs_fail_vault_ref():
    """Payload contains vault:secret/path#field → ERROR VAULT_REF_UNRESOLVED."""
    content = "-- setup\nSET v_pw = 'vault:secret/data/ships/prd#password';\n"
    issues = scan_vault_refs("ddl/Proc.spl", content, "/fake/Proc.spl")
    assert len(issues) == 1
    assert issues[0].line == 2
