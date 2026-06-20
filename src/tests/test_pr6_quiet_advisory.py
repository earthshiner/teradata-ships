"""
test_pr6_quiet_advisory.py — PR6 of the deterministic-deploy programme.

Covers the two code-side changes PR6 ships:

  * Stock provisioning tokens (``PERM_SPACE``, ``SPOOL_SPACE``,
    ``TEMP_SPACE``) are excluded from the "unreferenced token"
    warning that surfaces in inspect, package, and the trust report.
    A faithful reverse-harvested build no longer lands in
    ``READY_WITH_CAVEATS`` purely for the cosmetic reason that the
    scaffolded env config defines a few stock tokens the DDL doesn't
    interpolate.
  * The renamed ``warn_orphan_grants`` key (now
    ``warn_external_grants``) is no longer silently accepted. An old
    inspect.conf that still carries it raises a clear error at config-
    read time so the operator updates the key rather than inheriting
    the new INFO default unaware.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------
# Stock-token exclusion from "unreferenced" warnings
# ---------------------------------------------------------------


def test_stock_provisioning_tokens_not_warned_when_unused() -> None:
    """``PERM_SPACE`` / ``SPOOL_SPACE`` / ``TEMP_SPACE`` define
    provisioning intent rather than substituting into DDL bodies, so
    "never referenced in payload" is structural — not a deficiency.
    They are excluded from the unreferenced-token warning list."""
    from td_release_packager.token_engine import validate_tokens

    token_values = {
        "PERM_SPACE": "1G",
        "SPOOL_SPACE": "4G",
        "TEMP_SPACE": "1G",
        "DB_PREFIX": "CallCentre",
    }
    token_usage = {
        # A single DDL file references only DB_PREFIX.
        "payload/database/DDL/tables/Customer.tbl": {"DB_PREFIX"},
    }

    errors, warnings = validate_tokens(token_values, token_usage)

    assert errors == [], "Reference was satisfied — no errors expected"
    # The three stock tokens MUST NOT appear in the warnings; the
    # implementation is allowed to emit other warnings unrelated to
    # this rule.
    joined = "\n".join(warnings)
    for stock in ("PERM_SPACE", "SPOOL_SPACE", "TEMP_SPACE"):
        assert f"{{{{{stock}}}}}" not in joined, (
            f"Stock provisioning token {stock} should be excluded from "
            f"the unreferenced-token warning. Got: {joined}"
        )


def test_non_stock_unreferenced_token_still_warned() -> None:
    """Tokens that aren't on the reserved/stock list still produce the
    warning — the exclusion is targeted, not a blanket suppression."""
    from td_release_packager.token_engine import validate_tokens

    token_values = {
        "LEGACY_UNUSED_TOKEN": "value",
        "DB_PREFIX": "CallCentre",
    }
    token_usage = {
        "payload/database/DDL/tables/Customer.tbl": {"DB_PREFIX"},
    }

    _errors, warnings = validate_tokens(token_values, token_usage)

    joined = "\n".join(warnings)
    assert "{{LEGACY_UNUSED_TOKEN}}" in joined, (
        "Non-stock unreferenced tokens should still surface as warnings."
    )


def test_reserved_metadata_keys_remain_excluded() -> None:
    """Regression guard: the original three reserved keys
    (``SHIPS_ENV``, ``SHIPS_PROJECT``, ``ENV_PREFIX``) stay excluded
    after the stock-token additions."""
    from td_release_packager.token_engine import validate_tokens

    token_values = {
        "SHIPS_ENV": "DEV",
        "SHIPS_PROJECT": "CallCentre",
        "ENV_PREFIX": "A_D01",
        "DB_PREFIX": "CallCentre",
    }
    token_usage = {
        "payload/database/DDL/tables/Customer.tbl": {"DB_PREFIX"},
    }

    _errors, warnings = validate_tokens(token_values, token_usage)

    joined = "\n".join(warnings)
    for reserved in ("SHIPS_ENV", "SHIPS_PROJECT", "ENV_PREFIX"):
        assert f"{{{{{reserved}}}}}" not in joined


# ---------------------------------------------------------------
# warn_orphan_grants retired-key shim
# ---------------------------------------------------------------


def test_warn_orphan_grants_in_inspect_conf_raises_clear_error(
    tmp_path: Path,
) -> None:
    """An inspect.conf that still uses the retired
    ``warn_orphan_grants`` key fails the read with a message that
    names the new key (``warn_external_grants``) and the renaming
    rationale. Silently inheriting the new default would mask a
    config mistake."""
    from td_release_packager.validate import read_inspect_config

    conf = tmp_path / "inspect.conf"
    conf.write_text(
        "# stale config\nwarn_orphan_grants=ERROR\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc:
        read_inspect_config(str(conf))

    msg = str(exc.value)
    assert "warn_orphan_grants" in msg
    assert "warn_external_grants" in msg
    # The error mentions the line number so the operator can jump
    # straight to the offending row in their config.
    assert "line 2" in msg


def test_warn_external_grants_is_accepted(tmp_path: Path) -> None:
    """The renamed key reads cleanly. Regression guard for the shim."""
    from td_release_packager.validate import read_inspect_config

    conf = tmp_path / "inspect.conf"
    conf.write_text("warn_external_grants=WARNING\n", encoding="utf-8")

    rules = read_inspect_config(str(conf))
    assert rules["warn_external_grants"] == "WARNING"
