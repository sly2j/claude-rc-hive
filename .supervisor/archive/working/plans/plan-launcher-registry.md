# Plan: queen launcher registry, launcher choice, trust dialogue and naming

Status: implemented (S1-S5), landed on main as `c0dab66` (squash of branch `feat/queen-launcher-registry`, tip `402d89d`); manual queen check with the user still open. Base `ca37171`.

## Goal

Give hive a persisted, named set of Claude launchers with a default. Make the queen ask, through a real
choice UI, which launcher to use (default first, the others, "add new"), and ask about trust only when needed,
before every spawn. This prevents starting workers with the wrong Claude executable, where agent
definitions and skills are then missing. Also put the type and launcher first in instance names
(`<dir>-<launcher>-<machine>-<number>`) and rename the queen to `hive-queen-<machine>`. Non-goals: see Scope Notes.

Expected change size: about 450-550 lines across `hive/cli.py`, `hive/reconcile.py`, `hive/configedit.py`,
`hive/inspect.py`, `hive/queen/CLAUDE.md`, `hive/config.example.toml`, `README.md` and tests (the rename
touches many name assertions in `tests/test_tmux.py` and `tests/test_reconcile.py`).

## Trust model finding

Trust is **per Claude config dir**, not per launcher and not global. Claude Code records it as
`projects[<root>].hasTrustDialogAccepted` in `<config_dir>/.claude.json` (`hive/trust.py:25-29` `trust_file`,
`:44` `is_trusted`, `:88` `seed_trust`). The root is the git toplevel of the dir (`trust.py:32-41`). Hive uses
the entry's `config_dir`, else hive's own `$CLAUDE_CONFIG_DIR`, else `~/.claude.json` (`config.example.toml:21,41`,
`cli.py:128`). So the order is launcher -> trust -> launch, which `cmd_spawn` already follows: it resolves
command and config dir (`cli.py:689-695`), then `ensure_trust` (`cli.py:671-684`) exits 3 without side effects
when untrusted.

## Approach

Store launchers in `config.toml` as `[[launchers]]` with `[defaults] launcher = NAME`, managed through new
`hive launchers` subcommands on the existing configedit and lock machinery. `hive spawn --launcher NAME`
resolves from the registry, then `[queen] discover`. The queen runs `hive launchers`, asks with
`AskUserQuestion`, then spawns. The existing exit-3 path drives a second `AskUserQuestion` for trust. Instance
names move the existing `prefix` (already the short hostname) from the front to just before the index.

Rejected alternatives:
- A separate state file: config is the single source of truth (`cli.py:70-100`); configedit gives verified edits.
- Registry inside `[queen]`: plain `hive spawn` from a shell must also get the default launcher.
- Free-text confirmation (`queen/CLAUDE.md:28,38,47-48`): the user asked for a choice UI.
- A new `hive trust` command: `hive spawn` without `--trust` is already a side-effect-free check.
- A new `machine` config key: `prefix` already is the machine name (`cli.py:153`); a second key would diverge.
- Storing instance numbers: indexes are derived 1..count today (`reconcile.py:33-48`) and stay derived.

## Decisions

User decisions (2026-10-22), not to be reopened:

- **D1 Worker naming**: every worker instance is named `<type>-<machine>-<number>`, where a launcher-chosen
  type is `<dir>-<launcher>` (Decision 6), giving `<dir>-<launcher>-<machine>-<number>`. Default-launcher
  workers get the suffix too.
  - *machine* = existing `prefix` config key, default short hostname (`socket.gethostname().split(".")[0]`,
    `cli.py:153`). `load_config` now validates it against `[A-Za-z0-9._-]+`, the type rule at `cli.py:728`.
  - *number* = the existing derived index 1..count within the type (`reconcile.py:42-46`). Never stored. A
    count bump adds the next index; scale-down stops the highest first (`reconcile.py:51-58`); a dead pane is
    re-created under the same name. No change to allocation.
  - *window* = `<type>-<machine>` (was `<prefix>-<type>`), so the window is still the instance name without `-N`.
  - tmux session `<prefix>-agents` is unchanged.
  - Example: dir `~/src/api`, launcher `claude-supervisor`, host `gpu07.cluster.anl.gov`, second instance ->
    type `api-claude-supervisor`, window `api-claude-supervisor-gpu07`, instance and RC session name
    `api-claude-supervisor-gpu07-2`. A legacy type `api` (no launcher) becomes `api-gpu07-1`.
