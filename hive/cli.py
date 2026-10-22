"""hive: run N instances of each agent kind in tmux panes.

An agent kind is a directory plus a command. Each kind gets a tmux window,
each instance a pane. All tmux calls go to a dedicated server (tmux -L <socket>).
"""

import argparse
import fcntl
import json
import os
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from importlib import resources
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path

from hive.configedit import (ENV_KEY_RE, EditError, append_agent, append_launcher, set_count,
                             set_default_launcher, set_search_roots, verify, write_atomic)
from hive.dirs import MAX_DEPTH, PRIVATE_NAMES, list_dirs
from hive.inspect import DEFAULT_ROOTS, LauncherError, inspect_launcher, resolve_launcher
from hive.paths import PathError, expanduser
from hive.reconcile import (RESERVED_TYPES, Existing, New, ResolveError, RunningPane, desired_panes,
                            parse_records, plan, queen_name, resolve_target, sandbox_type)
from hive.trust import (TrustError, is_trusted, looks_like_trust_prompt, seed_trust,
                        trust_file, trust_root)

UNIT_NAME = "hive.service"

TOP_KEYS = {"prefix", "session", "tmux_socket", "login_shell", "shell",
            "defaults", "env", "service", "agents", "search_roots", "queen",
            "inspect_roots", "launchers"}
LAUNCHER_KEYS = {"name", "command", "config_dir", "description"}
LAUNCHER_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
AGENT_KEYS = {"type", "dir", "count", "command", "env", "config_dir"}
QUEEN_KEYS = {"command", "discover", "env", "config_dir"}
# Used when neither [queen] nor [defaults] sets a command.
DEFAULT_QUEEN_COMMAND = "claude -n {name} --remote-control {name} --permission-mode auto"
# Tool allowlist for the queen; the guard hook enforces the same set.
QUEEN_TOOLS = "Bash,Monitor,Read,Grep,Glob,Edit,Write,NotebookEdit,AskUserQuestion"
TYPE_RE = re.compile(r"[A-Za-z0-9._-]+")
# tmux rewrites or mis-targets names with these.
BAD_NAME_RE = re.compile(r"[.:\s]")
DISCOVER_TIMEOUT = 60
QUEEN_HEADER = "<!-- managed by hive; do not edit, put additions in CLAUDE.local.md -->\n"

UNIT_TEMPLATE = """\
[Unit]
Description=hive: agent sessions in tmux
Wants=network-online.target
After={after}

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
{extra_env}ExecStart={exe} up
ExecStop={exe} down
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


def warn(message):
    print(f"hive: warning: {message}", file=sys.stderr)


class ConfigError(Exception):
    """config.toml is unusable; the message names the file and the key."""


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def config_path():
    return config_home() / "hive" / "config.toml"


LOCK_WAIT = 30.0
_lock = None


def hold_lock(required=True):
    """Serialize commands that edit the config or the session, until exit.

    With required=False (`down`), a lock file that cannot be opened only warns:
    systemd's ExecStop must still be able to stop the session."""
    global _lock
    if _lock is not None:
        return
    path = config_home() / "hive" / ".lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a")
    except OSError as e:
        if not required:
            warn(f"cannot open lock {path}: {e.strerror or e}; continuing without it")
            return
        die(f"cannot open lock {path}: {e.strerror or e}", 1)
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() > deadline:
                die(f"another hive command holds {path}; try again", 1)
            time.sleep(0.1)
    _lock = f


# --- config ---------------------------------------------------------------

def check_keys(table, allowed, where, path):
    for key in table:
        if key not in allowed:
            raise ConfigError(f"{path}: unknown key '{key}' in {where} "
                              f"(allowed: {', '.join(sorted(allowed))})")


TOML_TYPES = {list: "an array", dict: "a table", int: "an integer", str: "a string"}


def check_type(value, expected, key, path):
    # bool is an int subclass; never accept it as a count
    if not isinstance(value, expected) or isinstance(value, bool):
        what = "an array of [[agents]] tables" if key == "agents" else TOML_TYPES[expected]
        raise ConfigError(f"{path}: '{key}' must be {what}")


def check_name(value, key, path):
    """prefix and session end up in tmux targets."""
    check_type(value, str, key, path)
    if not value or BAD_NAME_RE.search(value):
        raise ConfigError(f"{path}: '{key}' = {value!r} must be non-empty and contain no '.', "
                          "':' or whitespace (tmux rewrites or mis-targets them)")


def check_socket(value, key, path):
    """tmux -L takes any name without whitespace."""
    check_type(value, str, key, path)
    if not value or re.search(r"\s", value):
        raise ConfigError(f"{path}: '{key}' = {value!r} must be non-empty and contain no whitespace")


def check_text(value, key, path):
    """A non-blank string (command, shell)."""
    check_type(value, str, key, path)
    if not value.strip():
        raise ConfigError(f"{path}: '{key}' must not be empty")


def abs_path(text, label, path):
    """~ expanded, absolute; relative paths would depend on the caller's cwd."""
    if not text.strip():
        raise ConfigError(f"{path}: {label} must not be empty")
    try:
        directory = expanduser(text)
    except PathError as e:
        raise ConfigError(f"{path}: {label}: {e}") from None
    if not directory.is_absolute():
        raise ConfigError(f"{path}: {label} '{text}' must be an absolute path or start with ~")
    return str(directory)


CONFIG_DIR_ENV_MSG = ("hive never sets CLAUDE_CONFIG_DIR (the launched command does); "
                      "use config_dir to name the dir for the trust check")


def check_env(env, where, path):
    check_type(env, dict, where, path)
    for key, value in env.items():
        if not ENV_KEY_RE.fullmatch(key):
            raise ConfigError(f"{path}: {where}.{key}: env names must match {ENV_KEY_RE.pattern}")
        if not isinstance(value, str):
            raise ConfigError(f"{path}: {where}.{key} must be a string")
        if key == "CLAUDE_CONFIG_DIR":
            raise ConfigError(f"{path}: {where}.CLAUDE_CONFIG_DIR: {CONFIG_DIR_ENV_MSG}")


def check_config_dir(table, where, path):
    """Optional config_dir: ~ expanded, absolute. Only locates the trust file."""
    value = table.get("config_dir")
    if value is None:
        return None
    check_type(value, str, f"{where}.config_dir", path)
    return abs_path(value, f"{where}.config_dir", path)


def read_config(path):
    """Parse config.toml; ConfigError on a missing, unreadable or malformed file."""
    if not path.exists():
        raise ConfigError(f"{path} not found; run 'hive init' to create it")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {getattr(e, 'strerror', None) or e}") from None


def load_config():
    """The validated config, or exit 2 with a message."""
    try:
        return parse_config()
    except (ConfigError, PathError) as e:
        die(str(e))


