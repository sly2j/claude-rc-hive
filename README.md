# hive

Run N instances of each agent kind in tmux. A window is an agent kind, each pane is one instance. An agent kind is just a directory plus a command; hive knows nothing about what the command does.

The repository is `claude-rc-hive`; the program, Python package, config directory and systemd unit are all named `hive`.

## Install

```sh
uv tool install .            # from a checkout of this repo
# or: pipx install .
```

Debian 13 follows PEP 668, so `pip install --user` is blocked; use uv or pipx.

Needs tmux >= 3.0 (panes are tagged with a pane option, `@hive_name`; hive 1.5 only needed 2.6).

Update after pulling or changing the repo (both were verified to pick up a new `VERSION` and changed sources):

```sh
uv tool upgrade hive         # or: uv tool install --reinstall .
```

Then `hive init` writes `${XDG_CONFIG_HOME:-~/.config}/hive/config.toml` from the example; edit it and run `hive up`.

## Config

`${XDG_CONFIG_HOME:-~/.config}/hive/config.toml`. Unknown keys are errors. `hive init` writes a commented template from `hive/config.example.toml`.

| Key | Meaning |
| --- | --- |
| `prefix` | Name prefix. Default: short hostname. |
| `session` | tmux session name. Default: `<prefix>-agents`. |
| `tmux_socket` | `tmux -L` server name. Default: `hive`. |
| `login_shell` | `true` (default): each pane runs `<shell> -l`, so your login profile applies. `false`: panes run tmux's `default-command`. |
| `shell` | Shell for `login_shell`. Default: your login shell from passwd, else `$SHELL`, else `/bin/sh`. |
| `search_roots` | Optional list of directories (`~` expanded) where `hive spawn NAME` looks for a directory `NAME`. Missing roots are skipped. |
| `[defaults] command` | Command for every agent type without its own `command`. |
| `[queen] command` | Optional command for the queen. Default: `[defaults] command`, else `claude -n {name} --remote-control {name} --permission-mode auto`. |
| `[queen] discover` | Optional shell command that `hive discover` runs; its output is printed as is. `hive spawn --sandbox NAME` also runs it and reads one record per line (see `hive discover`). |
| `[queen] config_dir` | Optional absolute path (`~` expanded): the Claude config dir the queen's command uses. Only used to find the trust file `<config_dir>/.claude.json`; never exported. |
| `[queen] env` | Optional inline table, same rules as `[[agents]] env`, for the queen's pane. |
| `[env]` | Optional `KEY = "value"` strings, exported in every pane before the command. Values go inside double quotes in the pane shell: `$VAR` expands there, `\`, `"` and backticks are escaped. They are also set on the hive tmux server (`set-environment -g`, `$VAR` expanded by hive), so panes opened later inherit them. For anything your login profile does not set. `CLAUDE_CONFIG_DIR` is rejected here too; use `config_dir`. |
| `[service] after` | Optional list of units added to `After=` in `hive.service`. |
| `[[agents]] type` | Required, unique, not `queen`. Window name is `<prefix>-<type>`. |
| `[[agents]] dir` | Required, `~` expanded, must exist (checked by `up`, `restart`, `spawn`, `stop` and `reconcile`). Each pane starts with `cd <dir>`. |
| `[[agents]] count` | Number of panes, integer >= 0. Default: 1. `0` keeps the entry (dir, command, comments) without panes. |
| `[[agents]] command` | Optional override of `defaults.command`. One of the two is required. |
| `[[agents]] env` | Optional inline table of string values, e.g. `env = { FOO = "bar" }`. Exported in that type's panes after the global `[env]`, so it wins. Unlike `[env]`, not set on the tmux server. `spawn --env` names must match `[A-Za-z_][A-Za-z0-9_]*`. `CLAUDE_CONFIG_DIR` is rejected (hive never sets it); use `config_dir`. |
| `[[agents]] config_dir` | Optional absolute path (`~/` expanded), e.g. `config_dir = "~/.claude-sandbox"`: the Claude config dir that the type's command uses (a sandbox launcher's own dir). Only used to find the trust file `<config_dir>/.claude.json`; never exported. |

Placeholders in commands, replaced by plain text substitution (other braces such as `${VAR}` are untouched): `{name}` = `<prefix>-<type>-<index>` (index starts at 1), `{type}`, `{index}`, `{dir}`, `{prefix}`.

## Commands

