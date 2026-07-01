"""Lockstep between the fix registry and the rules catalogue.

The fix registry (``td_release_packager.fixers.FIX_REGISTRY``) and the
rules catalogue (``td_release_packager.rules_catalogue._RULES``) are
two hand-maintained sources of truth that need to agree:

* Every registered fixer must target a real catalogue rule (``rule_id``
  present in the catalogue) whose ``safe_fix_available`` is ``True`` —
  otherwise a fixer runs against a rule the catalogue claims can't be
  auto-fixed, and agents that consult the catalogue before dispatching
  a fix will get contradictory signals.

* Every catalogue rule marked ``safe_fix_available: True`` must either
  have a fixer registered, OR carry an explicit
  ``no_fixer_yet: True`` marker acknowledging the gap. This prevents
  a rule from being silently promoted to "auto-fixable" in the catalogue
  without anyone building the corresponding fixer.

These are the same invariants the decision-tree YAML ↔ HTML lockstep
test enforces for the SHIPS Navigator — a small self-consistency check
that costs nothing to run and catches "documentation says X, code does
Y" drift the moment it appears.
"""

from __future__ import annotations

from td_release_packager.fixers import FIX_REGISTRY
from td_release_packager.rules_catalogue import _RULES


class TestFixersMatchCatalogue:
    def test_every_registered_fixer_maps_to_a_catalogue_rule(self):
        missing = sorted(rid for rid in FIX_REGISTRY if rid not in _RULES)
        assert not missing, (
            "fixers registered against rules that don't exist in "
            f"rules_catalogue._RULES: {missing}. Add a catalogue entry or "
            "unregister the fixer."
        )

    def test_every_registered_fixer_targets_a_safe_fixable_rule(self):
        wrong_flag = sorted(
            rid
            for rid in FIX_REGISTRY
            if rid in _RULES and not _RULES[rid].get("safe_fix_available")
        )
        assert not wrong_flag, (
            "fixers registered against rules whose catalogue entry says "
            f"safe_fix_available=False: {wrong_flag}. Flip the catalogue "
            "entry to True (with a matching remediation note), or drop the "
            "fixer."
        )

    def test_every_safe_fixable_catalogue_rule_has_a_fixer_or_marker(self):
        gap = sorted(
            rid
            for rid, meta in _RULES.items()
            if meta.get("safe_fix_available")
            and rid not in FIX_REGISTRY
            and not meta.get("no_fixer_yet")
        )
        assert not gap, (
            "catalogue rules marked safe_fix_available=True but with no "
            f"registered fixer and no `no_fixer_yet: True` marker: {gap}. "
            "Either register a fixer for the rule or add `no_fixer_yet: "
            "True` to the catalogue entry to acknowledge the gap."
        )


class TestFixerSpecShape:
    def test_all_specs_have_a_valid_write_scope(self):
        # write_scope is validated at construction, so this really just
        # asserts every spec constructed successfully — but keeping the
        # assertion in the lockstep file means adding a new scope value
        # forces a matching test update, which forces a conversation.
        allowed = {"payload", "config"}
        bad = sorted(
            (spec.rule_id, spec.write_scope)
            for spec in FIX_REGISTRY.values()
            if spec.write_scope not in allowed
        )
        assert not bad, f"fixer specs with write_scope not in {sorted(allowed)}: {bad}"

    def test_default_on_specs_all_low_risk(self):
        """Default-on fixers should be low-risk per the catalogue.

        Rationale: a `ships fix` invocation with no flags will apply every
        default-on fixer. If a medium/high-risk fixer sneaks in as
        default-on, a routine `ships fix` run could produce surprising
        mutations. Opt-in-only (default_on=False) for anything above low.
        """
        risky_defaults = sorted(
            spec.rule_id
            for spec in FIX_REGISTRY.values()
            if spec.default_on
            and _RULES.get(spec.rule_id, {}).get("risk") not in {"low", None}
        )
        assert not risky_defaults, (
            f"fixers marked default_on=True but with risk != 'low' in the "
            f"catalogue: {risky_defaults}. Either downgrade the catalogue "
            "risk or flip the fixer to opt-in (default_on=False)."
        )