- **D2 Queen rename**: queen window, pane and RC session name `hive-queen-<machine>` (was `<prefix>-queen`).
  The type and target word `queen` and the `[queen]` table stay. `hive-queen` becomes a reserved type name,
  like `queen`, since a type `hive-queen` would produce the same window name.
- **D3**: discover records appear as launcher choices next to the registry.
- **D4**: with more than 3 launchers, show default + two others + "Add new"; the rest are reachable through
  "Other". The launcher question text lists every known launcher name, so the user can type one.
- **D5 Trust**: instruction-enforced trust is accepted. The queen asks once, the first time a dir is untrusted
  for the chosen launcher's config dir; seeding makes later spawns there silent. No hook enforcement.

Lead decisions:

1. **Storage**: `config.toml`. New top-level `[[launchers]]` (add to `TOP_KEYS`, `cli.py:35-37`) with
   `name` (required, `[a-z0-9][a-z0-9-]*`, unique), `command` (required, must contain `{name}` and
   `--remote-control`, rule from `cli.py:700-704`), `config_dir` (optional, `expand_config_dir`, `cli.py:646`),
   `description` (optional). Default in `[defaults] launcher` (`[defaults]` allows only `command`, `cli.py:166-168`).
2. **Default validation**: `load_config` checks `defaults.launcher` against `[[launchers]]` only; other names
   are checked against discover at use time, so loading config never runs discover.
3. **Name resolution**: registry first, then discover. A name in both: registry wins, the list marks the
   discover record `shadowed=yes`. `launchers add` refuses a name in either; `launchers default` accepts either.
   If discover is unset or fails, only the registry is used (`discover error=` line on failure).
4. **`--sandbox` stays** as an alias of `--launcher` (same resolver and exclusions, `cli.py:689-693`).
5. **Default for a new type**: no `--command`/`--launcher` -> `defaults.launcher` when set, else
   `defaults.command` as today. Existing types keep their stored command (`cli.py:706-716`).
6. **Type name**: a launcher-chosen new type is `<base>-<launcher>` via `sandbox_type` (`reconcile.py:124`),
   default launcher included, unless `--type` is given. When the default launcher is applied after resolution,
   re-resolve with the suffixed type as `type_override` (`reconcile.py:168-177`) so an existing type gets a
   count bump.
7. **Persisted agent entry**: unchanged; `append_agent` (`configedit.py:76`) stores literal `command` and
   `config_dir`, so later launcher edits do not change running types. No config migration for D1: names are
   derived from `type` and `prefix`.
8. **Legacy panes** (no forced restart): `parse_name` (`cli.py:325-335`) parses the new forms
   (`hive-queen-<prefix>`, `<type>-<prefix>-<N>`). In `running_panes` (`cli.py:338-359`), a pane whose
   `@hive_name` is not a desired name is tried as legacy (`<prefix>-queen`, `<prefix>-<type>-<N>`); if the
   canonical new name is desired, it is mapped to that name with `adopted=True` (`running_panes` stays
   read-only; it also serves `hive list` and dry-run). The reconcile adopt loop (`cli.py:443-449`, skipped on
   dry-run) then sets `@hive_name` and the title and renames the pane's window to `pane.window`, because
   reconcile splits into windows found by name (`cli.py:467`). The RC session keeps its old name until restart. Adopting only when the canonical name is desired
   resolves the ambiguity between the two forms (a type may contain `-` and equal the prefix).
9. **CLI**: `hive launchers` (list), `hive launchers add NAME COMMAND [--config-dir PATH] [--description TEXT]`,
   `hive launchers default NAME`. Nested subparsers, default `list`; register in `handlers`
   (`cli.py:969-973`) and the lock tuple (`:966`).
10. **List format**: key=value records like `hive discover`, default first, then registry, then discover:
    `launcher name=N source=config|discover default=yes|no config_dir=<path|default> command=<cmd> [description=..] [shadowed=yes]`.
    Discover failure: one `discover error=...` line, exit 0.
11. **Add validation** in hive: name regex and uniqueness; command template rule; token 0 exists (PATH or path)
    and is not a secrets/private path (refusal rule of `inspect.py:54-73`, without the `inspect_roots` check,
    because plain `claude` symlinks outside the default roots, `inspect.py:71`); exit 2 not found, exit 3
    refused; `config_dir` via `expand_config_dir`. Never executes the launcher. The first launcher added
    becomes default when none is set.