def lenient_config():
    """(config for `down` and `status`, warning or None) that works with a broken config.

    Falls back to the defaults for names the config does not set. A name that is set but
    invalid is an error: guessing the default socket could stop a different hive.
    """
    path = config_path()
    if not path.exists():
        raise ConfigError(f"{path} not found; run 'hive init' to create it")
    try:
        return parse_config(), None
    except (ConfigError, PathError) as e:
        problem = str(e)
    try:
        raw = read_config(path)
    except ConfigError:
        raw = top_level_names(path)

    def pick(key, default, check=check_name):
        if key not in raw:
            return default
        check(raw[key], key, path)
        return raw[key]

    prefix = pick("prefix", socket.gethostname().split(".")[0])
    session = pick("session", f"{prefix}-agents")
    sock = pick("tmux_socket", "hive", check_socket)
    return ({"prefix": prefix, "session": session, "socket": sock,
             "pane_shell": None, "env": {}, "agents": [], "config_path": str(path)},
            f"{problem}\n  using session {session} on socket {sock} (from the parsable lines "
            "of the config, else the defaults)")


def top_level_names(path):
    """prefix, session and tmux_socket from single top-level lines of a config TOML cannot parse.

    A typo elsewhere in the file must not send `down` to the default socket when the
    config names its own: that would stop a different hive."""
    found = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return found
    for line in lines:
        if line.lstrip().startswith("["):
            break  # top-level keys end at the first table
        m = re.match(r"\s*(prefix|session|tmux_socket)\s*=", line)
        if m:
            try:
                found.update(tomllib.loads(line))
            except tomllib.TOMLDecodeError as e:
                # Set but unreadable: guessing the default could hit another hive.
                raise ConfigError(f"{path}: cannot parse the {m.group(1)} line ({e}): {line.strip()}")
    return found


def parse_config():
    path = config_path()
    raw = read_config(path)

    check_keys(raw, TOP_KEYS, "top level", path)

    prefix = raw.get("prefix", socket.gethostname().split(".")[0])
    check_name(prefix, "prefix", path)
    # The machine part of every instance name; keep it to the type characters.
    if not TYPE_RE.fullmatch(prefix):
        raise ConfigError(f"{path}: prefix {prefix!r} may only contain letters, digits, '_' and '-'")
    session = raw.get("session", f"{prefix}-agents")
    check_name(session, "session", path)
    tmux_socket = raw.get("tmux_socket", "hive")
    check_socket(tmux_socket, "tmux_socket", path)

    login_shell = raw.get("login_shell", True)
    if not isinstance(login_shell, bool):
        raise ConfigError(f"{path}: 'login_shell' must be true or false")
    shell = raw.get("shell", default_shell())
    check_text(shell, "shell", path)

    defaults = raw.get("defaults", {})
    check_type(defaults, dict, "defaults", path)
    check_keys(defaults, {"command", "launcher"}, "[defaults]", path)
    default_command = defaults.get("command")
    if default_command is not None:
        check_text(default_command, "defaults.command", path)
    # Not checked against [[launchers]]: it may name a discover record, resolved at use time.
    default_launcher = defaults.get("launcher")
    if default_launcher is not None:
        check_type(default_launcher, str, "defaults.launcher", path)

    raw_launchers = raw.get("launchers", [])
    check_type(raw_launchers, list, "launchers", path)
    launchers = []
    launcher_names = set()
    for i, entry in enumerate(raw_launchers, 1):
        where = f"launchers[{i}]"
        check_type(entry, dict, where, path)
        check_keys(entry, LAUNCHER_KEYS, where, path)
        for key in ("name", "command"):
            if key not in entry:
                raise ConfigError(f"{path}: {where} is missing required key '{key}'")
            check_text(entry[key], f"{where}.{key}", path)
        if not LAUNCHER_NAME_RE.fullmatch(entry["name"]):
            raise ConfigError(f"{path}: {where}.name '{entry['name']}' must match [a-z0-9][a-z0-9-]*")
        if entry["name"] in launcher_names:
            raise ConfigError(f"{path}: duplicate launcher name '{entry['name']}'")
        launcher_names.add(entry["name"])
        check_launcher_command(entry["command"], f"{path}: {where}.command")
        description = entry.get("description")
        if description is not None:
            check_type(description, str, f"{where}.description", path)
        launchers.append({"name": entry["name"], "command": entry["command"],
                          "config_dir": check_config_dir(entry, where, path),
                          "description": description})

    env = raw.get("env", {})
    check_env(env, "env", path)

    service = raw.get("service", {})
    check_type(service, dict, "service", path)
    check_keys(service, {"after"}, "[service]", path)
    after = service.get("after", [])
    if not isinstance(after, list) or not all(isinstance(a, str) for a in after):
        raise ConfigError(f"{path}: service.after must be an array of strings")

    search_roots = raw.get("search_roots", [])
    if not isinstance(search_roots, list) or not all(isinstance(r, str) for r in search_roots):
        raise ConfigError(f"{path}: search_roots must be an array of strings")
    search_roots = [abs_path(r, "search_roots entry", path) for r in search_roots]

    inspect_roots = raw.get("inspect_roots", DEFAULT_ROOTS)
    if not isinstance(inspect_roots, list) or not all(isinstance(r, str) for r in inspect_roots):
        raise ConfigError(f"{path}: inspect_roots must be an array of strings")
    inspect_roots = [abs_path(r, "inspect_roots entry", path) for r in inspect_roots]

    queen = raw.get("queen", {})
    check_type(queen, dict, "queen", path)
    check_keys(queen, QUEEN_KEYS, "[queen]", path)
    queen_command = queen.get("command", default_command or DEFAULT_QUEEN_COMMAND)
    check_text(queen_command, "queen.command", path)
    discover = queen.get("discover")
    if discover is not None:
        check_text(discover, "queen.discover", path)
    queen_env = queen.get("env", {})
    check_env(queen_env, "queen.env", path)
    queen_config_dir = check_config_dir(queen, "queen", path)

    raw_agents = raw.get("agents", [])
    check_type(raw_agents, list, "agents", path)

    agents = []
    seen = set()
    for i, entry in enumerate(raw_agents, 1):
        where = f"agents[{i}]"
        check_type(entry, dict, where, path)
        check_keys(entry, AGENT_KEYS, where, path)
        for key in ("type", "dir"):
            if key not in entry:
                raise ConfigError(f"{path}: {where} is missing required key '{key}'")
        check_type(entry["type"], str, f"{where}.type", path)
        check_type(entry["dir"], str, f"{where}.dir", path)
        kind = entry["type"]
        if not kind:
            raise ConfigError(f"{path}: {where}.type must not be empty")
        if kind in RESERVED_TYPES:
            raise ConfigError(f"{path}: {where}.type '{kind}' is reserved for the hive queen")
        if not TYPE_RE.fullmatch(kind):
            raise ConfigError(f"{path}: {where}.type '{kind}' may only contain letters, digits, "
                              "'.', '_' and '-'")
        if kind in seen:
            raise ConfigError(f"{path}: duplicate agent type '{kind}'")
        seen.add(kind)

        directory = abs_path(entry["dir"], f"{where}.dir", path)

        count = entry.get("count", 1)
        check_type(count, int, f"{where}.count", path)
        if count < 0:
            raise ConfigError(f"{path}: {where}.count must be >= 0")

        command = entry.get("command", default_command)
        if command is None:
            raise ConfigError(f"{path}: {where} has no 'command' and [defaults] has no "
                              "'command'")
        check_text(command, f"{where}.command", path)

        agent_env = entry.get("env", {})
        check_env(agent_env, f"{where}.env", path)

        agents.append({"type": kind, "dir": directory, "count": count, "command": command,
                       "env": agent_env, "config_dir": check_config_dir(entry, where, path)})

    return {
        "prefix": prefix,
        "session": session,
        "socket": tmux_socket,
        # Pane command: a login shell, or None for tmux's default-command.
        "pane_shell": shlex.join([shell, "-l"]) if login_shell else None,
        "env": env,
        "after": after,
        "agents": agents,
        "search_roots": search_roots,
        "inspect_roots": inspect_roots,
        "queen": {"type": "queen", "dir": str(config_home() / "hive" / "queen"),
                  "command": queen_command_line(queen_command), "env": queen_env, "config_dir": queen_config_dir},
        "discover": discover,
        "launchers": launchers,
        "default_launcher": default_launcher,
        "config_path": str(path),
    }


