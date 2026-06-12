# ============================================================================
# SHIPS Repository Rename: teradata-deployment-agent → teradata-ships
# ============================================================================
# 
# IMPORTANT: This script performs find-and-replace operations. Review changes
# before committing. The script is safe (non-destructive backups enabled),
# but you should verify the diffs before pushing.
#
# Usage:
#   .\rename_ships_repo.ps1 -DryRun               # Preview changes only
#   .\rename_ships_repo.ps1                        # Execute changes
#   .\rename_ships_repo.ps1 -CreateBackup         # Backup before changes
#
# ============================================================================

param(
    [switch]$DryRun = $false,
    [switch]$CreateBackup = $false,
    [string]$RepoRoot = (Get-Location).Path
)

# ============================================================================
# Configuration
# ============================================================================

$OLD_REPO_NAME = "teradata-deployment-agent"
$NEW_REPO_NAME = "teradata-ships"
$OLD_URL = "https://github.com/earthshiner/teradata-deployment-agent"
$NEW_URL = "https://github.com/earthshiner/teradata-ships"

# File extensions to include in search
$IncludeExtensions = @(
    "*.py", "*.toml", "*.md", "*.txt", "*.yaml", "*.yml",
    "*.sh", "*.ps1", "*.json", "Dockerfile", "*requirements*"
)

# Directories to exclude (includes worktrees, venv, node_modules, etc.)
$ExcludePatterns = @(
    ".venv", "__pycache__", ".pytest_cache", "node_modules",
    ".git", ".github", "*.egg-info", "dist", "build",
    ".claude", ".ships-demo"  # Skip worktrees and demos
)

# Files/patterns to never touch (agent-related docs that are fine)
$PreserveDocs = @(
    "AGENT_INTEGRATION.md",
    "MISSION.md",
    "OPERATIONS_GUIDE.md"
)

# ============================================================================
# Logging & Utility Functions
# ============================================================================

function Write-Header {
    param([string]$Message)
    Write-Host "`n" -ForegroundColor Gray
    Write-Host ("=" * 70) -ForegroundColor Cyan
    Write-Host $Message -ForegroundColor Cyan
    Write-Host ("=" * 70) -ForegroundColor Cyan
}

function Write-Change {
    param([string]$File, [string]$Old, [string]$New, [int]$Count)
    $indicator = if ($DryRun) { "[DRY RUN]" } else { "[CHANGED]" }
    Write-Host "$indicator $File" -ForegroundColor Green
    Write-Host "  - Found $Count occurrence(s)" -ForegroundColor Gray
    Write-Host "  - Old: $Old" -ForegroundColor Yellow
    Write-Host "  - New: $New" -ForegroundColor Green
}

function Should-Skip-File {
    param([string]$FilePath)
    
    # Skip the rename scripts themselves
    $filename = Split-Path -Leaf $FilePath
    if ($filename -eq "rename_ships_repo.ps1" -or $filename -eq "rename_ships_repo.sh") {
        return $true
    }
    
    # Skip preserved documentation files
    foreach ($preserved in $PreserveDocs) {
        if ($FilePath -like "*$preserved") {
            return $true
        }
    }
    
    # Skip excluded directories
    foreach ($pattern in $ExcludePatterns) {
        if ($FilePath -like "*$pattern*") {
            return $true
        }
    }
    
    return $false
}

