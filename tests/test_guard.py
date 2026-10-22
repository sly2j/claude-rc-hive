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


def run(qdir, payload):
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(qdir)}
    result = subprocess.run([sys.executable, str(GUARD)], input=stdin, capture_output=True,
                            text=True, env=env, cwd=qdir)
    return result


def hook(qdir, payload):
    result = run(qdir, payload)
    return result.returncode, result.stderr


def asked(qdir, command):
    """Return the ask reason, or None if the guard did not answer ask."""
    result = run(qdir, {"tool_name": "Bash", "tool_input": {"command": command}})
    assert result.returncode == 0 and result.stderr == ""
    if not result.stdout:
        return None
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "ask"
    return out["permissionDecisionReason"]


CMD = "claude -n {name} --remote-control {name}"


def write_config(qdir, text=None):
    (qdir.parent / "config.toml").write_text(
        text if text is not None else f'[defaults]\ncommand = "{CMD}"\n\n'
        '[[agents]]\nname = "a"\ncommand = "other -n {name} --remote-control {name}"\n')


def bash(qdir, command):
    return hook(qdir, {"tool_name": "Bash", "tool_input": {"command": command}})


def tool(qdir, name, **tool_input):
    return hook(qdir, {"tool_name": name, "tool_input": tool_input})


