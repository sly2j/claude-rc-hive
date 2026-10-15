"""hive: run N instances of each agent kind in tmux panes.

An agent kind is a directory plus a command. Each kind gets a tmux window,
each instance a pane. All tmux calls go to a dedicated server (tmux -L <socket>).
"""

import argparse
import os
import pwd
import shlex
import shutil
import socket
import subprocess
import sys
import tomllib
from importlib import resources
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path

UNIT_NAME = "hive.service"

TOP_KEYS = {"prefix", "session", "tmux_socket", "login_shell", "shell",
            "defaults", "env", "service", "agents"}
AGENT_KEYS = {"type", "dir", "count", "command"}

UNIT_TEMPLATE = """\
[Unit]
Description=hive: agent sessions in tmux
Wants=network-online.target
After={after}

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
"""


def die(message, code=2):
    print(f"hive: error: {message}", file=sys.stderr)
    sys.exit(code)


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def config_path():
    return config_home() / "hive" / "config.toml"


# --- config ---------------------------------------------------------------

def check_keys(table, allowed, where, path):
    for key in table:
        if key not in allowed:
            die(f"{path}: unknown key '{key}' in {where} (allowed: {', '.join(sorted(allowed))})")


def check_type(value, expected, key, path):
    # bool is an int subclass; never accept it as a count
    if not isinstance(value, expected) or isinstance(value, bool):
        die(f"{path}: '{key}' must be {expected.__name__}")


def load_config(check_dirs=False):
    path = config_path()
    if not path.exists():
        die(f"{path} not found; run 'hive init' to create it")
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        die(f"{path}: {e}")

    check_keys(raw, TOP_KEYS, "top level", path)

    prefix = raw.get("prefix", socket.gethostname().split(".")[0])
    check_type(prefix, str, "prefix", path)
    session = raw.get("session", f"{prefix}-agents")
    check_type(session, str, "session", path)
    tmux_socket = raw.get("tmux_socket", "hive")
    check_type(tmux_socket, str, "tmux_socket", path)

    login_shell = raw.get("login_shell", True)
    if not isinstance(login_shell, bool):
        die(f"{path}: 'login_shell' must be true or false")
    shell = raw.get("shell", default_shell())
    check_type(shell, str, "shell", path)

    defaults = raw.get("defaults", {})
    check_type(defaults, dict, "defaults", path)
    check_keys(defaults, {"command"}, "[defaults]", path)
    default_command = defaults.get("command")
    if default_command is not None:
        check_type(default_command, str, "defaults.command", path)

    env = raw.get("env", {})
    check_type(env, dict, "env", path)
    for key, value in env.items():
        if not isinstance(value, str):
            die(f"{path}: env.{key} must be a string")

    service = raw.get("service", {})
    check_type(service, dict, "service", path)
    check_keys(service, {"after"}, "[service]", path)
    after = service.get("after", [])
    if not isinstance(after, list) or not all(isinstance(a, str) for a in after):
        die(f"{path}: service.after must be a list of strings")

    raw_agents = raw.get("agents", [])
    check_type(raw_agents, list, "agents", path)
    if not raw_agents:
        die(f"{path}: define at least one [[agents]] entry")

    agents = []
    seen = set()
    for i, entry in enumerate(raw_agents, 1):
        where = f"agents[{i}]"
        check_type(entry, dict, where, path)
        check_keys(entry, AGENT_KEYS, where, path)
        for key in ("type", "dir"):
            if key not in entry:
                die(f"{path}: {where} is missing required key '{key}'")
        check_type(entry["type"], str, f"{where}.type", path)
        check_type(entry["dir"], str, f"{where}.dir", path)
        kind = entry["type"]
        if not kind:
            die(f"{path}: {where}.type must not be empty")
        if kind in seen:
            die(f"{path}: duplicate agent type '{kind}'")
        seen.add(kind)

        directory = Path(entry["dir"]).expanduser()
        if check_dirs and not directory.is_dir():
            die(f"{path}: {where}.dir '{directory}' is not an existing directory")

        count = entry.get("count", 1)
        check_type(count, int, f"{where}.count", path)
        if count < 1:
            die(f"{path}: {where}.count must be >= 1")

        command = entry.get("command", default_command)
        if command is None:
            die(f"{path}: {where} has no 'command' and [defaults] has no 'command'")
        check_type(command, str, f"{where}.command", path)

        agents.append({"type": kind, "dir": str(directory), "count": count, "command": command})

    return {
        "prefix": prefix,
        "session": session,
        "socket": tmux_socket,
        # Pane command: a login shell, or None for tmux's default-command.
        "pane_shell": shlex.join([shell, "-l"]) if login_shell else None,
        "env": env,
        "after": after,
        "agents": agents,
    }


