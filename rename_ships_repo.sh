#!/bin/bash

# ============================================================================
# SHIPS Repository Rename: teradata-deployment-agent → teradata-ships
# ============================================================================
#
# IMPORTANT: This script performs find-and-replace operations. Review changes
# before committing. The script is safe (non-destructive backups enabled),
# but you should verify the diffs before pushing.
#
# Usage:
#   ./rename_ships_repo.sh --dry-run               # Preview changes only
#   ./rename_ships_repo.sh                          # Execute changes
#   ./rename_ships_repo.sh --backup                 # Backup before changes
#
# ============================================================================

set -euo pipefail

# ============================================================================
# Configuration
# ============================================================================

OLD_REPO_NAME="teradata-deployment-agent"
NEW_REPO_NAME="teradata-ships"
OLD_URL="https://github.com/earthshiner/teradata-deployment-agent"
NEW_URL="https://github.com/earthshiner/teradata-ships"

DRY_RUN=false
CREATE_BACKUP=false
REPO_ROOT="."

# ============================================================================
# Command-line argument parsing
# ============================================================================

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --backup)
            CREATE_BACKUP=true
            shift
            ;;
        --help)
            echo "Usage: $0 [OPTIONS] [REPO_ROOT]"
            echo ""
            echo "Options:"
            echo "  --dry-run      Preview changes only (do not modify files)"
            echo "  --backup       Create backup before making changes"
            echo "  --help         Show this help message"
            echo ""
            echo "Arguments:"
            echo "  REPO_ROOT      Repository root directory (default: current directory)"
            exit 0
            ;;
        *)
            REPO_ROOT="$1"
            shift
            ;;
    esac
done

# ============================================================================
# Logging & Utility Functions
# ============================================================================

# ANSI colour codes
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly CYAN='\033[0;36m'
readonly GRAY='\033[0;37m'
readonly NC='\033[0m' # No Colour

write_header() {
    echo ""
    echo -e "${CYAN}$(printf '=%.0s' {1..70})${NC}"
    echo -e "${CYAN}$1${NC}"
    echo -e "${CYAN}$(printf '=%.0s' {1..70})${NC}"
}

write_change() {
    local file="$1"
    local old="$2"
    local new="$3"
    local count="$4"
    
    if $DRY_RUN; then
        echo -e "${GREEN}[DRY RUN]${NC} $file"
    else
        echo -e "${GREEN}[CHANGED]${NC} $file"
    fi
    echo -e "  ${GRAY}- Found $count occurrence(s)${NC}"
    echo -e "  ${YELLOW}- Old: $old${NC}"
    echo -e "  ${GREEN}- New: $new${NC}"
}

