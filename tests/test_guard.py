import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parent.parent / "hive" / "queen" / "hooks" / "guard.py"
HOME = os.path.expanduser("~")


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "queen"
    (d / ".claude").mkdir(parents=True)
    return d


def hook(qdir, payload):
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(qdir)}
    result = subprocess.run([sys.executable, str(GUARD)], input=stdin, capture_output=True,
                            text=True, env=env, cwd=qdir)
    return result.returncode, result.stderr


def bash(qdir, command):
    return hook(qdir, {"tool_name": "Bash", "tool_input": {"command": command}})


def tool(qdir, name, **tool_input):
    return hook(qdir, {"tool_name": name, "tool_input": tool_input})


@pytest.mark.parametrize("command", [
    "hive list",
    "hive spawn x --trust",
    'hive inspect-launcher "claude -n x --remote-control x"',
    'hive spawn ~/work/api --command "claude -n {name} --remote-control {name}"',
    f"{HOME}/.local/bin/hive list",
])
def test_bash_allows_hive(qdir, command):
    assert bash(qdir, command) == (0, "")


@pytest.mark.parametrize("command", [
    "cat ~/.ssh/id_rsa",
    "hive list; cat x",
    "hive list | grep y",
    "hive list && ls",
    "hive list > out",
    "hive list $(cat x)",
    "hive list\ncat x",
    "./hive list",
    "FOO=1 hive list",
    "hive inspect-launcher ~/.local/bin/Private/run",
    'hive inspect-launcher "~/.local/bin/Private/run -n x"',
    "hive spawn ~/work/secrets.d",
    "hive spawn ~/work/priv*",
    "hive spawn ~/work/{p,}rivate",
    "hive spawn $'/home/u/work/priv\\x61te'",
])
def test_bash_blocks(qdir, command):
    code, err = bash(qdir, command)
    assert code == 2 and err.startswith("guard: ")


def test_read_inside_queen_dir(qdir):
    assert tool(qdir, "Read", file_path=str(qdir / "CLAUDE.md")) == (0, "")
    assert tool(qdir, "Grep", pattern="hive") == (0, "")
    assert tool(qdir, "Glob", pattern="**/*.md") == (0, "")


@pytest.mark.parametrize("name,tool_input", [
    ("Read", {"file_path": f"{HOME}/.claude.json"}),
    ("Read", {"file_path": f"{HOME}/work/x/secrets.toml"}),
    ("Read", {"file_path": f"{HOME}/work/private/notes.md"}),
    ("Grep", {"pattern": "x", "path": "/etc"}),
    ("Glob", {"pattern": f"{HOME}/.ssh/*"}),
    ("Glob", {"pattern": "*/../../x"}),
    ("Edit", {"file_path": "QDIR/.claude/settings.json", "old_string": "a", "new_string": "b"}),
    ("Write", {"file_path": "QDIR/$HOME/../.claude/settings.local.json", "content": "{}"}),
    ("Read", {"file_path": "QDIR/$PATH/" + "../" * 40 + "etc/hostname"}),
])
def test_file_tools_block(qdir, name, tool_input):
    tool_input = {k: v.replace("QDIR", str(qdir)) for k, v in tool_input.items()}
    code, err = tool(qdir, name, **tool_input)
    assert code == 2 and err.startswith("guard: ")


def test_symlink_out_of_queen_dir_blocked(qdir, tmp_path):
    (tmp_path / "outside").write_text("x")
    (qdir / "link").symlink_to(tmp_path / "outside")
    assert tool(qdir, "Read", file_path=str(qdir / "link"))[0] == 2


def test_monitor_checked_like_bash(qdir):
    assert tool(qdir, "Monitor", command="cat ~/.ssh/id_rsa")[0] == 2
    assert tool(qdir, "Monitor", command="hive list") == (0, "")


def test_other_tools_pass(qdir):
    assert tool(qdir, "WebSearch", query="x") == (0, "")


@pytest.mark.parametrize("stdin", ["{not json", "[]", '{"tool_name": "Bash"}'])
def test_malformed_input_blocks(qdir, stdin):
    code, err = hook(qdir, stdin)
    assert code == 2 and err.startswith("guard: ")