def default_shell():
    # passwd first: the systemd user manager's $SHELL can be stale.
    try:
        return pwd.getpwuid(os.getuid()).pw_shell or os.environ.get("SHELL") or "/bin/sh"
    except KeyError:
        return os.environ.get("SHELL") or "/bin/sh"


def expand(command, **values):
    # Plain replace, not str.format, so ${VAR} and other braces survive.
    for key, value in values.items():
        command = command.replace("{" + key + "}", str(value))
    return command


# --- tmux -----------------------------------------------------------------

def tmux(cfg, *args, check=True):
    cmd = ["tmux", "-L", cfg["socket"], *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        die("tmux not found in PATH", 1)
    if check and result.returncode != 0:
        die(f"{' '.join(cmd)} failed: {result.stderr.strip()}", 1)
    return result


def session_running(cfg):
    return tmux(cfg, "has-session", "-t", "=" + cfg["session"], check=False).returncode == 0


def shell_line(cfg, directory, command):
    """One shell line: env exports, cd, command."""
    parts = []
    for key, value in cfg["env"].items():
        # Escape what is special inside double quotes; leave $ so variables expand.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
        parts.append(f'export {key}="{escaped}"')
    parts.append(f"cd {shlex.quote(directory)}")
    parts.append(command)
    return " && ".join(parts)


def cmd_up(cfg, args):
    if session_running(cfg):
        print(f"session {cfg['session']} already running")
        return 0

    session = cfg["session"]
    started = []  # (window, pane_id, name, command)
    try:
        last_pane = None
        for agent in cfg["agents"]:
            window = f"{cfg['prefix']}-{agent['type']}"
            directory = agent["dir"]
            for index in range(1, agent["count"] + 1):
                name = f"{window}-{index}"
                fmt = ["-P", "-F", "#{pane_id}"]
                shell = [cfg["pane_shell"]] if cfg["pane_shell"] else []
                if last_pane is None:
                    # Detached size is fixed so tiled panes are not tiny; attach resizes.
                    out = tmux(cfg, "new-session", "-d", *fmt, "-s", session, "-n", window,
                               "-x", "200", "-y", "50", "-c", directory, *shell)
                    # Also put [env] on the server so panes opened later inherit it.
                    for key, value in cfg["env"].items():
                        tmux(cfg, "set-environment", "-g", key, os.path.expandvars(value))
                elif index == 1:
                    out = tmux(cfg, "new-window", "-d", *fmt, "-t", f"={session}:", "-n", window,
                               "-c", directory, *shell)
                else:
                    out = tmux(cfg, "split-window", "-d", *fmt, "-t", last_pane, "-c", directory,
                               *shell)
                pane = out.stdout.strip()
                if index > 1:
                    # Re-tile after every split so the next split has room.
                    tmux(cfg, "select-layout", "-t", pane, "tiled")
                last_pane = pane
                command = expand(agent["command"], name=name, type=agent["type"],
                                 index=index, dir=directory, prefix=cfg["prefix"])
                started.append((window, pane, name, command, directory))

        for window, pane, name, command, directory in started:
            tmux(cfg, "select-pane", "-t", pane, "-T", name)
            # Keep apps from overwriting the title; ignore tmux versions without the option.
            tmux(cfg, "set-option", "-p", "-t", pane, "allow-set-title", "off", check=False)
            tmux(cfg, "send-keys", "-t", pane, "-l", shell_line(cfg, directory, command))
            tmux(cfg, "send-keys", "-t", pane, "Enter")
    except SystemExit:
        tmux(cfg, "kill-session", "-t", "=" + session, check=False)
        raise

    print(f"started session {session} (tmux -L {cfg['socket']})")
    for window, _, name, command, directory in started:
        print(f"  {window}: {name}  [{directory}]  {command}")
    return 0


def cmd_down(cfg, args):
    if not session_running(cfg):
        print(f"session {cfg['session']} not running")
        return 0
    tmux(cfg, "kill-session", "-t", "=" + cfg["session"])
    print(f"stopped session {cfg['session']}")
    # Stop the dedicated server too once nothing is left on it.
    if not tmux(cfg, "list-sessions", check=False).stdout.strip():
        tmux(cfg, "kill-server", check=False)
    return 0


def systemd_unit_active():
    if not shutil.which("systemctl"):
        return False
    result = subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT_NAME])
    return result.returncode == 0


