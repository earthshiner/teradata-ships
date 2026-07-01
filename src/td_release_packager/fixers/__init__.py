"""Automated fixers for inspect-catalogue rules.

This package is the single source of truth for the fixer registry. Both
the CLI (``ships fix`` — see #521 — and ``ships inspect``'s legacy
``--fix-*`` flags until #522 removes them) and the MCP server
(``ships_fix``, ``ships_list_fixable_rules``) consume from
:data:`FIX_REGISTRY` here.

Each fixer lives in its own module under this package. Importing that
module calls :func:`register` at module scope, so ``FIX_REGISTRY`` is
populated the first time this package is imported.

Adding a new fixer:

1. Create ``fixers/<rule>.py`` with a function returning :class:`FixResult`.
2. Call ``register(FixerSpec(...))`` at import time.
3. Add ``from td_release_packager.fixers import <rule>`` to this file.
4. Extend ``test_fixers_registry_catalogue_lockstep.py`` if the new rule
   changes the registered/unregistered split.
"""

from td_release_packager.fixers._registry import (
    FIX_REGISTRY,
    FixerSpec,
    default_on_rules,
    register,
    registered_fixers,
)
from td_release_packager.fixers._result import FixResult, FixResultFile

# Register built-in fixers by importing their modules. Each module calls
# `register(...)` at import time; import order does not matter (rule ids
# are unique) but sorting alphabetically keeps diffs stable when new
# fixers land.
from td_release_packager.fixers import ddl_terminator  # noqa: E402, F401
from td_release_packager.fixers import non_ascii  # noqa: E402, F401

__all__ = [
    "FIX_REGISTRY",
    "FixResult",
    "FixResultFile",
    "FixerSpec",
    "default_on_rules",
    "register",
    "registered_fixers",
]
