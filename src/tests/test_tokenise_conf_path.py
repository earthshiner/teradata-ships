"""
test_tokenise_conf_path.py — canonical tokenise.conf path helper (#383).

Guards the single-source-of-truth path constant/helper and that no module
reintroduces a hard-coded ``config/tokenise.conf`` literal for file access.
"""

import os
import re
from pathlib import Path

from td_release_packager.project_paths import (
    TOKENISE_CONF_FILENAME,
    TOKENISE_CONF_RELPATH,
    tokenise_conf_path,
)

_SRC = Path(__file__).resolve().parents[1] / "td_release_packager"


class TestTokeniseConfPath:
    def test_helper_resolves_under_config(self):
        p = tokenise_conf_path(os.path.join("proj"))
        assert p.endswith(os.path.join("proj", "config", "tokenise.conf"))

    def test_constants(self):
        assert TOKENISE_CONF_FILENAME == "tokenise.conf"
        # The relative ref stays forward-slash for catalogue references.
        assert TOKENISE_CONF_RELPATH == "config/tokenise.conf"

    def test_no_hardcoded_join_literals_remain(self):
        """No module should rebuild the path with an inline join literal.

        The canonical surface is ``project_paths`` — everything else routes
        through ``tokenise_conf_path`` / ``TOKENISE_CONF_RELPATH``.
        """
        pattern = re.compile(
            r"""os\.path\.join\([^)]*["']config["']\s*,\s*["']tokenise\.conf["']""",
        )
        offenders = []
        for path in _SRC.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if pattern.search(text):
                offenders.append(path.name)
        assert not offenders, f"hard-coded tokenise.conf join in: {offenders}"