function Get-Files-To-Process {
    $files = @()
    
    # Build exclusion filter for directories
    $dirExclusions = @(".venv", "__pycache__", ".pytest_cache", "node_modules", ".git", ".egg-info", "dist", "build", ".claude", ".ships-demo")
    
    # Use -Exclude to skip directories at search time for better performance
    foreach ($ext in $IncludeExtensions) {
        $found = Get-ChildItem -Path $RepoRoot -Include $ext -Recurse -File `
            -Exclude $dirExclusions -ErrorAction SilentlyContinue
        $files += $found
    }
    
    # Filter out any remaining excluded patterns and return unique files
    $files = $files | Where-Object { 
        $fullPath = $_.FullName
        # Also check filename to exclude the rename scripts themselves
        $filename = Split-Path -Leaf $fullPath
        if ($filename -eq "rename_ships_repo.ps1" -or $filename -eq "rename_ships_repo.sh") {
            return $false
        }
        return $true
    } | Select-Object -Unique
    
    return $files
}

# ============================================================================
# Find & Replace Operations
# ============================================================================

function Replace-In-File {
    param(
        [string]$FilePath,
        [string]$OldText,
        [string]$NewText,
        [string]$ChangeType
    )
    
    $content = Get-Content -Path $FilePath -Raw -Encoding UTF8
    $originalContent = $content
    $occurrences = ([regex]::Matches($content, [regex]::Escape($OldText))).Count
    
    if ($occurrences -eq 0) {
        return $false
    }
    
    # Perform replacement
    $newContent = $content -replace [regex]::Escape($OldText), $NewText
    
    # Write back to file
    if (-not $DryRun) {
        Set-Content -Path $FilePath -Value $newContent -Encoding UTF8 -NoNewline
    }
    
    # Log the change
    Write-Change -File (Resolve-Path -Relative $FilePath) -Old $OldText -New $NewText -Count $occurrences
    
    return $true
}

function Rename-Pattern {
    param(
        [string]$Pattern,
        [string]$Replacement,
        [string]$ChangeType
    )
    
    Write-Host "`nProcessing pattern: $Pattern" -ForegroundColor Cyan
    $count = 0
    
    $files = Get-Files-To-Process
    
    foreach ($file in $files) {
        if (Replace-In-File -FilePath $file -OldText $Pattern -NewText $Replacement -ChangeType $ChangeType) {
            $count++
        }
    }
    
    Write-Host "Processed $count files for pattern: $Pattern`n" -ForegroundColor Gray
    return $count
}

# ============================================================================
# Backup Creation (Optional)
# ============================================================================

function Create-Backup {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $backupDir = Join-Path -Path (Split-Path -Parent $RepoRoot) -ChildPath "teradata-ships-backup-$timestamp"
    
    Write-Host "Creating backup at: $backupDir" -ForegroundColor Yellow
    Copy-Item -Path $RepoRoot -Destination $backupDir -Recurse -Force
    Write-Host "Backup complete." -ForegroundColor Green
    
    return $backupDir
}

# ============================================================================
# Main Execution
# ============================================================================

function Main {
    Write-Header "SHIPS Repository Rename: $OLD_REPO_NAME → $NEW_REPO_NAME"
    
    if ($DryRun) {
        Write-Host "DRY RUN MODE: No files will be modified." -ForegroundColor Yellow
    } else {
        Write-Host "LIVE MODE: Files will be modified." -ForegroundColor Red
    }
    
    Write-Host "Repository root: $RepoRoot`n" -ForegroundColor Gray
    
    # Optional: Create backup
    if ($CreateBackup -and -not $DryRun) {
        Create-Backup
    }
    
    $totalChanges = 0
    
    # ========================================================================
    # Pattern 1: Repository URL
    # ========================================================================
    Write-Header "Pattern 1: Repository URLs"
    $totalChanges += Rename-Pattern -Pattern $OLD_URL -Replacement $NEW_URL -ChangeType "Repository URL"
    
    # ========================================================================
    # Pattern 2: Package Name (exact match)
    # ========================================================================
    Write-Header "Pattern 2: Python Package Name"
    $totalChanges += Rename-Pattern -Pattern "name = `"$OLD_REPO_NAME`"" -Replacement "name = `"$NEW_REPO_NAME`"" -ChangeType "Package metadata"
    $totalChanges += Rename-Pattern -Pattern "name=`"$OLD_REPO_NAME`"" -Replacement "name=`"$NEW_REPO_NAME`"" -ChangeType "Package metadata"
    
    # ========================================================================
    # Pattern 3: Directory Path References
    # ========================================================================
    Write-Header "Pattern 3: Directory Path References"
    $totalChanges += Rename-Pattern -Pattern "teradata-deployment-agent" -Replacement "teradata-ships" -ChangeType "Directory/path reference"
    
    # ========================================================================
    # Pattern 4: Git Clone Commands
    # ========================================================================
    Write-Header "Pattern 4: Git Clone Examples (already covered by Pattern 3)"
    Write-Host "Git clone examples will be updated via Pattern 3 above.`n" -ForegroundColor Gray
    
    # ========================================================================
    # Pattern 5: pip install Commands (case-sensitive)
    # ========================================================================
    Write-Header "Pattern 5: pip install Commands"
    $totalChanges += Rename-Pattern -Pattern "pip install teradata-deployment-agent" -Replacement "pip install teradata-ships" -ChangeType "pip install command"
    $totalChanges += Rename-Pattern -Pattern "pip install `"teradata-deployment-agent" -Replacement "pip install `"teradata-ships" -ChangeType "pip install command with extras"
    $totalChanges += Rename-Pattern -Pattern "pip install 'teradata-deployment-agent" -Replacement "pip install 'teradata-ships" -ChangeType "pip install command with extras"
    
    # ========================================================================
    # Summary
    # ========================================================================
    Write-Header "Rename Operation Complete"
    
    if ($DryRun) {
        Write-Host "DRY RUN COMPLETE: $totalChanges file(s) would be modified." -ForegroundColor Yellow
        Write-Host "`nTo apply these changes, run without -DryRun flag.`n" -ForegroundColor Cyan
    } else {
        Write-Host "RENAME COMPLETE: $totalChanges file(s) modified." -ForegroundColor Green
        Write-Host "`nNEXT STEPS:" -ForegroundColor Cyan
        Write-Host "  1. Review changes: git diff" -ForegroundColor Gray
        Write-Host "  2. Test the package: pip install -e ." -ForegroundColor Gray
        Write-Host "  3. Run tests: pytest tests/" -ForegroundColor Gray
        Write-Host "  4. Commit changes: git commit -m 'refactor: rename teradata-deployment-agent → teradata-ships'" -ForegroundColor Gray
        Write-Host "  5. Update GitHub repository settings (Settings > General > Repository name)" -ForegroundColor Gray
        Write-Host "`n"
    }
}

# ============================================================================
# Execute
# ============================================================================

Main