| Command | Does |
| --- | --- |
| `hive up` | Start the session if it is not running, with the queen first. If it is running, reconcile it with the config: start missing panes, stop managed panes that are no longer in the config, leave unmanaged panes alone, never stop the queen. |
| `hive down` | Kill the session, and the hive tmux server once it is empty. |
| `hive restart` | `systemctl --user restart hive.service` if that unit is active, else `down` then `up`. |
| `hive status` | Windows, panes, pane titles and running commands; exit 1 if not running. |
| `hive attach` | Attach to the session. |
| `hive list` | Config and running state side by side (`key=value` lines), unmanaged panes, and drift. The `queen` and `agent` lines carry `trusted=yes\|no trust_file=<path>`. Each running managed pane gets a `pane name=<name> type=<type> id=<pane id>` record, with ` waiting=trust` when its screen shows the trust dialog. |
| `hive spawn TARGET [-n N] [--type TYPE] [--command CMD \| --sandbox NAME] [--config-dir PATH] [--env KEY=VALUE ...] [--trust]` | Add N workers (default 1): raise `count` of the type TARGET names, or add an `[[agents]]` entry for a directory (absolute, `~/...`, or a name under `search_roots`), then reconcile. `--command` must contain `{name}` and `--remote-control`. `--config-dir` writes `config_dir` into the new entry. `--sandbox NAME` takes `command` and `config_dir` from the `hive discover` record `name=NAME` (error if `[queen] discover` is unset or NAME is unknown) and defaults the type to `<base>-<NAME>`, base being TARGET's type or the directory's basename; it cannot be combined with `--command` or `--config-dir`. For an existing type, a different `command`, `config_dir` or `--env` set is rejected (exit 2, use a new `--type`). `--env` (repeatable) fills the new entry's `env` table. Exits 3 without changes if the directory is untrusted, unless `--trust` is given (see Workspace trust). If the session is not running, only the config is updated. |
| `hive stop TARGET [-n N]` | Remove N workers (default 1) of an existing type: lower `count`, then reconcile. The highest indices go first; the entry stays at `count = 0`. |
| `hive reconcile [--dry-run]` | Make the running session match the config; `--dry-run` only prints `would start` / `would stop`. Exit 1 if not running. |
| `hive discover` | Run `[queen] discover` and print its output verbatim. For `spawn --sandbox`, each line is one record of whitespace-separated `key=value` tokens, values with spaces double-quoted (shlex): `name=api config_dir=~/.claude-api command="claude-supervisor run api -- -n {name} --remote-control {name}"`. `name`, `config_dir` and `command` are required; unknown keys and blank lines are ignored; a bad line is an error naming it. |
| `hive init` | Write the example config if none exists. |
| `hive install-service [--enable]` | Write `~/.config/systemd/user/hive.service` and reload; `--enable` also runs `enable --now`. |
| `hive uninstall-service` | Disable, remove the unit, reload. |
| `hive --version` | Print the version. |

Panes are titled with the agent name and the app may not override it (`allow-set-title off`). Each pane runs a login shell and the command is typed into it, so the shell stays after the command exits (the pane closes only when you `exit` that shell).

Apply config changes with `hive up`: it reconciles a running session, so adding, removing or recounting agents needs no restart. Changes to `dir` or `command` of panes that are already running are not applied; stop and re-spawn them, or `hive restart`. Session-level keys (`prefix`, `session`, `tmux_socket`, `shell`, `login_shell`, `[env]` for existing panes) also need `hive restart`.

hive identifies its panes by the tmux pane option `@hive_name`, so the index in a name (`<prefix>-<type>-<index>`) is hive's. tmux's own `pane_index` (shown by `hive status`) is positional and shifts when a pane is closed. Panes with no hive name are unmanaged and are never touched.

Upgrading from 1.5: panes started by 1.5 carry only a title. `hive up` adopts them by exact title match while the config still names them; any other pane stays unmanaged until `hive restart`. The first `hive up` after the upgrade also adds the queen to the running session.

Run `hive up` from a plain shell, not from inside a Claude Code session: the tmux server inherits the caller's environment (for example the `CLAUDECODE` variables).

## Queen

The queen is a persistent Claude session, window `<prefix>-queen`, always started first with Remote Control on. It runs the `hive` commands for you: ask it for "one more X" or "one less X" and it edits `config.toml` through `hive spawn` and `hive stop`, which then reconcile the session. Config stays the single source of truth; the queen is never stopped by reconcile, only by `hive down`.

Its directory is `${XDG_CONFIG_HOME:-~/.config}/hive/queen`. hive manages `CLAUDE.md` and `.claude/settings.json` there and re-writes them whenever the queen starts and they differ from the packaged copy, so `uv tool upgrade hive` plus the next `hive up` re-syncs them. Put your own additions in `CLAUDE.local.md`; hive never writes it.

