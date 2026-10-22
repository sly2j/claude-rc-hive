---
domain: code
summary: queen asks launcher via AskUserQuestion per new type; trust confirmed by the guard's --trust prompt, not a second question
affects: [hive/queen/CLAUDE.md, hive/queen/hooks/guard.py, hive/cli.py]
verified: 2026-10-22
source: user 2026-10-22, .supervisor/archive/working/plans/plan-launcher-registry.md (D3-D5, Decisions 12-16; code on main c0dab66)
supersedes:
---
Before spawning a new type the queen asks, with `AskUserQuestion`, which launcher: default first, others, "Add a new launcher"; discover records count as choices; with more than 3, default plus two others plus "Add new", the rest via "Other", and the question text lists every name. Skipped when the user named a launcher or the type exists. Without `AskUserQuestion` it lists numbered choices and acts only on an explicit pick. `hive spawn` itself never prompts.
Trust: implemented as in hive/queen/CLAUDE.md "Workspace trust": run spawn without `--trust`; on exit 3 only, tell the user the directory, root and trust file, then rerun with `--trust`, whose guard confirmation prompt is the user's answer. This differs from plan Decision 12 (a second AskUserQuestion); the user accepted the implemented behavior, so the code is authoritative. D5: trust is instruction-enforced only, no hook enforcement. The guard (hive/queen/hooks/guard.py) was changed, contrary to plan Decision 15 (no guard change): it allows AskUserQuestion as is, and answers "ask" for `hive launchers add` because a registered launcher later runs via `spawn --launcher` without a --command prompt. AskUserQuestion is also in the queen's `--tools` list (QUEEN_TOOLS in hive/cli.py). The queen is told not to ask again in chat after that prompt.
Open: the manual check over remote control (launcher dialogue, trust prompt, legacy adoption on a running hive) is deferred to the user; issues arrive as a new task.