def queen_command_line(command):
    """The queen runs with a tool allowlist and no MCP servers, whatever command it got."""
    if not re.search(r"(^|\s)--tools(\s|=|$)", command):
        command += f" --tools {QUEEN_TOOLS}"
    if not re.search(r"(^|\s)--strict-mcp-config(\s|$)", command):
        command += " --strict-mcp-config"
    return command


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

# Set by Claude Code for its child processes. A tmux server started from inside a
# Claude session would hand them to every pane, and claude in a pane would then
# think it is nested in that session.
CLAUDE_SESSION_ENV = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
                      "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_BRIDGE_SESSION_ID",
                      "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
                      "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_EXECPATH",
                      "CLAUDE_CODE_SSE_PORT", "CLAUDE_PID", "CLAUDE_EFFORT"}


def tmux_env():
    return {k: v for k, v in os.environ.items() if k not in CLAUDE_SESSION_ENV}


def tmux(cfg, *args, check=True):
    cmd = ["tmux", "-L", cfg["socket"], *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=tmux_env())
    except FileNotFoundError:
        die("tmux not found in PATH", 1)
    if check and result.returncode != 0:
        die(f"{' '.join(cmd)} failed: {result.stderr.strip()}", 1)
    return result


def session_running(cfg):
    return tmux(cfg, "has-session", "-t", "=" + cfg["session"], check=False).returncode == 0


def shell_line(cfg, directory, command, env=None):
    """One shell line: [env] exports, then the type's env exports, cd, command."""
    parts = []
    for key, value in [*cfg["env"].items(), *(env or {}).items()]:
        # Escape what is special inside double quotes; leave $ so variables expand.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
        parts.append(f'export {key}="{escaped}"')
    parts.append(f"cd {shlex.quote(directory)}")
    parts.append(command)
    return " && ".join(parts)


def parse_name(prefix, name):
    """Return (type, index) for a hive pane name, or None if it is not one.

    Names are `hive-queen-<prefix>` and `<type>-<prefix>-<N>`.
    """
    if name == queen_name(prefix):
        return "queen", 1
    rest, _, index = name.rpartition("-")
    suffix = "-" + prefix
    if index.isdigit() and rest.endswith(suffix) and len(rest) > len(suffix):
        return rest[:-len(suffix)], int(index)
    return None


def parse_legacy_name(prefix, name):
    """Like parse_name, for the pre-launcher forms `<prefix>-queen`, `<prefix>-<type>-<N>`."""
    if not name.startswith(prefix + "-"):
        return None
    rest = name[len(prefix) + 1:]
    if rest == "queen":
        return "queen", 1
    kind, _, index = rest.rpartition("-")
    if kind and index.isdigit():
        return kind, int(index)
    return None


def running_panes(cfg):
    """Return ({name: RunningPane}, unmanaged) for the session's panes.

    A pane is managed when it has @hive_name, or (adopted) when its title equals
    a desired name exactly; everything else is unmanaged and never touched.
    A pane tagged with a legacy name whose canonical name is desired is adopted
    under the canonical name (reconcile retags and renames it).
    """
    fmt = "#{pane_id}\t#{window_id}\t#{window_name}\t#{@hive_name}\t#{pane_title}"
    out = tmux(cfg, "list-panes", "-s", "-t", "=" + cfg["session"], "-F", fmt)
    desired = {p.name for p in desired_panes(cfg)}
    running, unmanaged = {}, []
    for line in out.stdout.splitlines():
        pane_id, window_id, window_name, hive_name, title = line.split("\t", 4)
        adopted = False
        name = hive_name
        if not name and title in desired:
            name, adopted = title, True
        if name and name not in desired:
            legacy = parse_legacy_name(cfg["prefix"], name)
            if legacy:
                canonical = (queen_name(cfg["prefix"]) if legacy[0] == "queen"
                             else f"{legacy[0]}-{cfg['prefix']}-{legacy[1]}")
                if canonical in desired:
                    name, adopted = canonical, True
        parsed = parse_name(cfg["prefix"], name) if name else None
        if parsed is None:
            unmanaged.append({"pane": pane_id, "window": window_name, "title": title})
            continue
        running[name] = RunningPane(pane_id, window_id, window_name, name,
                                    parsed[0], parsed[1], adopted)
    return running, unmanaged


def window_ids(cfg):
    out = tmux(cfg, "list-windows", "-t", "=" + cfg["session"], "-F", "#{window_id}\t#{window_name}")
    return {name: wid for wid, name in (line.split("\t", 1) for line in out.stdout.splitlines())}


def expand_pane(cfg, pane):
    return expand(pane.command, name=pane.name, type=pane.type, index=pane.index,
                  dir=pane.dir, prefix=cfg["prefix"])


def setup_pane(cfg, pane, pane_id):
    """Title, tag, and type the command into a freshly created pane."""
    tmux(cfg, "select-pane", "-t", pane_id, "-T", pane.name)
    # Keep apps from overwriting the title; ignore tmux versions without the option.
    tmux(cfg, "set-option", "-p", "-t", pane_id, "allow-set-title", "off", check=False)
    tmux(cfg, "set-option", "-p", "-t", pane_id, "@hive_name", pane.name)
    tmux(cfg, "send-keys", "-t", pane_id, "-l",
         shell_line(cfg, pane.dir, expand_pane(cfg, pane), pane.env))
    tmux(cfg, "send-keys", "-t", pane_id, "Enter")


def start_pane(cfg, pane, window_id):
    """Start a pane in a new window (window_id None) or beside the window's last pane."""
    fmt = ["-P", "-F", "#{pane_id}"]
    shell = [cfg["pane_shell"]] if cfg["pane_shell"] else []
    if window_id is None:
        out = tmux(cfg, "new-window", "-d", *fmt, "-t", f"={cfg['session']}:", "-n", pane.window,
                   "-c", pane.dir, *shell)
        pane_id = out.stdout.strip()
    else:
        last = tmux(cfg, "list-panes", "-t", window_id, "-F", "#{pane_id}").stdout.split()[-1]
        out = tmux(cfg, "split-window", "-d", *fmt, "-t", last, "-c", pane.dir, *shell)
        pane_id = out.stdout.strip()
        # Re-tile after every split so the next split has room.
        tmux(cfg, "select-layout", "-t", pane_id, "tiled")
    setup_pane(cfg, pane, pane_id)
    return pane_id