First start: Claude Code asks to trust the new directory. Attach once (`hive attach`) and accept; until then `hive` calls go through the permission mode's approval.

## Workspace trust

Claude Code stores the answer to its "trust this folder?" dialog in a trust file as `projects["<root>"].hasTrustDialogAccepted = true`. `<root>` is the git repo root of the directory (`git rev-parse --show-toplevel`), else the directory itself. hive checks only that exact key: it ignores trust Claude Code would inherit from a trusted parent folder or, in a linked worktree, from the main checkout, so it may ask when Claude would not. The trust file is `<config_dir>/.claude.json` when the type has `config_dir`, else `$CLAUDE_CONFIG_DIR/.claude.json` if hive's own environment has `CLAUDE_CONFIG_DIR`, else `~/.claude.json`. Trusting a folder enables its `.claude/settings.json` allow rules, hooks and env.

hive never seeds trust silently. `hive spawn` checks the trust root before editing the config; if it is untrusted it prints `untrusted: <dir> (trust root <root>, trust file <file>); rerun with --trust after the user confirms` to stderr and exits 3 without changes. Rerun with `--trust` once you have confirmed: hive sets only that one key (it refuses a trust file that is not valid JSON), prints `trusted <root> in <file>`, and proceeds. `hive up` and `hive reconcile` (also `--dry-run`) print `untrusted <name> <root>` to stderr for each pane they would start in an untrusted root, and still start it; the pane shows the dialog, which you accept via `hive attach`. `hive list` marks such a pane `waiting=trust`.

For a spawn with a custom launcher command and no known `config_dir`, the queen tries to infer the launcher's config dir by reading the launcher script (it never runs it), and when it finds one tells you the evidence and asks you to confirm before it passes `--config-dir` (with no evidence it assumes the default trust file); its `.claude/settings.json` allows the read-only commands this needs (`command -v`, `which`, `readlink`, `file`, `head`, `cat`, `grep`, `printenv`, `ls`).

## Pane environment

Under the service, the tmux server starts with the bare systemd user environment, and a tmux pane can run a non-login shell (for example when tmux.conf sets `default-command`), which skips your profile (such as `~/.config/zsh/.zprofile`, where `SSH_AUTH_SOCK` for a systemd user ssh-agent is typically exported). That is why panes are login shells by default (`login_shell = true`): they get the same environment as an SSH login, without hive knowing about ssh or anything else. Use `[env]` only for variables your profile does not set. Pairing the unit with `[service] after = ["ssh-agent.service"]` makes sure the agent socket exists before the panes start.

## tmux socket

hive uses its own tmux server (`tmux -L hive`). Plain `tmux ls` and `tmux attach -t myhost-agents` use the default socket and do not see it. Use `hive attach` or `tmux -L hive attach -t myhost-agents`. Your `~/.config/tmux/tmux.conf` still applies.

The reason: `hive down` and `systemctl --user stop hive` must never touch other tmux sessions.

## systemd service

`hive install-service --enable` writes the unit below (the `After=` list comes from `[service] after`) and starts it at login/boot:

```ini
[Unit]
Description=hive: agent sessions in tmux
Wants=network-online.target
After=network-online.target ssh-agent.service

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

`hive up` forks the tmux server, which stays in the unit's cgroup while the unit is active. Once the service is installed, restart with `systemctl --user restart hive` (what `hive restart` does): a plain `hive down && hive up` would start a new tmux server outside the unit's cgroup, so `systemctl --user status hive` would not show it, and the cgroup kill would no longer back up `ExecStop`. `KillMode=control-group` is the default, kept explicit. tmux built with systemd support (Debian's 3.5a is) moves each pane's processes into its own `tmux-spawn-*.scope` unit, so `systemctl --user status hive` lists only the tmux server and the cgroup kill does not reach the agents; they end when `ExecStop` (`hive down`) kills the session and the panes get SIGHUP. `hive down` alone leaves the unit "active" (`RemainAfterExit`).

Logs: `journalctl --user -u hive`, or `journalctl --user-unit hive` where journald keeps no per-user files (`Storage=volatile`).

## Versioning

The version is in `VERSION`, scheme `1.x`. A pre-commit hook bumps it on every commit on `main`; the first commit is `1.0`. Enable the hook once per clone:

```sh
git config core.hooksPath .githooks
```

The hook computes the new version from `HEAD:VERSION`, so re-running after an aborted commit does not bump twice. Feature branches: rebase on `main`, then squash-merge as one commit, so `main` stays linear and gets exactly one increment. `git commit --amend` on `main` bumps again; amend with `--no-verify`.

## License

MIT.
