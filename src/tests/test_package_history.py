"""
test_package_history.py — non-linear package history check (issue #168).

Builds synthetic ``releases/`` trees (release_group.json + fake archives +
.sha256 sidecars) and asserts the check detects integrity mismatch, missing
required siblings, orphaned prereqs halves, intra-group build inconsistency,
cross-group build-number reuse with different contents, and out-of-order
build numbers — while staying silent on a clean, linear history.
"""

from __future__ import annotations

import hashlib
import json
import os

from td_release_packager.package_history import check_package_history

RULE = "non_linear_package_history"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_group(
    releases,
    group_name: str,
    env: str,
    name: str,
    packages,
):
    """packages: list of dicts {role, archive, requires, bytes, sidecar}.

    ``sidecar`` is "ok" (correct digest), "bad" (wrong digest), or "none".
    """
    gdir = releases / group_name
    gdir.mkdir(parents=True)
    pkg_docs = []
    for p in packages:
        archive = p["archive"]
        data = p.get("bytes", b"payload-" + archive.encode())
        (gdir / archive).write_bytes(data)
        sidecar = p.get("sidecar", "ok")
        if sidecar != "none":
            digest = _sha256(data) if sidecar == "ok" else _sha256(b"WRONG")
            (gdir / (archive + ".sha256")).write_text(
                f"{digest}  {archive}\n", encoding="utf-8"
            )
        pkg_docs.append(
            {
                "role": p["role"],
                "archive": archive,
                "checksum": archive + ".sha256",
                "requires": p.get("requires", []),
            }
        )
    doc = {
        "schema_version": "1.0",
        "release_group": group_name,
        "environment": env,
        "package_name": name,
        "packages": pkg_docs,
    }
    (gdir / "release_group.json").write_text(json.dumps(doc), encoding="utf-8")
    return gdir


def _rules(tmp_path):
    return check_package_history(str(tmp_path))


def _has(issues, needle):
    return any(needle in i.message for i in issues)


class TestNoOp:
    def test_no_releases_dir(self, tmp_path):
        assert _rules(tmp_path) == []

    def test_empty_releases_dir(self, tmp_path):
        (tmp_path / "releases").mkdir()
        assert _rules(tmp_path) == []

    def test_dir_without_manifest_skipped(self, tmp_path):
        (tmp_path / "releases" / "junk").mkdir(parents=True)
        assert _rules(tmp_path) == []

    def test_clean_linear_history(self, tmp_path):
        releases = tmp_path / "releases"
        _make_group(
            releases,
            "DEV_OMR_BUILD_0001_20260101000000",
            "DEV",
            "OMR",
            [
                {
                    "role": "main",
                    "archive": "DEV_OMR_BUILD_0001_20260101000000_01_main.zip",
                }
            ],
        )
        _make_group(
            releases,
            "DEV_OMR_BUILD_0002_20260102000000",
            "DEV",
            "OMR",
            [
                {
                    "role": "main",
                    "archive": "DEV_OMR_BUILD_0002_20260102000000_01_main.zip",
                }
            ],
        )
        assert _rules(tmp_path) == []


class TestIntegrity:
    def test_sidecar_mismatch_flagged(self, tmp_path):
        releases = tmp_path / "releases"
        _make_group(
            releases,
            "DEV_OMR_BUILD_0001_20260101000000",
            "DEV",
            "OMR",
            [
                {
                    "role": "main",
                    "archive": "DEV_OMR_BUILD_0001_20260101000000_01_main.zip",
                    "sidecar": "bad",
                }
            ],
        )
        issues = _rules(tmp_path)
        assert _has(issues, "Integrity sidecar mismatch")
        assert all(i.rule == RULE for i in issues)

    def test_missing_sidecar_flagged(self, tmp_path):
        releases = tmp_path / "releases"
        _make_group(
            releases,
            "DEV_OMR_BUILD_0001_20260101000000",
            "DEV",
            "OMR",
            [
                {
                    "role": "main",
                    "archive": "DEV_OMR_BUILD_0001_20260101000000_01_main.zip",
                    "sidecar": "none",
                }
            ],
        )
        assert _has(_rules(tmp_path), "no readable integrity sidecar")


