"""
test_binary_harvester.py — Tests for the binary-dependency harvester
that brings .jar / .c / .h artefacts into the SHIPS payload alongside
the SQL scripts that reference them.

Covers:
    1. Resolution — relative paths resolved against the source script
    2. Kind detection from file extension
    3. Missing-source handling (warn, skip copy, leave path alone)
    4. Copy operations (overwrite + skip-existing)
    5. Path rewriting (longest-first, multiple refs in one content)
    6. End-to-end harvest_binaries pipeline
"""

from __future__ import annotations

from pathlib import Path


from td_release_packager import binary_harvester as bh


def _write_bin(path: Path, contents: bytes = b"\x00\x01") -> Path:
    """Create a fake binary file at ``path`` and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return path


# ---------------------------------------------------------------
# resolve_dependencies
# ---------------------------------------------------------------


class TestResolveDependencies:
    def test_relative_path_resolved_against_source(self, tmp_path):
        # Source script lives in <tmp>/scripts/install.sjr
        # Reference '../bins/foo.jar' should resolve to <tmp>/bins/foo.jar
        source = tmp_path / "scripts" / "install.sjr"
        source.parent.mkdir()
        source.write_text("...", encoding="utf-8")
        # Make the binary actually exist so .exists is True
        _write_bin(tmp_path / "bins" / "foo.jar")

        deps = bh.resolve_dependencies(
            related_paths=["../bins/foo.jar"],
            source_file_path=str(source),
            destination_dir=str(tmp_path / "out"),
        )
        assert len(deps) == 1
        d = deps[0]
        assert os.path.normcase(d.source_path) == os.path.normcase(
            str(tmp_path / "bins" / "foo.jar")
        )
        assert d.exists is True
        assert d.kind == "JAR_BINARY"
        assert d.new_ref == "./foo.jar"
        assert d.original_ref == "../bins/foo.jar"

    def test_missing_source_marked_not_existing(self, tmp_path):
        source = tmp_path / "scripts" / "install.sjr"
        source.parent.mkdir()
        source.write_text("...", encoding="utf-8")

        deps = bh.resolve_dependencies(
            related_paths=["../bins/missing.jar"],
            source_file_path=str(source),
            destination_dir=str(tmp_path / "out"),
        )
        assert deps[0].exists is False

    def test_kind_per_extension(self, tmp_path):
        source = tmp_path / "f.fnc"
        source.write_text("...", encoding="utf-8")
        _write_bin(tmp_path / "foo.c")
        _write_bin(tmp_path / "foo.h")
        _write_bin(tmp_path / "foo.cpp")

        deps = bh.resolve_dependencies(
            related_paths=["foo.c", "foo.h", "foo.cpp"],
            source_file_path=str(source),
            destination_dir=str(tmp_path / "out"),
        )
        kinds = [d.kind for d in deps]
        assert kinds == ["C_SOURCE", "C_HEADER", "CPP_SOURCE"]

    def test_absolute_path_normalised(self, tmp_path):
        source = tmp_path / "f.fnc"
        source.write_text("...", encoding="utf-8")
        binfile = _write_bin(tmp_path / "foo.jar")

        deps = bh.resolve_dependencies(
            related_paths=[str(binfile)],
            source_file_path=str(source),
            destination_dir=str(tmp_path / "out"),
        )
        assert deps[0].exists is True


# ---------------------------------------------------------------
# copy_binaries
# ---------------------------------------------------------------


class TestCopyBinaries:
    def test_copies_to_destination(self, tmp_path):
        src = _write_bin(tmp_path / "src" / "foo.jar", b"jar-bytes")
        dest = tmp_path / "out" / "foo.jar"

        dep = bh.BinaryDependency(
            original_ref="../src/foo.jar",
            source_path=str(src),
            destination_path=str(dest),
            new_ref="./foo.jar",
            kind="JAR_BINARY",
            exists=True,
        )

        copied = bh.copy_binaries([dep])
        assert dest.exists()
        assert dest.read_bytes() == b"jar-bytes"
        assert copied == [dep]

    def test_missing_source_excluded_from_result(self, tmp_path):
        dep = bh.BinaryDependency(
            original_ref="x",
            source_path=str(tmp_path / "nope.jar"),
            destination_path=str(tmp_path / "dest.jar"),
            new_ref="./dest.jar",
            kind="JAR_BINARY",
            exists=False,
        )
        copied = bh.copy_binaries([dep])
        assert copied == []
        assert not (tmp_path / "dest.jar").exists()

    def test_skip_existing_when_overwrite_false(self, tmp_path):
        src = _write_bin(tmp_path / "src" / "foo.jar", b"new-bytes")
        dest = _write_bin(tmp_path / "out" / "foo.jar", b"existing-bytes")

        dep = bh.BinaryDependency(
            original_ref="../src/foo.jar",
            source_path=str(src),
            destination_path=str(dest),
            new_ref="./foo.jar",
            kind="JAR_BINARY",
            exists=True,
        )
        copied = bh.copy_binaries([dep], overwrite=False)
        assert copied == []
        assert dest.read_bytes() == b"existing-bytes"


# ---------------------------------------------------------------
# rewrite_content
# ---------------------------------------------------------------


class TestRewriteContent:
    def _dep(self, original: str, new: str) -> bh.BinaryDependency:
        return bh.BinaryDependency(
            original_ref=original,
            source_path="/nonexistent",
            destination_path="/nonexistent",
            new_ref=new,
            kind="JAR_BINARY",
            exists=True,
        )

    def test_simple_replacement(self):
        content = "CALL X('CJ!../JAVA/JAR/foo.jar', 'alias', 0);"
        deps = [self._dep("../JAVA/JAR/foo.jar", "./foo.jar")]
        out = bh.rewrite_content(content, deps)
        assert "../JAVA/JAR/foo.jar" not in out
        assert "./foo.jar" in out

    def test_multiple_replacements(self):
        content = "EXTERNAL NAME 'CS!a!../C/foo.c!CH!a!../C/foo.h';"
        deps = [
            self._dep("../C/foo.c", "./foo.c"),
            self._dep("../C/foo.h", "./foo.h"),
        ]
        out = bh.rewrite_content(content, deps)
        assert "../C/foo.c" not in out
        assert "../C/foo.h" not in out
        assert "./foo.c" in out
        assert "./foo.h" in out

    def test_longest_first_avoids_prefix_overlap(self):
        """If '../foo' were replaced before '../foo.c', the second
        rewrite would have nothing recognisable left to act on
        because '../foo.c' would have already become './foo.c' via
        the prefix substitution. Sorting longest-first prevents
        that — both refs survive intact."""
        content = "ref: ../foo and ref: ../foo.c"
        deps = [
            self._dep("../foo", "./foo"),
            self._dep("../foo.c", "./foo.c"),
        ]
        out = bh.rewrite_content(content, deps)
        # Both originals replaced cleanly
        assert "../foo" not in out
        # And both targets present
        assert "./foo " in out  # space after to distinguish from ./foo.c
        assert "./foo.c" in out

    def test_no_deps_returns_content_unchanged(self):
        content = "abc"
        assert bh.rewrite_content(content, []) == "abc"


# ---------------------------------------------------------------
# harvest_binaries (end-to-end)
# ---------------------------------------------------------------


class TestHarvestBinariesEndToEnd:
    def test_jar_install_full_pipeline(self, tmp_path):
        """Mirror the user's GCFR_UT_Install_Jar.ddl scenario:
        source script in scripts/, JARs in JAVA/JAR/, expect both
        copied + path rewritten."""
        # Source layout (mirrors user's actual structure):
        #   <root>/scripts/install.ddl
        #   <root>/JAVA/JAR/ExecLargeSqlJ.jar
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        source_script = scripts_dir / "install.ddl"
        source_script.write_text("placeholder", encoding="utf-8")

        jar = _write_bin(tmp_path / "JAVA" / "JAR" / "ExecLargeSqlJ.jar", b"jar-bytes")

        content = (
            "CALL SQLJ.INSTALL_JAR("
            "'CJ!../JAVA/JAR/ExecLargeSqlJ.jar', "
            "'JAR_EXECUTE_LARGE_SQL', 0);"
        )
        dest_dir = tmp_path / "payload" / "DDL" / "jar_install"
        dest_dir.mkdir(parents=True)

        result = bh.harvest_binaries(
            content=content,
            related_paths=["../JAVA/JAR/ExecLargeSqlJ.jar"],
            source_file_path=str(source_script),
            destination_dir=str(dest_dir),
        )

        # JAR copied to destination
        assert (dest_dir / "ExecLargeSqlJ.jar").read_bytes() == b"jar-bytes"
        # Path rewritten in content
        assert "../JAVA/JAR/" not in result.rewritten_content
        assert "./ExecLargeSqlJ.jar" in result.rewritten_content
        # No missing
        assert result.missing == []
        assert len(result.copied) == 1

    def test_c_udf_full_pipeline(self, tmp_path):
        """C UDF references .c and .h files in a sibling directory.
        Both should be copied and paths rewritten."""
        fnc_dir = tmp_path / "fncs"
        fnc_dir.mkdir()
        source_fnc = fnc_dir / "foo.fnc"
        source_fnc.write_text("placeholder", encoding="utf-8")

        c_src = _write_bin(tmp_path / "C" / "foo.c", b"c-source")
        h_src = _write_bin(tmp_path / "C" / "foo.h", b"c-header")

        content = (
            "CREATE FUNCTION x.foo (a INT) RETURNS INT\n"
            "LANGUAGE C NO SQL\n"
            "EXTERNAL NAME 'CS!foo!../C/foo.c!CH!foo_h!../C/foo.h';"
        )
        dest_dir = tmp_path / "payload" / "DDL" / "functions"
        dest_dir.mkdir(parents=True)

        result = bh.harvest_binaries(
            content=content,
            related_paths=["../C/foo.c", "../C/foo.h"],
            source_file_path=str(source_fnc),
            destination_dir=str(dest_dir),
        )

        assert (dest_dir / "foo.c").read_bytes() == b"c-source"
        assert (dest_dir / "foo.h").read_bytes() == b"c-header"
        # Both paths rewritten
        assert "../C/foo.c" not in result.rewritten_content
        assert "../C/foo.h" not in result.rewritten_content
        assert "./foo.c" in result.rewritten_content
        assert "./foo.h" in result.rewritten_content

    def test_missing_binary_warns_and_leaves_path(self, tmp_path):
        """Reference to a non-existent binary produces a warning;
        the original path is NOT rewritten so the broken reference
        stays visible to the user."""
        source = tmp_path / "scripts" / "broken.ddl"
        source.parent.mkdir()
        source.write_text("placeholder", encoding="utf-8")

        content = "CALL X('CJ!../missing/nope.jar', 'a', 0);"
        dest_dir = tmp_path / "out"
        dest_dir.mkdir()

        result = bh.harvest_binaries(
            content=content,
            related_paths=["../missing/nope.jar"],
            source_file_path=str(source),
            destination_dir=str(dest_dir),
        )

        assert len(result.missing) == 1
        assert result.copied == []
        # Path NOT rewritten
        assert "../missing/nope.jar" in result.rewritten_content
        # Warning produced
        assert any("not found" in w for w in result.warnings)

    def test_empty_related_paths_no_op(self, tmp_path):
        """A file with no related paths returns its content
        unchanged and reports nothing."""
        result = bh.harvest_binaries(
            content="CREATE TABLE x.t (id INT);",
            related_paths=[],
            source_file_path=str(tmp_path / "x.tbl"),
            destination_dir=str(tmp_path / "out"),
        )
        assert result.rewritten_content == "CREATE TABLE x.t (id INT);"
        assert result.copied == []
        assert result.missing == []


# ---------------------------------------------------------------
# Module-level setup
# ---------------------------------------------------------------


# Used by `os.path.normcase` checks on Windows
import os  # noqa: E402
