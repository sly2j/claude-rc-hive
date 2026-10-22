---
domain: code
summary: workers are named <type>-<machine>-<N>, queen hive-queen-<machine>; legacy panes adopted in place
affects: [hive/reconcile.py, hive/cli.py, hive/queen/CLAUDE.md]
verified: 2026-10-22
source: user 2026-10-22, .supervisor/archive/working/plans/plan-launcher-registry.md (D1, D2)
supersedes:
---
Worker instance, window and RC session names are `<type>-<machine>-<N>`; a launcher-chosen type is `<dir>-<launcher>`, so `<dir>-<launcher>-<machine>-<N>`. Machine is the existing `prefix` key (default short hostname, validated `[A-Za-z0-9._-]+`); N stays derived 1..count, never stored. Window is `<type>-<machine>`; tmux session `<prefix>-agents` unchanged. The queen is `hive-queen-<machine>` (was `<prefix>-queen`); `hive-queen` is a reserved type name like `queen`.
Why: type and launcher first makes wrong-launcher workers visible in names. Legacy panes (`<prefix>-queen`, `<prefix>-<type>-<N>`) are adopted without restart only when the canonical new name is desired: `running_panes` maps read-only, the reconcile adopt loop sets `@hive_name`, title and window; the RC session keeps the old name until restart. Legacy panes of removed types and 1.5 title-only panes stay unmanaged.
Landed in c0dab66; `queen_name` is in hive/reconcile.py, `parse_name` and `parse_legacy_name` in hive/cli.py, matching the above.
