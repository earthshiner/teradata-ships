"""
rollback.py — Feature rollback via git tag for SHIPS.

Feature rollback restores the database to a previous known-good version
by re-deploying from a git tag.  It is distinct from technical rollback
(which undoes a failed mid-deploy using pre-captured SHOW snapshots in
``_rollback/``).

Workflow
--------
1. Verify the tag exists in the local git repository.
2. Extract the tagged source tree via ``git archive`` into a temporary
   directory — no working-tree checkout is performed, so local
   uncommitted changes and the ``.build_counter`` are untouched.
3. Build a rollback package from the extracted source using the
   caller-supplied environment config (token values come from the
   current environment, not the tag, because infrastructure names
   evolve independently of object DDL).
4. Write the package to the output directory and return its path.

The build counter in the project directory is incremented so the
rollback package gets a unique, sequential build number.  The
``source_commit`` field in ``ships.build.json`` records the tag's commit hash,
making the rollback traceable in the audit trail.

Deployment
----------
The rollback package is a normal SHIPS package.  Deploy it with::

    python deploy.py --host <host> --user <user> --on-drift continue

``--on-drift continue`` is the recommended default for rollbacks: any
out-of-band changes made after the broken deploy may be part of the
problem.  The rollback's purpose is to restore the tagged version as the
authoritative schema — after deploy the drift baseline is updated to that
version.

If a hotfix must be preserved during rollback, use ``--on-drift skip``
instead.
"""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
import tempfile

from td_release_packager.builder import BuildConfig, build_package


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def verify_git_tag(project_dir: str, tag: str) -> str:
    """Verify that ``tag`` exists and return its commit hash.

    Args:
        project_dir: Root of the git repository.
        tag:         Tag name (e.g. ``v1.2.3``).

    Returns:
        The full commit SHA that the tag points to.

    Raises:
        ValueError: When the tag does not exist or git is not available.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/tags/{tag}"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise ValueError(
            "git is not available on PATH — cannot verify tag or extract source."
        )
    except subprocess.TimeoutExpired:
        raise ValueError("git rev-parse timed out after 30 seconds.")

    if result.returncode != 0:
        raise ValueError(
            f"Tag '{tag}' does not exist in the repository at {project_dir}.\n"
            f"  Run 'git tag -l' to list available tags.\n"
            f"  If the tag is on a remote, run 'git fetch --tags' first."
        )

    return result.stdout.strip()


def extract_tagged_source(project_dir: str, tag: str, dest_dir: str) -> None:
    """Extract the tagged source tree into ``dest_dir`` via ``git archive``.

    Uses ``git archive`` to produce a tar stream of the tag's tree and
    extracts it into ``dest_dir`` using Python's ``tarfile`` module.  The
    working tree is never modified — there is no checkout.

    Args:
        project_dir: Root of the git repository.
        tag:         Tag name.
        dest_dir:    Directory to extract into (must already exist).

    Raises:
        ValueError: When ``git archive`` fails.
    """
    try:
        result = subprocess.run(
            ["git", "archive", "--format=tar", tag],
            cwd=project_dir,
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(f"git archive timed out extracting tag '{tag}'.")

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git archive failed for tag '{tag}':\n  {stderr}")

    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as tf:
        tf.extractall(dest_dir, filter="data")


# ---------------------------------------------------------------------------
# Build counter helpers
# ---------------------------------------------------------------------------


def _read_build_number(project_dir: str) -> int:
    """Read the current build number from ``.ships/.build_counter``."""
    from td_release_packager.project_paths import build_counter_path

    counter_path = build_counter_path(project_dir)
    if not os.path.isfile(counter_path):
        raise FileNotFoundError(
            f"No .build_counter found at {counter_path}.\n"
            f"  Run 'td_release_packager scaffold' to create a project, or\n"
            f"  create .ships/.build_counter containing '0'."
        )
    return int(open(counter_path).read().strip())


def _write_build_number(project_dir: str, number: int) -> None:
    """Write ``number`` back to ``.ships/.build_counter``."""
    from td_release_packager.project_paths import (
        build_counter_path,
        ensure_ships_state_dir,
    )

    ensure_ships_state_dir(project_dir)
    counter_path = build_counter_path(project_dir)
    with open(counter_path, "w", encoding="utf-8") as f:
        f.write(str(number) + "\n")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_rollback_package(
    project_dir: str,
    tag: str,
    environment: str,
    env_config_file: str,
    package_name: str,
    output_dir: str,
    archive_format: str = "zip",
    author: str = "",
    description: str = "",
) -> tuple:
    """Build a rollback package from a git tag.

    Extracts the tagged source tree to a temporary directory and runs
    ``build_package`` against it using the supplied environment config.
    The build counter in ``project_dir`` is incremented so the rollback
    package gets a unique, sequential build number.

    Args:
        project_dir:    Root of the live project (contains ``.build_counter``
                        and is a git repository).
        tag:            Git tag to roll back to (e.g. ``v1.2.3``).
        environment:    Target environment string (e.g. ``PRD``).
        env_config_file: Absolute path to the environment ``.conf`` file.
                        Comes from the *current* project, not the tag —
                        infrastructure names evolve independently of DDL.
        package_name:   Package name for the archive filename.
        output_dir:     Directory where the archive will be written.
        archive_format: ``zip`` or ``tar.gz`` (default: ``zip``).
        author:         Optional author metadata for ships.build.json.
        description:    Optional description; defaults to
                        ``Rollback to <tag>`` when empty.

    Returns:
        ``(main_pair, companion_pair)`` exactly as ``build_package`` does,
        where each pair is ``(archive_path, BuildManifest)`` or ``None``.

    Raises:
        ValueError: When the tag does not exist, git is unavailable, or the
                    build fails.
        FileNotFoundError: When ``.build_counter`` is absent.
    """
    # 1. Verify the tag and get its commit hash for ships.build.json traceability.
    commit_hash = verify_git_tag(project_dir, tag)

    # 2. Increment the build counter in the live project so the rollback
    #    package gets a unique build number and future builds continue from
    #    the correct value.
    current = _read_build_number(project_dir)
    next_number = current + 1
    _write_build_number(project_dir, next_number)

    # 3. Extract the tagged source into a temp directory and build from it.
    with tempfile.TemporaryDirectory(prefix="ships_rollback_") as tmp_src:
        extract_tagged_source(project_dir, tag, tmp_src)

        rollback_description = description or f"Rollback to {tag}"

        config = BuildConfig(
            source_dir=tmp_src,
            environment=environment.upper(),
            package_name=package_name,
            env_config_file=env_config_file,
            build_number=next_number,
            output_dir=output_dir,
            archive_format=archive_format,
            author=author,
            description=rollback_description,
            source_commit=commit_hash,
            # Rollback from a tagged commit is always a clean source —
            # allow_dirty is irrelevant but defaults False for clarity.
            allow_dirty=False,
        )

        return build_package(config)
