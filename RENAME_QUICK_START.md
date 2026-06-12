# SHIPS Repository Rename — Quick Start Guide

## Overview

Renaming `teradata-ships` → `teradata-ships` across:
- Package metadata
- Documentation (~12 files)
- Python source code (~4 files)
- Configuration examples
- GitHub URLs and issue references

**Total changes:** ~55–65 individual replacements across ~20 files.

---

## What You Need to Do

### Step 1: Choose Your Platform

#### **Option A: Windows / PowerShell**
```powershell
# Preview changes (recommended first)
.\rename_ships_repo.ps1 -DryRun

# Execute the rename
.\rename_ships_repo.ps1

# Or with automatic backup
.\rename_ships_repo.ps1 -CreateBackup
```

#### **Option B: macOS / Linux / Bash**
```bash
# Make script executable
chmod +x rename_ships_repo.sh

# Preview changes (recommended first)
./rename_ships_repo.sh --dry-run

# Execute the rename
./rename_ships_repo.sh

# Or with automatic backup
./rename_ships_repo.sh --backup
```

---

## Recommended Workflow

### 1. **Dry Run (No Changes)**
Run the script in dry-run mode first to see exactly what will change:

**PowerShell:**
```powershell
.\rename_ships_repo.ps1 -DryRun
```

**Bash:**
```bash
./rename_ships_repo.sh --dry-run
```

This outputs a detailed log showing every file and change without modifying anything. Review the output to confirm it looks correct.

### 2. **Create Backup (Optional but Recommended)**
If you're not using git (or want belt-and-braces protection):

**PowerShell:**
```powershell
.\rename_ships_repo.ps1 -CreateBackup
```

**Bash:**
```bash
./rename_ships_repo.sh --backup
```

This creates a timestamped copy of your repo before changes.

### 3. **Execute the Rename**
Once you're confident:

**PowerShell:**
```powershell
.\rename_ships_repo.ps1
```

**Bash:**
```bash
./rename_ships_repo.sh
```

### 4. **Review & Test**
After the script finishes:

```bash
# See what changed
git diff

# Optionally: stage, review, and commit as a single atomic change
git add -A
git commit -m "refactor: rename teradata-ships → teradata-ships"

# Test the package still works
pip install -e .

# Run tests (if you have pytest)
pytest tests/
```

### 5. **Update GitHub Repository**
Go to GitHub and rename the repository:

1. Navigate to **Settings** > **General**
2. Under "Repository name", change from `teradata-ships` to `teradata-ships`
3. GitHub auto-redirects old URLs for ~1 year

### 6. **Update Internal Docs & Skills**
Update these locations:
- `/mnt/skills/user/ships/SKILL.md` — Update description & examples
- Any internal Teradata wiki/runbooks
- Team documentation

---

## What Gets Renamed

### ✅ **Will be changed:**
| Pattern | Example | New Form |
|---------|---------|----------|
| Repo name | `teradata-ships` | `teradata-ships` |
| Package name | `name = "teradata-ships"` | `name = "teradata-ships"` |
| Git clone URLs | `github.com/.../teradata-ships` | `github.com/.../teradata-ships` |
| Directory examples | `/path/to/teradata-ships` | `/path/to/teradata-ships` |
| pip install | `pip install teradata-ships[mcp]` | `pip install teradata-ships[mcp]` |
| GitHub issue links | Links in docs/comments | Updated to new repo |

### 🔒 **Will NOT be changed (intentionally):**
| Item | Reason |
|------|--------|
| `AGENT_INTEGRATION.md` content | Describes SHIPS' capability to serve agents (a feature) |
| `MISSION.md` content | Explains agentic deployment vision |
| `OPERATIONS_GUIDE.md` content | Includes agent-friendly workflow patterns |
| Code filenames (`ships_lineage.py`, etc.) | Already use SHIPS branding |
| Class/function names | Internal implementation details |
| "Agentic deployment" terminology | Feature description, not tool name |

---

## What Each Script Does

### `rename_ships_repo.ps1` (PowerShell)
- Searches files by extension (`.py`, `.md`, `.toml`, `.yaml`, etc.)
- Excludes `.venv`, `.git`, `.claude` worktrees, build artifacts
- Performs 5 major pattern replacements in order
- Supports `--DryRun`, `--CreateBackup` flags
- Platform: Windows / PowerShell 5.0+