@pytest.mark.parametrize("command", [
    "hive list",
    "hive stop x -n 2",
    'hive inspect-launcher "claude -n x --remote-control x"',
    f"{HOME}/.local/bin/hive list",
    "hive dirs",
    "hive dirs ~/work",
    "hive dirs claude-rc-hive --depth 2",
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


@pytest.mark.parametrize("command", [
    f'hive spawn x --command "{CMD}"',
    f'hive spawn x --command="{CMD}"',
    f'hive spawn x --comm "{CMD}"',
    'hive spawn x --command "other -n {name} --remote-control {name}"',
])
def test_spawn_listed_command_allowed(qdir, command):
    write_config(qdir)
    assert asked(qdir, command) is None


@pytest.mark.parametrize("flag", ["--command ", "--command=", "--comm ", "--com=", "--comma "])
def test_spawn_unlisted_command_asks(qdir, flag):
    write_config(qdir)
    reason = asked(qdir, f'hive spawn x {flag}"evil {{name}}"')
    assert "evil {name}" in reason and "not a command from config.toml" in reason


@pytest.mark.parametrize("flag", ["--env ", "--env=", "--en ", "--e="])
def test_spawn_env_asks(qdir, flag):
    write_config(qdir)
    reason = asked(qdir, f"hive spawn x {flag}BASH_ENV=/q/x.sh")
    assert "BASH_ENV=/q/x.sh" in reason and "sets environment" in reason


def test_spawn_listed_command_with_env_asks(qdir):
    write_config(qdir)
    reason = asked(qdir, f'hive spawn x --command "{CMD}" --env A=1 --env B=2')
    assert "A=1 B=2" in reason and "not a command" not in reason


def test_spawn_unlisted_command_and_env_one_reason(qdir):
    write_config(qdir)
    reason = asked(qdir, "hive spawn x --command evil --env A=1")
    assert "evil" in reason and "A=1" in reason


def test_env_denied_with_chaining(qdir):
    assert bash(qdir, "hive spawn x --env A=1; ls")[0] == 2


@pytest.mark.parametrize("text", [None, "[defaults\nbroken", ""])
def test_spawn_missing_or_broken_config_asks(qdir, text):
    if text is not None:
        write_config(qdir, text)
    assert CMD in asked(qdir, f'hive spawn x --command "{CMD}"')


def test_spawn_second_unlisted_command_asks(qdir):
    write_config(qdir)
    assert "evil" in asked(qdir, f'hive spawn x --command "{CMD}" --command evil')


def test_roots_add_asks(qdir):
    assert "~/projects" in asked(qdir, "hive roots add ~/projects")
    assert asked(qdir, "hive roots") is None


def test_chaining_still_denied_with_unlisted_command(qdir):
    code, err = bash(qdir, "hive spawn x --command evil; ls")
    assert code == 2 and err.startswith("guard: ")


@pytest.mark.parametrize("name,tool_input", [
    ("WebFetch", {"url": "http://x"}),
    ("WebSearch", {"query": "x"}),
    ("Agent", {"prompt": "x"}),
    ("mcp__x__y", {}),
    ("Unknown", {}),
])
def test_other_tools_denied(qdir, name, tool_input):
    code, err = tool(qdir, name, **tool_input)
    assert code == 2 and err.startswith("guard: ")


@pytest.mark.parametrize("name", [".mcp.json", ".claude.json", "CLAUDE.md"])
def test_protected_writes_denied(qdir, name):
    for t, key in (("Write", "file_path"), ("Edit", "file_path"), ("NotebookEdit", "notebook_path")):
        assert tool(qdir, t, **{key: str(qdir / name)})[0] == 2
    assert tool(qdir, "Read", file_path=str(qdir / name))[0] == 0


def test_claude_local_md_writable(qdir):
    assert tool(qdir, "Write", file_path=str(qdir / "CLAUDE.local.md"), content="x") == (0, "")


@pytest.mark.parametrize("pattern", [
    "{..,x}/*", "[.][.]/*", "..*", "../x", "*/../x", "*.{md,txt}",
])
def test_glob_bad_patterns_blocked(qdir, pattern):
    assert tool(qdir, "Glob", pattern=pattern)[0] == 2


@pytest.mark.parametrize("glob", ["../*", "{..,x}/*", "..*", "[.][.]/x"])
def test_grep_bad_glob_blocked(qdir, glob):
    assert tool(qdir, "Grep", pattern="x", glob=glob)[0] == 2
    assert tool(qdir, "Grep", pattern="x", glob="*.md") == (0, "")


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


@pytest.mark.parametrize("command", [
    "hive launchers",
    "hive launchers default x",
    "hive spawn foo --launcher x",
])
def test_launchers_commands_allowed(qdir, command):
    assert bash(qdir, command) == (0, "")


def test_launchers_add_asks(qdir):
    assert "claude --remote-control {name}" in asked(qdir, 'hive launchers add x "claude --remote-control {name}"')
    assert asked(qdir, "hive launchers default x") is None


def test_ask_user_question_allowed(qdir):
    assert tool(qdir, "AskUserQuestion", questions=[]) == (0, "")


def test_trust_prompt_names_target_after_launcher(qdir):
    assert "grants workspace trust to foo" in asked(qdir, "hive spawn --launcher x foo --trust")


@pytest.mark.parametrize("stdin", ["{not json", "[]", '{"tool_name": "Bash"}'])
def test_malformed_input_blocks(qdir, stdin):
    code, err = hook(qdir, stdin)
    assert code == 2 and err.startswith("guard: ")


@pytest.mark.parametrize("command,target", [
    ("hive spawn ~/work/api --trust", "~/work/api"),
    ("hive spawn --trust ~/work/api", "~/work/api"),
    ("hive spawn -n 2 --type t --tru api", "api"),
    ("hive spawn --type=t --env A=1 api --tr", "api"),
    ("hive spawn --trust -- -odd", "-odd"),
])
def test_spawn_trust_asks(qdir, command, target):
    write_config(qdir)
    reason = asked(qdir, command)
    assert f"hive spawn --trust grants workspace trust to {target}" in reason
    assert reason.endswith("; approve to run it")


def test_spawn_type_is_not_trust(qdir):
    write_config(qdir)
    assert bash(qdir, "hive spawn api --type trusted") == (0, "")


def test_down_asks(qdir):
    assert asked(qdir, "hive down") == (
        "hive down stops the whole hive, including this queen; approve to run it")


@pytest.mark.parametrize("command", ["hive down; ls", "hive spawn x --trust && ls"])
def test_deny_wins_over_trust_and_down(qdir, command):
    assert bash(qdir, command)[0] == 2


@pytest.mark.parametrize("command", ["hive -- down", "hive -- spawn x --trust",
                                     "hive --x spawn x --command evil"])
def test_options_before_subcommand_denied(qdir, command):
    assert bash(qdir, command)[0] == 2


def test_version_and_help_allowed(qdir):
    assert bash(qdir, "hive --version") == (0, "")
    assert bash(qdir, "hive --help") == (0, "")


def test_restart_asks(qdir):
    assert asked(qdir, "hive restart") == (
        "hive restart stops and restarts the whole hive, including this queen; approve to run it")