12. **Queen dialogue**: `AskUserQuestion` (not denied: `guard.py:114-115`, `settings.json:31`), on every spawn
    of a new type. Launcher question: default first "(default)", others, "Add a new launcher"; layout per D4.
    Trust question only on exit 3: "Trust <root> in <trust file> and launch" / "Cancel", wording from
    `queen/CLAUDE.md:47`.
13. **Add-new flow**: command via "Other", `hive inspect-launcher "CMD"`, config dir inferred per
    `queen/CLAUDE.md:32-43`; proposed name = basename of token 0 normalised to the name regex. One confirm
    question: "Add <name>" / "Add and make default" / "Cancel"; then `hive launchers add` and continue.
14. **Bypass**: skip the launcher question when the user named a launcher, or the type already exists. The
    trust question is never skipped. `hive spawn` never prompts.
15. **Guard**: no change; `check_bash` accepts any single `hive ...` command (`guard.py:45-65`).
16. **Fallback**: without `AskUserQuestion`, the queen lists numbered choices and acts only on an explicit pick.

## Outline

- `hive/reconcile.py`
  - `desired_panes` (`:33-48`): queen `name = f"hive-queen-{prefix}"` (`:38`); worker window
    `f"{type}-{prefix}"`, name `f"{window}-{i}"`. Pane shape unchanged.
  - Reserved type check (`:126`, `:165`): reject `hive-queen` as well as `queen`.
- `hive/cli.py`
  - `queen_name(prefix) -> str` used by reconcile (`:435`) and `hive list` (`:572`) instead of the f-strings.
  - `parse_name(prefix, name)` (`:325-335`): new forms; `parse_legacy_name(prefix, name)` for the old forms.
    Gotcha: parse the index with `rpartition("-")`, then require the remainder to end with `-<prefix>`.
  - `running_panes` (`:338-359`): legacy mapping per Decision 8, no tmux writes.
  - reconcile adopt loop (`:443-449`): also set the title and rename the window for legacy-adopted panes.
  - `load_config`: validate `prefix`; reserved `hive-queen` at `:225`; `TOP_KEYS` (`:35-37`) + `"launchers"`;
    `LAUNCHER_KEYS`; `[defaults]` + `launcher` (`:166-168`); returns `cfg["launchers"]`, `cfg["default_launcher"]`.
  - `cmd_spawn` reserved check (`:728`) + `hive-queen`.
  - `check_launcher_command(cmd, where)`: rule from `:700-704`, shared with `cmd_spawn`.
  - `launcher_record(cfg, name) -> dict` replaces `sandbox_record` (`:653-668`), same record shape.
  - `all_launchers(cfg) -> (records, discover_error)`; `cmd_launchers`, modelled on `cmd_roots` (`:774`).
  - `cmd_spawn` (`:687`): `args.launcher` (`--sandbox` same dest); Decisions 5 and 6 on the `New` branch only;
    `--type` help (`:920`). `build_parser` (`:905-951`): `launchers` subparser, `--launcher` (`:924`).
- `hive/inspect.py`: `resolve_launcher(command) -> Path` from `:54-73` (no `inspect_roots` check), raising on
  not-found/refused; shared by `inspect-launcher` and `launchers add`. No output change.
- `hive/configedit.py`: `append_launcher(text, name, command, config_dir=None, description=None)` (like
  `append_agent`, `:76`); `set_default_launcher(text, name)` replaces or inserts under `[defaults]`, appending
  the table when absent. Both through `verify` (`:130`). Insert directly after the header line.
- `hive/config.example.toml`: `prefix` comment (`:4-5`) says it is the machine part of names; `{name}` (`:13`)
  and window (`:37`) forms; commented
  `[[launchers]]` example; `# launcher = "claude"` under `[defaults]`.
- `hive/queen/CLAUDE.md`: naming line (`:3`) -> `<type>-<machine>-N`; command list (`:5-15`) + `hive launchers`;
  "start one in DIR" row (`:19-26`) -> dialogue; `--sandbox` -> `--launcher`; trust section (`:45-49`).
- `README.md`: window name and reserved `hive-queen` (`:45`), instance form (`:80`), queen window (`:90`),
  upgrade note (`:82`, legacy adoption; 1.5 title-only panes stay unmanaged), `{name}` (`:52`), spawn row and
  `--type` (`:64`), discover row (`:67`), trust (`:96-102`), new launchers section.

## Slices