should_skip_file() {
    local filepath="$1"
    local filename
    filename=$(basename "$filepath")
    
    # Skip the rename scripts themselves
    if [[ "$filename" == "rename_ships_repo.sh" ]] || \
       [[ "$filename" == "rename_ships_repo.ps1" ]]; then
        return 0
    fi
    
    # Skip preserved documentation files
    if [[ "$filepath" == *"AGENT_INTEGRATION.md" ]] || \
       [[ "$filepath" == *"MISSION.md" ]] || \
       [[ "$filepath" == *"OPERATIONS_GUIDE.md" ]]; then
        return 0
    fi
    
    # Skip excluded directories
    if [[ "$filepath" == *"/.venv"* ]] || \
       [[ "$filepath" == *"/__pycache__"* ]] || \
       [[ "$filepath" == *"/.pytest_cache"* ]] || \
       [[ "$filepath" == *"/node_modules"* ]] || \
       [[ "$filepath" == *"/.git"* ]] || \
       [[ "$filepath" == *".egg-info"* ]] || \
       [[ "$filepath" == */dist/* ]] || \
       [[ "$filepath" == */build/* ]] || \
       [[ "$filepath" == *"/.claude"* ]] || \
       [[ "$filepath" == *"/.ships-demo"* ]]; then
        return 0
    fi
    
    return 1
}

# ============================================================================
# Find & Replace Operations
# ============================================================================

replace_in_file() {
    local filepath="$1"
    local old_text="$2"
    local new_text="$3"
    
    # Use fgrep (fixed string grep, no regex) for reliable literal string matching
    if ! fgrep -q "$old_text" "$filepath" 2>/dev/null; then
        return 1
    fi
    
    # Count occurrences using fgrep -o
    local occurrences
    occurrences=$(fgrep -o "$old_text" "$filepath" 2>/dev/null | wc -l)
    
    # Perform replacement using sed (portable across Linux/macOS)
    if $DRY_RUN; then
        # Don't modify in dry-run mode
        :
    else
        # Escape special sed characters in the strings
        local old_escaped new_escaped
        old_escaped=$(printf '%s\n' "$old_text" | sed -e 's/[\/&]/\\&/g')
        new_escaped=$(printf '%s\n' "$new_text" | sed -e 's/[\/&]/\\&/g')
        
        # Use sed with -i flag (portable across Linux/macOS)
        if [[ "$OSTYPE" == "darwin"* ]]; then
            # macOS: -i requires backup extension (use empty string for no backup)
            sed -i '' "s/$old_escaped/$new_escaped/g" "$filepath"
        else
            # Linux
            sed -i "s/$old_escaped/$new_escaped/g" "$filepath"
        fi
    fi
    
    # Log the change
    write_change "$(realpath --relative-to="$REPO_ROOT" "$filepath" 2>/dev/null || echo "$filepath")" "$old_text" "$new_text" "$occurrences"
    
    return 0
}

rename_pattern() {
    local pattern="$1"
    local replacement="$2"
    local pattern_name="$3"
    
    echo ""
    echo -e "${CYAN}Processing pattern: $pattern${NC}"
    
    local count=0
    
    # Find all relevant files, pruning excluded directories at search time for performance
    # Skip: .venv, __pycache__, .pytest_cache, node_modules, .git, .egg-info, dist, build, .claude, .ships-demo
    # Also skip the rename scripts themselves
    while IFS= read -r filepath; do
        # Quick check: skip the rename scripts by filename
        if [[ "$(basename "$filepath")" == "rename_ships_repo.sh" ]] || \
           [[ "$(basename "$filepath")" == "rename_ships_repo.ps1" ]]; then
            continue
        fi
        
        # Skip preserved docs
        if [[ "$filepath" == *"AGENT_INTEGRATION.md"* ]] || \
           [[ "$filepath" == *"MISSION.md"* ]] || \
           [[ "$filepath" == *"OPERATIONS_GUIDE.md"* ]]; then
            continue
        fi
        
        if replace_in_file "$filepath" "$pattern" "$replacement"; then
            ((count++))
        fi
    done < <(find "$REPO_ROOT" \
        -type d \( -name ".venv" -o -name "__pycache__" -o -name ".pytest_cache" \
        -o -name "node_modules" -o -name ".git" -o -name ".egg-info" \
        -o -name "dist" -o -name "build" -o -name ".claude" -o -name ".ships-demo" \) -prune -o \
        -type f \( -name "*.py" -o -name "*.toml" -o -name "*.md" -o -name "*.txt" \
        -o -name "*.yaml" -o -name "*.yml" -o -name "*.sh" -o -name "*.ps1" \
        -o -name "*.json" -o -name "Dockerfile" -o -name "*requirements*" \) -print \
        2>/dev/null | sort)
    
    echo -e "${GRAY}Processed $count files for pattern: $pattern${NC}"
    return 0
}

# ============================================================================
# Backup Creation (Optional)
# ============================================================================

create_backup() {
    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)
    local backup_dir
    backup_dir="$(dirname "$REPO_ROOT")/teradata-ships-backup-$timestamp"
    
    echo -e "${YELLOW}Creating backup at: $backup_dir${NC}"
    cp -r "$REPO_ROOT" "$backup_dir"
    echo -e "${GREEN}Backup complete.${NC}"
    
    echo "$backup_dir"
}

# ============================================================================
# Main Execution
# ============================================================================

main() {
    write_header "SHIPS Repository Rename: $OLD_REPO_NAME → $NEW_REPO_NAME"
    
    if $DRY_RUN; then
        echo -e "${YELLOW}DRY RUN MODE: No files will be modified.${NC}"
    else
        echo -e "${RED}LIVE MODE: Files will be modified.${NC}"
    fi
    
    echo -e "${GRAY}Repository root: $REPO_ROOT${NC}"
    echo ""
    
    # Optional: Create backup
    if $CREATE_BACKUP && ! $DRY_RUN; then
        create_backup
    fi
    
    local total_changes=0
    
    # ========================================================================
    # Pattern 1: Repository URL
    # ========================================================================
    write_header "Pattern 1: Repository URLs"
    rename_pattern "$OLD_URL" "$NEW_URL" "Repository URL"
    ((total_changes++))
    
    # ========================================================================
    # Pattern 2: Package Name (exact match)
    # ========================================================================
    write_header "Pattern 2: Python Package Name"
    rename_pattern "name = \"$OLD_REPO_NAME\"" "name = \"$NEW_REPO_NAME\"" "Package metadata"
    ((total_changes++))
    rename_pattern "name=\"$OLD_REPO_NAME\"" "name=\"$NEW_REPO_NAME\"" "Package metadata"
    ((total_changes++))
    
    # ========================================================================
    # Pattern 3: Directory Path References
    # ========================================================================
    write_header "Pattern 3: Directory Path References"
    rename_pattern "$OLD_REPO_NAME" "$NEW_REPO_NAME" "Directory/path reference"
    ((total_changes++))
    
    # ========================================================================
    # Pattern 4: pip install Commands (case-sensitive)
    # ========================================================================
    write_header "Pattern 4: pip install Commands"
    rename_pattern "pip install teradata-deployment-agent" "pip install teradata-ships" "pip install command"
    ((total_changes++))
    rename_pattern "pip install \"teradata-deployment-agent" "pip install \"teradata-ships" "pip install command with extras"
    ((total_changes++))
    rename_pattern "pip install 'teradata-deployment-agent" "pip install 'teradata-ships" "pip install command with extras"
    ((total_changes++))
    
    # ========================================================================
    # Summary
    # ========================================================================
    write_header "Rename Operation Complete"
    
    if $DRY_RUN; then
        echo -e "${YELLOW}DRY RUN COMPLETE: Files would be processed as shown above.${NC}"
        echo -e "${CYAN}To apply these changes, run without --dry-run flag.${NC}"
    else
        echo -e "${GREEN}RENAME COMPLETE.${NC}"
        echo -e "${CYAN}NEXT STEPS:${NC}"
        echo -e "${GRAY}  1. Review changes: git diff${NC}"
        echo -e "${GRAY}  2. Test the package: pip install -e .${NC}"
        echo -e "${GRAY}  3. Run tests: pytest tests/${NC}"
        echo -e "${GRAY}  4. Commit changes: git commit -m 'refactor: rename teradata-deployment-agent → teradata-ships'${NC}"
        echo -e "${GRAY}  5. Update GitHub repository settings (Settings > General > Repository name)${NC}"
    fi
    
    echo ""
}

# ============================================================================
# Execute
# ============================================================================

main
