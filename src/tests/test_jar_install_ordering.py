"""
test_jar_install_ordering.py — Item 3 of the deployer follow-ups:
JAR-install scripts must deploy BEFORE the procedures that depend
on them.

Three orthogonal pieces are exercised:

    1. ``DDL_SUBDIR_ORDER`` constant places ``jar_install`` before
       ``procedures`` (and ``functions``) so any caller using the
       map gets the correct order.
    2. The deployer's default ``file_patterns`` includes ``*.sjr``
       so glob-mode discovery actually finds the install scripts.
    3. The new ``_check_jar_alias_coverage`` preflight phase fails
       when a Java procedure references a JAR alias that no
       jar_install script in the package provides — and passes
       quietly when coverage is complete.

The first two are tiny constant-shape assertions. The third is a
behavioural test on the preflight helper using small fabricated
``ParsedStatement`` instances (no live database needed).
"""

from __future__ import annotations

from database_package_deployer.deployer import _deploy_package_impl as deploy_package  # noqa: F401 (import sanity)
from database_package_deployer.models import (
    DeployStrategy,
    ObjectType,
    ParsedStatement,
)
from database_package_deployer.preflight import (
    _check_jar_alias_coverage,
    _extract_installed_aliases,
    _extract_referenced_aliases,
)
from td_release_packager.models import DDL_SUBDIR_ORDER


# ---------------------------------------------------------------
# DDL_SUBDIR_ORDER constant
# ---------------------------------------------------------------


class TestDDLSubdirOrder:
    """The constant must order jar_install before procedures and
    functions so any caller using the map deploys correctly."""

    def test_jar_install_before_procedures(self):
        assert DDL_SUBDIR_ORDER["jar_install"] < DDL_SUBDIR_ORDER["procedures"]

    def test_jar_install_before_functions(self):
        assert DDL_SUBDIR_ORDER["jar_install"] < DDL_SUBDIR_ORDER["functions"]

    def test_tables_before_views(self):
        # Sanity: the existing relative ordering still holds.
        assert DDL_SUBDIR_ORDER["tables"] < DDL_SUBDIR_ORDER["views"]

    def test_views_before_macros(self):
        assert DDL_SUBDIR_ORDER["views"] < DDL_SUBDIR_ORDER["macros"]


# ---------------------------------------------------------------
# Deployer default file_patterns
# ---------------------------------------------------------------


class TestDefaultFilePatternsIncludeSjr:
    """The deployer's glob-mode discovery must find .sjr files;
    without this pattern jar_install scripts ship in the payload but
    never deploy."""

    def test_sjr_in_default_patterns(self):
        # We can't easily call deploy_package without a cursor, so
        # introspect the default-patterns source directly. The test
        # would also catch an accidental rename.
        import inspect

        source = inspect.getsource(deploy_package)
        assert '"*.sjr"' in source, (
            ".sjr is missing from deploy_package's default file_patterns"
        )


# ---------------------------------------------------------------
# JAR alias extraction helpers
# ---------------------------------------------------------------


def _make_jar_install(alias: str, *, jar_path: str = "lib/foo.jar") -> ParsedStatement:
    """Build a minimal ParsedStatement of type JAR carrying a CALL
    SQLJ.INSTALL_JAR statement with the given alias."""
    text = f"CALL SQLJ.INSTALL_JAR('CJ!{jar_path}', '{alias}', 0);"
    return ParsedStatement(
        file_path=f"{alias}.sjr",
        ddl_text=text,
        original_text=text,
        database_name="",
        object_name=alias,
        object_type=ObjectType.JAR,
        strategy=DeployStrategy.DIRECT_EXECUTE,
        qualified_name=alias,
    )