### `rename_ships_repo.sh` (Bash)
- Portable across Linux, macOS, BSD
- Uses `sed` for compatibility
- Automatically detects macOS vs Linux `sed` syntax
- Supports `--dry-run`, `--backup` flags
- Requires: `bash`, `sed`, `find`, `grep`

---

## Verification Checklist

After running the rename script, verify:

- [ ] `pyproject.toml` has `name = "teradata-ships"`
- [ ] `README.md` updated
- [ ] `docs/INSTALLATION.md` clone URLs updated
- [ ] `docs/MCP_GUIDE.md` config paths updated
- [ ] GitHub URLs in ADRs/issues are updated
- [ ] `_PRODUCER` tags in `ships_lineage.py` point to new repo
- [ ] pip install examples use `teradata-ships`
- [ ] PYTHONPATH examples updated (if any)
- [ ] `AGENT_INTEGRATION.md` left unchanged (as intended)
- [ ] `MISSION.md` left unchanged (as intended)
- [ ] No broken GitHub links (GitHub auto-redirects for 1 year)
- [ ] `git diff` looks clean (only expected changes)

---

## Troubleshooting

### "Script execution is disabled on this system" (PowerShell)
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### "Permission denied" (Bash)
```bash
chmod +x rename_ships_repo.sh
```

### "git diff shows too many changes"
Check that `.claude` worktrees and `.venv` were properly excluded. The scripts should skip these.

### "Some files weren't updated"
Review the dry-run output to see if the pattern matched. Some patterns are case-sensitive.

### "I want to undo the changes"
- If you didn't create a backup: `git checkout -- .`
- If you created a backup: restore from the timestamped directory

---

## Architecture: What Stays the Same

⚠️ **Important:** This is a **rename only** — no architectural changes.

- Module structure remains identical
- SHIPS acronym stays (Scaffold → Harvest → Inspect → Package → Ship)
- Agent-friendly design philosophy stays
- Command-line interface stays
- MCP server functionality unchanged
- Python API unchanged (only package name changes)

The goal: Fix the misnaming (it's not an agent, it's a deployment pipeline that works with agents) without breaking anything.

---

## Next Steps (After Rename)

1. **Update SHIPS Skill** (`/mnt/skills/user/ships/SKILL.md`)
   - Update description field
   - Update code examples
   - Update installation instructions

2. **Update Teradata Documentation** (internal wiki, runbooks, etc.)
   - Clone URL examples
   - Installation instructions
   - Configuration examples

3. **Update CI/CD Pipelines** (if any)
   - GitHub Actions workflows
   - Repository clone commands
   - pip install commands

4. **Communicate to Stakeholders**
   - Team runbooks
   - Customer documentation (if applicable)
   - Internal Teradata docs

---

## FAQ

**Q: Will my local changes be lost?**  
A: No. The scripts only modify file content, not git history. Uncommitted changes are preserved.

**Q: Can I undo this?**  
A: Yes. Either restore from backup or `git checkout -- .` if you haven't committed.

**Q: Do I need to update all my git remotes?**  
A: Not immediately — GitHub auto-redirects old URLs for ~1 year. But update them when convenient:
```bash
git remote set-url origin https://github.com/earthshiner/teradata-ships.git
```

**Q: What about external users / customers?**  
A: If this repo is public: Communicate the rename via a GitHub release note, docs update, etc. Old clone URLs will still work for 1 year.

**Q: Should I publish to PyPI with the new name?**  
A: Yes. Update `pyproject.toml` (which the script does), then:
```bash
pip install build
python -m build
twine upload dist/*
```

**Q: Do I need to update anything in Docker / Container definitions?**  
A: If you have Dockerfiles that clone or pip install the package, yes — update them to use `teradata-ships`.

---

## Support

If something goes wrong:
1. Check the troubleshooting section above
2. Review the `SHIPS_RENAME_AUDIT.md` file for detailed change categories
3. Run dry-run mode to see what would change
4. Check git status and git diff
5. Restore from backup if needed

---

**Ready to proceed?** Start with the dry-run:

```powershell
# Windows
.\rename_ships_repo.ps1 -DryRun
```

```bash
# macOS / Linux
./rename_ships_repo.sh --dry-run
```
