"""
cascade.py — Five-layer configuration resolver.

The orchestrator design ranks configuration sources from highest
to lowest precedence:

    Layer 5  CLI flags                          (per-invocation)
    Layer 4  Environment properties             (per-environment)
    Layer 3  Project config (ships.yaml et al)  (committed to git)
    Layer 2  Platform template (--template)     (cross-project)
    Layer 1  Hard-coded defaults                (in code)

For any setting, the highest layer that defines a value wins; lower
layers provide fallbacks. The resolved view (value plus the source
layer that supplied it) is what gets recorded to ``decisions.json``,
so callers don't just need the value — they need provenance.

This module exposes a single class, ``Cascade``, plus the supporting
``LayerSource`` enum and ``ResolvedSetting`` dataclass.

Settings are addressed by **dotted path** (e.g. ``stages.generate.strict``).
Each layer holds a plain dict; the resolver walks the path through
each layer in turn and returns the first hit.

Layer-violation rules from the design's per-setting matrix
(e.g. token VALUES are env-only) are enforced via
``register_layer_constraint()`` — a simple set of restricted paths
per layer. Violations raise ``CascadeConfigError`` at construction
time so the user gets a precise pointer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------
# Public types
# ---------------------------------------------------------------


class LayerSource(Enum):
    """The five cascade layers, ordered low → high precedence."""

    LAYER_1_DEFAULTS = "layer-1"
    LAYER_2_TEMPLATE = "layer-2"
    LAYER_3_PROJECT = "layer-3"
    LAYER_4_ENV = "layer-4"
    LAYER_5_CLI = "layer-5"


# Highest-precedence-first iteration order for resolution.
_RESOLUTION_ORDER: Tuple[LayerSource, ...] = (
    LayerSource.LAYER_5_CLI,
    LayerSource.LAYER_4_ENV,
    LayerSource.LAYER_3_PROJECT,
    LayerSource.LAYER_2_TEMPLATE,
    LayerSource.LAYER_1_DEFAULTS,
)


@dataclass(frozen=True)
class ResolvedSetting:
    """
    A resolved configuration setting, with provenance.

    Attributes:
        value:        The effective value used for this run.
        source:       Which layer supplied the value.
        source_path:  Best-effort filesystem origin (e.g. ``ships.yaml``,
                      ``config/properties/DEV.properties``, or
                      ``"default"`` / ``"cli"``). Useful in
                      ``decisions.json`` for human review.
    """

    value: Any
    source: LayerSource
    source_path: str


# ---------------------------------------------------------------
# Errors
# ---------------------------------------------------------------


class CascadeConfigError(Exception):
    """Raised on cascade construction or resolution failures."""


class SettingNotFound(KeyError):
    """Raised when ``Cascade.resolve()`` finds no value for a path."""


# ---------------------------------------------------------------
# Cascade
# ---------------------------------------------------------------


class Cascade:
    """
    Five-layer configuration resolver.

    Each layer is a plain dict (or ``None`` if not provided). The
    resolver walks a dotted path through the layers in precedence
    order — Layer 5 first, Layer 1 last — and returns the first
    hit as a ``ResolvedSetting``.

    Layer-violation constraints (e.g. token VALUES must come from
    Layer 4 only, never Layer 3) are enforced at construction:
    a path that is restricted to certain layers cannot have a value
    in any other layer. Construction raises ``CascadeConfigError``
    with a precise pointer if violated.

    Typical use::

        cascade = Cascade(
            defaults=LAYER_1_DEFAULTS,
            template=loaded_template_dict,
            project=loaded_ships_yaml_dict,
            env_properties=loaded_properties_dict,
            cli=parsed_cli_overrides,
            source_paths={
                LayerSource.LAYER_3_PROJECT: "ships.yaml",
                LayerSource.LAYER_4_ENV:     "config/properties/DEV.properties",
            },
        )
        strict = cascade.resolve("stages.generate.strict")
        # strict.value == True, strict.source == LayerSource.LAYER_3_PROJECT
    """

    def __init__(
        self,
        *,
        defaults: Optional[Dict[str, Any]] = None,
        template: Optional[Dict[str, Any]] = None,
        project: Optional[Dict[str, Any]] = None,
        env_properties: Optional[Dict[str, Any]] = None,
        cli: Optional[Dict[str, Any]] = None,
        source_paths: Optional[Dict[LayerSource, str]] = None,
        layer_constraints: Optional[Dict[str, List[LayerSource]]] = None,
    ):
        """
        Build a cascade from the five layer dicts.

        Args:
            defaults:        Layer 1 dict — typically ``LAYER_1_DEFAULTS``
                             from ``ships_yaml``.
            template:        Layer 2 dict — parsed ``--template`` file
                             (platform standard) or None.
            project:         Layer 3 dict — parsed ``ships.yaml`` or None.
            env_properties:  Layer 4 dict — parsed properties for the
                             active environment, or None.
            cli:             Layer 5 dict — explicit per-invocation
                             overrides supplied as a nested dict
                             (e.g. ``{"stages": {"generate": {"strict": True}}}``).
            source_paths:    Per-layer ``source_path`` strings used in
                             ``ResolvedSetting``. Layers without an entry
                             default to a generic label.
            layer_constraints: Optional map of dotted-path → list of
                             layers permitted to set that path. Any
                             layer NOT in the list that nevertheless
                             defines the path raises
                             ``CascadeConfigError``.

        Raises:
            CascadeConfigError: If any layer-constraint is violated.
        """
        self._layers: Dict[LayerSource, Optional[Dict[str, Any]]] = {
            LayerSource.LAYER_1_DEFAULTS: defaults,
            LayerSource.LAYER_2_TEMPLATE: template,
            LayerSource.LAYER_3_PROJECT: project,
            LayerSource.LAYER_4_ENV: env_properties,
            LayerSource.LAYER_5_CLI: cli,
        }

        default_paths = {
            LayerSource.LAYER_1_DEFAULTS: "default",
            LayerSource.LAYER_2_TEMPLATE: "template",
            LayerSource.LAYER_3_PROJECT: "ships.yaml",
            LayerSource.LAYER_4_ENV: "properties",
            LayerSource.LAYER_5_CLI: "cli",
        }
        if source_paths:
            default_paths.update(source_paths)
        self._source_paths: Dict[LayerSource, str] = default_paths

        self._constraints: Dict[str, List[LayerSource]] = (
            dict(layer_constraints) if layer_constraints else {}
        )
        self._validate_constraints()

    # ----- resolution ------------------------------------------

    def resolve(self, path: str) -> ResolvedSetting:
        """
        Resolve a dotted setting path against the cascade.

        Walks layers in precedence order (5 → 1) and returns the
        first hit. ``None`` values in a layer are treated as "not
        set" — only present-and-non-None counts as defined.

        Args:
            path: Dotted path, e.g. ``stages.generate.strict``.

        Returns:
            A ``ResolvedSetting`` recording the value, source layer,
            and source path.

        Raises:
            SettingNotFound: No layer defines a value for ``path``.
        """
        for layer in _RESOLUTION_ORDER:
            data = self._layers.get(layer)
            if data is None:
                continue
            found, value = _walk(data, path)
            if found:
                return ResolvedSetting(
                    value=value,
                    source=layer,
                    source_path=self._source_paths[layer],
                )
        raise SettingNotFound(f"no value for setting {path!r} in any layer")

    def get(self, path: str, default: Any = None) -> Any:
        """
        Resolve ``path`` and return the bare value, or ``default``
        if no layer supplies it.

        Args:
            path:    Dotted path.
            default: Returned if the path is unresolved.

        Returns:
            The resolved value, or ``default`` on miss.
        """
        try:
            return self.resolve(path).value
        except SettingNotFound:
            return default

    def has(self, path: str) -> bool:
        """True if any layer defines ``path``."""
        try:
            self.resolve(path)
            return True
        except SettingNotFound:
            return False

    # ----- introspection ---------------------------------------

    def layer_for(self, path: str) -> Optional[LayerSource]:
        """Return the source layer that supplies ``path``, or None."""
        try:
            return self.resolve(path).source
        except SettingNotFound:
            return None

    # ----- internal --------------------------------------------

    def _validate_constraints(self) -> None:
        """
        Enforce ``layer_constraints``.

        For every restricted path, check each layer NOT in the
        permitted list. If that layer defines a value at the path,
        raise ``CascadeConfigError``.
        """
        for path, allowed_layers in self._constraints.items():
            allowed_set = set(allowed_layers)
            for layer in LayerSource:
                if layer in allowed_set:
                    continue
                data = self._layers.get(layer)
                if data is None:
                    continue
                found, _ = _walk(data, path)
                if found:
                    allowed_names = ", ".join(sorted(L.value for L in allowed_layers))
                    raise CascadeConfigError(
                        f"{path}: not permitted at {layer.value} — "
                        f"allowed layers are: {allowed_names}. "
                        f"Source: {self._source_paths[layer]}"
                    )


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _walk(data: Dict[str, Any], path: str) -> Tuple[bool, Any]:
    """
    Walk a dotted path into a nested dict.

    Treats a present key with the value ``None`` as "not set", so a
    layer can explicitly clear a setting via ``key: null`` only if
    the consumer wants that semantic — for cascade resolution, a
    None value is invisible. (If you need null-as-value, wrap it.)

    Args:
        data: The layer dict to walk.
        path: Dotted path (e.g. ``stages.generate.strict``).

    Returns:
        ``(found, value)``. ``found`` is True only if every segment
        existed AND the final value is not None. ``value`` is
        meaningless when ``found`` is False.
    """
    cur: Any = data
    for segment in path.split("."):
        if not isinstance(cur, dict) or segment not in cur:
            return False, None
        cur = cur[segment]
    if cur is None:
        return False, None
    return True, cur