def _make_java_procedure(db: str, name: str, *, alias: str) -> ParsedStatement:
    """Build a minimal ParsedStatement of type PROCEDURE referencing a JAR
    alias via EXTERNAL NAME inside a LANGUAGE JAVA body."""
    text = (
        f"CREATE PROCEDURE {db}.{name} ()\n"
        f"LANGUAGE JAVA\n"
        f"EXTERNAL NAME '{alias}:com.example.{name}.run'\n"
        f"PARAMETER STYLE JAVA;\n"
    )
    return ParsedStatement(
        file_path=f"{db}.{name}.spl",
        ddl_text=text,
        original_text=text,
        database_name=db,
        object_name=name,
        object_type=ObjectType.PROCEDURE,
        strategy=DeployStrategy.REPLACE_IN_PLACE,
        qualified_name=f"{db}.{name}",
    )


def _make_spl_procedure(db: str, name: str) -> ParsedStatement:
    """A regular SPL procedure (no LANGUAGE JAVA, no JAR ref)."""
    text = f"CREATE PROCEDURE {db}.{name} ()\nBEGIN\n    SET v = 1;\nEND;\n"
    return ParsedStatement(
        file_path=f"{db}.{name}.spl",
        ddl_text=text,
        original_text=text,
        database_name=db,
        object_name=name,
        object_type=ObjectType.PROCEDURE,
        strategy=DeployStrategy.REPLACE_IN_PLACE,
        qualified_name=f"{db}.{name}",
    )


class TestExtractInstalledAliases:
    """``_extract_installed_aliases`` walks JAR-typed parsed DDLs
    and returns the upper-cased alias set."""

    def test_empty_input_returns_empty_set(self):
        assert _extract_installed_aliases([]) == set()

    def test_single_install_jar(self):
        installed = _extract_installed_aliases([_make_jar_install("MyJar")])
        assert installed == {"MYJAR"}

    def test_multiple_install_jars(self):
        installed = _extract_installed_aliases(
            [_make_jar_install("AliasA"), _make_jar_install("AliasB")]
        )
        assert installed == {"ALIASA", "ALIASB"}

    def test_replace_jar_also_collected(self):
        text = "CALL SQLJ.REPLACE_JAR('CJ!lib/foo.jar', 'ReplacedAlias');"
        parsed = ParsedStatement(
            file_path="ReplacedAlias.sjr",
            ddl_text=text,
            original_text=text,
            database_name="",
            object_name="ReplacedAlias",
            object_type=ObjectType.JAR,
            strategy=DeployStrategy.DIRECT_EXECUTE,
            qualified_name="ReplacedAlias",
        )
        assert _extract_installed_aliases([parsed]) == {"REPLACEDALIAS"}

    def test_non_jar_ddls_ignored(self):
        # A procedure with INSTALL_JAR-shaped text in a comment must
        # NOT contribute to the installed-aliases set.
        proc = _make_java_procedure("MyDb", "myProc", alias="SomeJar")
        installed = _extract_installed_aliases([proc])
        assert installed == set()


class TestExtractReferencedAliases:
    """``_extract_referenced_aliases`` walks PROCEDURE-typed parsed
    DDLs that contain LANGUAGE JAVA and pulls each EXTERNAL NAME's
    JAR alias."""

    def test_java_procedure_alias_extracted(self):
        proc = _make_java_procedure("MyDb", "myProc", alias="MyJar")
        refs = _extract_referenced_aliases([proc])
        assert len(refs) == 1
        ref_proc, alias = refs[0]
        assert alias == "MYJAR"
        assert ref_proc.qualified_name == "MyDb.myProc"

    def test_spl_procedure_ignored(self):
        proc = _make_spl_procedure("MyDb", "spl_proc")
        assert _extract_referenced_aliases([proc]) == []

    def test_non_procedure_ignored(self):
        jar = _make_jar_install("Foo")
        assert _extract_referenced_aliases([jar]) == []


# ---------------------------------------------------------------
# _check_jar_alias_coverage
# ---------------------------------------------------------------


