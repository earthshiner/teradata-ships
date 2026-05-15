"""
test_classifier.py — Tests for the rich content-based classifier
(``td_release_packager.classifier``).

Covers:
    1. Plain type detection (TABLE, VIEW, MACRO, etc.)
    2. Sub-types (FUNCTION_C, FUNCTION_SQL, PROCEDURE_JAVA,
       PROCEDURE_SPL)
    3. External-reference extraction for C UDFs and Java procedures
    4. Filename-vs-content mismatch warnings
    5. Confidence labels — HIGH for explicit dialect, MEDIUM for
       defaulted, LOW for filename mismatch
    6. base_type() helper
"""

from __future__ import annotations


from td_release_packager import classifier as cls


# ---------------------------------------------------------------
# Plain types
# ---------------------------------------------------------------


class TestPlainTypes:
    def test_create_table(self):
        r = cls.classify("foo.tbl", "CREATE MULTISET TABLE x.t (id INT);")
        assert r.type == "TABLE"
        assert r.confidence == "HIGH"

    def test_create_view(self):
        r = cls.classify("v.viw", "CREATE VIEW x.v AS SELECT 1 AS a;")
        assert r.type == "VIEW"
        assert r.confidence == "HIGH"

    def test_create_macro(self):
        r = cls.classify("m.mcr", "CREATE MACRO x.m AS (SELECT 1);")
        assert r.type == "MACRO"

    def test_create_database(self):
        r = cls.classify("d.db", "CREATE DATABASE x;")
        assert r.type == "DATABASE"

    def test_jar_install_script(self):
        r = cls.classify(
            "install.sjr",
            "CALL SQLJ.INSTALL_JAR('CJ!../foo.jar', 'jar_alias', 0);",
        )
        assert r.type == "JAR"
        assert r.confidence == "HIGH"

    def test_jar_replace_script(self):
        r = cls.classify(
            "replace.sjr",
            "CALL SQLJ.REPLACE_JAR('CJ!../foo.jar', 'jar_alias');",
        )
        assert r.type == "JAR"

    def test_unclassified_returns_none(self):
        r = cls.classify("random.sql", "SELECT 1 AS dummy;")
        assert r.type is None


# ---------------------------------------------------------------
# DML — INSERT / UPDATE / DELETE / MERGE
# ---------------------------------------------------------------