### 1. Instance naming and queen rename
Delivers: names `<type>-<machine>-N`, queen `hive-queen-<machine>`, legacy panes adopted without restart.
Units: `hive/reconcile.py` + `tests/test_reconcile.py`; `hive/cli.py` + `tests/test_tmux.py` +
`tests/test_config.py`; `README.md` + `hive/queen/CLAUDE.md` + `hive/config.example.toml` (naming lines only).
Tests:
- `test_desired_panes_names` (update `test_reconcile.py:8-55`): prefix `swarm`, type `a` count 2 ->
  `a-swarm-1`, `a-swarm-2`, window `a-swarm`; queen `hive-queen-swarm`
- `test_parse_name_hyphenated_type`: prefix `t`, `t-x-t-1` -> `("t-x", 1)`; `hive-queen-t` -> `("queen", 1)`
- `test_legacy_pane_adopted`: running pane `@hive_name=t-a-1` in window `t-a`, desired `a-t-1` -> no
  start/stop, option and title `a-t-1`, window renamed `a-t`; then count 2 -> `a-t-2` splits into `a-t`,
  no window `t-a`
- `test_legacy_dry_run_no_writes`: `hive reconcile --dry-run` and `hive list` with a legacy pane -> `@hive_name` unchanged
- `test_legacy_queen_adopted`: running `t-queen` -> adopted as `hive-queen-t`, no second queen
- `test_legacy_unknown_unmanaged`: `t-zzz-1` with no type `zzz` -> unmanaged, untouched
- `test_hive_queen_type_reserved`: config type `hive-queen` -> exit 2; `spawn --type hive-queen` -> exit 2
- `test_bad_prefix`: `prefix = "a b"` -> exit 2
- update name assertions in `test_tmux.py` (`t-queen`, `t-a-1`, `t-b-api-1`, `t-gone-1`; `:258`
  `endswith`) and `test_config.py:177`
Checkpoint: `uv run pytest -q`

### 2. Registry in config and `hive launchers` list
Delivers: `[[launchers]]` and `[defaults] launcher` load and validate; `hive launchers` prints merged records.
Units: `hive/cli.py` + `tests/test_config.py`; `hive/config.example.toml` + `README.md`.
Tests:
- `test_launchers_parsed`: two blocks -> `cfg["launchers"]` names in order
- `test_launcher_bad_name`: `name="Bad Name"` -> exit 2
- `test_launcher_duplicate_name`: two `name="x"` -> exit 2
- `test_launcher_command_needs_placeholders`: no `{name}` -> exit 2
- `test_default_launcher_unknown_registry_ok`: default not in registry -> loads
- `test_launchers_list_default_first`: default=`b` -> first record `name=b default=yes`
- `test_launchers_list_includes_discover`: discover with `x` -> record `source=discover`
- `test_launchers_list_discover_error`: failing discover -> registry records + `discover error=`, exit 0
- extend `test_init_writes_a_config_that_up_accepts` (`test_config.py:169`)
Checkpoint: `uv run pytest -q tests/test_config.py`

### 3. `hive spawn --launcher` and default launcher
Delivers: spawn takes command and config dir from a named or default launcher; trust checked in its config dir.
Units: `hive/cli.py` + `tests/test_reconcile.py`/`tests/test_tmux.py`.
Tests:
- `test_spawn_launcher_registry`: `--launcher sup` -> agent `type=<base>-sup`, command, config_dir copied;
  instance `<base>-sup-<prefix>-1`
- `test_spawn_sandbox_alias`: `--sandbox x` -> same as `--launcher x`
- `test_spawn_launcher_registry_shadows_discover`: same name in both -> registry command
- `test_spawn_default_launcher_new_type`: `defaults.launcher=sup` -> type `<base>-sup`, sup's command
- `test_spawn_default_launcher_existing_type`: existing `<base>-sup` -> count bump, command unchanged
- `test_spawn_launcher_untrusted_uses_launcher_config_dir`: -> exit 3 naming sup's trust file, config unchanged
- `test_spawn_launcher_with_command_rejected`: -> exit 2
- `test_spawn_unknown_launcher`: -> exit 2 listing names
Checkpoint: `uv run pytest -q tests/test_reconcile.py tests/test_tmux.py`

### 4. `hive launchers add` and `hive launchers default`
Delivers: validated, persisted launchers and default selection.
Units: `hive/configedit.py` + `tests/test_configedit.py`; `hive/inspect.py` + `tests/test_inspect.py`;
then `hive/cli.py` + `tests/test_config.py`.
Tests:
- `test_append_launcher_roundtrip`: -> `verify` passes, comments kept
- `test_set_default_launcher_replace` / `_insert` / `_no_table`: replace value; add under header; append table
- `test_launchers_add_ok`: -> listed, first add becomes default
- `test_launchers_add_symlink_outside_roots`: PATH symlink to a target outside `inspect_roots` -> accepted
- `test_launchers_add_duplicate`: name in registry or discover -> exit 2
- `test_launchers_add_not_found`: -> exit 2, config unchanged
- `test_launchers_add_private_refused`: under a `private` path -> exit 3, config unchanged
- `test_launchers_default_unknown`: -> exit 2
Checkpoint: `uv run pytest -q`

