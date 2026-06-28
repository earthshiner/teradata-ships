"""
test_token_ref.py — shared token-reference vocabulary (#383 follow-up).

Guards the building blocks both eponymous_rename and infer_grants now compose
from, and confirms the two consumers agree on the canonical token-reference
grammar (whole-name token, prefix-token + suffix, literal, quoted object name).
"""

import re

from td_release_packager import token_ref
from td_release_packager.eponymous_rename import extract_eponymous_name
from td_release_packager.infer_grants import RE_TOKEN_REF


class TestBuildingBlocks:
    def test_token_atom_matches_whole_name_token(self):
        assert re.fullmatch(token_ref.TOKEN_ATOM, "{{DOM_STD_T}}")
        assert re.fullmatch(token_ref.TOKEN_ATOM, "{{DB_PREFIX}}")

    def test_token_atom_rejects_non_token(self):
        assert not re.fullmatch(token_ref.TOKEN_ATOM, "PlainName")

    def test_object_name_quoted_or_bare(self):
        assert re.fullmatch(token_ref.OBJECT_NAME, "Customer")
        assert re.fullmatch(token_ref.OBJECT_NAME, '"My Object"')

    def test_name_segment_keeps_prefix_token_whole(self):
        # The whole prefix-token + literal suffix must match (issue #309).
        m = re.fullmatch(token_ref.NAME_SEGMENT, "{{DB_PREFIX}}_DOM_STD_T")
        assert m and m.group(0) == "{{DB_PREFIX}}_DOM_STD_T"

    def test_db_literal_part_letter_start_bounded(self):
        assert re.match(token_ref.DB_LITERAL_PART, "CallCentre_DOM_BUS_V")
        # Must start with a letter (the bounded literal matcher).
        assert not re.match(token_ref.DB_LITERAL_PART + r"$", "_leading")


class TestConsumersAgree:
    def test_eponymous_parses_prefix_token_reference(self):
        result = extract_eponymous_name(
            "CREATE TABLE {{DB_PREFIX}}_DOM_STD_T.Customer (id INTEGER);"
        )
        assert result is not None
        _filename, qualified, obj_type = result
        assert qualified == "{{DB_PREFIX}}_DOM_STD_T.Customer"
        assert obj_type == "TABLE"

    def test_infer_grants_matches_prefix_token_reference(self):
        m = RE_TOKEN_REF.search("SELECT * FROM {{DB_PREFIX}}_DOM_BUS_V.call_summary")
        assert m is not None
        assert m.group(1) == "{{DB_PREFIX}}_DOM_BUS_V"
        assert m.group(2) == "call_summary"

    def test_infer_grants_matches_whole_name_token(self):
        m = RE_TOKEN_REF.search("SELECT * FROM {{DOM_DATABASE_V}}.thing")
        assert m is not None
        assert m.group(1) == "{{DOM_DATABASE_V}}"
