"""
metadata_export — catalogue metadata export for AI-native data products (#244).

A neutral product-metadata model (``model``) extracted once from a SHIPS package
(``extract``), then projected into per-catalogue bundles (``alation``,
``collibra``). Adding a new catalogue means adding a renderer, not re-reading the
package.
"""

from td_release_packager.metadata_export.extract import (
    MetadataExtractError,
    extract_product_metadata,
)
from td_release_packager.metadata_export.model import ProductMetadata

#: Registry of available catalogue renderers: name -> render(meta, generated_at).
RENDERERS = {}


def _register() -> None:
    from td_release_packager.metadata_export import alation, collibra

    RENDERERS["alation"] = alation.render
    RENDERERS["collibra"] = collibra.render


_register()

__all__ = [
    "ProductMetadata",
    "MetadataExtractError",
    "extract_product_metadata",
    "RENDERERS",
]