class TestDMLPatterns:
    """SHIPS classifies INSERT/UPDATE/DELETE/MERGE as ``DML`` so that
    seed-data, registration metadata, and reference-data loads land in
    ``payload/database/DML/`` and ship with the package — not silently
    dropped as 'unclassified'."""

    def test_insert_into(self):
        r = cls.classify(
            "seed.dml",
            "INSERT INTO db.t (id, name) VALUES (1, 'a');",
        )
        assert r.type == "DML"
        assert r.confidence == "HIGH"

    def test_insert_with_select(self):
        r = cls.classify(
            "load.sql",
            "INSERT INTO db.t SELECT * FROM db.staging;",
        )
        assert r.type == "DML"

    def test_update_set(self):
        r = cls.classify(
            "fix.sql",
            "UPDATE db.t SET name = 'x' WHERE id = 1;",
        )
        assert r.type == "DML"

    def test_update_from(self):
        """Teradata's UPDATE-FROM syntax: target then FROM clause then SET."""
        r = cls.classify(
            "fix.sql",
            "UPDATE db.t FROM db.other o SET t.name = o.name WHERE t.id = o.id;",
        )
        assert r.type == "DML"

    def test_delete_from(self):
        r = cls.classify(
            "purge.sql",
            "DELETE FROM db.t WHERE id < 100;",
        )
        assert r.type == "DML"

    def test_merge_into(self):
        r = cls.classify(
            "upsert.sql",
            "MERGE INTO db.t USING db.src s ON t.id = s.id "
            "WHEN MATCHED THEN UPDATE SET t.x = s.x "
            "WHEN NOT MATCHED THEN INSERT (id, x) VALUES (s.id, s.x);",
        )
        assert r.type == "DML"

    def test_update_statistics_classified_as_statistics_not_dml(self):
        """``UPDATE STATISTICS`` is a Teradata synonym for
        ``COLLECT STATISTICS`` — must classify as STATISTICS, not DML."""
        r = cls.classify("stats.stt", "UPDATE STATISTICS ON db.t;")
        assert r.type == "STATISTICS"

    def test_collect_statistics_still_classified_as_statistics(self):
        """Existing COLLECT STATISTICS path must keep working."""
        r = cls.classify("stats.stt", "COLLECT STATISTICS ON db.t;")
        assert r.type == "STATISTICS"

    def test_delete_database_not_classified_as_dml(self):
        """Teradata's destructive ``DELETE DATABASE foo ALL`` is a teardown
        operation, not a deployable DML statement. Without ``FROM`` it must
        not match the DML pattern."""
        r = cls.classify("teardown.sql", "DELETE DATABASE foo ALL;")
        assert r.type is None

    def test_procedure_with_embedded_dml_classifies_as_procedure(self):
        """A CREATE PROCEDURE whose body contains INSERT/UPDATE must
        classify as PROCEDURE — DML patterns are last in the table so
        the earlier PROCEDURE pattern wins."""
        ddl = (
            "CREATE PROCEDURE db.refresh_summary()\n"
            "BEGIN\n"
            "    DELETE FROM db.summary;\n"
            "    INSERT INTO db.summary SELECT * FROM db.detail;\n"
            "    UPDATE db.summary SET refreshed_at = CURRENT_TIMESTAMP;\n"
            "END;"
        )
        r = cls.classify("refresh.spl", ddl)
        assert r.type == "PROCEDURE_SPL"

    def test_dml_has_dml_extension_and_subdir(self):
        """DML files map to ``.dml`` extension and ``DML`` subdir."""
        assert cls.TYPE_TO_EXTENSION["DML"] == ".dml"
        assert cls.TYPE_TO_SUBDIR["DML"] == "DML"


# ---------------------------------------------------------------
# Sub-types
# ---------------------------------------------------------------


class TestFunctionSubtypes:
    def test_function_c_explicit_language(self):
        ddl = (
            "CREATE FUNCTION x.foo (a INT) RETURNS INT\n"
            "LANGUAGE C\n"
            "NO SQL\n"
            "PARAMETER STYLE SQL\n"
            "EXTERNAL NAME 'CS!foo!../foo.c!CH!foo_h!../foo.h';"
        )
        r = cls.classify("foo.fnc", ddl)
        assert r.type == "FUNCTION_C"
        assert r.base_type == "FUNCTION"
        assert r.confidence == "HIGH"

    def test_function_sql_default(self):
        """No LANGUAGE clause → SQL function."""
        ddl = "CREATE FUNCTION x.add_one (a INT) RETURNS INT RETURN a + 1;"
        r = cls.classify("foo.fnc", ddl)
        assert r.type == "FUNCTION_SQL"
        assert r.base_type == "FUNCTION"
        # No explicit LANGUAGE → MEDIUM confidence
        assert r.confidence == "MEDIUM"

    def test_function_sql_explicit_language(self):
        ddl = "CREATE FUNCTION x.add_one (a INT) RETURNS INT LANGUAGE SQL RETURN a + 1;"
        r = cls.classify("foo.fnc", ddl)
        assert r.type == "FUNCTION_SQL"


