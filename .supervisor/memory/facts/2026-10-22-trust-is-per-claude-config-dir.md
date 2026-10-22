---
domain: code
summary: workspace trust is per Claude config dir (projects[root].hasTrustDialogAccepted), so launcher precedes trust
affects: [hive/trust.py, hive/cli.py]
verified: 2026-10-22
source: .supervisor/archive/working/plans/plan-launcher-registry.md (Trust model finding)
supersedes:
---
Claude Code records trust as `projects[<git toplevel>].hasTrustDialogAccepted` in `<config_dir>/.claude.json`. Hive uses the entry's `config_dir`, else its own `$CLAUDE_CONFIG_DIR`, else `~/.claude.json`. Trust is therefore per config dir, not per launcher or global, and order is launcher, then trust, then launch; `cmd_spawn` exits 3 without side effects when untrusted. A wrong `config_dir` on a launcher makes hive check and seed the wrong file.
