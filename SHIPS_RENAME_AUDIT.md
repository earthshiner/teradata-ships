# SHIPS Repository Rename Audit
## teradata-ships → teradata-ships

**Date:** 2026-06-12  
**Status:** Audit complete — Ready for execution  
**Total files to update:** ~80 across main repo + worktrees  
**Total individual changes:** ~500+

---

## Executive Summary

The rename affects:
1. **Package metadata** (pyproject.toml, setup.py)
2. **Documentation** (8 key docs + ADRs + session notes)
3. **Code comments & docstrings** (~25 Python files)
4. **GitHub URLs** (ADRs, issue references, code producer tags)
5. **Environment configuration** (pip install examples, MCP config paths, PYTHONPATH)

**Important distinction:** "Agent" terminology in documentation (AGENT_INTEGRATION.md, MISSION.md, OPERATIONS_GUIDE.md) describes SHIPS' *capability to serve autonomous agents*, not SHIPS itself. This language stays — we're fixing the tool name, not its mission.

---

## File Categories & Required Changes

### 1. **Package Metadata** (Critical)
| File | Change | Notes |
|------|--------|-------|
| `pyproject.toml` | `name = "teradata-ships"` → `name = "teradata-ships"` | Affects PyPI, imports, pip install commands |
| `pyproject.toml` | `repository = "https://github.com/earthshiner/teradata-ships"` | Update homepage URL |
| `.venv/Scripts/activate_this.py` (generated) | `VIRTUAL_ENV_PROMPT` string | Will regenerate after rename |

**Action:** 1 file, 2-3 changes. This is the single most important change.

---

### 2. **Documentation Files** (High Priority)

#### Primary User-Facing Docs
| File | References | Count | Notes |
|------|-----------|-------|-------|
| `docs/INSTALLATION.md` | Clone URLs, PYTHONPATH setup, directory paths | ~10 | Update all git clone examples + env var references |
| `docs/MCP_GUIDE.md` | Config `cwd` paths, clone URLs, directory references | ~8 | Installation + configuration examples |
| `docs/PITCH.md` | Clone URL, project directory name | ~3 | Sales/pitch deck reference |
| `README.md` | Directory name in examples | ~1-2 | Top-level overview |

#### Architecture & Design Docs
| File | References | Count | Notes |
|------|-----------|-------|-------|
| `docs/adr/0001-record-architecture-decisions.md` | "teradata-ships repository" | ~3 | Architectural context |
| `docs/adr/0009-configurable-deploy-intent-with-waiver.md` | "teradata-ships does at package" | ~3 | Feature description |
| `docs/adr/0011-sha256-package-integrity-fingerprinting.md` | GitHub issue link | ~1 | Issue reference |
| `docs/design-rationale/README.md` | GitHub issue link | ~1 | Design context |

#### Feature/Integration Docs
| File | References | Count | Notes |
|------|-----------|-------|-------|
| `docs/AGENT_INTEGRATION.md` | No repo name refs (keep "agent" language) | 0 | No changes needed — describes agent-friendly features |
| `docs/MISSION.md` | No direct repo name refs (keep "agent" language) | 0 | No changes needed — describes mission |
| `docs/OPERATIONS_GUIDE.md` | No direct repo name refs | 0 | No changes needed |
| `docs/MCP_README.md` | `pip install teradata-ships[mcp]` | ~2 | Update pip install command |

#### Session Notes & Runbooks
| File | References | Count | Notes |
|------|-----------|-------|-------|
| `docs/sessions/2026-05-06-runsheet-*.md` | GitHub PR/issue links | ~5 | Historical record — update for accuracy |
| `docs/sessions/SHIPS_Roadmap_Plan.md` | Repo URL | ~1 | Update repo reference |
| `docs/sessions/SHIPS_Security_Hardening_Spec.md` | Repo name | ~1 | Update repo reference |
| `docs/sessions/pr-claude-config-*.md` | Issue reference | ~1 | Historical reference |

**Action:** ~12 files, ~40 changes total.

---

### 3. **Python Source Code** (High Priority)

#### Package Metadata & Configuration
| File | Pattern | Example | Notes |
|------|---------|---------|-------|
| `src/ships_lineage.py` | `_PRODUCER` URL | `"https://github.com/earthshiner/teradata-ships"` | 2 occurrences |
| `src/ships_mcp.py` | Config example comment | MCP `cwd` path example | Update documentation string |
| `src/td_release_packager/graph_export.py` | `_PRODUCER` URL | `"https://github.com/earthshiner/teradata-ships"` | 1 occurrence |
| `src/td_release_packager/otel.py` | pip install comment | `pip install "teradata-ships[otel]"` | Update code comment |

**Files affected:** 4 files, ~6 code changes + comments

---

### 4. **CI/CD & Configuration**

#### GitHub Actions Workflows
**Pattern:** Look for `.github/workflows/*.yml` files that may reference:
- Repo clone URLs
- Pip install commands  
- Working directory assumptions

