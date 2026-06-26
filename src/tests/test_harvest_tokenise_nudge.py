"""
test_harvest_tokenise_nudge.py — Tests for the harvest-time
"no tokenisation ran" nudge (issue #409).

When a source carries hardcoded database names but harvest applies
no tokenisation (no ``config/tokenise.conf``, no ``--auto-tokenise``,
no ``--token-map``), the names are left literal and only surface two
stages later as inspect ``hardcoded_name`` warnings. Harvest now says
so immediately and lists the fixes, so the silent miss becomes an
actionable nudge.

Two branches are pinned:
  * no tokenise.conf at all → "No tokenisation ran" + remedies.
  * tokenise.conf present but matched nothing → "matched no rule".
"""

from __future__ import annotations

import sys

import pytest

from td_release_packager.cli import main


def _invoke_harvest(source, project, *extra):
    """Run ``td_release_packager harvest`` via main() and return the
    exit code. Mirrors the dispatcher-level harness used elsewhere so
    the argparse wiring is exercised, not just the handler."""
    argv = [
        "td_release_packager",
        "harvest",
        "--source",
        str(source),
        "--project",
        str(project),
        *extra,
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        with pytest.raises(SystemExit) as ei:
            main()
        return int(ei.value.code) if ei.value.code is not None else 0
    finally:
        sys.argv = old_argv


def _write_hardcoded_source(tmp_path):
    """A single table whose database name is a hardcoded literal —
    a token candidate that harvest will detect."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "booking.tbl").write_text(
        "CREATE TABLE CustomerDNA_DOM_STD_T.booking (Id INTEGER);\n",
        encoding="utf-8",
    )
    return source


class TestNoTokeniseConf:
    """Plain harvest, hardcoded names, nothing to tokenise them."""

    def test_warns_when_no_tokenisation_ran(self, tmp_path, tmp_project, capsys):
        source = _write_hardcoded_source(tmp_path)

        rc = _invoke_harvest(source, tmp_project)
        out = capsys.readouterr().out

        assert rc == 0
        # The literal name is reported as a candidate ...
        assert "CustomerDNA_DOM_STD_T" in out
        # ... and the nudge spells out the consequence + the fixes.
        assert "No tokenisation ran" in out
        assert "hardcoded_name" in out
        assert "config/tokenise.conf" in out
        assert "--auto-tokenise" in out
        # The escape hatch for intentionally-fixed names is offered too.
        assert "hardcoded_name = OFF" in out


class TestTokeniseConfPresentButNoMatch:
    """tokenise.conf exists but its pattern matches none of the
    hardcoded names — distinct, prefix/pattern-focused message."""

    def test_warns_rule_matched_nothing(self, tmp_path, tmp_project, capsys):
        source = _write_hardcoded_source(tmp_path)
        config = tmp_project / "config"
        config.mkdir(parents=True, exist_ok=True)
        # A well-formed rule whose prefix can never match CustomerDNA_.
        (config / "tokenise.conf").write_text(
            "regex::(?<!\\{)\\bWRONGPREFIX_([A-Za-z0-9]+):={{$1}}\n",
            encoding="utf-8",
        )

        rc = _invoke_harvest(source, tmp_project)
        out = capsys.readouterr().out

        assert rc == 0
        assert "CustomerDNA_DOM_STD_T" in out
        assert "matched no rule" in out
        # The generic "no tokenisation ran" path must NOT fire here.
        assert "No tokenisation ran" not in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