### 5. Queen dialogue
Delivers: launcher -> (add new) -> trust via `AskUserQuestion`, then spawn.
Units: `hive/queen/CLAUDE.md`; `tests/test_guard.py`.
Tests:
- extend `test_other_tools_pass` (`test_guard.py:107`) with `AskUserQuestion` -> `(0, "")`
- `test_launchers_commands_allowed`: `hive launchers`, `hive launchers add x "claude --remote-control {name}"`,
  `hive launchers default x`, `hive spawn foo --launcher x` -> `(0, "")`
Checkpoint: `uv run pytest -q tests/test_guard.py`, then the manual check in Verification.

## Verification

`uv run pytest -q` passes. Manual, with the user: on a running hive from `ca37171`, install the branch and run
`hive up`; expect no new queen and no restarted workers (legacy adoption), `hive list` showing new names. Then
with two launchers (`claude`, `claude-supervisor`) and an untrusted dir, ask the queen "spin up a worker in
<dir>" over remote control; expect the launcher choice (default first, add new, question text listing all
names), then one trust choice naming that launcher's trust file, then a pane named
`<dir>-<launcher>-<machine>-1` with `trusted=yes`. A second spawn in the same dir asks no trust question.

## Risks

- `AskUserQuestion` rendering over remote control and under `--permission-mode auto` is unverified; Decision 16.
- A wrong `config_dir` on a launcher makes hive check and seed the wrong trust file; mitigated by
  inspect-and-confirm, not prevented.
- Adopted legacy panes keep their old RC session names until restarted; the remote-control client shows old
  names for them. A remote client bookmarked on `<host>-queen` must switch to `hive-queen-<host>` after restart.
- Legacy adoption only covers panes whose canonical name is desired; a legacy pane of a since-removed type
  stays unmanaged (as today for unparseable panes). Panes from 1.5 that carry only a title (`cli.py:351-353`)
  also stay unmanaged until `hive restart`.
- `launchers add` stores a command that later runs in a pane: same exposure as `spawn --command` today.
- Line-wise config edits: an unusual `[defaults]` layout must fail `verify`, not corrupt the file.
- Accepted risk (D5): trust is enforced only by queen instructions; nothing stops the queen adding `--trust`
  itself (true today). The user accepted this; no hook enforcement.

## Scope Notes

In: naming change and queen rename with legacy adoption, registry, default, list/add/default commands,
`--launcher` with `--sandbox` alias, queen dialogue, docs.
Out: launcher remove/rename (hand-edit config), migrating existing agents to launcher names, recording the
launcher name on `[[agents]]`, hook-enforced trust, changes to trust storage or `inspect-launcher` output,
pushing.
User decisions D1-D5 (2026-10-22) are binding; `executor` must not reopen them.

## Assumptions

- `hive discover` record shape (`name`, `command`, `config_dir`) is stable; checked by `parse_records` tests.
- tmux and remote control accept names of this length; checked in the manual Verification run.

## Open Questions

None.

## Log

- 2026-10-22 imported from the sibling worktree `claude-rc-hive-launcher-registry`. Verified: main `c0dab66` tree equals branch tip `402d89d` except `VERSION`; all S1-S5 symbols and tests present on main; `uv run pytest -q` on main: 328 passed. Not verified: the manual queen check over remote control.

## Todo

- [x] S1 naming in `desired_panes`, `queen_name`, `parse_name` + legacy adoption, `hive-queen` reserved, prefix check
- [x] S1 name assertions updated, new tests pass; README/queen CLAUDE.md/example naming lines
- [x] S2 config schema, `load_config` validation, `hive launchers` list, example config, README
- [x] S2 tests pass
- [x] S3 `launcher_record`, `--launcher` + `--sandbox` alias, default-launcher fallback
- [x] S3 tests pass
- [x] S4 `append_launcher`, `set_default_launcher`, `resolve_launcher`, `launchers add|default`
- [x] S4 tests pass
- [x] S5 queen CLAUDE.md dialogue, guard tests
- [ ] Full suite passes; manual queen check with the user
