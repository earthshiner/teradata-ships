import logging

from database_package_deployer.cli import (
    _attach_deploy_file_logger,
    _build_arg_parser,
    _configure_logging,
    _print_package_result,
    _print_preflight_result,
)
from database_package_deployer.models import (
    DeployState,
    ObjectDeployResult,
    ObjectType,
    PackageDeployResult,
    PreflightCheck,
    PreflightResult,
)


def test_quiet_can_be_passed_before_deploy_command():
    parser = _build_arg_parser()

    args = parser.parse_args(["--quiet", "deploy", "pkg"])

    assert args.quiet is True
    assert args.command == "deploy"


def test_quiet_can_be_passed_after_deploy_command():
    parser = _build_arg_parser()

    args = parser.parse_args(["deploy", "pkg", "--quiet"])

    assert args.quiet is True
    assert args.command == "deploy"


def test_quiet_logging_keeps_console_to_warning():
    args = _build_arg_parser().parse_args(["deploy", "pkg", "--quiet"])

    _configure_logging(args)

    root = logging.getLogger()
    assert root.level == logging.INFO
    assert root.handlers[0].level == logging.WARNING


def test_deploy_file_logger_writes_package_log(tmp_path):
    args = _build_arg_parser().parse_args(["deploy", str(tmp_path), "--quiet"])
    _configure_logging(args)

    log_file = _attach_deploy_file_logger(args)
    logging.getLogger("database_package_deployer.test").info("backend detail")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_file.startswith(str(tmp_path))
    assert "backend detail" in open(log_file, encoding="utf-8").read()


def test_quiet_preflight_prints_compact_summary(capsys):
    result = PreflightResult(
        passed=True,
        databases=["GDEV1P_BB"],
        object_count={"TABLE": 2, "PROCEDURE": 1},
        errors=0,
        warnings=0,
    )

    _print_preflight_result(result, quiet=True)

    out = capsys.readouterr().out
    assert "Pre-flight ready" in out
    assert "3 objects" in out
    assert "1 databases" in out
    assert "Objects:" not in out


def test_quiet_package_result_prints_summary_and_failed_objects(capsys):
    result = PackageDeployResult(
        deployment_id="deploy-123",
        manifest_path="/pkg/context/ships.deploy.json",
        total=2,
        completed=1,
        failed=1,
        report_path="/pkg/reports/deployment.html",
        results=[
            ObjectDeployResult(
                database_name="GDEV1P_BB",
                object_name="P_Broken",
                object_type=ObjectType.PROCEDURE,
                state=DeployState.FAILED,
                error="compile failed",
            )
        ],
    )

    _print_package_result(result, quiet=True, log_file="/pkg/logs/deploy.log")

    out = capsys.readouterr().out
    assert "Deployment failed" in out
    assert "2 total" in out
    assert "1 deployed" in out
    assert "1 failed" in out
    assert "P_Broken: compile failed" in out
    assert "Log:      /pkg/logs/deploy.log" in out
    assert "Deployment: deploy-123" not in out


def test_quiet_preflight_still_prints_blockers(capsys):
    result = PreflightResult(
        passed=False,
        databases=["GDEV1P_BB"],
        object_count={"JAR": 1},
        errors=1,
        warnings=1,
        checks=[
            PreflightCheck(
                check_name="privilege",
                passed=False,
                database="GDEV1P_BB",
                message="missing CREATE EXTERNAL PROCEDURE",
            ),
            PreflightCheck(
                check_name="jar_alias",
                passed=True,
                database="GDEV1P_BB",
                message="alias already installed by another script",
                severity="WARNING",
            ),
        ],
    )

    _print_preflight_result(result, quiet=True)

    out = capsys.readouterr().out
    assert "Pre-flight blocked" in out
    assert "missing CREATE EXTERNAL PROCEDURE" in out
    assert "alias already installed" in out
