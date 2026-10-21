"""End-to-end test against a throwaway tmux server (never the real `hive` socket)."""

import os
import shutil
import subprocess
import sys
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
command = "sleep 600"

[[agents]]
type = "a"
dir = "{a}"
count = 2
"""


class Hive:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.config = tmp_path / "hive" / "config.toml"
        self.env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path), "SHELL": "/bin/sh"}

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
    cfg.write_text(CONFIG.format(socket=SOCKET, a=tmp_path / "a"))
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
    assert h.names() == {"t-queen", "t-a-1", "t-a-2"}
    assert h.windows() == {"t-queen", "t-a"}
    assert h.run("up").stdout.strip() == "no changes"

    # materialized queen files; CLAUDE.local.md is never touched
    qdir = tmp_path / "hive" / "queen"
    claude_md = qdir / "CLAUDE.md"
    assert claude_md.read_text().startswith("<!-- managed by hive;")
    assert (qdir / ".claude" / "settings.json").is_file()
    local = qdir / "CLAUDE.local.md"
    local.write_text("mine\n")

    out = h.run("list").stdout.splitlines()
    assert out[0].startswith("session=t-agents socket=" + SOCKET + " running=yes")
    assert "queen name=t-queen running=yes" in out[1]
    agent = next(line for line in out if line.startswith("agent type=a "))
    assert "count=2 running=2" in agent and 'command="sleep 600"' in agent
    assert not any(line.startswith(("drift", "unmanaged")) for line in out)

    # an unmanaged pane next to the managed ones
    assert h.tmux("split-window", "-d", "-t", "=t-agents:t-a").returncode == 0
    managed_ids = {line.split()[0] for line in h.tmux(
        "list-panes", "-s", "-t", "=t-agents", "-F", "#{pane_id} #{@hive_name}").stdout.splitlines()
        if len(line.split()) == 2}
    unmanaged = h.pane_ids() - managed_ids
    assert len(unmanaged) == 1
    assert any(line.startswith("unmanaged pane=") for line in h.run("list").stdout.splitlines())

    # spawn an existing type
    r = h.run("spawn", "a")
    assert r.returncode == 0, r.stderr
    assert f"config agent=a count=2->3 file={h.config}" in r.stdout
    assert "started t-a-3" in r.stdout
    assert h.names() == {"t-queen", "t-a-1", "t-a-2", "t-a-3"}

    # stop two: the highest indices go
    r = h.run("stop", "a", "-n", "2")
    assert r.returncode == 0, r.stderr
    assert "stopped t-a-3" in r.stdout and "stopped t-a-2" in r.stdout
    assert h.names() == {"t-queen", "t-a-1"}
    assert "\ncount = 1\n" in h.config.read_text()
    assert unmanaged <= h.pane_ids()

    # stop more than exists
    r = h.run("stop", "a", "-n", "2")
    assert r.returncode == 2 and "cannot stop 2" in r.stderr

    # spawn a new directory: new entry, new window
    r = h.run("spawn", str(tmp_path / "b"))
    assert r.returncode == 0, r.stderr
    assert "config agent=b count=0->1" in r.stdout
    assert "started t-b-1" in r.stdout
    assert h.windows() == {"t-queen", "t-a", "t-b"}
    assert h.names() == {"t-queen", "t-a-1", "t-b-1"}
    assert 'type = "b"' in h.config.read_text()

    # a pane started by hive 1.5 (title only, no @hive_name) is adopted by exact title;
    # a pane whose title is not a desired name stays unmanaged
    a1 = next(line.split()[0] for line in h.tmux(
        "list-panes", "-s", "-t", "=t-agents", "-F", "#{pane_id} #{@hive_name}").stdout.splitlines()
        if line.endswith(" t-a-1"))
    h.tmux("set-option", "-p", "-u", "-t", a1, "@hive_name")
    h.tmux("select-pane", "-t", next(iter(unmanaged)), "-T", "t-a-9")
    assert "t-a-1" not in h.names()
    assert h.run("reconcile", "--dry-run").stdout.strip() == "no changes"
    r = h.run("reconcile")
    assert r.returncode == 0 and r.stdout.strip() == "adopted t-a-1"
    assert h.tmux("display-message", "-p", "-t", a1, "#{@hive_name}").stdout.strip() == "t-a-1"
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
    assert h.names() == {"t-queen", "t-a-1", "t-b-1"}

    # reconcile is idle, and never touches the unmanaged pane
    r = h.run("reconcile", "--dry-run")
    assert r.returncode == 0 and r.stdout.strip() == "no changes"
    assert unmanaged <= h.pane_ids()

    # a queen that died is restarted, and its managed files are re-synced
    claude_md.write_text("tampered\n")
    queen_pane = next(line.split()[0] for line in h.tmux(
        "list-panes", "-s", "-t", "=t-agents", "-F", "#{pane_id} #{@hive_name}").stdout.splitlines()
        if line.endswith(" t-queen"))
    h.tmux("kill-pane", "-t", queen_pane)
    assert "t-queen" not in h.names()
    r = h.run("reconcile", "--dry-run")
    assert r.stdout.strip() == "would start t-queen"
    assert claude_md.read_text() == "tampered\n"
    r = h.run("reconcile")
    assert r.returncode == 0 and "started t-queen" in r.stdout
    assert "t-queen" in h.names()
    assert claude_md.read_text().startswith("<!-- managed by hive;")
    assert local.read_text() == "mine\n"
    assert unmanaged <= h.pane_ids()

    # down
    r = h.run("down")
    assert r.returncode == 0
    assert h.tmux("has-session", "-t", "=t-agents").returncode != 0
    assert "running=no" in h.run("list").stdout.splitlines()[0]
