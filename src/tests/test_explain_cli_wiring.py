"""
test_explain_cli_wiring.py — Tests for the EXPLAIN CLI subcommand (#269).

Verifies that ``python -m database_package_deployer explain`` is wired
into the parser and dispatch, and that the subcommand accepts the same
connection arguments as ``deploy``. The engine itself
(``explain_package``) has its own coverage elsewhere.
"""

from __future__ import annotations

import pytest

from database_package_deployer.cli import _build_arg_parser


# ---------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------


class TestExplainSubcommandRegistered:
    def test_explain_appears_in_top_level_help(self, capsys):
        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--help"])
        captured = capsys.readouterr()
        assert "explain" in captured.out

    def test_explain_subcommand_parses(self):
        parser = _build_arg_parser()
        args = parser.parse_args(
            [
                "explain",
                "/tmp/pkg",
                "--host",
                "td.example.com",
                "--user",
                "tester",
                "--password",
                "secret",
            ]
        )
        assert args.command == "explain"
        assert args.package_dir == "/tmp/pkg"
        assert args.host == "td.example.com"

    def test_explain_requires_package_dir(self, capsys):
        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["explain"])

    def test_explain_accepts_order_file(self):
        parser = _build_arg_parser()
        args = parser.parse_args(
            [
                "explain",
                "/tmp/pkg",
                "--order-file",
                "/tmp/order.txt",
                "--host",
                "h",
                "--user",
                "u",
                "--password",
                "p",
            ]
        )
        assert args.order_file == "/tmp/order.txt"

    def test_explain_accepts_quiet_flag(self):
        parser = _build_arg_parser()
        args = parser.parse_args(
            [
                "explain",
                "/tmp/pkg",
                "--quiet",
                "--host",
                "h",
                "--user",
                "u",
                "--password",
                "p",
            ]
        )
        assert getattr(args, "quiet", False) is True

    def test_quiet_can_precede_explain_command(self):
        # Mirrors test_deployer_cli_quiet.py — the global --quiet/--verbose
        # flags must work in front of any subcommand.
        parser = _build_arg_parser()
        args = parser.parse_args(
            [
                "--quiet",
                "explain",
                "/tmp/pkg",
                "--host",
                "h",
                "--user",
                "u",
                "--password",
                "p",
            ]
        )
        assert args.quiet is True
        assert args.command == "explain"


# ---------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------


class TestDispatchToExplain:
    def test_dispatch_routes_explain_to_cmd_explain(self, monkeypatch):
        """``main()`` routes args.command == 'explain' to _cmd_explain."""
        called = {"hit": False}

        def _fake_cmd_explain(args):
            called["hit"] = True
            assert args.package_dir == "/tmp/pkg"

        monkeypatch.setattr(
            "database_package_deployer.cli._cmd_explain", _fake_cmd_explain
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "database_package_deployer",
                "explain",
                "/tmp/pkg",
                "--host",
                "h",
                "--user",
                "u",
                "--password",
                "p",
            ],
        )
        from database_package_deployer.cli import main

        main()
        assert called["hit"]
