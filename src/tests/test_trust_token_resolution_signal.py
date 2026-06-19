"""
Tests for the ``token_resolution_clean`` trust signal in trust.py.

These pin spec §4's pass/warn/fail rules against the artefact JSON shape
that token_resolution_artefact.compute_artefact produces.
"""

from __future__ import annotations

import json

import pytest

from td_release_packager.trust import (
    TRUST_FAIL,
    TRUST_PASS,
    TRUST_UNKNOWN,
    TRUST_WARN,
    _token_resolution_signal,
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _write_artefact(tmp_path, envs):
    """Write a minimal token-resolution artefact with the given env entries."""
    payload = {
        "schema_version": "1.0",
        "generated_at": "2026-06-19T00:00:00+00:00",
        "environments": envs,
    }
    target = tmp_path / "context" / "ships.token_resolution.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


def _env(
    *,
    name="DEV",
    undefined=None,
    unused=None,
    clobbers=None,
    collisions=None,
    rejected=None,
):
    return {
        "env": name,
        "defined": 0,
        "undefined": undefined or [],
        "unused": unused or [],
        "empty": [],
        "roles": {},
        "clobbers": clobbers or [],
        "collisions": collisions or [],
        "rejected_allowlist": rejected or [],
    }


# ---------------------------------------------------------------------
# Spec §4 outcomes
# ---------------------------------------------------------------------


class TestPass:
    def test_no_envs_passes(self, tmp_path):
        _write_artefact(tmp_path, [])
        sig = _token_resolution_signal(str(tmp_path))
        assert sig.status == TRUST_PASS

    def test_clean_env_passes(self, tmp_path):
        _write_artefact(tmp_path, [_env()])
        sig = _token_resolution_signal(str(tmp_path))
        assert sig.status == TRUST_PASS

    def test_scalar_only_passes(self, tmp_path):
        """Scalar collisions are OFF per spec §3a and not warning-class."""
        _write_artefact(
            tmp_path,
            [
                _env(
                    collisions=[
                        {"value": "1e9", "tokens": ["P", "S"], "class": "scalar"}
                    ]
                )
            ],
        )
        sig = _token_resolution_signal(str(tmp_path))
        assert sig.status == TRUST_PASS


class TestWarn:
    def test_unused_tokens_warn(self, tmp_path):
        _write_artefact(tmp_path, [_env(unused=["GHOST"])])
        sig = _token_resolution_signal(str(tmp_path))
        assert sig.status == TRUST_WARN
        assert "unused" in sig.message.lower()

    def test_env_label_collision_warns(self, tmp_path):
        _write_artefact(
            tmp_path,
            [
                _env(
                    collisions=[
                        {
                            "value": "AGNOSTIC",
                            "tokens": ["ENV_PREFIX", "SHIPS_ENV"],
                            "class": "env_label",
                        }
                    ]
                )
            ],
        )
        sig = _token_resolution_signal(str(tmp_path))
        assert sig.status == TRUST_WARN

    def test_identity_alias_collision_warns(self, tmp_path):
        _write_artefact(
            tmp_path,
            [
                _env(
                    collisions=[
                        {
                            "value": "ProdDb",
                            "tokens": ["PRIMARY", "ALIAS"],
                            "class": "alias",
                        }
                    ]
                )
            ],
        )
        sig = _token_resolution_signal(str(tmp_path))
        assert sig.status == TRUST_WARN


class TestFail:
    def test_clobber_fails(self, tmp_path):
        _write_artefact(
            tmp_path,
            [
                _env(
                    clobbers=[
                        {
                            "physical_name": "db.x",
                            "sources": ["a.viw", "b.viw"],
                            "tokens": ["A", "B"],
                        }
                    ]
                )
            ],
        )
        sig = _token_resolution_signal(str(tmp_path))
        assert sig.status == TRUST_FAIL
        assert "clobber" in sig.message.lower() or "fail" in sig.message.lower()

    def test_undefined_fails(self, tmp_path):
        _write_artefact(tmp_path, [_env(undefined=["MISSING"])])
        sig = _token_resolution_signal(str(tmp_path))
        assert sig.status == TRUST_FAIL

    def test_rejected_allowlist_fails(self, tmp_path):
        _write_artefact(
            tmp_path,
            [
                _env(
                    rejected=[
                        {
                            "tokens": ["A", "B"],
                            "value": "db.x",
                            "reason": "tried to mask",
                        }
                    ]
                )
            ],
        )
        sig = _token_resolution_signal(str(tmp_path))
        assert sig.status == TRUST_FAIL

    def test_failure_in_one_env_fails_overall(self, tmp_path):
        _write_artefact(
            tmp_path,
            [
                _env(name="DEV"),
                _env(name="PRD", undefined=["MISSING"]),
            ],
        )
        sig = _token_resolution_signal(str(tmp_path))
        assert sig.status == TRUST_FAIL


class TestUnknown:
    def test_absent_artefact_is_unknown(self, tmp_path):
        """No artefact means the audit never ran — not safe to assume PASS."""
        sig = _token_resolution_signal(str(tmp_path))
        assert sig.status == TRUST_UNKNOWN
        assert "not found" in sig.message.lower()
