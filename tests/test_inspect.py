import io
import os

import pytest

from hive import cli
from hive.inspect import inspect_launcher


def run(command, roots, environ=None):
    out, err = io.StringIO(), io.StringIO()
    code = inspect_launcher(command, [str(r) for r in roots], out, err,
                            environ if environ is not None else {"PATH": ""})
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def bindir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    return d


def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_script_with_config_dir_export(bindir):
    script(bindir / "launch", 'export CLAUDE_CONFIG_DIR="$HOME/.claude-api"\nexec claude "$@"\necho hi\n')
    code, out, err = run("launch -n {name} --remote-control {name}", [bindir],
                         {"PATH": str(bindir), "CLAUDE_CONFIG_DIR": "/x/y"})
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0] == f"path={bindir / 'launch'} resolved={bindir / 'launch'} kind=script shebang='#!/bin/sh' lines=4"
    assert "line=2 text='export CLAUDE_CONFIG_DIR=\"$HOME/.claude-api\"'" in lines
    assert "line=3 text='exec claude \"$@\"'" in lines
    assert not any(l.startswith("line=4") for l in lines)
    assert lines[-1] == "env CLAUDE_CONFIG_DIR=/x/y"


def test_secret_like_line_redacted(bindir):
    script(bindir / "launch", "API_KEY=sk-123\nexport ANTHROPIC_TOKEN = abc\nexec claude\n")
    code, out, _ = run(str(bindir / "launch"), [bindir])
    assert code == 0
    assert "sk-123" not in out and "abc" not in out
    assert "line=2 redacted=secret-like" in out
    assert "line=3 redacted=secret-like" in out
    assert "line=4 text='exec claude'" in out
    assert out.splitlines()[-1] == "env CLAUDE_CONFIG_DIR=unset"


def test_binary_has_no_lines(bindir):
    (bindir / "bin").write_bytes(b"\x7fELF\x00\x00claude CLAUDE_CONFIG_DIR=x\n")
    code, out, _ = run(str(bindir / "bin"), [bindir])
    assert code == 0
    assert out.splitlines() == [f"path={bindir / 'bin'} resolved={bindir / 'bin'} kind=binary",
                                "env CLAUDE_CONFIG_DIR=unset"]


def test_symlink_to_outside_refused(tmp_path, bindir):
    target = script(tmp_path / "elsewhere" / "real", "exec claude\n")
    (bindir / "launch").symlink_to(target)
    code, out, err = run(str(bindir / "launch"), [bindir])
    assert code == 3 and out == ""
    assert err.startswith(f"refused: {bindir / 'launch'} -> {target} is outside inspect_roots")


def test_outside_roots_refused(tmp_path, bindir):
    other = script(tmp_path / "other" / "launch", "exec claude\n")
    code, out, err = run(str(other), [bindir])
    assert code == 3 and out == ""
    assert err == f"refused: {other} is outside inspect_roots ({bindir})\n"


def test_not_found(bindir):
    code, out, err = run("nosuchlauncher -n x", [bindir], {"PATH": str(bindir)})
    assert code == 2 and out == ""
    assert err == "not found: nosuchlauncher\n"


def test_private_path_refused_even_inside_root(bindir):
    launcher = script(bindir / "Private" / "run", "exec claude\n")
    code, out, err = run(f"{launcher} -n x", [bindir])
    assert code == 3 and out == ""
    assert "secrets/private" in err


def test_line_cap(bindir):
    script(bindir / "launch", "claude\n" * 250)
    code, out, _ = run(str(bindir / "launch"), [bindir])
    assert code == 0
    assert sum(1 for l in out.splitlines() if l.startswith("line=")) == 200
    assert "more=yes" in out


def test_inspect_roots_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "hive" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('inspect_roots = ["~/bin", "/opt"]\n[defaults]\ncommand = "c"\n')
    assert cli.load_config()["inspect_roots"] == [os.path.expanduser("~/bin"), "/opt"]
    path.write_text('[defaults]\ncommand = "c"\n')
    assert cli.load_config()["inspect_roots"][0] == os.path.expanduser("~/.local/bin")
    path.write_text('inspect_roots = ["bin"]\n[defaults]\ncommand = "c"\n')
    with pytest.raises(SystemExit):
        cli.load_config()


def test_unreadable_script_is_reported(bindir):
    if os.geteuid() == 0:
        pytest.skip("root can read mode-000 files")
    p = script(bindir / "wrap", "exec claude\n")
    p.chmod(0)
    try:
        code, out, err = run(str(p), [bindir])
    finally:
        p.chmod(0o755)
    assert code == 2 and out == "" and "unreadable" in err


def test_unknown_user_command_not_found(bindir):
    code, out, err = run("~nosuchuser_hive/x", [bindir])
    assert code == 2 and "unknown user" in err
