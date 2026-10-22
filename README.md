# hive

hive runs Claude Code Remote Control sessions in tmux: N instances of each agent type, on a dedicated tmux server, with one extra coordinator session called the queen. You describe the agents in one config file; `hive up` starts them, and each one is then reachable by name from the Claude app or claude.ai through Remote Control. Use it to keep a fixed set of remote-controllable Claude sessions running on a machine, start them at login with systemd, and add or remove instances by asking the queen.

The repository is `claude-rc-hive`; the program, Python package, config directory and systemd unit are all named `hive`.

Terms used throughout are defined in [Concepts](#concepts).

## What it does

- Starts one tmux window per agent type and one pane per instance, all on its own tmux server (`tmux -L hive`), so other tmux sessions are never touched.
- Keeps the running session in line with `config.toml`: `hive up` on a running hive starts missing panes and stops surplus ones, without a restart.
- Starts the queen first: a Claude session that edits the config for you through `hive spawn` and `hive stop` ("one more X", "one less X").
- Checks Claude Code workspace trust before adding a directory. hive never seeds trust silently: it writes the trust key only when you pass `--trust`.
- Restricts the queen with Claude Code permission rules and a guard hook, so it can run single `hive` commands and touch files only in its own directory (see [Queen security model](#queen-security-model)).
- Optionally installs a systemd user unit that starts the hive at login or boot.

## What it doesn't do

- It does not sandbox or isolate agents. Each pane runs its command as your user, in its directory. `--launcher NAME` (alias `--sandbox`) selects a launcher from `[[launchers]]` or `[queen] discover`; it is not an OS sandbox.
- It does not manage Claude login, accounts, or the Remote Control connection. The command in each pane does that.
- It does not know what a command does. An agent type is a directory plus a command; hive types the command into a shell and leaves it there. A command that exits is not restarted (the shell stays).
- It does not apply a changed `dir` or `command` to panes that are already running.
- It does not route work to agents or watch their output. The queen adds and removes workers; it does not do their work.
- It targets Linux with tmux. The service needs systemd user units.

## Concepts

| Term | Meaning |
| --- | --- |
| agent type | One `[[agents]]` entry: a `type` name, a `dir`, a `count` and a `command`. One tmux window per type, named `<type>-<machine>`. (Called "agent kind" in `hive --help`.) |
| worker, instance | One running copy of an agent type, in one tmux pane. `hive spawn` adds workers, `hive stop` removes them. |
| pane name | `<type>-<machine>-<index>` (index from 1), or `hive-queen-<machine>`; `<machine>` is `prefix`. hive stores it in the tmux pane option `@hive_name`, sets it as the pane title, and the default command uses it as the Remote Control session name. |
| managed / unmanaged pane | A pane with `@hive_name` (or whose title equals a configured pane name, which hive then adopts) is managed. Every other pane in the session is unmanaged and never touched. |
| queen | A persistent Claude session in window `hive-queen-<machine>` that runs `hive` commands for you. |
| hive config directory (hive dir) | `${XDG_CONFIG_HOME:-~/.config}/hive`. Holds `config.toml`, the lock file `.lock`, and the queen's directory `queen/`. |
| search root | A directory listed in `search_roots`. `hive spawn NAME` looks for a directory `NAME` under each one; `hive dirs` lists them. |
| trust root | The directory whose trust Claude Code records: the git toplevel of an agent's `dir`, else the `dir` itself. |
| trust file | The `.claude.json` that holds Claude Code's trust answers. See [Workspace trust](#workspace-trust). |
| launcher | A named launch command, often a wrapper that runs Claude with its own Claude config dir. `[[launchers]]` and `[queen] discover` list them; `hive spawn --launcher NAME` uses one. See [Launchers](#launchers). |
| `config_dir` | The Claude config dir an agent's command uses. hive reads it only to find the trust file and never exports it. |

## Getting started

### Prerequisites

- Linux, with git.
- tmux >= 3.0.
- Python >= 3.11, and uv (or pipx) to install hive.
- Claude Code CLI (`claude`), logged in, with Remote Control. The queen's guard hook needs Claude Code >= 2.1.295 (for the hook's `onFailure` setting) and `python3` on `PATH`.
- Optional: systemd (user units) to start the hive at login.

### Install

```sh
uv tool install git+https://github.com/sly2j/claude-rc-hive
# or, from a checkout:   uv tool install .
# or with pipx:          pipx install git+https://github.com/sly2j/claude-rc-hive
```

On distributions that follow PEP 668 (for example Debian 13), `pip install --user` is blocked; use uv or pipx.

### First hive

1. Write the config:

   ```sh
   hive init
   ```

   This creates `~/.config/hive/config.toml` (or under `$XDG_CONFIG_HOME`). It defines no agents; the default command for agents is `claude -n {name} --remote-control {name} --permission-mode auto`.

2. Add an agent type for a project directory:

   ```sh
   hive spawn ~/work/my-project
   ```

   If Claude Code does not trust that directory yet, hive prints `untrusted: ...` and exits 3 without changing anything. Trusting a folder enables its `.claude/settings.json` allow rules, hooks and env. If you accept that, rerun with `--trust`:

   ```sh
   hive spawn ~/work/my-project --trust
   ```

   The session is not running yet, so this only adds an `[[agents]]` entry with type `my-project`.

3. Start the hive:

   ```sh
   hive up
   ```

   It starts the queen, then one pane per worker, and prints a `started <name>` line for each.

4. Accept the queen's trust dialog. On first start Claude Code asks whether to trust the queen's directory, and `hive up` warns `untrusted hive-queen-<machine> ...`. Attach, accept the dialog in the queen's pane, and detach with the tmux prefix followed by `d` (`Ctrl-b d` by default):

   ```sh
   hive attach
   ```

5. Connect. In the Claude app or on claude.ai, open Remote Control; each agent appears under its pane name, for example `my-project-myhost-1`, and the queen as `hive-queen-myhost` (`myhost` being the short hostname, the default prefix).

6. Check state:

   ```sh
   hive status   # windows, panes, titles, running commands
   hive list     # config and running state side by side
   ```

7. Stop everything:

   ```sh
   hive down
   ```

To start the hive at login, run `hive install-service --enable` (see [systemd service](#systemd-service)).

## Configuration

The config file is `${XDG_CONFIG_HOME:-~/.config}/hive/config.toml`. Unknown keys are errors. `hive init` writes a commented template from `hive/config.example.toml`.

| Key | Meaning |
| --- | --- |
| `prefix` | Machine part of instance and window names (`[A-Za-z0-9_-]+`). Default: short hostname. |
| `session` | tmux session name. Default: `<prefix>-agents`. |
| `tmux_socket` | `tmux -L` server name. Default: `hive`. Set a different one in any test or isolated config, or it talks to your real hive. |
| `login_shell` | `true` (default): each pane runs `<shell> -l`, so your login profile applies. `false`: panes run tmux's `default-command`. |
| `shell` | Shell for `login_shell`. Default: your login shell from passwd, else `$SHELL`, else `/bin/sh`. |
| `search_roots` | Optional list of directories where `hive spawn NAME` looks for a directory `NAME`, and that `hive dirs` lists. `hive spawn` skips roots that do not exist; `hive dirs` reports them as `root=<path> error=missing`. `hive roots add\|remove PATH` edits it. |
| `inspect_roots` | Optional list of the only directories `hive inspect-launcher` reads from. Default: `["~/.local/bin", "/usr/local/bin", "/usr/bin", "/bin", "/opt"]`. |
| `[defaults] command` | Command for every agent type without its own `command`. |
| `[defaults] launcher` | Optional name of the default launcher for new types: a `[[launchers]]` name or a `[queen] discover` name. Only checked against `[[launchers]]` at load; other names are looked up in discover when used. |
| `[queen] command` | Optional command for the queen. Default: `[defaults] command`, else `claude -n {name} --remote-control {name} --permission-mode auto`. |
| `[queen] discover` | Optional shell command that `hive discover` runs; its output is printed as is. `hive spawn --launcher NAME` also runs it (timeout 60 s) and reads one record per line (see `hive discover`). |
| `[queen] config_dir` | Optional Claude config dir of the queen's command. Only used to find the trust file `<config_dir>/.claude.json`; never exported. |
| `[queen] env` | Optional inline table, same rules as `[[agents]] env`, for the queen's pane. |
| `[env]` | Optional `KEY = "value"` strings, exported in every pane before the command. See [Panes and environment](#panes-and-environment). |
| `[service] after` | Optional list of units added to `After=` in `hive.service`. |
| `[[launchers]] name` | Required, unique, `[a-z0-9][a-z0-9-]*`. |
| `[[launchers]] command` | Required; must contain `{name}` and `--remote-control`. |
| `[[launchers]] config_dir` | Optional absolute path (`~/` expanded): the Claude config dir the launcher uses; never exported. |
| `[[launchers]] description` | Optional free text. |
| `[[agents]] type` | Required, unique, not `queen` or `hive-queen`; letters, digits, `.`, `_` and `-` only. Window name is `<type>-<machine>`, `<machine>` being `prefix`. |
| `[[agents]] dir` | Required. Each pane starts with `cd <dir>`. If it does not exist, `up`, `restart` and `reconcile` skip that type's panes and start everything else, `hive list` marks the type `dir_missing=yes`, and `spawn` of that type is refused. |
| `[[agents]] count` | Number of panes, integer >= 0. Default: 1. `0` keeps the entry (dir, command, comments) without panes. |
| `[[agents]] command` | Optional override of `[defaults] command`. One of the two is required. |
| `[[agents]] env` | Optional inline table of string values, e.g. `env = { FOO = "bar" }`. Exported in that type's panes after the global `[env]`, so it wins. Unlike `[env]`, not set on the tmux server. |
| `[[agents]] config_dir` | Optional, e.g. `config_dir = "~/.claude-sandbox"`: the Claude config dir that the type's command uses (a sandbox launcher's own dir). Only used to find the trust file `<config_dir>/.claude.json`; never exported. |

Validation rules:

- `prefix` and `session` must be non-empty and contain no `.`, `:` or whitespace, since tmux rewrites or mis-targets such names.
- `tmux_socket` must be non-empty and contain no whitespace (`x.y` is valid).
- `search_roots` entries, `dir`, `config_dir` and `inspect_roots` entries must be absolute or start with `~` (expanded), and must not be empty.
- Commands and `shell` must not be empty.
- Env names (in `[env]`, `env` tables and `spawn --env`) must match `[A-Za-z_][A-Za-z0-9_]*`. `CLAUDE_CONFIG_DIR` is rejected everywhere, because hive never sets it; use `config_dir`.

Placeholders in commands, replaced by plain text substitution (other braces such as `${VAR}` are untouched): `{name}` = `<type>-<machine>-<index>` (`<machine>` is `prefix`; index starts at 1), `{type}`, `{index}`, `{dir}`, `{prefix}`.

### Applying changes

Run `hive up`: it reconciles a running session, so adding, removing or recounting agents needs no restart. Changes to `dir` or `command` of panes that are already running are not applied; stop and re-spawn them, or `hive restart`. Session-level keys (`prefix`, `session`, `tmux_socket`, `shell`, `login_shell`, and `[env]` for existing panes) also need `hive restart`.

Upgrading from the `<prefix>-<type>-<index>` naming: `hive up` and reconcile adopt those panes without a restart, renaming them in place (the queen from `<prefix>-queen` to `hive-queen-<machine>`). Their Remote Control session keeps its old name until restarted; a remote client bookmarked on `<host>-queen` must switch to `hive-queen-<host>` after the restart.

## Commands

| Command | Does |
| --- | --- |
| `hive up` | Start the session if it is not running, with the queen first. Prints `started session <session> (tmux -L <socket>)`, then `started <name>  [<dir>]  <command>` per pane. If it is running, reconcile it with the config (same output as `hive reconcile`). Types whose `dir` is missing are skipped with a summary warning, and the exit code stays 0. If startup fails, hive removes the half-built session. |
| `hive down` | Kill the session, and the hive tmux server once it is empty. Prints `stopped session <session>`, or `session <session> not running` (exit 0). Works with a broken `config.toml`: it warns `config problem: ...` and takes `prefix`, `session` and `tmux_socket` from the config's parsable top-level lines, falling back to the defaults for names the config does not set. A name that is set but invalid is still an error (exit 2). |
| `hive restart` | `systemctl --user restart hive.service` if that unit is active, else `down` then `up`. |
| `hive status` | Prints `session <session>`, then one line per window and one per pane (`pane <index>: <title>  (<command>)`), then `attach: tmux -L <socket> attach -t <session>`. Exit 1 and `not running` if the session is not running. Works with a broken `config.toml`, like `down`. |
| `hive attach` | Attach to the session (exit 1 if not running). |
| `hive list` | Config and running state side by side, as `key=value` lines. See [hive list](#hive-list). |
| `hive spawn TARGET [-n N] [--type TYPE] [--command CMD \| --launcher NAME] [--config-dir PATH] [--env KEY=VALUE ...] [--trust]` | Add N workers of a type or a new directory, then reconcile. See [hive spawn](#hive-spawn). |
| `hive stop TARGET [-n N]` | Remove N workers (default 1) of an existing type: lower `count`, then reconcile. The highest indices go first; the entry stays at `count = 0`. |
| `hive reconcile [--dry-run]` | Make the running session match the config: adopt panes titled with a configured pane name (`adopted <name>`), start missing panes, stop managed panes no longer in the config (`stopped <name>`), leave unmanaged panes alone, never stop the queen. Prints `no changes` when there is nothing to do. `--dry-run` only prints `would start <name>`, `would stop <name>` and `would skip <name>: dir <dir> does not exist`. Exit 1 if not running. |
| `hive discover` | Run `[queen] discover` and print its output verbatim. See [hive discover](#hive-discover). |
| `hive launchers` | List the known launchers, one `launcher name=N source=config\|discover default=yes\|no config_dir=<path\|default> command=<cmd> [description=...] [shadowed=yes]` record each: the default first, then the registry, then `[queen] discover` records. See Launchers. |
| `hive launchers add NAME COMMAND [--config-dir PATH] [--description TEXT]` | Append a `[[launchers]]` entry to `config.toml`. NAME matches `[a-z0-9][a-z0-9-]*` and is not already a registry or discover name (exit 2). COMMAND must contain `{name}` and `--remote-control`, and its executable (first word) must exist on `PATH` or as a path (exit 2) and not be under a `secrets`/`private` path (exit 3). hive never runs it. The config is unchanged on any error. The first launcher added becomes `[defaults] launcher` when none is set. |
| `hive launchers default NAME` | Write `[defaults] launcher = NAME`. NAME is a registry or discover name (exit 2 otherwise). |
| `hive dirs [ROOT] [--depth N]` | Read-only: list the directories under the search roots. See [hive dirs](#hive-dirs). |
| `hive roots [add PATH \| remove PATH]` | Show `search_roots`, or add or remove one. See [hive roots](#hive-roots). |
| `hive inspect-launcher COMMAND` | Read-only: show a launcher's `CLAUDE_CONFIG_DIR` evidence. See [hive inspect-launcher](#hive-inspect-launcher). |
| `hive init` | Write the example config if none exists and print `wrote <path>; it has no agents yet, so 'hive up' starts only the queen; add agents with 'hive spawn DIR' or by editing it` (else print `<path> already exists`). Then print `config=<path> session=<session> socket=<socket>; set tmux_socket in the config to keep a test or isolated config off your real hive's tmux server`. The example defines no agents; the commented `[[agents]]` entry shows the keys. |
| `hive install-service [--enable]` | Write `~/.config/systemd/user/hive.service` and reload; `--enable` also runs `enable --now`. Without `--enable` it prints the follow-up commands `systemctl --user enable --now hive.service` and `loginctl enable-linger $USER`. Needs `systemctl` (else `systemctl not found`, exit 1) and a valid config. See [systemd service](#systemd-service). |
| `hive uninstall-service` | Disable, remove the unit, reload. Prints `not installed: <path> does not exist` and exits 0 if there is no unit. |
| `hive --version` | Print `hive <version>`. |

### hive list

`hive list`

Config and running state as `key=value` lines, in this order: a header `session=... socket=... running=yes|no config=<path>`; the `queen` line; one `agent type=... dir=... count=... running=... command=...` line per type (with ` dir_missing=yes` when the dir is gone); one `pane name=<name> type=<type> id=<pane id>` line per running managed pane (with ` waiting=trust` when its screen shows the trust dialog); one `unmanaged pane=... window=... title=...` line per unmanaged pane; `drift start=<names|none> stop=<names|none>` when the session differs from the config; then `search_roots=<comma-separated>` and `discover=<command>`. The `queen` and `agent` lines carry `trusted=yes|no trust_file=<path>`.

### hive spawn

`hive spawn TARGET [-n N] [--type TYPE] [--command CMD | --launcher NAME] [--config-dir PATH] [--env KEY=VALUE ...] [--trust]`

Add N workers (default 1): raise `count` of the type TARGET names, or add an `[[agents]]` entry for a directory (absolute, `~/...`, or a name under `search_roots`), then reconcile. Prints `config agent=<type> count=<old>-><new> file=<path>`. `--command` must contain `{name}` and `--remote-control`. `--config-dir` writes `config_dir` into the new entry. `--launcher NAME` (`--sandbox` is an alias) copies `command` and `config_dir` from the launcher into the new `[[agents]]` entry; NAME is a `[[launchers]]` name or a `hive discover` record `name=NAME` (registry wins; error if unknown, or if `[queen] discover` is unset and NAME is not in the registry). A new type chosen with a launcher is `<base>-<NAME>` unless `--type` is given, base being TARGET's type or the directory's basename. `--launcher` cannot be combined with `--command` or `--config-dir`. Without `--command` or `--launcher`, a new type uses `[defaults] launcher` when set (type suffixed likewise), else `[defaults] command`. An existing type keeps its stored command and only gets a count bump. Spawn exits 2 when the resulting type already exists with a different `command`, `config_dir` or `--env` set (use a new `--type`), and when the final type is reserved (e.g. dir `hive` with launcher `queen`). `--env` (repeatable) fills the new entry's `env` table. Exits 3 without changes if the directory is untrusted, unless `--trust` is given (see [Workspace trust](#workspace-trust)). If the session is not running, only the config is updated.

### hive discover

`hive discover`

Run `[queen] discover` and print its output verbatim; the exit code is the command's. For `spawn --launcher`, each line is one record of whitespace-separated `key=value` tokens, values with spaces double-quoted (shlex): `name=api config_dir=~/.claude-api command="claude-supervisor run api -- -n {name} --remote-control {name}"`. Records are also usable as launcher names. `name`, `config_dir` and `command` are required; unknown keys and blank lines are ignored; a bad line is an error naming it.

### hive dirs

`hive dirs [ROOT] [--depth N]`

Read-only: list the directories under each search root (or under ROOT: a search root, a directory inside one, or a name resolved as for `spawn`), N levels deep (default 1, max 3). Prints `root=<path>` per root (`root=<path> error=missing`, `error=not a directory` or the OS error if it cannot be read), then `dir=<path> name=<basename> type=<type>|none` per directory, `type` being the `[[agents]]` entry using that directory. A subdirectory that cannot be read prints `dir=<path> error=<reason>`. Skips hidden entries and `secrets`/`private` directories, lists a symlink only if it resolves inside `search_roots` and never descends into one; at most 500 records, then `more=yes`. A ROOT outside `search_roots` or under a `secrets`/`private` path is `refused: ...`, exit 3; unset `search_roots` exits 2; exit 1 if no root could be listed.

### hive roots

`hive roots [add PATH | remove PATH]`

Without arguments print `root=<path>` per search root (`roots=none` if unset). `add` appends an existing directory to `search_roots` in `config.toml` (written as `~/...` when PATH starts with `~`, else resolved) and prints `added <path>`, or `unchanged` if already present; refused (exit 3) under a `secrets`/`private` path. `remove` drops the entry naming that directory (`removed <path>`, or `not found: ...`, exit 2). Only rewrites a one-line `search_roots = [...]` (or inserts one) and keeps the rest of the file as is.

### hive inspect-launcher

`hive inspect-launcher COMMAND`

Read-only look at the launcher of a launch command (one argument, e.g. `"claude-supervisor run api -- -n {name}"`): resolves its first word on `PATH` and through symlinks, and reads it only if both paths are under `inspect_roots` and not under a `secrets`/`private` path (else `refused: ...`, exit 3; `not found: ...` for a missing, non-regular or unreadable file, exit 2). Prints `path=... resolved=... kind=binary`, or for a script `path=... resolved=... kind=script shebang=<first line> lines=<n>` (plus `truncated=yes` beyond 1 MiB); then for scripts the lines mentioning `CLAUDE_CONFIG_DIR`, `claude`, `exec`, `env`, `export`, `HOME=` or `XDG_CONFIG_HOME` as `line=N text=...` (secret-like lines as `line=N redacted=secret-like`), at most 200, then `more=yes`; and last `env CLAUDE_CONFIG_DIR=<value>|unset` from hive's own environment. Never runs the launcher.

Errors go to stderr as `hive: error: <message>`, warnings as `hive: warning: <message>`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | OK. Also `hive down` when the session is not running. |
| 1 | Runtime error (tmux or systemctl missing or failed, lock timeout, file not writable), or the session is not running (`status`, `attach`, `reconcile`). `hive dirs` when no root could be listed. |
| 2 | Usage or config error. Also `hive roots remove` with a path not in `search_roots`, and `inspect-launcher` `not found`. |
| 3 | Untrusted or refused: `spawn` of an untrusted directory without `--trust`, and `refused:` from `dirs`, `roots add` and `inspect-launcher`. |
| 130 | Interrupted (Ctrl-C). |

`hive discover` returns the discover command's exit code, `hive attach` returns tmux's, and `hive restart` through systemd returns `systemctl`'s.

## Panes and environment

Panes are titled with the pane name and the app may not override it (`allow-set-title off`). Each pane runs a login shell (or tmux's `default-command` when `login_shell = false`), and the command is typed into it, so the shell stays after the command exits (the pane closes only when you `exit` that shell).

hive identifies its panes by the tmux pane option `@hive_name`, so the index in a pane name is hive's. tmux's own `pane_index` (shown by `hive status`) is positional and shifts when a pane is closed. A pane without `@hive_name` whose title equals a configured pane name is adopted (tagged) by `hive up` or `hive reconcile`.

When a pane starts, hive warns if its command lacks `--remote-control` (the pane will not be reachable through Remote Control) or if the command's program is not on hive's `PATH` (the pane's login shell may have a different one). These are warnings only.

Each pane runs one shell line: `export KEY="value"` for every `[env]` entry, then for the type's `env`, then `cd <dir>`, then the command. Values go inside double quotes in the pane shell: `\`, `"` and backticks are escaped and `$` is not, so `$VAR` expands and `$(cmd)` runs in the pane shell. `[env]` is also set on the hive tmux server (`set-environment -g`, with only `$VAR` expanded, by hive), so panes opened later inherit it. Use `[env]` for anything your login profile does not set.

Under the service, the tmux server starts with the bare systemd user environment, and a tmux pane can run a non-login shell (for example when tmux.conf sets `default-command`), which skips your profile (such as `~/.config/zsh/.zprofile`, where `SSH_AUTH_SOCK` for a systemd user ssh-agent is typically exported). That is why panes are login shells by default: they get the same environment as an SSH login, without hive knowing about ssh or anything else. Pairing the unit with `[service] after = ["ssh-agent.service"]` makes sure the agent socket exists before the panes start.

The tmux server inherits the environment of the `hive` call that starts it. hive drops the variables Claude Code sets for its child processes (`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_SESSION_ID` and the like) from its tmux calls, so starting the hive from inside a Claude session does not make every agent think it is nested in it. Anything else in that shell's environment still reaches the panes.

## tmux socket

hive uses its own tmux server (`tmux -L hive`, or the `tmux_socket` you set). Plain `tmux ls` and `tmux attach -t myhost-agents` use the default socket and do not see it. Use `hive attach` or `tmux -L hive attach -t myhost-agents`. Your `~/.config/tmux/tmux.conf` still applies.

The reason: `hive down` and `systemctl --user stop hive` must never touch other tmux sessions.

## Locking

`up`, `down`, `spawn`, `stop`, `reconcile`, `roots` (also when only listing) and `launchers add|default` take a lock on `${XDG_CONFIG_HOME:-~/.config}/hive/.lock`, so the queen and you can run them at the same time. `restart` takes it only when it runs `down` and `up` itself; a restart through systemd takes it in the unit's `hive down` and `hive up`. A command waits up to 30 s for the lock, then exits 1 with `another hive command holds <path>; try again`. `hive down` still runs if the lock file cannot be opened, so systemd's `ExecStop` can always stop the session.

## Queen

The queen is a persistent Claude session, window and Remote Control session `hive-queen-<machine>`, always started first. It runs the `hive` commands for you: ask it for "one more X" or "one less X" and it edits `config.toml` through `hive spawn` and `hive stop`, which then reconcile the session. Config stays the single source of truth; the queen is never stopped by reconcile, only by `hive down`.

With the default `[queen] command` the queen runs with Remote Control on. A custom `[queen] command` without `--remote-control` gets only a warning when the queen starts.

Its directory is `${XDG_CONFIG_HOME:-~/.config}/hive/queen`. hive manages `CLAUDE.md`, `.claude/settings.json` and `.claude/hooks/guard.py` there and rewrites them when they are missing or differ from the packaged copy. This happens only when the queen pane starts: on a fresh `hive up`, or when reconcile finds the queen missing. After upgrading hive, run `hive restart` to re-sync them. Put your own additions in `CLAUDE.local.md`; hive never writes it.

For a spawn with a custom launcher command and no known `config_dir`, the queen tries to infer the launcher's config dir with `hive inspect-launcher` (it never runs the launcher). When it finds one, it tells you the evidence and asks you to confirm before it passes `--config-dir`; with no evidence it assumes the default trust file.

### Queen security model

The queen runs with `--permission-mode auto` by default, so its limits come from the flags hive adds to its command and the files hive manages in its directory:

- The queen's tools are limited to Bash, Monitor, Read, Grep, Glob, Edit, Write, NotebookEdit and AskUserQuestion (for its launcher choice dialogue). hive appends `--tools Bash,Monitor,Read,Grep,Glob,Edit,Write,NotebookEdit,AskUserQuestion --strict-mcp-config` to the queen command unless the command already has those flags, and the guard hook denies every other tool (WebFetch, WebSearch, Agent, MCP tools) with `guard: tool '<name>' is not available to the queen`.
- `.claude/settings.json` allows `Bash(hive:*)` and denies the Read tool on `~/.ssh`, `~/.claude`, `~/.claude.json`, `~/.gnupg`, `~/.aws`, `.env` files and `secrets`/`private` paths.
- A PreToolUse guard hook (`.claude/hooks/guard.py`, matcher `*`) checks every tool call. If the hook fails (it crashes, or `python3` is missing), the call is blocked; this needs Claude Code >= 2.1.295, and older versions let the call through.
- Shell tools (Bash, Monitor): only a single `hive` command is allowed, with no `;`, `&`, `|`, backticks, `$(`, redirects or newlines; path arguments may not use globs or brace expansion or point under a `secrets`/`private` path. No options before the subcommand (`hive -- down` is blocked); only `hive --version`, `-h` and `--help` are allowed there. Anything else is blocked.
- These `hive` commands get a confirmation prompt that names what they do (when one call matches several, the reasons are combined into one prompt):
  - `hive spawn --command CMD`, unless CMD equals a command already in `config.toml` (`[defaults] command` or an `[[agents]] command`). It also prompts when `config.toml` cannot be read. `--command=CMD` and abbreviations such as `--comm` count too.
  - `hive spawn` with any `--env`, since a variable such as `BASH_ENV` could get around the command check.
  - `hive spawn ... --trust`.
  - `hive roots add PATH`.
  - `hive launchers add`, since a registered launcher then runs through `spawn --launcher` without a `--command` prompt.
  - `hive down` and `hive restart`, which stop the whole hive, the queen included.
- A blocked call stays blocked; the prompt never overrides it.
- File tools: only paths inside the queen's directory, never under a `secrets`/`private` path. Glob and Grep patterns may not contain `..` or a path part that mixes `{` or `[` with `.`. No writes to its `.claude/`, `.mcp.json`, `.claude.json` or `CLAUDE.md`; `CLAUDE.local.md` is writable.

What this does not cover: every other single `hive` command runs without a prompt, including `hive stop`. The workers the queen spawns are ordinary panes and get none of these restrictions.

## Launchers

A launcher is a named launch command, optionally with its own Claude config dir. The registry is the `[[launchers]]` list in `config.toml`; `[defaults] launcher` names the default for new types. `hive launchers` lists the registry plus the `[queen] discover` records. The registry wins on a name in both, and the discover record is then marked `shadowed=yes`. If `discover` is unset it adds nothing; if it fails, the registry is still listed, followed by a `discover error=<reason>` line (exit 0). Use one with `hive spawn --launcher NAME`. Add one with `hive launchers add` and change the default with `hive launchers default`; check the command first with `hive inspect-launcher "CMD"` to infer its `config_dir`. To rename or remove a launcher, edit `config.toml` by hand.

## Workspace trust

Claude Code stores the answer to its "trust this folder?" dialog in a trust file as `projects["<root>"].hasTrustDialogAccepted = true`. `<root>` is the trust root: the git repo root of the directory (`git rev-parse --show-toplevel`), else the directory itself. hive checks only that exact key. It ignores trust Claude Code would inherit from a trusted parent folder or, in a linked worktree, from the main checkout, so it may ask when Claude would not.

The trust file is `<config_dir>/.claude.json` when the agent type (or `[queen]`) has `config_dir`. Without `config_dir`, it is `$CLAUDE_CONFIG_DIR/.claude.json` if hive's own environment has `CLAUDE_CONFIG_DIR`, else `~/.claude.json`. With `spawn --launcher`, the chosen launcher's `config_dir` is used. So the systemd service, a plain shell and the queen can each resolve a different trust file and disagree on whether a directory is trusted. Set `config_dir` to pin it.

hive never seeds trust silently. `hive spawn` checks the trust root (in the chosen launcher's `config_dir`, its `.claude.json`) before editing the config; if it is untrusted it prints `untrusted: <dir> (trust root <root>, trust file <file>); rerun with --trust after the user confirms` to stderr and exits 3 without changes. Rerun with `--trust` once you have confirmed: hive sets only that one key, under Claude Code's lock on the file (it refuses a trust file that is not valid JSON), prints `trusted <root> in <file>`, and proceeds.

`hive up` and `hive reconcile` (also `--dry-run`) warn `untrusted <name> <root>` for each pane they would start in an untrusted root, and still start it. The pane shows the dialog, which you accept via `hive attach`. `hive list` marks such a pane `waiting=trust`.

## systemd service

`hive install-service --enable` writes `~/.config/systemd/user/hive.service`, reloads systemd and starts the unit; it then starts at login (at boot only with `loginctl enable-linger $USER`). Without `--enable` it writes and reloads only, and prints those two commands (`systemctl --user enable --now hive.service`, `loginctl enable-linger $USER`). It checks for `systemctl` first and exits 1 with `systemctl not found` if it is missing. The unit always goes to `~/.config/systemd/user`, where the systemd user manager looks. If `XDG_CONFIG_HOME` is set to something other than `~/.config` when you install, the unit gets an `Environment=XDG_CONFIG_HOME=...` line, so the service reads the same config as your shell.

`ExecStart` and `ExecStop` use `%h/.local/bin/hive`, or the `hive` found on `PATH` if it is elsewhere. `After=` is `network-online.target` plus the units in `[service] after`. With the defaults the unit is:

```ini
[Unit]
Description=hive: agent sessions in tmux
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/bin/hive up
ExecStop=%h/.local/bin/hive down
# The tmux server forked by `hive up` stays in this cgroup while the unit is
# active. tmux may move pane processes into their own tmux-spawn-*.scope units;
# `hive down` ends them by killing the session.
KillMode=control-group

[Install]
WantedBy=default.target
```

`hive up` forks the tmux server, which stays in the unit's cgroup while the unit is active. Once the service is installed, restart with `systemctl --user restart hive` (what `hive restart` does). A plain `hive down && hive up` would start a new tmux server outside the unit's cgroup, so `systemctl --user status hive` would not show it, and the cgroup kill would no longer back up `ExecStop`.

`KillMode=control-group` is the default, kept explicit. tmux built with systemd support (Debian's 3.5a is) moves each pane's processes into its own `tmux-spawn-*.scope` unit. `systemctl --user status hive` then lists only the tmux server, and the cgroup kill does not reach the agents; they end when `ExecStop` (`hive down`) kills the session and the panes get SIGHUP. `hive down` alone leaves the unit "active" (`RemainAfterExit`).

Logs: `journalctl --user -u hive`, or `journalctl --user-unit hive` where journald keeps no per-user files (`Storage=volatile`).

## Troubleshooting

- **`tmux ls` shows nothing.** hive runs on its own tmux server. Use `hive attach`, `hive status`, or `tmux -L hive ls`.
- **`untrusted: ...` and exit 3 from `hive spawn`.** The directory's trust root is not trusted in the trust file shown. Confirm, then rerun with `--trust`. If the file shown is not the one your command's Claude uses, set `config_dir` (or `--config-dir`).
- **`hive: warning: untrusted <name> ...` from `hive up`, or `waiting=trust` in `hive list`.** The pane shows Claude's trust dialog. `hive attach` and accept it.
- **`skipped <name>: dir <dir> does not exist`.** The type's `dir` is gone. Fix the path in `config.toml`, or `hive stop <type>` until its count is 0.
- **`'<program>' not found in PATH` warning, or a pane shows `command not found`.** The command's program is not on the pane's `PATH`. Under the service, panes get the login shell's `PATH`; check that your profile adds the directory (for example `~/.local/bin`).
- **An agent does not appear in Remote Control.** Check that its command contains `--remote-control {name}`, then `hive attach` and look at the pane: Claude may be waiting at a trust dialog or a login prompt.
- **`another hive command holds <path>; try again`.** Another `hive` command (possibly the queen's) is running. Retry when it finishes.
- **A broken `config.toml`.** Most commands exit 2 naming the file and key. `hive down` and `hive status` still work, with a `config problem: ...` warning, using `prefix`, `session` and `tmux_socket` from the config's parsable top-level lines or the defaults. If one of those names is set but invalid, they exit 2 too; fix it first.
- **`config edit produced an unexpected result`.** hive's line editor cannot safely rewrite this config (for example a `count =` line inside a multi-line string). Edit `config.toml` by hand.
- **The queen still follows old instructions after an upgrade.** Its files re-sync only when the queen starts; run `hive restart`.

## Uninstall

```sh
hive down
hive uninstall-service           # if you installed it
uv tool uninstall hive           # or: pipx uninstall hive
rm -r ~/.config/hive             # config.toml, .lock and queen/ (or under $XDG_CONFIG_HOME)
```

Trust keys that `hive spawn --trust` wrote stay in Claude Code's trust file.

## Development

```sh
git clone https://github.com/sly2j/claude-rc-hive
cd claude-rc-hive
uv sync
uv run --frozen pytest -q
uv run hive --version
```

The tests need git. `tests/test_tmux.py` runs a throwaway tmux server on its own socket with a temporary `HOME`, and is skipped when tmux is not installed. Run `uv sync` after pulling, or `uv run hive --version` keeps reporting the version of the last sync.

To update an installed copy after pulling or changing the repo:

```sh
uv tool upgrade hive         # or: uv tool install --reinstall .
```

### Versioning

The version is in `VERSION`, scheme `1.x`. A pre-commit hook bumps it on every commit on `main`, and does nothing on other branches; the first commit is `1.0`. Enable the hook once per clone:

```sh
git config core.hooksPath .githooks
```

The hook computes the new version from `HEAD:VERSION`, so re-running after an aborted commit does not bump twice. Feature branches: rebase on `main`, then squash-merge as one commit, so `main` stays linear and gets exactly one increment. `git commit --amend` on `main` bumps again; amend with `--no-verify`.

## License

MIT. See `LICENSE`.
