"""hive: run N instances of each agent kind in tmux panes.

An agent kind is a directory plus a command. Each kind gets a tmux window,
each instance a pane. All tmux calls go to a dedicated server (tmux -L <socket>).
"""

import argparse
import json
import os
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tomllib
from importlib import resources
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path

from hive.configedit import (ENV_KEY_RE, EditError, append_agent, set_count,
                             set_search_roots, verify, write_atomic)
from hive.dirs import MAX_DEPTH, PRIVATE_NAMES, list_dirs
from hive.inspect import DEFAULT_ROOTS, inspect_launcher
from hive.reconcile import (Existing, ResolveError, RunningPane, desired_panes,
                            parse_records, plan, resolve_target, sandbox_type)
from hive.trust import (TrustError, is_trusted, looks_like_trust_prompt, seed_trust,
                        trust_file, trust_root)

UNIT_NAME = "hive.service"

TOP_KEYS = {"prefix", "session", "tmux_socket", "login_shell", "shell",
            "defaults", "env", "service", "agents", "search_roots", "queen",
            "inspect_roots"}
AGENT_KEYS = {"type", "dir", "count", "command", "env", "config_dir"}
QUEEN_KEYS = {"command", "discover", "env", "config_dir"}
# Used when neither [queen] nor [defaults] sets a command, so 1.5 configs keep loading.
DEFAULT_QUEEN_COMMAND = "claude -n {name} --remote-control {name} --permission-mode auto"
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


CONFIG_DIR_ENV_MSG = ("hive never sets CLAUDE_CONFIG_DIR (the launched command does); "
                      "use config_dir to name the dir for the trust check")


def check_env(env, where, path):
    check_type(env, dict, where, path)
    for key, value in env.items():
        if not isinstance(value, str):
            die(f"{path}: {where}.{key} must be a string")
        if key == "CLAUDE_CONFIG_DIR":
            die(f"{path}: {where}.CLAUDE_CONFIG_DIR: {CONFIG_DIR_ENV_MSG}")


