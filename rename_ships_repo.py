#!/usr/bin/env python3
"""
SHIPS Repository Rename: teradata-deployment-agent → teradata-ships

A fast, reliable Python script for renaming the repository across all files.

Usage:
    python3 rename_ships_repo.py --dry-run               # Preview changes
    python3 rename_ships_repo.py                          # Execute changes
    python3 rename_ships_repo.py --help                   # Show help
"""

import os
import sys
import argparse
import re
from pathlib import Path
from typing import List, Tuple

# ============================================================================
# Configuration
# ============================================================================

OLD_REPO_NAME = "teradata-deployment-agent"
NEW_REPO_NAME = "teradata-ships"
OLD_URL = "https://github.com/earthshiner/teradata-deployment-agent"
NEW_URL = "https://github.com/earthshiner/teradata-ships"

# File extensions to search
INCLUDE_EXTENSIONS = {
    ".py", ".toml", ".md", ".txt", ".yaml", ".yml",
    ".sh", ".ps1", ".json", ""  # empty string for files like Dockerfile, requirements
}

# Directory names to exclude
EXCLUDE_DIRS = {
    ".venv", "__pycache__", ".pytest_cache", "node_modules",
    ".git", ".egg-info", "dist", "build", ".claude", ".ships-demo"
}

# Files to never modify
SKIP_FILES = {
    "rename_ships_repo.sh", "rename_ships_repo.ps1", "rename_ships_repo.py"
}

# Docs to preserve (contain "agent" terminology describing features, not the tool name)
PRESERVE_DOCS = {
    "AGENT_INTEGRATION.md", "MISSION.md", "OPERATIONS_GUIDE.md"
}

# ============================================================================
# Patterns to replace (in order)
# ============================================================================

PATTERNS = [
    (OLD_URL, NEW_URL, "Repository URL"),
    (f'name = "{OLD_REPO_NAME}"', f'name = "{NEW_REPO_NAME}"', "Package name (with spaces)"),
    (f'name="{OLD_REPO_NAME}"', f'name="{NEW_REPO_NAME}"', "Package name (no spaces)"),
    (OLD_REPO_NAME, NEW_REPO_NAME, "Directory/path reference"),
    ("pip install teradata-deployment-agent", "pip install teradata-ships", "pip install"),
    ('pip install "teradata-deployment-agent', 'pip install "teradata-ships', "pip install with quotes"),
    ("pip install 'teradata-deployment-agent", "pip install 'teradata-ships", "pip install with single quotes"),
]

# ============================================================================
# Utility Functions
# ============================================================================

def should_skip_file(filepath: Path) -> bool:
    """Check if a file should be skipped."""
    filename = filepath.name
    
    # Skip the rename scripts
    if filename in SKIP_FILES:
        return True
    
    # Skip preserved documentation files
    if filename in PRESERVE_DOCS:
        return True
    
    # Skip files in excluded directories
    for exclude_dir in EXCLUDE_DIRS:
        if exclude_dir in filepath.parts:
            return True
    
    return False

def should_include_file(filepath: Path) -> bool:
    """Check if a file should be included in the search."""
    # Check extension
    ext = filepath.suffix
    name = filepath.name
    
    # Match by extension or by name (for Dockerfile, requirements files, etc.)
    if ext in INCLUDE_EXTENSIONS:
        return True
    
    if name == "Dockerfile" or "requirements" in name:
        return True
    
    return False

def find_files(repo_root: Path) -> List[Path]:
    """Find all files to process."""
    files = []
    
    for filepath in repo_root.rglob("*"):
        if not filepath.is_file():
            continue
        
        if should_skip_file(filepath):
            continue
        
        if should_include_file(filepath):
            files.append(filepath)
    
    return sorted(files)

def process_file(filepath: Path, pattern: str, replacement: str, dry_run: bool = True) -> int:
    """
    Process a single file with a pattern replacement.
    
    Returns the number of replacements made.
    """
    try:
        content = filepath.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return 0
    
    # Count occurrences
    count = content.count(pattern)
    
    if count == 0:
        return 0
    
    # Perform replacement
    new_content = content.replace(pattern, replacement)
    
    # Write back if not dry-run
    if not dry_run:
        filepath.write_text(new_content, encoding="utf-8")
    
    return count

def print_header(text: str) -> None:
    """Print a formatted header."""
    print()
    print("=" * 70)
    print(text)
    print("=" * 70)

def print_change(filepath: Path, pattern: str, replacement: str, count: int, dry_run: bool = True) -> None:
    """Print information about a change."""
    indicator = "[DRY RUN]" if dry_run else "[CHANGED]"
    print(f"{indicator} {filepath}")
    print(f"  - Found {count} occurrence(s)")
    print(f"  - Old: {pattern}")
    print(f"  - New: {replacement}")

# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Rename teradata-deployment-agent -> teradata-ships across the repository"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files"
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Repository root directory (default: current directory)"
    )
    
    args = parser.parse_args()
    repo_root = args.repo.resolve()
    dry_run = args.dry_run
    
    # Validate repository
    if not repo_root.exists():
        print(f"Error: Repository path does not exist: {repo_root}", file=sys.stderr)
        sys.exit(1)
    
    # Header
    print_header("SHIPS Repository Rename: teradata-deployment-agent -> teradata-ships")
    
    if dry_run:
        print("DRY RUN MODE: No files will be modified.")
    else:
        print("LIVE MODE: Files will be modified.")
    
    print(f"Repository root: {repo_root}\n")
    
    # Find files
    print("Scanning repository for files to process...", end="", flush=True)
    files = find_files(repo_root)
    print(f" Found {len(files)} files.\n")
    
    total_changes = 0
    
    # Process each pattern
    for pattern, replacement, pattern_name in PATTERNS:
        print_header(f"Pattern: {pattern_name}")
        print(f"Searching: {pattern}")
        print()
        
        files_changed = 0
        pattern_total_changes = 0
        
        for filepath in files:
            count = process_file(filepath, pattern, replacement, dry_run)
            
            if count > 0:
                # Get relative path for display
                try:
                    rel_path = filepath.relative_to(repo_root)
                except ValueError:
                    rel_path = filepath
                
                print_change(rel_path, pattern, replacement, count, dry_run)
                files_changed += 1
                pattern_total_changes += count
        
        if files_changed == 0:
            print(f"No matches found for: {pattern}\n")
        else:
            print(f"\nPattern summary: {files_changed} file(s), {pattern_total_changes} change(s)\n")
        
        total_changes += pattern_total_changes
    
    # Final summary
    print_header("Rename Operation Complete")
    
    if dry_run:
        print(f"DRY RUN COMPLETE: {total_changes} total change(s) would be made.")
        print("\nTo apply these changes, run without --dry-run flag.\n")
    else:
        print(f"RENAME COMPLETE: {total_changes} total change(s) made.")
        print("\nNEXT STEPS:")
        print("  1. Review changes: git diff")
        print("  2. Test the package: pip install -e .")
        print("  3. Run tests: pytest tests/")
        print('  4. Commit changes: git commit -m "refactor: rename teradata-deployment-agent -> teradata-ships"')
        print("  5. Update GitHub repository settings (Settings > General > Repository name)\n")

if __name__ == "__main__":
    main()
