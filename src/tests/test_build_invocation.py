"""
test_build_invocation.py — Build-invocation snapshot + Build Provenance
fallback (issue #397).

Covers:
    1. redact_args — secret flag values masked, safe flags / normal args
       left intact, three arg shapes handled.
    2. snapshot — shape, redaction, injectable timestamp, version stamps.
    3. Serialisation round-trip through BuildManifest.__dict__ (the dict
       that is written verbatim to context/ships.build.json).
    4. _build_provenance_tab fallback — renders the invocation when no
       decisions stages are present; "not available" only when neither
       source exists; timeline still wins when stages exist.
"""

from __future__ import annotations

import json

from td_release_packager.build_invocation import REDACTED, redact_args, snapshot
from td_release_packager.models import BuildManifest
from td_release_packager.package_report import _build_provenance_tab


class TestRedactArgs:
    def test_space_separated_secret_flag_masks_next_token(self):
        assert redact_args(["--password", "hunter2"]) == ["--password", REDACTED]

    def test_inline_secret_flag_masks_value_keeps_flag(self):
        assert redact_args(["--password=hunter2"]) == [f"--password={REDACTED}"]

    def test_bare_key_value_secret_masked(self):
        assert redact_args(["TD_PASSWORD=hunter2"]) == [f"TD_PASSWORD={REDACTED}"]

    def test_signing_key_flag_masked(self):
        assert redact_args(["--signing-key", "/k.pem"]) == ["--signing-key", REDACTED]

    def test_safe_public_key_flag_not_masked(self):
        # --public-key is a safe-to-record path, despite containing "key".
        assert redact_args(["--public-key", "pub.pem"]) == ["--public-key", "pub.pem"]

    def test_normal_args_untouched(self):
        argv = ["--project", "/p", "--env", "DEV", "--name", "OMR"]
        assert redact_args(argv) == argv

    def test_does_not_mutate_input(self):
        argv = ["--password", "hunter2"]
        redact_args(argv)
        assert argv == ["--password", "hunter2"]


class TestSnapshot:
    def test_shape_and_redaction(self):
        snap = snapshot(
            command="ships package",
            args=["--project", "/p", "--password", "hunter2"],
            cwd="/work",
            env_config="config/env/DEV.conf",
            timestamp="2026-06-27T00:00:00+00:00",
        )
        assert snap["command"] == "ships package"
        assert snap["args"] == ["--project", "/p", "--password", REDACTED]
        assert snap["cwd"] == "/work"
        assert snap["env_config"] == "config/env/DEV.conf"
        assert snap["timestamp"] == "2026-06-27T00:00:00+00:00"
        assert snap["ships_version"]
        assert snap["python_version"]

    def test_timestamp_defaults_when_omitted(self):
        snap = snapshot("ships package", [], "/work")
        # ISO-8601 with timezone — a real value, not None.
        assert snap["timestamp"]
        assert "T" in snap["timestamp"]


class TestManifestRoundTrip:
    """build_invocation survives the JSON round-trip that writes
    context/ships.build.json (manifest.__dict__ is dumped verbatim)."""

    def test_round_trip(self):
        inv = snapshot(
            "ships package",
            ["--project", "/p"],
            "/work",
            timestamp="2026-06-27T00:00:00+00:00",
        )
        manifest = BuildManifest(
            build_number="0001",
            environment="DEV",
            package_name="OMR",
            package_filename="OMR.zip",
            timestamp="2026-06-27T00:00:00+00:00",
            build_invocation=inv,
        )
        reloaded = json.loads(json.dumps(manifest.__dict__))
        assert reloaded["build_invocation"] == inv

    def test_defaults_to_none(self):
        manifest = BuildManifest(
            build_number="0001",
            environment="DEV",
            package_name="OMR",
            package_filename="OMR.zip",
            timestamp="2026-06-27T00:00:00+00:00",
        )
        assert manifest.build_invocation is None


class TestProvenanceFallback:
    def test_fallback_renders_invocation_when_no_stages(self):
        inv = snapshot(
            "ships package",
            ["--project", "/p", "--password", "hunter2"],
            "/work",
            env_config="config/env/DEV.conf",
            timestamp="2026-06-27T00:00:00+00:00",
        )
        html = _build_provenance_tab([], inv)
        assert "ships package" in html
        assert "--project" in html
        assert REDACTED in html
        assert "hunter2" not in html
        assert "config/env/DEV.conf" in html
        # The fallback (not the empty placeholder) is what rendered.
        assert "showing the recorded build invocation" in html

    def test_not_available_when_neither_source(self):
        html = _build_provenance_tab([], None)
        assert "not available" in html

    def test_timeline_still_used_when_stages_present(self):
        stages = [{"stage": "package", "status": "success", "outputs": {}}]
        html = _build_provenance_tab(stages, None)
        # The stage timeline renders the stage name; the fallback prose does not.
        assert "package" in html
        assert "showing the recorded build invocation" not in html
