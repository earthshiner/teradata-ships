"""
td_release_packager — SHIPS: Scaffold, Harvest, Inspect, Package, Ship
======================================================================

Standardised Teradata DDL deployment methodology.

    Scaffold:  python -m td_release_packager scaffold --name MyProject
    Harvest:   python -m td_release_packager harvest --source /raw/ --project .
    Inspect:   python -m td_release_packager inspect --source .
    Package:   python -m td_release_packager package --source . --env DEV ...
    Ship:      python deploy.py --host myserver --user dbc
"""

__version__ = "1.1.0"

from td_release_packager.builder import build_package
from td_release_packager.scaffolder import scaffold_project
from td_release_packager.build_counter import next_build_number, read_build_number
from td_release_packager.token_engine import (
    read_properties,
    substitute_tokens,
    validate_tokens,
)
from td_release_packager.models import (
    BuildConfig,
    BuildManifest,
    DeployPhase,
)