class TestPairing:
    def test_requires_missing_sibling(self, tmp_path):
        releases = tmp_path / "releases"
        _make_group(
            releases,
            "DEV_OMR_BUILD_0001_20260101000000",
            "DEV",
            "OMR",
            [
                {
                    "role": "main",
                    "archive": "DEV_OMR_BUILD_0001_20260101000000_02_main.zip",
                    "requires": ["DEV_OMR_BUILD_0001_20260101000000_01_prereqs.zip"],
                }
            ],
        )
        assert _has(_rules(tmp_path), "requires sibling")

    def test_orphan_prereqs_half(self, tmp_path):
        releases = tmp_path / "releases"
        _make_group(
            releases,
            "DEV_OMR_BUILD_0001_20260101000000",
            "DEV",
            "OMR",
            [
                {
                    "role": "prereqs",
                    "archive": "DEV_OMR_BUILD_0001_20260101000000_01_prereqs.zip",
                }
            ],
        )
        assert _has(_rules(tmp_path), "no matching main")

    def test_complete_pair_clean(self, tmp_path):
        releases = tmp_path / "releases"
        pre = "DEV_OMR_BUILD_0001_20260101000000_01_prereqs.zip"
        main = "DEV_OMR_BUILD_0001_20260101000000_02_main.zip"
        _make_group(
            releases,
            "DEV_OMR_BUILD_0001_20260101000000",
            "DEV",
            "OMR",
            [
                {"role": "prereqs", "archive": pre},
                {"role": "main", "archive": main, "requires": [pre]},
            ],
        )
        assert _rules(tmp_path) == []


class TestIntraGroupConsistency:
    def test_mixed_builds_in_one_group(self, tmp_path):
        releases = tmp_path / "releases"
        _make_group(
            releases,
            "DEV_OMR_BUILD_0001_20260101000000",
            "DEV",
            "OMR",
            [
                {
                    "role": "prereqs",
                    "archive": "DEV_OMR_BUILD_0001_20260101000000_01_prereqs.zip",
                },
                # Wrong build number for this group.
                {
                    "role": "main",
                    "archive": "DEV_OMR_BUILD_0002_20260101000000_02_main.zip",
                },
            ],
        )
        assert _has(_rules(tmp_path), "different builds")


class TestCrossGroup:
    def test_reused_build_number_different_contents(self, tmp_path):
        releases = tmp_path / "releases"
        _make_group(
            releases,
            "DEV_OMR_BUILD_0005_20260101000000",
            "DEV",
            "OMR",
            [
                {
                    "role": "main",
                    "archive": "DEV_OMR_BUILD_0005_20260101000000_01_main.zip",
                    "bytes": b"AAA",
                }
            ],
        )
        _make_group(
            releases,
            "DEV_OMR_BUILD_0005_20260102000000",
            "DEV",
            "OMR",
            [
                {
                    "role": "main",
                    "archive": "DEV_OMR_BUILD_0005_20260102000000_01_main.zip",
                    "bytes": b"BBB",
                }
            ],
        )
        assert _has(_rules(tmp_path), "reused across")

    def test_out_of_order_build_numbers(self, tmp_path):
        releases = tmp_path / "releases"
        # Build 5 first (older ts), build 3 later (newer ts) → out of order.
        _make_group(
            releases,
            "DEV_OMR_BUILD_0005_20260101000000",
            "DEV",
            "OMR",
            [
                {
                    "role": "main",
                    "archive": "DEV_OMR_BUILD_0005_20260101000000_01_main.zip",
                }
            ],
        )
        _make_group(
            releases,
            "DEV_OMR_BUILD_0003_20260202000000",
            "DEV",
            "OMR",
            [
                {
                    "role": "main",
                    "archive": "DEV_OMR_BUILD_0003_20260202000000_01_main.zip",
                }
            ],
        )
        assert _has(_rules(tmp_path), "Non-linear build history")

    def test_different_cohorts_independent(self, tmp_path):
        # Same build number across DIFFERENT (env,name) is fine.
        releases = tmp_path / "releases"
        _make_group(
            releases,
            "DEV_OMR_BUILD_0001_20260101000000",
            "DEV",
            "OMR",
            [
                {
                    "role": "main",
                    "archive": "DEV_OMR_BUILD_0001_20260101000000_01_main.zip",
                    "bytes": b"A",
                }
            ],
        )
        _make_group(
            releases,
            "DEV_GCFR_BUILD_0001_20260101000000",
            "DEV",
            "GCFR",
            [
                {
                    "role": "main",
                    "archive": "DEV_GCFR_BUILD_0001_20260101000000_01_main.zip",
                    "bytes": b"B",
                }
            ],
        )
        assert _rules(tmp_path) == []


class TestSeverityAndRemediation:
    def test_severity_stamped(self, tmp_path):
        releases = tmp_path / "releases"
        _make_group(
            releases,
            "DEV_OMR_BUILD_0001_20260101000000",
            "DEV",
            "OMR",
            [
                {
                    "role": "main",
                    "archive": "DEV_OMR_BUILD_0001_20260101000000_01_main.zip",
                    "sidecar": "bad",
                }
            ],
        )
        issues = check_package_history(str(tmp_path), severity="ERROR")
        assert issues and all(i.severity == "ERROR" for i in issues)
        assert issues[0].remediation["requires_human_review"] is True