def trust_state(directory, config_dir):
    """(trust root, trust file, trusted?) for a pane in `directory`."""
    root = trust_root(directory)
    return root, trust_file(config_dir), is_trusted(root, config_dir)


def warn_untrusted(cfg, pane):
    """Warn only: hive never records trust without an explicit --trust."""
    root, _, trusted = trust_state(pane.dir, pane.config_dir)
    if not trusted:
        warn(f"untrusted {pane.name} {root}; attach and accept Claude's trust prompt, "
             "or run 'hive spawn --trust' after the user confirms")


_warned = set()


def preflight(cfg, pane):
    """Warn (never fail) about a command that probably will not work as a pane."""
    command = expand_pane(cfg, pane)
    if "--remote-control" not in command and ("remote", command) not in _warned:
        _warned.add(("remote", command))
        warn(f"{pane.name}: command has no --remote-control, so the pane will not be "
             f"reachable from Claude's remote sessions: {command}")
    try:
        tokens = shlex.split(command)
    except ValueError:
        return
    # Skip leading VAR=value assignments.
    program = next((t for t in tokens if not re.fullmatch(r"[A-Za-z_]\w*=.*", t)), None)
    if program and ("which", program) not in _warned and not shutil.which(program):
        _warned.add(("which", program))
        warn(f"{pane.name}: '{program}' not found in PATH; the pane will show 'command not "
             "found' (the pane's login shell may have a different PATH)")


