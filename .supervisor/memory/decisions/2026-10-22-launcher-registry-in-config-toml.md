---
domain: code
summary: launchers live in config.toml [[launchers]] with [defaults] launcher; registry wins over discover
affects: [hive/cli.py, hive/configedit.py, hive/inspect.py, hive/config.example.toml]
verified: 2026-10-22
source: .supervisor/archive/working/plans/plan-launcher-registry.md (Decisions 1-11)
supersedes:
---
Named launchers are `[[launchers]]` in config.toml (`name` `[a-z0-9][a-z0-9-]*` unique, `command` containing `{name}` and `--remote-control`, optional `config_dir`, `description`); the default is `[defaults] launcher`. Managed by `hive launchers` (list, `add NAME COMMAND`, `default NAME`) via configedit with verify. Rejected: separate state file (config is the single source), registry inside `[queen]` (shell spawns need the default too), a new `machine` key.
Resolution: registry first, then `[queen] discover`; a shadowed discover record is marked `shadowed=yes`; `add` refuses a name in either; `load_config` validates the default against the registry only and never runs discover. `--sandbox` stays as alias of `--launcher`. A new type without `--command`/`--launcher` uses `defaults.launcher` (type `<base>-<launcher>`), else `defaults.command`; existing types keep their stored command. `append_agent` stores literal command and config_dir, so later launcher edits do not change running types. `add` checks token 0 exists and is not secrets/private (exit 2 not found, 3 refused) and never executes it; first added becomes default.
As committed (c0dab66, code over plan text): a launcher-suffixed type that already exists with a different command or config_dir is refused (`check_launcher_type`) with a hint to give `--type`, not silently bumped; when discover fails, `launchers add` only warns that collisions with discover records are unchecked, and `launchers default` warns likewise; `launchers add` prints `launcher added name=.. default=yes|no file=..`; `queen_name` lives in hive/reconcile.py, `RESERVED_TYPES` there too.
Out of scope: launcher remove/rename (hand-edit), migrating existing agents.