def check_config_dir(table, where, path):
    """Optional config_dir: ~ expanded, absolute. Only locates the trust file."""
    value = table.get("config_dir")
    if value is None:
        return None
    check_type(value, str, f"{where}.config_dir", path)
    return expand_config_dir(value, f"{path}: {where}.config_dir")


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
    check_env(env, "env", path)

    service = raw.get("service", {})
    check_type(service, dict, "service", path)
    check_keys(service, {"after"}, "[service]", path)
    after = service.get("after", [])
    if not isinstance(after, list) or not all(isinstance(a, str) for a in after):
        die(f"{path}: service.after must be a list of strings")

    search_roots = raw.get("search_roots", [])
    if not isinstance(search_roots, list) or not all(isinstance(r, str) for r in search_roots):
        die(f"{path}: search_roots must be a list of strings")
    search_roots = [str(Path(r).expanduser()) for r in search_roots]

    inspect_roots = raw.get("inspect_roots", DEFAULT_ROOTS)
    if not isinstance(inspect_roots, list) or not all(isinstance(r, str) for r in inspect_roots):
        die(f"{path}: inspect_roots must be a list of strings")
    inspect_roots = [str(Path(r).expanduser()) for r in inspect_roots]
    for r in inspect_roots:
        if not Path(r).is_absolute():
            die(f"{path}: inspect_roots entry '{r}' must be an absolute path or start with ~")

    queen = raw.get("queen", {})
    check_type(queen, dict, "queen", path)
    check_keys(queen, QUEEN_KEYS, "[queen]", path)
    queen_command = queen.get("command", default_command or DEFAULT_QUEEN_COMMAND)
    check_type(queen_command, str, "queen.command", path)
    discover = queen.get("discover")
    if discover is not None:
        check_type(discover, str, "queen.discover", path)
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
                die(f"{path}: {where} is missing required key '{key}'")
        check_type(entry["type"], str, f"{where}.type", path)
        check_type(entry["dir"], str, f"{where}.dir", path)
        kind = entry["type"]
        if not kind:
            die(f"{path}: {where}.type must not be empty")
        if kind == "queen":
            die(f"{path}: {where}.type 'queen' is reserved for the hive queen")
        if kind in seen:
            die(f"{path}: duplicate agent type '{kind}'")
        seen.add(kind)

        directory = Path(entry["dir"]).expanduser()
        if check_dirs and not directory.is_dir():
            die(f"{path}: {where}.dir '{directory}' is not an existing directory")

        count = entry.get("count", 1)
        check_type(count, int, f"{where}.count", path)
        if count < 0:
            die(f"{path}: {where}.count must be >= 0")

        command = entry.get("command", default_command)
        if command is None:
            die(f"{path}: {where} has no 'command' and [defaults] has no 'command'")
        check_type(command, str, f"{where}.command", path)

        agent_env = entry.get("env", {})
        check_env(agent_env, f"{where}.env", path)

        agents.append({"type": kind, "dir": str(directory), "count": count, "command": command,
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
                  "command": queen_command, "env": queen_env, "config_dir": queen_config_dir},
        "discover": discover,
        "config_path": str(path),
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
    """Return (type, index) for a hive pane name, or None if it is not one."""
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
        print(f"untrusted {pane.name} {root}", file=sys.stderr)


def materialize_queen(cfg):
    """Write the packaged queen instructions into the queen dir when missing or different."""
    qdir = Path(cfg["queen"]["dir"])
    (qdir / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
    pkg = resources.files("hive").joinpath("queen")
    files = {
        qdir / "CLAUDE.md": QUEEN_HEADER.encode() + pkg.joinpath("CLAUDE.md").read_bytes(),
        qdir / ".claude" / "settings.json": pkg.joinpath("settings.json").read_bytes(),
        qdir / ".claude" / "hooks" / "guard.py": pkg.joinpath("hooks", "guard.py").read_bytes(),
    }
    for path, data in files.items():
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)


def reconcile(cfg, dry_run=False, announce_idle=True):
    """Make the running session match the config: adopt, start, then stop."""
    session = cfg["session"]
    if not session_running(cfg):
        die(f"session {session} not running; run 'hive up'", 1)
    queen_name = f"{cfg['prefix']}-queen"
    running, _ = running_panes(cfg)
    to_start, to_stop = plan(desired_panes(cfg), running, queen_name)
    adopted = [r for r in running.values() if r.adopted]
    acted = False

    if not dry_run:
        # Cheap; panes opened later inherit it.
        for key, value in cfg["env"].items():
            tmux(cfg, "set-environment", "-g", key, os.path.expandvars(value))
    # Adoption only tags a running pane, so --dry-run does not report it.
    for r in adopted if not dry_run else []:
        tmux(cfg, "set-option", "-p", "-t", r.pane_id, "@hive_name", r.name)
        print(f"adopted {r.name}")
        acted = True

    windows = None
    for pane in to_start:
        acted = True
        warn_untrusted(cfg, pane)
        if dry_run:
            print(f"would start {pane.name}")
            continue
        if pane.name == queen_name:
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

    if not acted and announce_idle:
        print("no changes")
    return 0


def cmd_up(cfg, args):
    if session_running(cfg):
        return reconcile(cfg)

    session = cfg["session"]
    first = desired_panes(cfg)[0]  # the queen
    warn_untrusted(cfg, first)
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
    except SystemExit:
        tmux(cfg, "kill-session", "-t", "=" + session, check=False)
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


def quote(text):
    return json.dumps(text, ensure_ascii=False)


def trust_fields(cfg, entry):
    _, path, trusted = trust_state(entry["dir"], entry["config_dir"])
    return f"trusted={'yes' if trusted else 'no'} trust_file={path}"


def cmd_list(cfg, args):
    queen_name = f"{cfg['prefix']}-queen"
    up = session_running(cfg)
    running, unmanaged = running_panes(cfg) if up else ({}, [])
    print(f"session={cfg['session']} socket={cfg['socket']} running={'yes' if up else 'no'} "
          f"config={cfg['config_path']}")
    print(f"queen name={queen_name} running={'yes' if queen_name in running else 'no'} "
          f"dir={cfg['queen']['dir']} {trust_fields(cfg, cfg['queen'])}")
    for agent in cfg["agents"]:
        n = sum(1 for r in running.values() if r.type == agent["type"])
        print(f"agent type={agent['type']} dir={agent['dir']} count={agent['count']} "
              f"running={n} command={quote(agent['command'])} {trust_fields(cfg, agent)}")
    for r in running.values():
        screen = tmux(cfg, "capture-pane", "-p", "-t", r.pane_id, check=False).stdout
        waiting = " waiting=trust" if looks_like_trust_prompt(screen) else ""
        print(f"pane name={r.name} type={r.type} id={r.pane_id}{waiting}")
    for u in unmanaged:
        print(f"unmanaged pane={u['pane']} window={u['window']} title={quote(u['title'])}")
    if up:
        to_start, to_stop = plan(desired_panes(cfg), running, queen_name)
        if to_start or to_stop:
            print(f"drift start={','.join(p.name for p in to_start) or 'none'} "
                  f"stop={','.join(r.name for r in to_stop) or 'none'}")
    print(f"search_roots={','.join(cfg['search_roots'])}")
    print(f"discover={cfg['discover'] or ''}")
    return 0


def edit_config(cfg, edit, mutate):
    """Apply a text edit to config.toml: verified against `mutate`, then written atomically."""
    path = cfg["config_path"]
    with open(path, encoding="utf-8", newline="") as f:
        old = f.read()
    try:
        new = edit(old)
        verify(old, new, mutate, path)
    except EditError as e:
        die(str(e), 2)
    write_atomic(path, new)


def set_count_edit(cfg, kind, new_count):
    def mutate(doc):
        next(a for a in doc["agents"] if a["type"] == kind)["count"] = new_count
    edit_config(cfg, lambda text: set_count(text, kind, new_count), mutate)


def after_edit(cfg, kind, old_count, new_count):
    print(f"config agent={kind} count={old_count}->{new_count} file={cfg['config_path']}")
    if not session_running(cfg):
        print("session not running; config updated, run 'hive up'")
        return 0
    return reconcile(load_config(check_dirs=True))


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
            die(f"--env CLAUDE_CONFIG_DIR: {CONFIG_DIR_ENV_MSG} (--config-dir or --sandbox)")
        env[key] = value
    return env


def expand_config_dir(text, where):
    directory = Path(text).expanduser()
    if not directory.is_absolute():
        die(f"{where} '{text}' must be absolute or start with ~")
    return str(directory)


def sandbox_record(cfg, name):
    """The `[queen] discover` record with name == `name`."""
    if not cfg["discover"]:
        die("--sandbox needs [queen] discover, which is not configured")
    out = subprocess.run(cfg["discover"], shell=True, capture_output=True, text=True)
    if out.returncode != 0:
        die(f"discover command failed (exit {out.returncode}): {out.stderr.strip()}", 1)
    try:
        records = parse_records(out.stdout)
    except ResolveError as e:
        die(str(e))
    for record in records:
        if record["name"] == name:
            return record
    die(f"no sandbox '{name}' in discover output; available: "
        f"{', '.join(r['name'] for r in records) or 'none'}")


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


def cmd_spawn(cfg, args):
    command, config_dir, type_name = args.agent_command, args.config_dir, args.type
    if args.sandbox is not None:
        if command is not None or config_dir is not None:
            die("--sandbox takes command and config_dir from discover; "
                "do not combine it with --command or --config-dir")
        record = sandbox_record(cfg, args.sandbox)
        command, config_dir = record["command"], record["config_dir"]
        if type_name is None:
            try:
                type_name = sandbox_type(cfg, args.target, args.sandbox)
            except ResolveError as e:
                die(str(e))
    if command is not None and not ("{name}" in command and "--remote-control" in command):
        die("--command must contain {name} and --remote-control")
    expanded_dir = (expand_config_dir(config_dir, "config_dir") if config_dir is not None
                    else None)
    env = parse_env_args(args.env)
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
        ensure_trust(entry["dir"], entry["config_dir"], args.trust)
        old, new = entry["count"], entry["count"] + args.n
        set_count_edit(cfg, entry["type"], new)
        return after_edit(cfg, entry["type"], old, new)

    # The type ends up unquoted in names and commands; keep it to safe characters.
    if target.type == "queen" or not re.fullmatch(r"[A-Za-z0-9._-]+", target.type):
        die(f"'{target.type}' is not a usable agent type for '{target.dir}'; "
            "give --type with letters, digits, '.', '_' or '-'")
    new_agent = {"type": target.type, "dir": target.stored_dir, "count": args.n}
    if command is not None:
        new_agent["command"] = command
    if config_dir is not None:
        new_agent["config_dir"] = config_dir
    if env:
        new_agent["env"] = env
    ensure_trust(target.dir, expanded_dir, args.trust)
    edit_config(cfg,
                lambda text: append_agent(text, target.type, target.stored_dir, args.n,
                                          command, env, config_dir),
                lambda doc: doc.setdefault("agents", []).append(new_agent))
    return after_edit(cfg, target.type, 0, args.n)


def cmd_stop(cfg, args):
    target = resolve_or_die(cfg, args.target)
    if not isinstance(target, Existing):
        die(f"no agent type '{target.type}'; hive list shows the types")
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
    return Path(text).expanduser().resolve() == resolved


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
    with open(cfg["config_path"], "rb") as f:
        raw = tomllib.load(f).get("search_roots", [])
    resolved = Path(args.path).expanduser().resolve()
    if args.action == "remove":
        i = next((i for i, r in enumerate(raw) if same_dir(r, resolved)), None)
        if i is None:
            print(f"not found: {resolved} is not in search_roots", file=sys.stderr)
            return 2
        edit_config(cfg, lambda text: set_search_roots(text, raw[:i] + raw[i + 1:]),
                    lambda doc: doc["search_roots"].pop(i))
        print(f"removed {raw[i]}")
        return 0
    given = Path(args.path).expanduser()
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


def cmd_discover(cfg, args):
    if not cfg["discover"]:
        die("no discover command configured; set [queen] discover", 2)
    return subprocess.run(cfg["discover"], shell=True).returncode


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


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hive", description="Run N instances of each agent kind in tmux panes.")
    parser.add_argument("--version", action="version", version=f"hive {get_version()}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("up", help="start the session, or reconcile it with the config if running")
    sub.add_parser("down", help="stop the session (and the hive tmux server if empty)")
    sub.add_parser("restart", help="restart via systemd if installed, else down + up")
    sub.add_parser("status", help="show windows and panes (exit 1 if not running)")
    sub.add_parser("attach", help="attach to the session")
    sub.add_parser("list", help="show config and running state, unmanaged panes, drift")
    p = sub.add_parser("spawn", help="add workers (edits config, then reconciles)")
    p.add_argument("target", metavar="TARGET",
                   help="agent type, or a directory (absolute, ~/..., or a name under search_roots)")
    p.add_argument("-n", type=positive_int, default=1, help="number of workers to add (default 1)")
    p.add_argument("--type", help="agent type name for a new entry (default: directory name)")
    p.add_argument("--command", dest="agent_command", help="command template; must contain {name} and --remote-control")
    p.add_argument("--config-dir", dest="config_dir", metavar="PATH",
                   help="Claude config dir the command uses; only locates the trust file")
    p.add_argument("--sandbox", metavar="NAME",
                   help="take command and config_dir from the [queen] discover record NAME")
    p.add_argument("--env", action="append", metavar="KEY=VALUE",
                   help="env for a new type, exported after [env] (repeatable)")
    p.add_argument("--trust", action="store_true",
                   help="record Claude Code workspace trust for the directory (only after the user agreed)")
    p = sub.add_parser("stop", help="remove workers (lowers count, then reconciles)")
    p.add_argument("target", metavar="TARGET", help="agent type")
    p.add_argument("-n", type=positive_int, default=1, help="number of workers to remove (default 1)")
    p = sub.add_parser("reconcile", help="make the running session match the config")
    p.add_argument("--dry-run", action="store_true", help="only print what would change")
    sub.add_parser("discover", help="run [queen] discover and print its output")
    p = sub.add_parser("dirs", help="read-only: list directories under search_roots")
    p.add_argument("root", metavar="ROOT", nargs="?",
                   help="a search root, a directory inside one, or a name as for spawn")
    p.add_argument("--depth", type=depth_int, default=1, metavar="N",
                   help=f"levels to list (default 1, max {MAX_DEPTH})")
    p = sub.add_parser("roots", help="show search_roots, or add/remove one (edits config)")
    p.add_argument("action", nargs="?", choices=["add", "remove"])
    p.add_argument("path", nargs="?", metavar="PATH")
    p = sub.add_parser("inspect-launcher",
                       help="read-only: show a launcher's CLAUDE_CONFIG_DIR evidence (files under inspect_roots only)")
    p.add_argument("launch_command", metavar="COMMAND", help="the full launch command, as one argument")
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
    cfg = load_config(check_dirs=args.command in ("up", "restart", "spawn", "stop", "reconcile"))
    handlers = {"up": cmd_up, "down": cmd_down, "restart": cmd_restart,
                "status": cmd_status, "attach": cmd_attach, "list": cmd_list,
                "spawn": cmd_spawn, "stop": cmd_stop, "reconcile": cmd_reconcile,
                "discover": cmd_discover, "inspect-launcher": cmd_inspect_launcher,
                "dirs": cmd_dirs, "roots": cmd_roots}
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
