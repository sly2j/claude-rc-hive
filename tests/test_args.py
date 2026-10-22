"""Launcher args: config, placement in the command, configedit, and hive spawn --arg."""

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from hive import cli
from hive.configedit import append_agent, verify
from hive.reconcile import desired_panes

ROOT = Path(__file__).resolve().parent.parent

BASE = """\
prefix = "t"

[defaults]
command = "claude -n {name} --remote-control {name}"
"""


@pytest.fixture
def write(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def _write(text):
        path = tmp_path / "hive" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    return _write


def fails(capsys, text_in_error, code=2):
    with pytest.raises(SystemExit) as exc:
        cli.load_config()
    assert exc.value.code == code
    err = capsys.readouterr().err
    assert err.startswith("hive: error: ")
    assert text_in_error in err


def agent(extra="", type_="a", dir_="/a"):
    return f'\n[[agents]]\ntype = "{type_}"\ndir = "{dir_}"\n{extra}'


def with_defaults(line):
    return BASE.replace("[defaults]\n", f"[defaults]\n{line}\n")


# 1. config

def test_args_inherited_from_defaults(write):
    write(with_defaults('args = ["--fable"]') + agent())
    cfg = cli.load_config()
    assert cfg["agents"][0]["args"] == ["--fable"]
    assert cfg["queen"]["args"] == ["--fable"]


def test_agent_args_replace_defaults(write):
    write(with_defaults('args = ["--fable", "--x"]')
          + agent('args = ["--y"]\n') + agent("args = []\n", "b", "/b"))
    cfg = cli.load_config()
    assert cfg["agents"][0]["args"] == ["--y"]
    assert cfg["agents"][1]["args"] == []


def test_queen_own_args(write):
    write(with_defaults('args = ["--fable"]') + '\n[queen]\nargs = ["--q"]\n' + agent())
    cfg = cli.load_config()
    assert cfg["queen"]["args"] == ["--q"]
    assert cfg["agents"][0]["args"] == ["--fable"]


def test_args_absent_everywhere(write):
    write(BASE + agent())
    cfg = cli.load_config()
    assert cfg["agents"][0]["args"] == [] and cfg["queen"]["args"] == []


@pytest.mark.parametrize("bad", ['"x"', "[1]", '[""]'])
@pytest.mark.parametrize("where", ["defaults", "agents", "queen"])
def test_args_invalid(write, capsys, where, bad):
    if where == "defaults":
        text = with_defaults(f"args = {bad}") + agent()
    elif where == "agents":
        text = BASE + agent(f"args = {bad}\n")
    else:
        text = BASE + f"\n[queen]\nargs = {bad}\n" + agent()
    write(text)
    fails(capsys, "args' must be an array of non-empty strings")


# 2. expansion

@pytest.mark.parametrize("command, args, expected", [
    ("claude -n {name}", ["--fable", "x"], "claude -n n --fable x"),
    ("claude -n {name}", [], "claude -n n"),
    ("claude-supervisor run fable {args} -- -n {name}", ["--a", "b"],
     "claude-supervisor run fable --a b -- -n n"),
    ("claude {args} -n {name}", [], "claude  -n n"),
    ("claude {name}", ["a b", "it's"], "claude n 'a b' 'it'\"'\"'s'"),
    ("claude {name}", ["--tag={name}"], "claude n --tag=n"),
])
def test_expand_command(command, args, expected):
    assert cli.expand_command(command, args, name="n") == expected


# 3. panes

def test_desired_panes_carry_args(write):
    write(with_defaults('args = ["--fable"]') + agent('args = ["--y"]\n', dir_="/tmp"))
    panes = desired_panes(cli.load_config())
    assert panes[0].type == "queen" and tuple(panes[0].args) == ("--fable",)
    assert tuple(panes[1].args) == ("--y",)


# 4. configedit

def append_expected(args):
    return lambda doc: doc.setdefault("agents", []).append(
        {"type": "a", "dir": "/a", "count": 1, "args": args})


def test_append_agent_args():
    new = append_agent(BASE, "a", "/a", 1, args=["--fable", "a b"])
    assert 'args = ["--fable", "a b"]' in new.splitlines()
    verify(BASE, new, append_expected(["--fable", "a b"]))


# 5. spawn (session not running: only the config is edited)

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
discover = "{discover}"
"""


class Hive:
    def __init__(self, tmp_path):
        self.config = tmp_path / "hive" / "config.toml"
        # HOME too, so trust checks read tmp_path/.claude.json, never the real one.
        self.env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path), "SHELL": "/bin/sh",
                    "HOME": str(tmp_path)}
        self.env.pop("CLAUDE_CONFIG_DIR", None)

    def run(self, *args):
        return subprocess.run([sys.executable, "-m", "hive.cli", *args], cwd=ROOT,
                              env=self.env, capture_output=True, text=True)

    def agents(self):
        return tomllib.loads(self.config.read_text()).get("agents", [])


@pytest.fixture
def hive(tmp_path):
    socket = f"hive-args-test-{os.getpid()}"
    for d in "abc":
        (tmp_path / d).mkdir()
    (tmp_path / "hive").mkdir()
    discover = tmp_path / "discover.sh"
    discover.write_text(
        "#!/bin/sh\n"
        "echo 'name=api config_dir=%s command=\"sleep 600 # {name} --remote-control\" "
        "args=--fable'\n" % (tmp_path / "cc"))
    discover.chmod(0o755)
    (tmp_path / "hive" / "config.toml").write_text(
        CONFIG.format(socket=socket, discover=discover))
    try:
        yield Hive(tmp_path)
    finally:
        if shutil.which("tmux"):
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


def test_spawn_persists_args(hive, tmp_path):
    r = hive.run("spawn", str(tmp_path / "a"), "--type", "a", "--trust", "--arg=--fable", "--arg", "x")
    assert r.returncode == 0, r.stderr
    assert hive.agents()[0]["args"] == ["--fable", "x"]
    lines = [l for l in hive.run("list").stdout.splitlines() if "type=a " in l]
    assert lines and 'args=["--fable", "x"]' in lines[0]


def test_spawn_same_args_raises_count_different_refused(hive, tmp_path):
    a = str(tmp_path / "a")
    assert hive.run("spawn", a, "--type", "a", "--trust", "--arg=--fable", "--arg", "x").returncode == 0
    r = hive.run("spawn", a, "--type", "a", "--arg=--fable", "--arg", "x")
    assert r.returncode == 0, r.stderr
    assert hive.agents()[0]["count"] == 2
    before = hive.config.read_text()
    r = hive.run("spawn", a, "--type", "a", "--arg=--other")
    assert r.returncode == 2
    assert "runs args" in r.stderr
    assert hive.config.read_text() == before


def test_spawn_sandbox_args_from_record_and_override(hive, tmp_path):
    r = hive.run("spawn", str(tmp_path / "b"), "--sandbox", "api", "--trust")
    assert r.returncode == 0, r.stderr
    r = hive.run("spawn", str(tmp_path / "c"), "--sandbox", "api", "--trust", "--arg=--other")
    assert r.returncode == 0, r.stderr
    by_dir = {Path(a["dir"]).name: a for a in hive.agents()}
    assert by_dir["b"]["args"] == ["--fable"]
    assert by_dir["c"]["args"] == ["--other"]


# 6. integration: queen allowlist, registry and default launchers, bad discover args, naming

def queen_cfg(write, queen_args):
    write(BASE + f"\n[queen]\nargs = {queen_args}\n" + agent())
    return cli.load_config()["queen"]


def test_queen_tools_arg_suppresses_hive_tools(write):
    for given in ('["--tools", "Read"]', '["--tools=Read"]'):
        q = queen_cfg(write, given)
        assert "--tools" not in q["command"]
        assert q["command"].endswith("--strict-mcp-config")


@pytest.mark.parametrize("args", ['["--fable"]',
                                  '["--append-system-prompt", "use --tools and --strict-mcp-config"]'])
def test_queen_plain_args_keep_full_allowlist(write, args):
    q = queen_cfg(write, args)
    assert f"--tools {cli.QUEEN_TOOLS}" in q["command"]
    assert "--strict-mcp-config" in q["command"]


def add_registry(hive, default=False):
    text = hive.config.read_text()
    if default:
        text = text.replace("[defaults]\n", '[defaults]\nlauncher = "reg"\n')
    hive.config.write_text(text + '\n[[launchers]]\nname = "reg"\n'
                           'command = "sleep 600 # {name} --remote-control"\n')


def test_spawn_registry_launcher_stores_no_args_unless_given(hive, tmp_path):
    add_registry(hive)
    r = hive.run("spawn", str(tmp_path / "a"), "--launcher", "reg", "--trust")
    assert r.returncode == 0, r.stderr
    r = hive.run("spawn", str(tmp_path / "b"), "--launcher", "reg", "--trust", "--arg=--x")
    assert r.returncode == 0, r.stderr
    by_dir = {Path(a["dir"]).name: a for a in hive.agents()}
    assert "args" not in by_dir["a"]
    assert by_dir["b"]["args"] == ["--x"]


def test_spawn_default_launcher_discover_record_args(hive, tmp_path):
    # [defaults] launcher names the discover record "api" (args=--fable).
    hive.config.write_text(hive.config.read_text().replace(
        "[defaults]\n", '[defaults]\nlauncher = "api"\n'))
    r = hive.run("spawn", str(tmp_path / "a"), "--trust")
    assert r.returncode == 0, r.stderr
    (entry,) = hive.agents()
    assert entry["type"] == "a-api" and entry["args"] == ["--fable"]


def test_spawn_default_launcher_registry_with_explicit_arg(hive, tmp_path):
    add_registry(hive, default=True)
    r = hive.run("spawn", str(tmp_path / "a"), "--trust", "--arg=--x")
    assert r.returncode == 0, r.stderr
    (entry,) = hive.agents()
    assert entry["type"] == "a-reg" and entry["args"] == ["--x"]


def test_spawn_unbalanced_record_args_names_launcher(hive, tmp_path):
    (tmp_path / "discover.sh").write_text(
        "#!/bin/sh\n"
        "echo 'name=api config_dir=/tmp/cc command=\"sleep 600 # {name} --remote-control\" args=\"'\"'\"'x\"'\n")
    before = hive.config.read_text()
    r = hive.run("spawn", str(tmp_path / "a"), "--launcher", "api", "--trust")
    assert r.returncode == 2
    assert "launcher 'api': args:" in r.stderr
    assert hive.config.read_text() == before


def test_expanded_pane_uses_instance_name_with_args(write):
    write(with_defaults('args = ["--fable"]') + agent(dir_="/tmp"))
    cfg = cli.load_config()
    pane = desired_panes(cfg)[1]
    assert pane.name == "a-t-1"
    assert cli.expand_pane(cfg, pane) == "claude -n a-t-1 --remote-control a-t-1 --fable"


def test_spawn_empty_arg_refused(hive, tmp_path):
    before = hive.config.read_text()
    r = hive.run("spawn", str(tmp_path / "a"), "--trust", "--arg=")
    assert r.returncode == 2
    assert "--arg must not be empty" in r.stderr
    assert hive.config.read_text() == before


def test_spawn_empty_record_arg_refused(hive, tmp_path):
    (tmp_path / "discover.sh").write_text(
        "#!/bin/sh\ncat <<'EOF'\n"
        "name=api config_dir=/tmp/cc command=\"sleep 600 # {name} --remote-control\" args=\"x ''\"\n"
        "EOF\n")
    before = hive.config.read_text()
    r = hive.run("spawn", str(tmp_path / "a"), "--launcher", "api", "--trust")
    assert r.returncode == 2
    assert "launcher 'api': args: empty argument" in r.stderr
    assert hive.config.read_text() == before