class TestProcedureSubtypes:
    def test_procedure_java(self):
        ddl = (
            "CREATE PROCEDURE x.foo()\n"
            "LANGUAGE JAVA\n"
            "PARAMETER STYLE JAVA\n"
            "EXTERNAL NAME 'jar_alias:com.example.Foo.bar';"
        )
        r = cls.classify("foo.spl", ddl)
        assert r.type == "PROCEDURE_JAVA"
        assert r.base_type == "PROCEDURE"
        assert r.confidence == "HIGH"

    def test_procedure_spl_default(self):
        """No LANGUAGE clause → SPL (Teradata's default for procedures)."""
        ddl = (
            "CREATE PROCEDURE x.update_thing (IN p INT)\n"
            "BEGIN\n"
            "  UPDATE x.t SET v = p;\n"
            "END;"
        )
        r = cls.classify("foo.spl", ddl)
        assert r.type == "PROCEDURE_SPL"
        assert r.base_type == "PROCEDURE"

    def test_procedure_cpp(self):
        ddl = (
            "CREATE PROCEDURE x.raise_exception(IN p CHAR(6))\n"
            "LANGUAGE CPP\n"
            "NO SQL\n"
            "PARAMETER STYLE SQL\n"
            "EXTERNAL NAME 'CS!RaiseException!../P_GCFR_XSP/RaiseException.cpp!F!RaiseException';"
        )
        r = cls.classify("raise_exception.spl", ddl)
        assert r.type == "PROCEDURE_CPP"
        assert r.base_type == "PROCEDURE"
        assert r.confidence == "HIGH"


# ---------------------------------------------------------------
# External-reference extraction
# ---------------------------------------------------------------


class TestExternalReferences:
    def test_c_udf_externals_extracted(self):
        ddl = (
            "CREATE FUNCTION x.foo (a INT) RETURNS INT\n"
            "LANGUAGE C NO SQL\n"
            "EXTERNAL NAME 'CS!foo!../FOO/foo.c!CH!foo_h!../FOO/foo.h';"
        )
        r = cls.classify("foo.fnc", ddl)
        assert r.type == "FUNCTION_C"
        # Both .c and .h paths captured, in declared order
        assert "../FOO/foo.c" in r.related_files
        assert "../FOO/foo.h" in r.related_files

    def test_c_udf_short_form_external(self):
        ddl = (
            "CREATE FUNCTION x.foo (a INT) RETURNS INT\n"
            "LANGUAGE C NO SQL\n"
            "EXTERNAL NAME 'CS!../FOO/foo.c';"
        )
        r = cls.classify("foo.fnc", ddl)
        assert r.type == "FUNCTION_C"
        assert "../FOO/foo.c" in r.related_files

    def test_c_udf_no_external_warns(self):
        """FUNCTION_C with no EXTERNAL NAME → warning."""
        ddl = "CREATE FUNCTION x.foo (a INT) RETURNS INT LANGUAGE C NO SQL;"
        r = cls.classify("foo.fnc", ddl)
        assert r.type == "FUNCTION_C"
        assert any("no .c/.h" in w for w in r.warnings)

    def test_java_procedure_jar_alias_extracted(self):
        ddl = (
            "CREATE PROCEDURE x.foo()\n"
            "LANGUAGE JAVA\n"
            "EXTERNAL NAME 'jar_execute_large_sql:com.example.Foo.bar';"
        )
        r = cls.classify("foo.spl", ddl)
        assert r.type == "PROCEDURE_JAVA"
        assert r.related_files == ["jar_execute_large_sql"]

    def test_java_procedure_no_alias_warns(self):
        """PROCEDURE_JAVA without a colon-separated alias → warning."""
        ddl = (
            "CREATE PROCEDURE x.foo()\n"
            "LANGUAGE JAVA\n"
            "EXTERNAL NAME 'plain_string_no_colon';"
        )
        r = cls.classify("foo.spl", ddl)
        assert r.type == "PROCEDURE_JAVA"
        assert any("no JAR alias" in w for w in r.warnings)

    def test_cpp_procedure_source_external_extracted(self):
        ddl = (
            "CREATE PROCEDURE x.raise_exception(IN p CHAR(6))\n"
            "LANGUAGE CPP NO SQL PARAMETER STYLE SQL\n"
            "EXTERNAL NAME 'CS!RaiseException!../P_GCFR_XSP/RaiseException.cpp!F!RaiseException';"
        )
        r = cls.classify("raise_exception.spl", ddl)
        assert r.type == "PROCEDURE_CPP"
        assert r.related_files == ["../P_GCFR_XSP/RaiseException.cpp"]


