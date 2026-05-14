"""Tests for CLI version arguments."""

from __future__ import annotations

import pytest

from td_release_packager import __version__
from td_release_packager.cli import _build_parser


def test_root_parser_supports_version(capsys):
    parser = _build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--version"])

    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_subcommand_parser_supports_version(capsys):
    parser = _build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["inspect", "--version"])

    assert excinfo.value.code == 0
    assert f"td_release_packager inspect {__version__}" in capsys.readouterr().out
