"""End-to-end test against a throwaway tmux server (never the real `hive` socket)."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")

ROOT = Path(__file__).resolve().parent.parent
SOCKET = f"hive-test-{os.getpid()}"

CONFIG = """\
prefix = "t"
session = "t-agents"
tmux_socket = "{socket}"
login_shell = false
shell = "/bin/sh"

[defaults]
command = "sleep 600"

[queen]
command = "sleep 600 #"
discover = "{discover}"

[[agents]]
type = "a"
dir = "{a}"
count = 2
"""


class Hive:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.config = tmp_path / "hive" / "config.toml"
        # HOME too, so trust checks read tmp_path/.claude.json, never the real one.
        self.env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path), "SHELL": "/bin/sh",
                    "HOME": str(tmp_path)}
        self.env.pop("CLAUDE_CONFIG_DIR", None)

    def run(self, *args):
        return subprocess.run([sys.executable, "-m", "hive.cli", *args], cwd=ROOT,
                              env=self.env, capture_output=True, text=True)

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", SOCKET, *args], capture_output=True, text=True)

    def names(self):
        out = self.tmux("list-panes", "-s", "-t", "=t-agents", "-F", "#{@hive_name}").stdout
        return {n for n in out.split() if n}

    def windows(self):
        return set(self.tmux("list-windows", "-t", "=t-agents", "-F", "#{window_name}").stdout.split())

    def pane_ids(self):
        return set(self.tmux("list-panes", "-s", "-t", "=t-agents", "-F", "#{pane_id}").stdout.split())


@pytest.fixture
def hive(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    cfg = tmp_path / "hive" / "config.toml"
    cfg.parent.mkdir()
    # A fake sandbox launcher listing: one record per line.
    discover = tmp_path / "discover.sh"
    discover.write_text("#!/bin/sh\necho 'name=api config_dir=%s "
                        "command=\"sleep 600 # {name} --remote-control\"'\n" % (tmp_path / "cc"))
    discover.chmod(0o755)
    cfg.write_text(CONFIG.format(socket=SOCKET, a=tmp_path / "a", discover=discover))
    h = Hive(tmp_path)
    try:
        # Guard: make sure every child talks to the throwaway socket and config
        # before any mutating command runs.
        first = h.run("list").stdout.splitlines()[0]
        assert f"socket={SOCKET}" in first and "session=t-agents" in first, first
        yield h
    finally:
        h.tmux("kill-server")


def test_lifecycle(hive, tmp_path):
    h = hive
    assert "running=no" in h.run("list").stdout.splitlines()[0]

    # up: the queen first, then the agents
    r = h.run("up")
    assert r.returncode == 0, r.stderr
    assert h.names() == {"hive-queen-t", "a-t-1", "a-t-2"}
    # untrusted panes are started anyway, with a warning; nothing is seeded
    assert f"hive: warning: untrusted a-t-1 {tmp_path / 'a'}" in r.stderr
    assert "--trust" in r.stderr
    assert f"untrusted hive-queen-t {tmp_path / 'hive' / 'queen'}" in r.stderr
    assert not (tmp_path / ".claude.json").exists()
    assert h.windows() == {"hive-queen-t", "a-t"}
    assert h.run("up").stdout.strip() == "no changes"

    # materialized queen files; CLAUDE.local.md is never touched
    qdir = tmp_path / "hive" / "queen"
    claude_md = qdir / "CLAUDE.md"
    assert claude_md.read_text().startswith("<!-- managed by hive;")
    assert (qdir / ".claude" / "settings.json").is_file()
    assert (qdir / ".claude" / "hooks" / "guard.py").is_file()
    local = qdir / "CLAUDE.local.md"
    local.write_text("mine\n")

    out = h.run("list").stdout.splitlines()
    assert out[0].startswith("session=t-agents socket=" + SOCKET + " running=yes")
    assert "queen name=hive-queen-t running=yes" in out[1]
    agent = next(line for line in out if line.startswith("agent type=a "))
    assert "count=2 running=2" in agent and 'command="sleep 600"' in agent
    assert agent.endswith(f"trusted=no trust_file={tmp_path / '.claude.json'}")
    assert sum(line.startswith("pane name=") for line in out) == 3
    assert not any("waiting=" in line for line in out)

    # a pane showing the trust dialog is reported as waiting
    a2 = next(line.split()[0] for line in h.tmux(
        "list-panes", "-s", "-t", "=t-agents", "-F", "#{pane_id} #{@hive_name}").stdout.splitlines()
        if line.endswith(" a-t-2"))
    h.tmux("send-keys", "-t", a2, "-l", "Do you trust the files in this folder?")
    for _ in range(50):
        out = h.run("list").stdout.splitlines()
        if f"pane name=a-t-2 type=a id={a2} waiting=trust" in out:
            break
        time.sleep(0.1)
    else:
        raise AssertionError(out)
    h.tmux("send-keys", "-t", a2, "C-u")
    assert not any(line.startswith(("drift", "unmanaged")) for line in out)

    # an unmanaged pane next to the managed ones
    assert h.tmux("split-window", "-d", "-t", "=t-agents:a-t").returncode == 0
    managed_ids = {line.split()[0] for line in h.tmux(
        "list-panes", "-s", "-t", "=t-agents", "-F", "#{pane_id} #{@hive_name}").stdout.splitlines()
        if len(line.split()) == 2}
    unmanaged = h.pane_ids() - managed_ids
    assert len(unmanaged) == 1
    assert any(line.startswith("unmanaged pane=") for line in h.run("list").stdout.splitlines())

    # spawn into an untrusted dir: exit 3, nothing changes
    before = h.config.read_bytes()
    r = h.run("spawn", "a")
    assert r.returncode == 3
    root = tmp_path / "a"
    assert r.stderr == (f"untrusted: {root} (trust root {root}, trust file "
                        f"{tmp_path / '.claude.json'}); rerun with --trust after the user confirms\n")
    assert h.config.read_bytes() == before and not (tmp_path / ".claude.json").exists()

    # spawn an existing type with --trust
    r = h.run("spawn", "a", "--trust")
    assert r.returncode == 0, r.stderr
    assert f"trusted {root} in {tmp_path / '.claude.json'}" in r.stdout
    assert "untrusted" not in r.stderr
    assert f"config agent=a count=2->3 file={h.config}" in r.stdout
    assert "started a-t-3" in r.stdout
    assert h.names() == {"hive-queen-t", "a-t-1", "a-t-2", "a-t-3"}

    # stop two: the highest indices go
    r = h.run("stop", "a", "-n", "2")
    assert r.returncode == 0, r.stderr
    assert "stopped a-t-3" in r.stdout and "stopped a-t-2" in r.stdout
    assert h.names() == {"hive-queen-t", "a-t-1"}
    assert "\ncount = 1\n" in h.config.read_text()
    assert unmanaged <= h.pane_ids()

    # stop more than exists
    r = h.run("stop", "a", "-n", "2")
    assert r.returncode == 2 and "cannot stop 2" in r.stderr

    # spawn a new directory: new entry, new window
    r = h.run("spawn", str(tmp_path / "b"), "--trust")
    assert r.returncode == 0, r.stderr
    assert "config agent=b count=0->1" in r.stdout
    assert "started b-t-1" in r.stdout

    # --sandbox: command and config_dir from discover; trust lives in the sandbox's store
    cc = tmp_path / "cc"
    before = h.config.read_bytes()
    r = h.run("spawn", "b", "--sandbox", "api")
    assert r.returncode == 3 and f"trust file {cc / '.claude.json'})" in r.stderr
    assert h.config.read_bytes() == before
    for bad, msg in ((["--sandbox", "nope"], "no launcher 'nope'; available: api"),
                     (["--sandbox", "api", "--command", "x {name} --remote-control"],
                      "do not combine it with --command or --config-dir"),
                     (["--env", "CLAUDE_CONFIG_DIR=/x"], "hive never sets CLAUDE_CONFIG_DIR")):
        r = h.run("spawn", "b", *bad)
        assert r.returncode == 2 and msg in r.stderr, r.stderr
    assert h.config.read_bytes() == before
    r = h.run("spawn", "b", "--sandbox", "api", "--trust")
    assert r.returncode == 0, r.stderr
    assert f"trusted {tmp_path / 'b'} in {cc / '.claude.json'}" in r.stdout
    assert "config agent=b-api count=0->1" in r.stdout and "started b-api-t-1" in r.stdout
    text = h.config.read_text()
    assert 'command = "sleep 600 # {name} --remote-control"' in text
    assert f'config_dir = "{cc}"' in text
    out = h.run("list").stdout.splitlines()
    assert next(line for line in out if line.startswith("agent type=b-api ")).endswith(
        f"trusted=yes trust_file={cc / '.claude.json'}")
    assert next(line for line in out if line.startswith("agent type=b ")).endswith(
        f"trusted=yes trust_file={tmp_path / '.claude.json'}")
    r = h.run("stop", "b-api")
    assert r.returncode == 0 and "stopped b-api-t-1" in r.stdout
    assert h.windows() == {"hive-queen-t", "a-t", "b-t"}
    assert h.names() == {"hive-queen-t", "a-t-1", "b-t-1"}
    assert 'type = "b"' in h.config.read_text()

    # a pane started by hive 1.5 (title only, no @hive_name) is adopted by exact title;
    # a pane whose title is not a desired name stays unmanaged
    a1 = next(line.split()[0] for line in h.tmux(
        "list-panes", "-s", "-t", "=t-agents", "-F", "#{pane_id} #{@hive_name}").stdout.splitlines()
        if line.endswith(" a-t-1"))
    h.tmux("set-option", "-p", "-u", "-t", a1, "@hive_name")
    h.tmux("select-pane", "-t", next(iter(unmanaged)), "-T", "a-t-9")
    assert "a-t-1" not in h.names()
    assert h.run("reconcile", "--dry-run").stdout.strip() == "no changes"
    r = h.run("reconcile")
    assert r.returncode == 0 and r.stdout.strip() == "adopted a-t-1"
    assert h.tmux("display-message", "-p", "-t", a1, "#{@hive_name}").stdout.strip() == "a-t-1"
    assert unmanaged <= h.pane_ids()

    # a directory name that is not a usable type needs --type
    (tmp_path / "My Vault").mkdir()
    r = h.run("spawn", str(tmp_path / "My Vault"))
    assert r.returncode == 2 and "give --type" in r.stderr

    # rejected requests change nothing
    before = h.config.read_bytes()
    r = h.run("spawn", "a", "--command", "claude -n {name}")
    assert r.returncode == 2
    assert "--command must contain {name} and --remote-control" in r.stderr
    r = h.run("spawn", "a", "--command", "claude --remote-control x")
    assert r.returncode == 2
    assert "--command must contain {name} and --remote-control" in r.stderr
    r = h.run("spawn", "a", "--command", "other {name} --remote-control {name}")
    assert r.returncode == 2 and "type 'a' runs 'sleep 600'" in r.stderr
    for cmd in ("stop", "spawn"):
        r = h.run(cmd, "queen")
        assert r.returncode == 2 and "the queen is always running" in r.stderr
    r = h.run("stop", "nonexistent")
    assert r.returncode == 2
    assert h.config.read_bytes() == before
    assert h.names() == {"hive-queen-t", "a-t-1", "b-t-1"}

    # reconcile is idle, and never touches the unmanaged pane
    r = h.run("reconcile", "--dry-run")
    assert r.returncode == 0 and r.stdout.strip() == "no changes"
    assert unmanaged <= h.pane_ids()

    # a queen that died is restarted, and its managed files are re-synced
    claude_md.write_text("tampered\n")
    queen_pane = next(line.split()[0] for line in h.tmux(
        "list-panes", "-s", "-t", "=t-agents", "-F", "#{pane_id} #{@hive_name}").stdout.splitlines()
        if line.endswith(" hive-queen-t"))
    h.tmux("kill-pane", "-t", queen_pane)
    assert "hive-queen-t" not in h.names()
    r = h.run("reconcile", "--dry-run")
    assert r.stdout.strip() == "would start hive-queen-t"
    assert claude_md.read_text() == "tampered\n"
    r = h.run("reconcile")
    assert r.returncode == 0 and "started hive-queen-t" in r.stdout
    assert "hive-queen-t" in h.names()
    assert claude_md.read_text().startswith("<!-- managed by hive;")
    assert local.read_text() == "mine\n"
    assert unmanaged <= h.pane_ids()

    # down
    r = h.run("down")
    assert r.returncode == 0
    assert h.tmux("has-session", "-t", "=t-agents").returncode != 0
    assert "running=no" in h.run("list").stdout.splitlines()[0]


def test_missing_dir_is_skipped_and_claude_env_is_dropped(hive, tmp_path):
    h = hive
    gone = tmp_path / "gone"
    with open(h.config, "a") as f:
        f.write(f'\n[[agents]]\ntype = "gone"\ndir = "{gone}"\n')
    # as if `hive up` ran from inside a Claude Code session
    h.env["CLAUDECODE"] = "1"
    r = h.run("reconcile", "--dry-run")
    assert r.returncode == 1  # not running yet
    r = h.run("up")
    assert r.returncode == 0, r.stderr
    assert f"skipped gone-t-1: dir {gone} does not exist" in r.stderr
    assert "hive: warning: 1 agent(s) skipped: dir missing; see 'hive list'" in r.stderr
    assert h.names() == {"hive-queen-t", "a-t-1", "a-t-2"}
    assert h.tmux("show-environment", "-g", "CLAUDECODE").returncode != 0
    r = h.run("reconcile", "--dry-run")
    assert f"would skip gone-t-1: dir {gone} does not exist" in r.stderr
    out = h.run("list").stdout.splitlines()
    assert next(l for l in out if l.startswith("agent type=gone ")).endswith(" dir_missing=yes")
    assert "dir_missing" not in next(l for l in out if l.startswith("agent type=a "))
    r = h.run("spawn", "gone", "--trust")
    assert r.returncode == 2 and "does not exist" in r.stderr
    r = h.run("stop", "gone")
    assert r.returncode == 0, r.stderr
    gone.mkdir()
    r = h.run("spawn", "gone", "--trust")
    assert r.returncode == 0 and "started gone-t-1" in r.stdout, r.stderr


def spawn_session(h):
    """Start the session with only the queen, then let tests plant panes by hand."""
    assert h.run("up").returncode == 0


def pane_id_of(h, name):
    return next(line.split()[0] for line in h.tmux(
        "list-panes", "-s", "-t", "=t-agents", "-F", "#{pane_id} #{@hive_name}").stdout.splitlines()
        if line.endswith(" " + name))


def make_legacy(h, name, window):
    """Retag a managed pane and its window as an old-style (pre-rename) one."""
    pid = pane_id_of(h, name)
    old = f"t-{name.rsplit('-t-', 1)[0]}-{name.rsplit('-', 1)[1]}" if name != "hive-queen-t" else "t-queen"
    h.tmux("set-option", "-p", "-t", pid, "@hive_name", old)
    h.tmux("select-pane", "-t", pid, "-T", old)
    h.tmux("rename-window", "-t", pid, window)
    return pid, old


def test_legacy_pane_adopted(hive):
    h = hive
    spawn_session(h)
    pid, old = make_legacy(h, "a-t-1", "t-a")
    assert old == "t-a-1"
    assert h.windows() == {"hive-queen-t", "t-a"}
    r = h.run("reconcile")
    assert r.returncode == 0 and "adopted a-t-1" in r.stdout and "started a-t-2" not in r.stdout
    assert "stopped" not in r.stdout and "started a-t-1" not in r.stdout
    assert h.tmux("display-message", "-p", "-t", pid, "#{@hive_name}").stdout.strip() == "a-t-1"
    assert h.tmux("display-message", "-p", "-t", pid, "#{pane_title}").stdout.strip() == "a-t-1"
    assert h.windows() == {"hive-queen-t", "a-t"}
    assert h.pane_ids() >= {pid}
    assert h.names() == {"hive-queen-t", "a-t-1", "a-t-2"}
    assert h.run("reconcile").stdout.strip() == "no changes"


def test_legacy_dry_run_no_writes(hive):
    h = hive
    spawn_session(h)
    pid, old = make_legacy(h, "a-t-1", "t-a")
    for cmd in (("reconcile", "--dry-run"), ("list",)):
        r = h.run(*cmd)
        assert r.returncode == 0 and "adopted" not in r.stdout
        assert h.tmux("display-message", "-p", "-t", pid, "#{@hive_name}").stdout.strip() == old
        assert h.windows() == {"hive-queen-t", "t-a"}


def test_legacy_queen_adopted(hive):
    h = hive
    spawn_session(h)
    pid, old = make_legacy(h, "hive-queen-t", "t-queen")
    assert old == "t-queen"
    r = h.run("reconcile")
    assert r.returncode == 0 and "adopted hive-queen-t" in r.stdout and "started" not in r.stdout
    assert h.windows() == {"hive-queen-t", "a-t"}
    assert h.names() == {"hive-queen-t", "a-t-1", "a-t-2"}
    assert h.tmux("display-message", "-p", "-t", pid, "#{@hive_name}").stdout.strip() == "hive-queen-t"


def test_legacy_unknown_unmanaged(hive):
    h = hive
    spawn_session(h)
    pid = pane_id_of(h, "a-t-1")
    h.tmux("set-option", "-p", "-t", pid, "@hive_name", "t-zzz-1")
    r = h.run("reconcile")
    assert "adopted" not in r.stdout
    assert h.tmux("display-message", "-p", "-t", pid, "#{@hive_name}").stdout.strip() == "t-zzz-1"
    assert pid in h.pane_ids()
    assert "unmanaged pane=" + pid in h.run("list").stdout


def test_spawn_hive_queen_type_reserved(hive, tmp_path):
    r = hive.run("spawn", str(tmp_path / "b"), "--type", "hive-queen")
    assert r.returncode == 2


SUP = "sleep 601 # {name} --remote-control"


def add_launchers(h, extra="", default=None):
    """Append registry launchers (sup, with a config dir) and optionally [defaults] launcher."""
    text = h.config.read_text()
    if default:
        text = text.replace("[defaults]\n", f'[defaults]\nlauncher = "{default}"\n')
    text += (f'\n[[launchers]]\nname = "sup"\ncommand = "{SUP}"\n'
             f'config_dir = "{h.tmp / "supcc"}"\n' + extra)
    h.config.write_text(text)


def test_spawn_launcher_registry(hive, tmp_path):
    h = hive
    add_launchers(h)
    r = h.run("spawn", str(tmp_path / "b"), "--launcher", "sup", "--trust")
    assert r.returncode == 0, r.stderr
    assert f"trusted {tmp_path / 'b'} in {tmp_path / 'supcc' / '.claude.json'}" in r.stdout
    text = h.config.read_text()
    assert 'type = "b-sup"' in text and f'command = "{SUP}"' in text
    assert f'config_dir = "{tmp_path / "supcc"}"' in text
    assert h.run("up").returncode == 0
    assert h.names() == {"hive-queen-t", "a-t-1", "a-t-2", "b-sup-t-1"}


def test_spawn_sandbox_alias(hive, tmp_path):
    h = hive
    r = h.run("spawn", str(tmp_path / "b"), "--sandbox", "api", "--trust")
    assert r.returncode == 0, r.stderr
    text = h.config.read_text()
    assert 'type = "b-api"' in text and 'command = "sleep 600 # {name} --remote-control"' in text
    assert f'config_dir = "{tmp_path / "cc"}"' in text


def test_spawn_launcher_registry_shadows_discover(hive, tmp_path):
    h = hive
    text = h.config.read_text() + (f'\n[[launchers]]\nname = "api"\ncommand = "{SUP}"\n')
    h.config.write_text(text)
    r = h.run("spawn", str(tmp_path / "b"), "--launcher", "api", "--trust")
    assert r.returncode == 0, r.stderr
    text = h.config.read_text()
    assert 'type = "b-api"' in text
    assert text.count(f'command = "{SUP}"') == 2  # launcher entry + the new agent
    assert f'config_dir = "{tmp_path / "cc"}"' not in text


def test_spawn_default_launcher_new_type(hive, tmp_path):
    h = hive
    add_launchers(h, default="sup")
    r = h.run("spawn", str(tmp_path / "b"), "--trust")
    assert r.returncode == 0, r.stderr
    text = h.config.read_text()
    assert 'type = "b-sup"' in text and 'type = "b"' not in text
    assert text.rstrip().endswith("\n".join(
        ["[[agents]]", 'type = "b-sup"', f'dir = "{tmp_path / "b"}"', "count = 1",
         f'command = "{SUP}"', f'config_dir = "{tmp_path / "supcc"}"']))
    # --type wins over the suffix; an existing type is not given the default launcher
    r = h.run("spawn", "a", "--trust")
    assert r.returncode == 0, r.stderr
    assert "config agent=a count=2->3" in r.stdout
    (tmp_path / "c").mkdir()
    r = h.run("spawn", str(tmp_path / "c"), "--type", "cc", "--trust")
    assert r.returncode == 0 and 'type = "cc"' in h.config.read_text(), r.stderr


def test_spawn_default_launcher_bad_discover_command(hive, tmp_path):
    h = hive
    (tmp_path / "discover.sh").write_text("#!/bin/sh\necho 'name=bad config_dir= command=\"badcmd --no-rc\"'\n")
    h.config.write_text(h.config.read_text().replace("[defaults]\n", '[defaults]\nlauncher = "bad"\n'))
    before = h.config.read_text()
    r = h.run("spawn", str(tmp_path / "b"), "--trust")
    assert r.returncode == 2 and "--remote-control" in r.stderr
    assert h.config.read_text() == before


def test_spawn_default_launcher_existing_type(hive, tmp_path):
    h = hive
    add_launchers(h, default="sup")
    assert h.run("spawn", str(tmp_path / "b"), "--trust").returncode == 0
    before = h.config.read_text()
    r = h.run("spawn", str(tmp_path / "b"))
    assert r.returncode == 0, r.stderr
    assert "config agent=b-sup count=1->2" in r.stdout
    after = h.config.read_text()
    assert after == before.replace("count = 1\ncommand", "count = 2\ncommand")
    assert after.count(f'command = "{SUP}"') == before.count(f'command = "{SUP}"')


def test_spawn_launcher_untrusted_uses_launcher_config_dir(hive, tmp_path):
    h = hive
    add_launchers(h)
    before = h.config.read_bytes()
    r = h.run("spawn", str(tmp_path / "b"), "--launcher", "sup")
    assert r.returncode == 3
    assert f"trust file {tmp_path / 'supcc' / '.claude.json'})" in r.stderr
    assert h.config.read_bytes() == before and not (tmp_path / "supcc").exists()


def test_spawn_launcher_with_command_rejected(hive, tmp_path):
    h = hive
    add_launchers(h)
    before = h.config.read_bytes()
    for extra in (["--command", "x {name} --remote-control"], ["--config-dir", "/x"]):
        r = h.run("spawn", str(tmp_path / "b"), "--launcher", "sup", *extra)
        assert r.returncode == 2 and "do not combine it with --command" in r.stderr, r.stderr
    assert h.config.read_bytes() == before


def test_spawn_unknown_launcher(hive, tmp_path):
    h = hive
    add_launchers(h)
    before = h.config.read_bytes()
    r = h.run("spawn", str(tmp_path / "b"), "--launcher", "nope")
    assert r.returncode == 2 and "no launcher 'nope'; available: sup, api" in r.stderr, r.stderr
    assert h.config.read_bytes() == before


def test_spawn_launcher_reserved_final_type(hive, tmp_path):
    h = hive
    add_launchers(h, extra=f'\n[[launchers]]\nname = "queen"\ncommand = "{SUP}"\n')
    before = h.config.read_bytes()
    d = tmp_path / "x" / "hive"
    d.mkdir(parents=True)
    r = h.run("spawn", str(d), "--launcher", "queen", "--trust")
    assert r.returncode == 2 and "queen" in r.stderr, r.stderr
    # the default launcher path is checked on the final type too
    text = h.config.read_text().replace("[defaults]\n", '[defaults]\nlauncher = "queen"\n')
    h.config.write_text(text)
    before = h.config.read_bytes()
    r = h.run("spawn", str(d), "--trust")
    assert r.returncode == 2 and "queen" in r.stderr, r.stderr
    assert h.config.read_bytes() == before


def test_spawn_launcher_type_collision_refused(hive, tmp_path):
    h = hive
    other = "other {name} --remote-control"
    add_launchers(h, extra=f'\n[[launchers]]\nname = "claude-sup"\ncommand = "{other}"\n')
    api_claude, api = tmp_path / "api-claude", tmp_path / "api"
    api_claude.mkdir()
    api.mkdir()
    # dir api-claude + sup and dir api + claude-sup both give api-claude-sup
    r = h.run("spawn", str(api_claude), "--launcher", "sup", "--trust")
    assert r.returncode == 0 and "config agent=api-claude-sup count=0->1" in r.stdout, r.stderr
    before = h.config.read_bytes()
    r = h.run("spawn", str(api), "--launcher", "claude-sup", "--trust")
    assert r.returncode == 2, r.stderr
    assert f"type 'api-claude-sup' already exists for '{api_claude}' and runs '{SUP}'" in r.stderr
    assert h.config.read_bytes() == before
    # the same launcher on the same type is a count bump
    r = h.run("spawn", str(api_claude), "--launcher", "sup")
    assert r.returncode == 0 and "config agent=api-claude-sup count=1->2" in r.stdout, r.stderr
    # the default launcher is held to the same rule
    h.config.write_text(h.config.read_text().replace(
        "[defaults]\n", '[defaults]\nlauncher = "claude-sup"\n'))
    before = h.config.read_bytes()
    r = h.run("spawn", str(api), "--trust")
    assert r.returncode == 2 and "already exists" in r.stderr, r.stderr
    assert h.config.read_bytes() == before