# ---------------------------------------------------------------
# Filename mismatch
# ---------------------------------------------------------------


class TestFilenameMismatch:
    def test_filename_says_table_but_content_is_view(self):
        r = cls.classify("foo.tbl", "CREATE VIEW x.v AS SELECT 1;")
        assert r.type == "VIEW"  # content wins
        assert any("Filename mismatch" in w for w in r.warnings)
        # LOW confidence because the filename was misleading
        assert r.confidence == "LOW"

    def test_consistent_filename_no_warning(self):
        r = cls.classify("foo.tbl", "CREATE TABLE x.t (id INT);")
        assert not any("Filename mismatch" in w for w in r.warnings)
        assert r.confidence == "HIGH"

    def test_generic_extension_no_warning(self):
        r = cls.classify("foo.sql", "CREATE TABLE x.t (id INT);")
        assert not any("Filename mismatch" in w for w in r.warnings)

    def test_subtype_satisfies_base_extension(self):
        """A .fnc file containing FUNCTION_C should NOT warn — the
        sub-type is included in the .fnc expected set."""
        ddl = (
            "CREATE FUNCTION x.foo (a INT) RETURNS INT\n"
            "LANGUAGE C NO SQL EXTERNAL NAME 'CS!../foo.c';"
        )
        r = cls.classify("foo.fnc", ddl)
        assert not any("Filename mismatch" in w for w in r.warnings)

    def test_real_user_case_ddl_extension_with_jar_install(self):
        """The user's GCFR_UT_Install_Jar.ddl — file is named .ddl
        but content is a JAR install script. .ddl is generic so no
        mismatch warning."""
        ddl = (
            "DATABASE {{GCFR_P_UT}};\n"
            "CALL SQLJ.INSTALL_JAR('CJ!../JAVA/JAR/foo.jar', 'foo_alias', 0);"
        )
        r = cls.classify("install.ddl", ddl)
        assert r.type == "JAR"
        # Generic .ddl → no mismatch
        assert not any("Filename mismatch" in w for w in r.warnings)


# ---------------------------------------------------------------
# Confidence labelling
# ---------------------------------------------------------------


class TestConfidence:
    def test_grant_only_match_is_medium(self):
        """Single-keyword DCL match is more prone to false positives;
        rate it MEDIUM rather than HIGH."""
        r = cls.classify("g.dcl", "GRANT SELECT ON x.t TO u;")
        assert r.type == "GRANT"
        assert r.confidence == "MEDIUM"

    def test_filename_mismatch_drops_to_low(self):
        r = cls.classify("foo.viw", "CREATE TABLE x.t (id INT);")
        assert r.confidence == "LOW"


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


class TestBaseType:
    def test_subtype_maps_to_base(self):
        assert cls.base_type("FUNCTION_C") == "FUNCTION"
        assert cls.base_type("FUNCTION_SQL") == "FUNCTION"
        assert cls.base_type("PROCEDURE_JAVA") == "PROCEDURE"
        assert cls.base_type("PROCEDURE_SPL") == "PROCEDURE"

    def test_plain_type_passthrough(self):
        assert cls.base_type("TABLE") == "TABLE"
        assert cls.base_type("VIEW") == "VIEW"

    def test_none_passthrough(self):
        assert cls.base_type(None) is None


class TestExtractCExternals:
    def test_alias_form(self):
        paths = cls.extract_c_externals("CS!foo!../foo.c!CH!foo_h!../foo.h")
        assert paths == ["../foo.c", "../foo.h"]

    def test_short_form(self):
        paths = cls.extract_c_externals("CS!../foo.c")
        assert paths == ["../foo.c"]

    def test_empty_body(self):
        assert cls.extract_c_externals("") == []


class TestExtractJarAlias:
    def test_alias_extracted(self):
        assert cls.extract_jar_alias("my_jar:com.x.Foo.bar") == "my_jar"

    def test_no_colon_returns_none(self):
        assert cls.extract_jar_alias("plain_string") is None
