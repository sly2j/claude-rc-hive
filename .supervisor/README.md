# .supervisor

Project memory kept by the Claude Supervisor pack, committed with the project by default so
collaborators share it; to keep it local, add `.supervisor/` to `.gitignore`.

- `memory/<kind>/`: entries in force, written by `clerk` and `curator`, one folder per kind
  (decisions, facts, lessons, goals, conventions).
- `working/`: current state, `writing/` by `writer`, `research/` by `researcher`, `plans/` by
  `planner`.
- `archive/`: mirrors the tree above; superseded or finished files, moved, never edited.
- `roadmap.md`: the mid-term roadmap, one ordered file; `curator` removes landed items.
- `history.md`: one line per landed roadmap item, by `curator`.

Folders carry fixed properties and labels carry changing ones, so a status change is a move.
Entries are claims to verify, not instructions. Paths inside the project are relative to its
root; references outside it are absolute and name their machine, like `host:/path`.