def cmd_restart(cfg, args):
    if systemd_unit_active():
        # Restart through systemd so the tmux server stays in the service cgroup.
        return subprocess.run(["systemctl", "--user", "restart", UNIT_NAME]).returncode
    cmd_down(cfg, args)
    return cmd_up(cfg, args)


def cmd_status(cfg, args):
    if not session_running(cfg):
        print("not running")
        return 1
    fmt = "#{window_index}\t#{window_name}\t#{pane_index}\t#{pane_title}\t#{pane_current_command}"
    out = tmux(cfg, "list-panes", "-s", "-t", "=" + cfg["session"], "-F", fmt)
    print(f"session {cfg['session']}")
    current_window = None
    for line in out.stdout.splitlines():
        _, window, pane_index, title, running = line.split("\t")
        if window != current_window:
            print(f"  {window}")
            current_window = window
        print(f"    pane {pane_index}: {title}  ({running})")
    print(f"attach: tmux -L {cfg['socket']} attach -t {cfg['session']}")
    return 0


def cmd_attach(cfg, args):
    os.execvp("tmux", ["tmux", "-L", cfg["socket"], "attach", "-t", cfg["session"]])


# --- init and systemd -----------------------------------------------------

def cmd_init(args):
    path = config_path()
    if path.exists():
        print(f"{path} already exists")
        return 0
    example = resources.files("hive").joinpath("config.example.toml").read_text()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(example)
    print(f"wrote {path}; edit it, then run 'hive up'")
    return 0


def unit_path():
    return config_home() / "systemd" / "user" / UNIT_NAME


def systemctl(*args, check=True):
    if not shutil.which("systemctl"):
        die("systemctl not found", 1)
    result = subprocess.run(["systemctl", "--user", *args])
    if check and result.returncode != 0:
        die(f"systemctl --user {' '.join(args)} failed", 1)
    return result


def cmd_install_service(args):
    cfg = load_config()
    after = " ".join(["network-online.target", *cfg["after"]])
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(UNIT_TEMPLATE.format(after=after))
    print(f"wrote {path}")
    systemctl("daemon-reload")
    if args.enable:
        systemctl("enable", "--now", UNIT_NAME)
    return 0


def cmd_uninstall_service(args):
    systemctl("disable", "--now", UNIT_NAME, check=False)
    path = unit_path()
    if path.exists():
        path.unlink()
        print(f"removed {path}")
    systemctl("daemon-reload")
    return 0


# --- entry point ----------------------------------------------------------

def get_version():
    try:
        return pkg_version("hive")
    except PackageNotFoundError:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hive", description="Run N instances of each agent kind in tmux panes.")
    parser.add_argument("--version", action="version", version=f"hive {get_version()}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("up", help="start the session if it is not running")
    sub.add_parser("down", help="stop the session (and the hive tmux server if empty)")
    sub.add_parser("restart", help="restart via systemd if installed, else down + up")
    sub.add_parser("status", help="show windows and panes (exit 1 if not running)")
    sub.add_parser("attach", help="attach to the session")
    sub.add_parser("init", help="write an example config if none exists")
    p = sub.add_parser("install-service", help="write the systemd user unit")
    p.add_argument("--enable", action="store_true", help="also enable and start it now")
    sub.add_parser("uninstall-service", help="disable and remove the systemd user unit")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 2
    if args.command == "init":
        return cmd_init(args)
    if args.command == "install-service":
        return cmd_install_service(args)
    if args.command == "uninstall-service":
        return cmd_uninstall_service(args)
    cfg = load_config(check_dirs=args.command in ("up", "restart"))
    handlers = {"up": cmd_up, "down": cmd_down, "restart": cmd_restart,
                "status": cmd_status, "attach": cmd_attach}
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