**Files to check:**
- Any `.github/workflows/*.yml` that runs `pip install` or references the repo

---

### 5. **SHIPS Skill Documentation** (Internal)
**Location:** `/mnt/skills/user/ships/SKILL.md`  
**Changes needed:**
- Description field mentions repo
- Any code examples with git clone
- Installation instructions

---

## Non-Changes (Important)

### Keep These As-Is:
1. **Agent-related terminology in docs** — AGENT_INTEGRATION.md, MISSION.md, OPERATIONS_GUIDE.md describe SHIPS' capability to serve autonomous agents. This is a feature, not a bug. Keep phrases like:
   - "agentic deployment"
   - "autonomous deployment agents"
   - "agent deployment patterns"
   - "an agent should be able to..."

2. **Tool names in code**:
   - `ships_lineage.py` (filename stays)
   - `ships_mcp.py` (filename stays)
   - `td_release_packager` module (stays)
   - Class names, function names — only update docstrings/comments if they mention the repo name

---

## Change Patterns (Regex Reference)

### Pattern 1: Repository URL
```
https://github.com/earthshiner/teradata-ships
→
https://github.com/earthshiner/teradata-ships
```

### Pattern 2: Package Name (case-sensitive)
```
name = "teradata-ships"
→
name = "teradata-ships"
```

### Pattern 3: Directory References in Code/Docs
```
/path/to/teradata-ships
→
/path/to/teradata-ships
```

### Pattern 4: pip install Commands
```
pip install teradata-ships
pip install "teradata-ships[otel]"
pip install "teradata-ships[mcp]"
→
pip install teradata-ships
pip install "teradata-ships[otel]"
pip install "teradata-ships[mcp]"
```

---

## Execution Roadmap

### Phase 1: Package Metadata (DO FIRST)
- [ ] Rename `pyproject.toml` package name
- [ ] Update any `setup.py` if present
- [ ] Update repository URLs in pyproject.toml

### Phase 2: Primary Documentation
- [ ] INSTALLATION.md (clone URLs, PYTHONPATH examples)
- [ ] MCP_GUIDE.md (config paths, examples)
- [ ] PITCH.md (clone example)
- [ ] README.md

### Phase 3: ADRs & Design Docs
- [ ] Architecture Decision Records in `docs/adr/`
- [ ] Design rationale docs

### Phase 4: Code & Code Comments
- [ ] ships_lineage.py (_PRODUCER, docstrings)
- [ ] ships_mcp.py (config examples)
- [ ] graph_export.py (_PRODUCER)
- [ ] otel.py (pip install comment)
- [ ] Any other source files with inline comments

### Phase 5: Session Notes & Runbooks
- [ ] Historical session records
- [ ] Planning & spec documents

### Phase 6: Skills & External Docs
- [ ] Update SHIPS skill (`/mnt/skills/user/ships/SKILL.md`)
- [ ] Any customer-facing or internal technical documentation

### Phase 7: Git & GitHub
- [ ] Rename GitHub repository from `teradata-ships` → `teradata-ships`
- [ ] Verify all internal links still resolve (GitHub auto-redirects old URLs for 1 year)

---

## Verification Checklist

After all changes, verify:
- [ ] `pyproject.toml` shows `name = "teradata-ships"`
- [ ] All `git clone` examples point to `teradata-ships`
- [ ] All `pip install` commands reference `teradata-ships`
- [ ] All `/path/to/` examples in docs reference `teradata-ships`
- [ ] All GitHub issue/PR links still work (or are updated)
- [ ] `_PRODUCER` tags in code reference correct repo
- [ ] PYTHONPATH examples updated (if any)
- [ ] MCP config examples use correct `cwd`
- [ ] Agent-related language in MISSION/AGENT_INTEGRATION docs is preserved
- [ ] No commented-out code references old name
- [ ] README & main docs pages are consistent

---

## Summary Statistics

| Category | Files | Changes | Complexity |
|----------|-------|---------|-----------|
| Package Metadata | 1 | 2-3 | **Critical** |
| Documentation | ~12 | ~40 | High |
| Python Source | 4 | ~6 | Medium |
| Configuration | TBD | TBD | Medium |
| Skills | 1 | ~3 | Medium |
| Total | ~20 | ~55-65 | **Medium-High** |

**Note:** Worktrees contain duplicates; focus on main repo + `/mnt/skills/user/ships/`.

---

## After the Rename

### For PyPI (if publishing):
- Package becomes available as `pip install teradata-ships`
- Old package `teradata-ships` can be deprecated/yanked if desired
- Update any public documentation linking to PyPI

### For GitHub:
- Old repo URL redirects for ~1 year (automatic GitHub behaviour)
- Update any external links (Teradata docs, marketing materials, etc.)

### For Internal Usage:
- Update team runbooks
- Update GitHub Copilot / MCP configurations with new `cwd` paths
- Update any CI/CD that references the old repo

---

**Next step:** Execute the find-and-replace script provided separately.