def materialize_queen(cfg):
    """Write the packaged queen instructions into the queen dir when missing or different."""
    qdir = Path(cfg["queen"]["dir"])
    try:
        (qdir / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
        pkg = resources.files("hive").joinpath("queen")
        files = {
            qdir / "CLAUDE.md": QUEEN_HEADER.encode() + pkg.joinpath("CLAUDE.md").read_bytes(),
            qdir / ".claude" / "settings.json": pkg.joinpath("settings.json").read_bytes(),
            qdir / ".claude" / "hooks" / "guard.py":
                pkg.joinpath("hooks", "guard.py").read_bytes(),
        }
        for path, data in files.items():
            if not path.exists() or path.read_bytes() != data:
                path.write_bytes(data)
    except OSError as e:
        die(f"cannot write the queen dir {qdir}: {e.strerror or e}", 1)


def reconcile(cfg, dry_run=False, announce_idle=True):
    """Make the running session match the config: adopt, start, then stop."""
    session = cfg["session"]
    if not session_running(cfg):
        die(f"session {session} not running; run 'hive up'", 1)
    queen = queen_name(cfg["prefix"])
    running, _ = running_panes(cfg)
    wanted = desired_panes(cfg)
    to_start, to_stop = plan(wanted, running, queen)
    windows_of = {p.name: p.window for p in wanted}
    adopted = [r for r in running.values() if r.adopted]
    acted = False

    if not dry_run:
        # Cheap; panes opened later inherit it.
        for key, value in cfg["env"].items():
            tmux(cfg, "set-environment", "-g", key, os.path.expandvars(value))
    # Adoption only retags a running pane, so --dry-run does not report it.
    for r in adopted if not dry_run else []:
        tmux(cfg, "set-option", "-p", "-t", r.pane_id, "@hive_name", r.name)
        tmux(cfg, "select-pane", "-t", r.pane_id, "-T", r.name)
        # A legacy window (<prefix>-<type>, <prefix>-queen) is renamed so new panes split into it.
        legacy_window = f"{cfg['prefix']}-{r.type}"
        if r.window_name == legacy_window != windows_of[r.name]:
            tmux(cfg, "rename-window", "-t", r.window_id, windows_of[r.name])
        print(f"adopted {r.name}")
        acted = True

    windows = None
    skipped = set()
    for pane in to_start:
        acted = True
        if pane.name != queen and not Path(pane.dir).is_dir():
            # One stale entry must not keep the rest of the hive down.
            print(f"{'would skip' if dry_run else 'skipped'} {pane.name}: "
                  f"dir {pane.dir} does not exist", file=sys.stderr)
            skipped.add(pane.type)
            continue
        warn_untrusted(cfg, pane)
        if dry_run:
            print(f"would start {pane.name}")
            continue
        preflight(cfg, pane)
        if pane.name == queen:
            materialize_queen(cfg)
        if windows is None:
            windows = window_ids(cfg)
        window_id = windows.get(pane.window)
        start_pane(cfg, pane, window_id)
        if window_id is None:
            windows = window_ids(cfg)
        print(f"started {pane.name}  [{pane.dir}]  {expand_pane(cfg, pane)}")

    for r in to_stop:
        acted = True
        if not dry_run:
            tmux(cfg, "kill-pane", "-t", r.pane_id)
        print(f"{'would stop' if dry_run else 'stopped'} {r.name}")

    if skipped:
        warn(f"{len(skipped)} agent(s) {'would be ' if dry_run else ''}skipped: "
             "dir missing; see 'hive list'")
    if not acted and announce_idle:
        print("no changes")
    return 0


def cmd_up(cfg, args):
    if session_running(cfg):
        return reconcile(cfg)

    session = cfg["session"]
    first = desired_panes(cfg)[0]  # the queen
    warn_untrusted(cfg, first)
    preflight(cfg, first)
    try:
        materialize_queen(cfg)
        shell = [cfg["pane_shell"]] if cfg["pane_shell"] else []
        # Detached size is fixed so tiled panes are not tiny; attach resizes.
        out = tmux(cfg, "new-session", "-d", "-P", "-F", "#{pane_id}", "-s", session,
                   "-n", first.window, "-x", "200", "-y", "50", "-c", first.dir, *shell)
        # Also put [env] on the server so panes opened later inherit it.
        for key, value in cfg["env"].items():
            tmux(cfg, "set-environment", "-g", key, os.path.expandvars(value))
        setup_pane(cfg, first, out.stdout.strip())
        print(f"started session {session} (tmux -L {cfg['socket']})")
        print(f"started {first.name}  [{first.dir}]  {expand_pane(cfg, first)}")
        reconcile(cfg, announce_idle=False)
    except BaseException:
        # Do not leave a half-built session behind (the queen's tmux call may be what failed).
        tmux(cfg, "kill-session", "-t", "=" + session, check=False)
        print(f"hive: startup failed; removed session {session}", file=sys.stderr)
        raise
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
    hold_lock()
    cmd_down(cfg, args)
    return cmd_up(cfg, args)


def cmd_status(cfg, args):
    if not session_running(cfg):
        print("not running")
        return 1
    # pane_title last: it is the only field a program can put a tab in.
    fmt = "#{window_index}\t#{window_name}\t#{pane_index}\t#{pane_current_command}\t#{pane_title}"
    out = tmux(cfg, "list-panes", "-s", "-t", "=" + cfg["session"], "-F", fmt)
    print(f"session {cfg['session']}")
    current_window = None
    for line in out.stdout.splitlines():
        _, window, pane_index, running, title = line.split("\t", 4)
        if window != current_window:
            print(f"  {window}")
            current_window = window
        print(f"    pane {pane_index}: {title}  ({running})")
    print(f"attach: tmux -L {cfg['socket']} attach -t {cfg['session']}")
    return 0


def cmd_attach(cfg, args):
    if not session_running(cfg):
        die(f"session {cfg['session']} not running; run 'hive up'", 1)
    os.execvp("tmux", ["tmux", "-L", cfg["socket"], "attach", "-t", cfg["session"]])


def quote(text):
    return json.dumps(text, ensure_ascii=False)


def trust_fields(cfg, entry):
    _, path, trusted = trust_state(entry["dir"], entry["config_dir"])
    return f"trusted={'yes' if trusted else 'no'} trust_file={path}"


def cmd_list(cfg, args):
    queen = queen_name(cfg["prefix"])
    up = session_running(cfg)
    running, unmanaged = running_panes(cfg) if up else ({}, [])
    print(f"session={cfg['session']} socket={cfg['socket']} running={'yes' if up else 'no'} "
          f"config={cfg['config_path']}")
    print(f"queen name={queen} running={'yes' if queen in running else 'no'} "
          f"dir={cfg['queen']['dir']} {trust_fields(cfg, cfg['queen'])}")
    for agent in cfg["agents"]:
        n = sum(1 for r in running.values() if r.type == agent["type"])
        print(f"agent type={agent['type']} dir={agent['dir']} count={agent['count']} "
              f"running={n} command={quote(agent['command'])} {trust_fields(cfg, agent)}"
              + ("" if Path(agent["dir"]).is_dir() else " dir_missing=yes"))
    for r in running.values():
        screen = tmux(cfg, "capture-pane", "-p", "-t", r.pane_id, check=False).stdout
        waiting = " waiting=trust" if looks_like_trust_prompt(screen) else ""
        print(f"pane name={r.name} type={r.type} id={r.pane_id}{waiting}")
    for u in unmanaged:
        print(f"unmanaged pane={u['pane']} window={u['window']} title={quote(u['title'])}")
    if up:
        to_start, to_stop = plan(desired_panes(cfg), running, queen)
        if to_start or to_stop:
            print(f"drift start={','.join(p.name for p in to_start) or 'none'} "
                  f"stop={','.join(r.name for r in to_stop) or 'none'}")
    print(f"search_roots={','.join(cfg['search_roots'])}")
    print(f"discover={cfg['discover'] or ''}")
    return 0


def edit_config(cfg, edit, mutate):
    """Apply a text edit to config.toml: verified against `mutate`, then written atomically."""
    path = cfg["config_path"]
    try:
        with open(path, encoding="utf-8", newline="") as f:
            old = f.read()
    except (OSError, UnicodeDecodeError) as e:
        die(f"cannot read {path}: {getattr(e, 'strerror', None) or e}", 1)
    try:
        new = edit(old)
        verify(old, new, mutate, path)
    except EditError as e:
        die(str(e), 2)
    try:
        write_atomic(path, new)
    except OSError as e:
        die(f"cannot write {path}: {e.strerror or e}", 1)


def check_config_writable(cfg):
    """Fail before side effects (seeding trust) when the edit cannot be written."""
    path = Path(cfg["config_path"])
    if not (os.access(path, os.W_OK) and os.access(path.parent, os.W_OK)):
        die(f"cannot write {path}: not writable (the file and its directory must be)", 1)


def set_count_edit(cfg, kind, new_count):
    def mutate(doc):
        next(a for a in doc["agents"] if a["type"] == kind)["count"] = new_count
    edit_config(cfg, lambda text: set_count(text, kind, new_count), mutate)


def after_edit(cfg, kind, old_count, new_count):
    print(f"config agent={kind} count={old_count}->{new_count} file={cfg['config_path']}")
    if not session_running(cfg):
        print("session not running; config updated, run 'hive up'")
        return 0
    return reconcile(load_config())


def resolve_or_die(cfg, target, type_override=None):
    try:
        return resolve_target(cfg, target, type_override)
    except ResolveError as e:
        die(str(e), 2)


def parse_env_args(items):
    env = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not ENV_KEY_RE.fullmatch(key):
            die(f"--env '{item}' must be KEY=VALUE with KEY matching {ENV_KEY_RE.pattern}")
        if key == "CLAUDE_CONFIG_DIR":
            die(f"--env CLAUDE_CONFIG_DIR: {CONFIG_DIR_ENV_MSG} (--config-dir or --launcher)")
        env[key] = value
    return env


def expand_config_dir(text, where):
    directory = expanduser(text)
    if not directory.is_absolute():
        die(f"{where} '{text}' must be absolute or start with ~")
    return str(directory)


def check_launcher_command(command, where):
    if not ("{name}" in command and "--remote-control" in command):
        raise ConfigError(f"{where} must contain {{name}} and --remote-control")


def run_discover(cfg):
    """Run `[queen] discover`: (records, error message, exit code); records is None on error."""
    try:
        out = subprocess.run(cfg["discover"], shell=True, capture_output=True, text=True,
                             timeout=DISCOVER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f"discover command timed out after {DISCOVER_TIMEOUT}s: {cfg['discover']}", 1
    if out.returncode != 0:
        return None, f"discover command failed (exit {out.returncode}): {out.stderr.strip()}", 1
    try:
        return parse_records(out.stdout), None, 0
    except ResolveError as e:
        return None, str(e), 2


def all_launchers(cfg):
    """(registry records, then discover records; discover error or None). Never raises on discover."""
    records = [{"name": l["name"], "source": "config", "config_dir": l["config_dir"],
                "command": l["command"], "description": l["description"], "shadowed": False}
               for l in cfg["launchers"]]
    if not cfg["discover"]:
        return records, None
    found, error, _ = run_discover(cfg)
    if error is not None:
        return records, error
    registry = {r["name"] for r in records}
    for r in found:
        records.append({"name": r["name"], "source": "discover", "config_dir": r["config_dir"] or None,
                        "command": r["command"], "description": r.get("description"),
                        "shadowed": r["name"] in registry})
    return records, None


def launcher_record(cfg, name):
    """The launcher called `name`: the registry first, then the `[queen] discover` records."""
    for l in cfg["launchers"]:
        if l["name"] == name:
            return {"name": name, "command": l["command"], "config_dir": l["config_dir"]}
    known = [l["name"] for l in cfg["launchers"]]
    if cfg["discover"]:
        records, error, code = run_discover(cfg)
        if error is not None:
            die(error, code)
        for record in records:
            if record["name"] == name:
                return {**record, "config_dir": record["config_dir"] or None}
        known += [r["name"] for r in records if r["name"] not in known]
    die(f"no launcher '{name}'; available: {', '.join(known) or 'none'}", 2)


def check_launcher_type(cfg, type_name, command, config_dir):
    """Refuse a launcher whose suffixed type is already taken by a different command or config dir."""
    for entry in cfg["agents"]:
        if entry["type"] == type_name and (entry["command"] != command
                                           or entry["config_dir"] != config_dir):
            die(f"type '{type_name}' already exists for '{entry['dir']}' and runs "
                f"'{entry['command']}'; the launcher's command differs. Give --type <name> "
                "to start it under another type")


def ensure_trust(directory, config_dir, seed):
    """Exit 3 unless the pane's trust root is trusted, or --trust records it now."""
    root, path, trusted = trust_state(directory, config_dir)
    if trusted:
        return
    if not seed:
        print(f"untrusted: {directory} (trust root {root}, trust file {path}); "
              "rerun with --trust after the user confirms", file=sys.stderr)
        sys.exit(3)
    try:
        seed_trust(root, config_dir)
    except (TrustError, OSError) as e:
        die(f"cannot record trust for {root}: {e}", 1)
    print(f"trusted {root} in {path}")


def expand_dir_or_none(config_dir):
    return expand_config_dir(config_dir, "config_dir") if config_dir is not None else None


def cmd_spawn(cfg, args):
    command, config_dir, type_name = args.agent_command, args.config_dir, args.type
    record = None
    if args.launcher is not None:
        if command is not None or config_dir is not None:
            die("--launcher takes command and config_dir from the launcher; "
                "do not combine it with --command or --config-dir")
        record = getattr(args, "launcher_record", None) or launcher_record(cfg, args.launcher)
        command, config_dir = record["command"], record["config_dir"]
        if type_name is None:
            try:
                type_name = sandbox_type(cfg, args.target, args.launcher)
            except ResolveError as e:
                die(str(e))
        check_launcher_type(cfg, type_name, command, expand_dir_or_none(config_dir))
    if command is not None:
        check_launcher_command(command, "--command")
    expanded_dir = expand_dir_or_none(config_dir)
    env = parse_env_args(args.env)
    target = resolve_or_die(cfg, args.target, type_name)
    if (isinstance(target, New) and record is None and command is None and config_dir is None
            and cfg["default_launcher"]):
        # A new type with no command given runs the default launcher, named after it.
        record = launcher_record(cfg, cfg["default_launcher"])
        command, config_dir = record["command"], record["config_dir"]
        check_launcher_command(command, f"launcher '{record['name']}' command")
        expanded_dir = expand_dir_or_none(config_dir)
        if type_name is None:
            try:
                type_name = sandbox_type(cfg, args.target, record["name"])
            except ResolveError as e:
                die(str(e))
        check_launcher_type(cfg, type_name, command, expanded_dir)
        target = resolve_or_die(cfg, args.target, type_name)
    if isinstance(target, Existing):
        entry = target.entry
        if command is not None and command != entry["command"]:
            die(f"type '{entry['type']}' runs '{entry['command']}'; to run a different command "
                f"in the same directory give a new type: hive spawn {entry['dir']} "
                "--type <name> --command '...'")
        if config_dir is not None and expanded_dir != entry["config_dir"]:
            die(f"type '{entry['type']}' has config_dir '{entry['config_dir'] or ''}'; to use "
                f"another one in the same directory give a new type: hive spawn {entry['dir']} "
                "--type <name> ...")
        if args.env is not None and env != entry["env"]:
            die(f"type '{entry['type']}' has a different env; to use another env in the same "
                f"directory give a new type: hive spawn {entry['dir']} --type <name> --env ...")
        if not Path(entry["dir"]).is_dir():
            die(f"type '{entry['type']}' has dir '{entry['dir']}', which does not exist; "
                "fix or remove the entry in config.toml")
        check_config_writable(cfg)
        ensure_trust(entry["dir"], entry["config_dir"], args.trust)
        old, new = entry["count"], entry["count"] + args.n
        set_count_edit(cfg, entry["type"], new)
        return after_edit(cfg, entry["type"], old, new)

    # The type ends up unquoted in names and commands; keep it to safe characters.
    if target.type in RESERVED_TYPES or not TYPE_RE.fullmatch(target.type):
        suggestion = re.sub(r"[^A-Za-z0-9._-]+", "-", target.type).strip("-") or "agent"
        if suggestion in RESERVED_TYPES:
            suggestion = f"{suggestion}-agent"
        die(f"'{target.type}' is not a usable agent type for '{target.dir}'; "
            "give --type with letters, digits, '.', '_' or '-', e.g. "
            f"--type {suggestion}")
    new_agent = {"type": target.type, "dir": target.stored_dir, "count": args.n}
    if command is not None:
        new_agent["command"] = command
    if config_dir is not None:
        new_agent["config_dir"] = config_dir
    if env:
        new_agent["env"] = env
    check_config_writable(cfg)
    ensure_trust(target.dir, expanded_dir, args.trust)
    edit_config(cfg,
                lambda text: append_agent(text, target.type, target.stored_dir, args.n,
                                          command, env, config_dir),
                lambda doc: doc.setdefault("agents", []).append(new_agent))
    return after_edit(cfg, target.type, 0, args.n)


def cmd_stop(cfg, args):
    if (args.target != "queen" and not any(a["type"] == args.target for a in cfg["agents"])
            and "/" not in args.target and not args.target.startswith(("~", "."))):
        die(f"no agent type '{args.target}'; 'hive list' shows the types")
    target = resolve_or_die(cfg, args.target)
    if not isinstance(target, Existing):
        die(f"no agent type '{target.type}'; 'hive list' shows the types")
    entry = target.entry
    if args.n > entry["count"]:
        die(f"type '{entry['type']}' has count {entry['count']}; cannot stop {args.n}")
    old, new = entry["count"], entry["count"] - args.n
    set_count_edit(cfg, entry["type"], new)
    return after_edit(cfg, entry["type"], old, new)


def cmd_reconcile(cfg, args):
    return reconcile(cfg, dry_run=args.dry_run)


def cmd_inspect_launcher(cfg, args):
    return inspect_launcher(args.launch_command, cfg["inspect_roots"], sys.stdout, sys.stderr)


def cmd_dirs(cfg, args):
    return list_dirs(cfg, args.root, args.depth, sys.stdout, sys.stderr)


def same_dir(text, resolved):
    return expanduser(text).resolve() == resolved


def cmd_roots(cfg, args):
    if args.action is None:
        if args.path is not None:
            die("hive roots takes 'add PATH' or 'remove PATH'")
        for r in cfg["search_roots"]:
            print(f"root={Path(r).absolute()}")
        if not cfg["search_roots"]:
            print("roots=none")
        return 0
    if args.path is None:
        die(f"hive roots {args.action} needs a PATH")
    raw = read_config(Path(cfg["config_path"])).get("search_roots", [])
    resolved = expanduser(args.path).resolve()
    if args.action == "remove":
        i = next((i for i, r in enumerate(raw) if same_dir(r, resolved)), None)
        if i is None:
            print(f"not found: {resolved} is not in search_roots", file=sys.stderr)
            return 2
        edit_config(cfg, lambda text: set_search_roots(text, raw[:i] + raw[i + 1:]),
                    lambda doc: doc["search_roots"].pop(i))
        print(f"removed {raw[i]}")
        return 0
    given = expanduser(args.path)
    if any(p.lower() in PRIVATE_NAMES for p in (*given.parts, *resolved.parts)):
        print(f"refused: {resolved} is under a secrets/private path", file=sys.stderr)
        return 3
    if not resolved.is_dir():
        die(f"'{args.path}' is not an existing directory")
    if any(same_dir(r, resolved) for r in raw):
        print("unchanged")
        return 0
    spelling = os.path.normpath(args.path) if args.path.startswith("~") else str(resolved)
    edit_config(cfg, lambda text: set_search_roots(text, [*raw, spelling]),
                lambda doc: doc.setdefault("search_roots", []).append(spelling))
    print(f"added {spelling}")
    return 0


def cmd_launchers(cfg, args):
    action = getattr(args, "launchers_action", None) or "list"
    return {"list": launchers_list, "add": launchers_add, "default": launchers_default}[action](cfg, args)


def launchers_add(cfg, args):
    name = args.name
    if not LAUNCHER_NAME_RE.fullmatch(name):
        die(f"launcher name '{name}' must match {LAUNCHER_NAME_RE.pattern}", 2)
    records, error = all_launchers(cfg)
    if error is not None:
        print(f"warning: discover failed, collisions with its records are not checked: {error}",
              file=sys.stderr)
    if any(r["name"] == name for r in records):
        die(f"launcher '{name}' already exists; names must be unique across the registry and discover", 2)
    check_launcher_command(args.launch_command, "COMMAND")
    try:
        resolve_launcher(args.launch_command)
    except LauncherError as e:
        label = "refused" if e.code == 3 else "not found"
        print(f"{label}: {e}", file=sys.stderr)
        return e.code
    config_dir = (expand_config_dir(args.config_dir, "--config-dir")
                  if args.config_dir is not None else None)
    first = cfg["default_launcher"] is None

    def edit(text):
        text = append_launcher(text, name, args.launch_command, config_dir, args.description)
        return set_default_launcher(text, name) if first else text

    def mutate(doc):
        launcher = {"name": name, "command": args.launch_command}
        if config_dir is not None:
            launcher["config_dir"] = config_dir
        if args.description is not None:
            launcher["description"] = args.description
        doc.setdefault("launchers", []).append(launcher)
        if first:
            doc.setdefault("defaults", {})["launcher"] = name

    edit_config(cfg, edit, mutate)
    print(f"launcher added name={quote(name)} default={'yes' if first else 'no'} file={cfg['config_path']}")
    return 0


def launchers_default(cfg, args):
    records, error = all_launchers(cfg)
    names = list(dict.fromkeys(r["name"] for r in records))
    if args.name not in names:
        if error is not None:
            print(f"warning: discover failed: {error}", file=sys.stderr)
        die(f"no launcher '{args.name}'; available: {', '.join(names) or 'none'}", 2)
    edit_config(cfg, lambda text: set_default_launcher(text, args.name),
                lambda doc: doc.setdefault("defaults", {}).__setitem__("launcher", args.name))
    print(f"default launcher={quote(args.name)} file={cfg['config_path']}")
    return 0


def launchers_list(cfg, args):
    records, error = all_launchers(cfg)
    default = cfg["default_launcher"]

    def is_default(r):
        return r["name"] == default and not r["shadowed"]

    # Stable sort: the default first, then registry, then discover, each in source order.
    for r in sorted(records, key=lambda r: not is_default(r)):
        line = (f"launcher name={quote(r['name'])} source={r['source']} "
                f"default={'yes' if is_default(r) else 'no'} "
                f"config_dir={quote(r['config_dir'] or 'default')} command={quote(r['command'])}")
        if r["description"]:
            line += f" description={quote(r['description'])}"
        if r["shadowed"]:
            line += " shadowed=yes"
        print(line)
    if error is not None:
        print(f"discover error={quote(error)}")
    return 0


def cmd_discover(cfg, args):
    if not cfg["discover"]:
        die("no discover command configured; set [queen] discover", 2)
    return subprocess.run(cfg["discover"], shell=True).returncode


# --- init and systemd -----------------------------------------------------

def cmd_init(args):
    path = config_path()
    if path.exists():
        print(f"{path} already exists")
    else:
        example = resources.files("hive").joinpath("config.example.toml").read_text()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(example)
        except OSError as e:
            die(f"cannot write {path}: {e.strerror or e}", 1)
        print(f"wrote {path}; it has no agents yet, so 'hive up' starts only the queen; "
              "add agents with 'hive spawn DIR' or by editing it")
    cfg, _ = lenient_config()
    print(f"config={path} session={cfg['session']} socket={cfg['socket']}; "
          "set tmux_socket in the config to keep a test or isolated config off your "
          "real hive's tmux server")
    return 0


def unit_path():
    # Where the systemd user manager looks, whatever XDG_CONFIG_HOME says here.
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def systemctl(*args, check=True):
    if not shutil.which("systemctl"):
        die("systemctl not found", 1)
    result = subprocess.run(["systemctl", "--user", *args])
    if check and result.returncode != 0:
        die(f"systemctl --user {' '.join(args)} failed", 1)
    return result


def unit_quote(text, command=True):
    """Escape for an ExecStart (command) or Environment value: %, $, and quote when needed."""
    text = text.replace("%", "%%")
    if command:
        text = text.replace("$", "$$")
    if re.search(r'[\s"\'\\]', text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def service_exe():
    """The hive executable for the unit: %h/.local/bin/hive unless installed elsewhere."""
    found = shutil.which("hive")
    default = Path.home() / ".local" / "bin" / "hive"
    if found is None or Path(found).absolute() == default:
        return "%h/.local/bin/hive"
    return unit_quote(str(Path(found).absolute()))


def service_extra_env():
    """Pass a non-default XDG_CONFIG_HOME through, so the service reads the same config."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if not xdg or Path(xdg) == Path.home() / ".config":
        return ""
    return "Environment=" + unit_quote(f"XDG_CONFIG_HOME={xdg}", command=False) + "\n"


def cmd_install_service(args):
    if not shutil.which("systemctl"):
        die("systemctl not found", 1)
    cfg = load_config()
    after = " ".join(["network-online.target", *cfg["after"]])
    path = unit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(UNIT_TEMPLATE.format(after=after, exe=service_exe(),
                                             extra_env=service_extra_env()))
    except OSError as e:
        die(f"cannot write {path}: {e.strerror or e}", 1)
    print(f"wrote {path}")
    systemctl("daemon-reload")
    if args.enable:
        systemctl("enable", "--now", UNIT_NAME)
    else:
        print(f"next: systemctl --user enable --now {UNIT_NAME}\n"
              "to start at boot without logging in: loginctl enable-linger $USER")
    return 0


def cmd_uninstall_service(args):
    path = unit_path()
    if not path.exists():
        print(f"not installed: {path} does not exist")
        return 0
    systemctl("disable", "--now", UNIT_NAME, check=False)
    try:
        path.unlink()
    except OSError as e:
        die(f"cannot remove {path}: {e.strerror or e}", 1)
    print(f"removed {path}")
    systemctl("daemon-reload")
    return 0


# --- entry point ----------------------------------------------------------

def get_version():
    try:
        return pkg_version("hive")
    except PackageNotFoundError:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()


def positive_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: '{text}'")
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def depth_int(text):
    value = positive_int(text)
    if value > MAX_DEPTH:
        raise argparse.ArgumentTypeError(f"must be <= {MAX_DEPTH}")
    return value


EPILOG = """\
exit codes:
  0  ok
  1  runtime error, or the session is not running
  2  usage or config error
  3  untrusted or refused (spawn without --trust, dirs/roots refusals)
"""


BROKEN_CONFIG_NOTE = (
    "Works with a broken config.toml: prefix, session and tmux_socket are read from its "
    "parsable top-level lines, and only names the config does not set fall back to the "
    "defaults. A name that is set but invalid or unparsable exits 2 instead of guessing.")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hive", description="Run N instances of each agent kind in tmux panes.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"hive {get_version()}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("up", help="start the session, or reconcile it with the config if running",
                   description="Start the session with the queen and every configured agent, "
                   "or reconcile it with the config if it is already running. Agents whose "
                   "dir is missing are skipped with a warning.")
    sub.add_parser("down", help="stop the session (and the hive tmux server if empty)",
                   description="Stop the session, and the hive tmux server once it is empty. "
                   + BROKEN_CONFIG_NOTE)
    sub.add_parser("restart", help="restart via systemd if the unit is active, else down + up",
                   description="Restart via systemd if the hive.service unit is active, "
                   "else run down and then up.")
    sub.add_parser("status", help="show windows and panes (exit 1 if not running)",
                   description="Show windows and panes; exit 1 if the session is not running. "
                   + BROKEN_CONFIG_NOTE)
    sub.add_parser("attach", help="attach to the session")
    sub.add_parser("list", help="show config and running state, unmanaged panes, drift")
    p = sub.add_parser("spawn", help="add workers (edits config, then reconciles)",
                       description="Add workers: raise the count of an existing agent type, or "
                       "add a new type for a directory. Exit 3 if the directory is untrusted "
                       "(rerun with --trust after the user confirms).")
    p.add_argument("target", metavar="TARGET",
                   help="agent type, or a directory (absolute, ~/..., or a name under search_roots)")
    p.add_argument("-n", type=positive_int, default=1, help="number of workers to add (default 1)")
    p.add_argument("--type", help="agent type name for a new entry (default: directory name, plus "
                   "-<launcher> when a launcher is chosen; letters, digits, '.', '_' and '-' only)")
    p.add_argument("--command", dest="agent_command", metavar="CMD",
                   help="command template; must contain {name} and --remote-control")
    p.add_argument("--config-dir", dest="config_dir", metavar="PATH",
                   help="Claude config dir the command uses; only locates the trust file")
    p.add_argument("--launcher", "--sandbox", dest="launcher", metavar="NAME",
                   help="take command and config_dir from launcher NAME (registry, then "
                        "[queen] discover); --sandbox is an alias")
    p.add_argument("--env", action="append", metavar="KEY=VALUE",
                   help="env for a new type, exported after [env] (repeatable)")
    p.add_argument("--trust", action="store_true",
                   help="record Claude Code workspace trust for the directory (only after the user agreed)")
    p = sub.add_parser("stop", help="remove workers (lowers count, then reconciles)")
    p.add_argument("target", metavar="TARGET", help="agent type")
    p.add_argument("-n", type=positive_int, default=1, help="number of workers to remove (default 1)")
    p = sub.add_parser("reconcile", help="make the running session match the config",
                       description="Start missing panes, stop surplus ones and adopt panes "
                       "titled with a desired name, so the running session matches the config.")
    p.add_argument("--dry-run", action="store_true", help="only print what would change")
    p = sub.add_parser("launchers", help="list the launcher registry and discover records")
    launchers_sub = p.add_subparsers(dest="launchers_action")
    launchers_sub.add_parser("list", help="list launchers (default action)")
    lp = launchers_sub.add_parser("add", help="register a launcher in config.toml (never runs it)")
    lp.add_argument("name", metavar="NAME")
    lp.add_argument("launch_command", metavar="COMMAND", help="the full launch command, as one argument")
    lp.add_argument("--config-dir", metavar="PATH", help="CLAUDE_CONFIG_DIR the launcher uses")
    lp.add_argument("--description", metavar="TEXT")
    lp = launchers_sub.add_parser("default", help="set the default launcher (edits config)")
    lp.add_argument("name", metavar="NAME")
    sub.add_parser("discover", help="run [queen] discover and print its output")
    p = sub.add_parser("dirs", help="read-only: list directories under search_roots")
    p.add_argument("root", metavar="ROOT", nargs="?",
                   help="a search root, a directory inside one, or a name as for spawn")
    p.add_argument("--depth", type=depth_int, default=1, metavar="N",
                   help=f"levels to list (default 1, max {MAX_DEPTH})")
    p = sub.add_parser("roots", help="show search_roots, or add/remove one (edits config)")
    p.add_argument("action", nargs="?", choices=["add", "remove"],
                   help="add or remove a search root; omit to list them")
    p.add_argument("path", nargs="?", metavar="PATH",
                   help="the directory to add or remove (required with an action)")
    p = sub.add_parser("inspect-launcher",
                       help="read-only: show a launcher's CLAUDE_CONFIG_DIR evidence (files under inspect_roots only)")
    p.add_argument("launch_command", metavar="COMMAND", help="the full launch command, as one argument")
    sub.add_parser("init", help="write an example config if none exists")
    p = sub.add_parser("install-service", help="write the systemd user unit")
    p.add_argument("--enable", action="store_true", help="also enable and start it now")
    sub.add_parser("uninstall-service", help="disable and remove the systemd user unit")
    return parser


LOCKED = ("up", "down", "spawn", "stop", "reconcile", "roots")


def run():
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
    if args.command in ("down", "status"):
        # These must work with a broken config.toml: systemd's ExecStop runs `down`.
        cfg, problem = lenient_config()
        if problem:
            warn(f"config problem: {problem}")
    else:
        cfg = load_config()
    if args.command in LOCKED or (args.command == "launchers"
                                  and getattr(args, "launchers_action", None) in ("add", "default")):
        if (args.command == "spawn" and args.launcher is not None
                and args.agent_command is None and args.config_dir is None):
            # discover may be slow: run it before taking the lock.
            args.launcher_record = launcher_record(cfg, args.launcher)
        hold_lock(required=args.command != "down")
        if args.command != "down":
            cfg = load_config()  # fresh copy: another command may have edited it meanwhile
    handlers = {"up": cmd_up, "down": cmd_down, "restart": cmd_restart,
                "status": cmd_status, "attach": cmd_attach, "list": cmd_list,
                "spawn": cmd_spawn, "stop": cmd_stop, "reconcile": cmd_reconcile,
                "discover": cmd_discover, "inspect-launcher": cmd_inspect_launcher,
                "dirs": cmd_dirs, "roots": cmd_roots, "launchers": cmd_launchers}
    return handlers[args.command](cfg, args)


def main():
    try:
        return run()
    except KeyboardInterrupt:
        print("hive: interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # The reader went away (| head); keep the interpreter's exit flush quiet.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 1
    except (ConfigError, PathError) as e:
        die(str(e))


if __name__ == "__main__":
    sys.exit(main())
