# hive

Run N instances of each agent kind in tmux. A window is an agent kind, each pane is one instance. An agent kind is just a directory plus a command; hive knows nothing about what the command does.

## Install

```sh
uv tool install .            # from a checkout of this repo
# or: pipx install .
```

Debian 13 follows PEP 668, so `pip install --user` is blocked; use uv or pipx.

Update after pulling or changing the repo (all three were verified to pick up a new `VERSION` and changed sources):

```sh
uv tool upgrade hive         # or: uv tool install --reinstall .
```

Then `hive init` writes `${XDG_CONFIG_HOME:-~/.config}/hive/config.toml` from the example; edit it and run `hive up`.

## Config

`${XDG_CONFIG_HOME:-~/.config}/hive/config.toml`. Unknown keys are errors. See `config.example.toml`.

| Key | Meaning |
| --- | --- |
| `prefix` | Name prefix. Default: short hostname. |
| `session` | tmux session name. Default: `<prefix>-agents`. |
| `tmux_socket` | `tmux -L` server name. Default: `hive`. |
| `login_shell` | `true` (default): each pane runs `<shell> -l`, so your login profile applies. `false`: panes run tmux's `default-command`. |
| `shell` | Shell for `login_shell`. Default: your login shell from passwd, else `$SHELL`, else `/bin/sh`. |
| `[defaults] command` | Command for every agent type without its own `command`. |
| `[env]` | Optional `KEY = "value"` strings, exported in every pane before the command. Values go inside double quotes in the pane shell: `$VAR` expands there, `\`, `"` and backticks are escaped. They are also set on the hive tmux server (`set-environment -g`, `$VAR` expanded by hive), so panes opened later inherit them. For anything your login profile does not set. |
| `[service] after` | Optional list of units added to `After=` in `hive.service`. |
| `[[agents]] type` | Required, unique. Window name is `<prefix>-<type>`. |
| `[[agents]] dir` | Required, `~` expanded, must exist (checked by `up`). Each pane starts with `cd <dir>`. |
| `[[agents]] count` | Number of panes, integer >= 1. Default: 1. |
| `[[agents]] command` | Optional override of `defaults.command`. One of the two is required. |

Placeholders in commands, replaced by plain text substitution (other braces such as `${VAR}` are untouched): `{name}` = `<prefix>-<type>-<index>` (index starts at 1), `{type}`, `{index}`, `{dir}`, `{prefix}`.

## Commands

| Command | Does |
| --- | --- |
| `hive up` | Start the session if it is not running (otherwise says so, exit 0). |
| `hive down` | Kill the session, and the hive tmux server once it is empty. |
| `hive restart` | `systemctl --user restart hive.service` if that unit is active, else `down` then `up`. |
| `hive status` | Windows, panes, pane titles and running commands; exit 1 if not running. |
| `hive attach` | Attach to the session. |
| `hive init` | Write the example config if none exists. |
| `hive install-service [--enable]` | Write `~/.config/systemd/user/hive.service` and reload; `--enable` also runs `enable --now`. |
| `hive uninstall-service` | Disable, remove the unit, reload. |
| `hive --version` | Print the version. |

Panes are titled with the agent name and the app may not override it (`allow-set-title off`). Each pane runs a login shell and the command is typed into it, so the shell stays after the command exits (the pane closes only when you `exit` that shell).

Run `hive up` from a plain shell, not from inside a Claude Code session: the tmux server inherits the caller's environment (for example the `CLAUDECODE` variables).

## Pane environment

Under the service, the tmux server starts with the bare systemd user environment, and a plain tmux pane runs a non-login shell, which skips your profile (for example `~/.config/zsh/.zprofile`, where `SSH_AUTH_SOCK` for a systemd user ssh-agent is typically exported). That is why panes are login shells by default (`login_shell = true`): they get the same environment as an SSH login, without hive knowing about ssh or anything else. Use `[env]` only for variables your profile does not set. Pairing the unit with `[service] after = ["ssh-agent.service"]` makes sure the agent socket exists before the panes start.

## tmux socket

hive uses its own tmux server (`tmux -L hive`). Plain `tmux ls` and `tmux attach -t swarm-agents` use the default socket and do not see it. Use `hive attach` or `tmux -L hive attach -t swarm-agents`. Your `~/.config/tmux/tmux.conf` still applies.

The reason: `hive down` and `systemctl --user stop hive` must never touch other tmux sessions, and the whole server lives in the service cgroup.

## systemd service

`hive install-service --enable` writes the unit below (the `After=` list comes from `[service] after`) and starts it at login/boot:

```ini
[Unit]
Description=hive: agent sessions in tmux
Wants=network-online.target
After=network-online.target ssh-agent.service obsidian-sync.service

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

`hive up` forks the tmux server, which stays in the unit's cgroup while the unit is active. Once the service is installed, restart with `systemctl --user restart hive` (what `hive restart` does): a plain `hive down && hive up` would start a new tmux server outside the cgroup, which `systemctl stop` then no longer cleans up. `KillMode=control-group` is the default, kept explicit. tmux built with systemd support (Debian's 3.5a is) moves each pane's processes into its own `tmux-spawn-*.scope` unit, so `systemctl --user status hive` lists only the tmux server and the cgroup kill does not reach the agents; they end when `ExecStop` (`hive down`) kills the session and the panes get SIGHUP. `hive down` alone leaves the unit "active" (`RemainAfterExit`).

Logs: `journalctl --user -u hive`. Where the journal keeps no per-user files (journald `Storage=volatile`, as set on Raspberry Pi OS), that prints "No journal files were found"; use `journalctl --user-unit hive` instead (needs the `adm` group or root).

## Versioning

The version is in `VERSION`, scheme `1.x`. A pre-commit hook bumps it on every commit on `main`; the first commit is `1.0`. Enable the hook once per clone:

```sh
git config core.hooksPath .githooks
```

The hook computes the new version from `HEAD:VERSION`, so re-running after an aborted commit does not bump twice. Feature branches: rebase on `main`, then squash-merge as one commit, so `main` stays linear and gets exactly one increment.

## License

MIT.