class TestJarAliasCoverage:
    """The preflight phase that ties the package's jar_install scripts
    to its Java procedures."""

    def test_no_java_procedures_silent(self):
        """No Java procedures in the package → rule is silently
        inactive (no checks emitted, the report stays uncluttered)."""
        checks = _check_jar_alias_coverage([_make_spl_procedure("MyDb", "PlainSpl")])
        assert checks == []

    def test_alias_installed_emits_info_pass(self):
        """When the procedure's alias is provided by an in-package
        jar_install, the check passes as INFO so the report can show
        the pairing without cluttering the error count.

        The message normalises the alias to upper-case (Teradata
        identifier rules), so we compare upper-cased."""
        checks = _check_jar_alias_coverage(
            [
                _make_jar_install("MyJar"),
                _make_java_procedure("MyDb", "myProc", alias="MyJar"),
            ]
        )
        assert len(checks) == 1
        check = checks[0]
        assert check.passed is True
        assert check.severity == "INFO"
        assert "MYJAR" in check.message.upper()

    def test_alias_missing_emits_error(self):
        """When the procedure's alias is not installed by anything in
        the package, the check fails ERROR-severity and names the
        missing alias plus what IS installed."""
        checks = _check_jar_alias_coverage(
            [
                _make_jar_install("OtherJar"),
                _make_java_procedure("MyDb", "myProc", alias="MyJar"),
            ]
        )
        # One pass for the install script's own existence is NOT
        # emitted (only procedure-side checks fire), so we should
        # see exactly one entry — the failing reference.
        assert len(checks) == 1
        check = checks[0]
        assert check.passed is False
        assert check.severity == "ERROR"
        # Case-insensitive match: aliases are upper-cased in the report.
        assert "MYJAR" in check.message.upper()
        assert "OTHERJAR" in check.message.upper()

    def test_no_install_at_all_emits_error_with_none_summary(self):
        """When the package contains a Java procedure but no install
        script at all, the message says 'Installed aliases: (none)'
        — clearer than a bare empty list."""
        checks = _check_jar_alias_coverage(
            [
                _make_java_procedure("MyDb", "myProc", alias="MyJar"),
            ]
        )
        assert len(checks) == 1
        assert checks[0].passed is False
        assert "(none)" in checks[0].message

    def test_alias_match_is_case_insensitive(self):
        """Teradata identifiers are case-insensitive — an INSTALL_JAR
        with alias 'MyJar' must satisfy a procedure referencing
        'MYJAR' (and vice versa)."""
        checks = _check_jar_alias_coverage(
            [
                _make_jar_install("MyJar"),
                _make_java_procedure("Db", "p", alias="MYJAR"),
            ]
        )
        assert len(checks) == 1
        assert checks[0].passed is True

    def test_multiple_procedures_one_install(self):
        """A single install script can satisfy multiple procedures —
        each procedure produces its own (passing) check."""
        checks = _check_jar_alias_coverage(
            [
                _make_jar_install("SharedJar"),
                _make_java_procedure("Db", "p1", alias="SharedJar"),
                _make_java_procedure("Db", "p2", alias="SharedJar"),
            ]
        )
        assert len(checks) == 2
        assert all(c.passed for c in checks)

    def test_mixed_pass_and_fail(self):
        """Two procedures, only one with a matching install — the
        report carries both outcomes, one INFO pass and one ERROR
        fail."""
        checks = _check_jar_alias_coverage(
            [
                _make_jar_install("InstalledJar"),
                _make_java_procedure("Db", "good", alias="InstalledJar"),
                _make_java_procedure("Db", "bad", alias="MissingJar"),
            ]
        )
        assert len(checks) == 2
        passed = [c for c in checks if c.passed]
        failed = [c for c in checks if not c.passed]
        assert len(passed) == 1
        assert len(failed) == 1
        assert "INSTALLEDJAR" in passed[0].message.upper()
        assert "MISSINGJAR" in failed[0].message.upper()
