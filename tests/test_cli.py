"""CLI robustness: config errors, lenient down/status, locking, service unit, warnings.

Runs main() with HOME and XDG_CONFIG_HOME under tmp_path. tmux is never the real one:
tests either stub cli.tmux or put a fake `tmux` first on PATH.
"""

import os
import stat
import sys
from types import SimpleNamespace

import pytest

from hive import cli

SOCKET = f"hive-test-cli-{os.getpid()}"
BASE = f'prefix = "t"\ntmux_socket = "{SOCKET}"\n\n[defaults]\ncommand = "sleep 600 {{name}} --remote-control"\n'


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(cli, "_lock", None)
    monkeypatch.setattr(cli, "_warned", set())
    cfg = xdg / "hive" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(BASE)
    return SimpleNamespace(tmp=tmp_path, home=home, xdg=xdg, cfg=cfg)


def hive(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["hive", *argv])
    try:
        code = cli.main()
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    assert "Traceback" not in out.err
    return code, out.out, out.err


def fake_bin(tmp_path, monkeypatch, name, body="exit 0"):
    """A fake executable that logs its arguments to <name>.log; PATH is only that dir."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    script = d / name
    log = d / f"{name}.log"
    script.write_text(f'#!/bin/sh\necho "$@" >> {log}\n{body}\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return log


# --- config validation (R1, R6, R15, U6) -------------------------------------

@pytest.mark.parametrize("line,key", [
    ('prefix = "a.b"', "prefix"), ('prefix = "a:b"', "prefix"), ('prefix = "a b"', "prefix"),
    ('prefix = ""', "prefix"), ('session = "x.y"', "session"), ('session = "x y"', "session"),
])
def test_names_with_dot_colon_space_are_rejected(env, monkeypatch, capsys, line, key):
    env.cfg.write_text(line + "\n" + BASE.split("\n", 1)[1].replace(f'tmux_socket = "{SOCKET}"\n', "")
                       if key != "tmux_socket" else line + '\nprefix = "t"\n[defaults]\ncommand = "c"\n')
    code, _, err = hive(monkeypatch, capsys, "list")
    assert code == 2 and err.startswith("hive: error: ") and f"'{key}'" in err


@pytest.mark.parametrize("kind", ["a b", "a:b", "a/b"])
def test_agent_type_is_checked_like_spawn(env, monkeypatch, capsys, kind):
    env.cfg.write_text(BASE + f'\n[[agents]]\ntype = "{kind}"\ndir = "/tmp"\n')
    code, _, err = hive(monkeypatch, capsys, "list")
    assert code == 2 and "agents[1].type" in err and kind in err


@pytest.mark.parametrize("snippet,text", [
    ('search_roots = ["rel"]\n', "search_roots entry 'rel' must be an absolute path"),
    ('search_roots = [""]\n', "search_roots entry must not be empty"),
    ('search_roots = ["~nosuchuser_hive/x"]\n', "unknown user"),
])
def test_bad_search_roots(env, monkeypatch, capsys, snippet, text):
    env.cfg.write_text(snippet + BASE)
    code, _, err = hive(monkeypatch, capsys, "list")
    assert code == 2 and text in err


@pytest.mark.parametrize("dir_,text", [("rel", "agents[1].dir 'rel' must be an absolute path"),
                                       ("", "agents[1].dir must not be empty")])
def test_bad_agent_dir(env, monkeypatch, capsys, dir_, text):
    env.cfg.write_text(BASE + f'\n[[agents]]\ntype = "a"\ndir = "{dir_}"\n')
    code, _, err = hive(monkeypatch, capsys, "list")
    assert code == 2 and text in err


@pytest.mark.parametrize("extra", ['[defaults]\ncommand = "  "\n', 'shell = ""\n'])
def test_empty_command_and_shell_are_rejected(env, monkeypatch, capsys, extra):
    env.cfg.write_text(extra if extra.startswith("[") else extra + BASE)
    code, _, err = hive(monkeypatch, capsys, "list")
    assert code == 2 and "must not be empty" in err


def test_toml_type_messages(env, monkeypatch, capsys):
    env.cfg.write_text("agents = 3\n" + BASE)
    assert "'agents' must be an array of [[agents]] tables" in hive(monkeypatch, capsys, "list")[2]
    env.cfg.write_text(BASE + '\n[[agents]]\ntype = "a"\ndir = "/tmp"\ncount = "x"\n')
    assert "'agents[1].count' must be an integer" in hive(monkeypatch, capsys, "list")[2]
    env.cfg.write_text('login_shell = 1\n' + BASE)
    assert "'login_shell' must be true or false" in hive(monkeypatch, capsys, "list")[2]


# --- R3: unreadable config ---------------------------------------------------

def test_config_is_a_directory(env, monkeypatch, capsys):
    env.cfg.unlink()
    env.cfg.mkdir()
    code, _, err = hive(monkeypatch, capsys, "list")
    assert code == 2 and f"cannot read {env.cfg}" in err


def test_config_not_utf8(env, monkeypatch, capsys):
    env.cfg.write_bytes(b'prefix = "\xff\xfe"\n')
    code, _, err = hive(monkeypatch, capsys, "list")
    assert code == 2 and f"cannot read {env.cfg}" in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_config_unreadable(env, monkeypatch, capsys):
    env.cfg.chmod(0)
    code, _, err = hive(monkeypatch, capsys, "list")
    assert code == 2 and f"cannot read {env.cfg}" in err


# --- R2: down and status survive a broken config ------------------------------

def test_down_with_broken_config_uses_the_parsable_socket(env, monkeypatch, capsys):
    log = fake_bin(env.tmp, monkeypatch, "tmux", "exit 1")
    env.cfg.write_text(BASE + "\nbogus = 1\n")
    code, out, err = hive(monkeypatch, capsys, "down")
    assert code == 0 and "not running" in out
    assert "hive: warning: config problem" in err and "unknown key 'bogus'" in err
    assert f"-L {SOCKET} has-session -t =t-agents" in log.read_text()


def test_down_with_unparsable_config_falls_back_to_defaults(env, monkeypatch, capsys):
    log = fake_bin(env.tmp, monkeypatch, "tmux", "exit 1")
    env.cfg.write_text("this is [not toml\n")
    code, out, err = hive(monkeypatch, capsys, "down")
    assert code == 0 and "hive: warning: config problem" in err
    assert "-L hive has-session" in log.read_text()
    code, out, err = hive(monkeypatch, capsys, "status")
    assert code == 1 and out == "not running\n" and "config problem" in err


def test_down_with_unparsable_config_keeps_its_own_socket(env, monkeypatch, capsys):
    # A typo after tmux_socket must not send `down` to the default `hive` socket.
    log = fake_bin(env.tmp, monkeypatch, "tmux", "exit 1")
    env.cfg.write_text(f'tmux_socket = "{SOCKET}"\nbogus = [\n')
    code, _, err = hive(monkeypatch, capsys, "down")
    assert code == 0 and f"on socket {SOCKET}" in err
    assert f"-L {SOCKET} has-session" in log.read_text()
    assert "-L hive " not in log.read_text()


def test_dotted_socket_name_is_valid(env, monkeypatch, capsys):
    env.cfg.write_text(BASE.replace(f'tmux_socket = "{SOCKET}"', 'tmux_socket = "x.y"'))
    code, _, err = hive(monkeypatch, capsys, "list")
    assert "tmux_socket" not in err
    assert cli.parse_config()["socket"] == "x.y"


@pytest.mark.parametrize("bad", ['prefix = "a.b"', 'session = "x:y"', "prefix = 3",
                                 "tmux_socket = 'mysock", 'tmux_socket = "a b"', 'tmux_socket = ""'])
def test_down_with_a_set_but_invalid_name_dies(env, monkeypatch, capsys, bad):
    log = fake_bin(env.tmp, monkeypatch, "tmux", "exit 1")
    socket_line = "" if bad.startswith("tmux_socket") else f'tmux_socket = "{SOCKET}"\n'
    env.cfg.write_text(f'{bad}\n{socket_line}prefix2 = [\n')
    code, _, err = hive(monkeypatch, capsys, "down")
    assert code == 2
    assert not log.exists() or "-L hive" not in log.read_text()


def test_down_without_a_config_still_errors(env, monkeypatch, capsys):
    env.cfg.unlink()
    code, _, err = hive(monkeypatch, capsys, "down")
    assert code == 2 and "not found" in err


def test_other_commands_die_on_a_broken_config(env, monkeypatch, capsys):
    env.cfg.write_text(BASE + "\nbogus = 1\n")
    for cmd in ("list", "up", "reconcile"):
        assert hive(monkeypatch, capsys, cmd)[0] == 2


# --- R9: lock after config; lock errors --------------------------------------

def test_no_config_means_no_lock_file(env, monkeypatch, capsys):
    env.cfg.unlink()
    code, _, err = hive(monkeypatch, capsys, "up")
    assert code == 2 and "not found" in err
    assert not (env.xdg / "hive" / ".lock").exists()


def test_lock_dir_not_creatable(env, monkeypatch, capsys):
    blocker = env.tmp / "file"
    blocker.write_text("")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocker))
    with pytest.raises(SystemExit) as exc:
        cli.hold_lock()
    assert exc.value.code == 1
    assert "cannot open lock" in capsys.readouterr().err


def test_down_continues_without_lock(env, monkeypatch, capsys):
    blocker = env.tmp / "file"
    blocker.write_text("")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocker))
    cli.hold_lock(required=False)  # warns, does not exit
    assert cli._lock is None
    assert "continuing without it" in capsys.readouterr().err


# --- R10 ---------------------------------------------------------------------

def test_ctrl_c_and_broken_pipe(monkeypatch, capsys):
    def interrupt():
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "run", interrupt)
    assert cli.main() == 130
    assert capsys.readouterr().err == "hive: interrupted\n"

    def pipe():
        raise BrokenPipeError
    monkeypatch.setattr(cli, "run", pipe)
    monkeypatch.setattr(os, "dup2", lambda *a: None)
    assert cli.main() == 1
    assert capsys.readouterr().err == ""


# --- R4, R5 ------------------------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_spawn_in_a_read_only_config_dir_seeds_no_trust(env, monkeypatch, capsys):
    proj = env.tmp / "proj"
    proj.mkdir()
    (env.xdg / "hive" / ".lock").write_text("")
    (env.xdg / "hive").chmod(0o555)
    try:
        code, _, err = hive(monkeypatch, capsys, "spawn", str(proj), "--trust")
    finally:
        (env.xdg / "hive").chmod(0o755)
    assert code == 1 and f"cannot write {env.cfg}" in err
    assert not (env.home / ".claude.json").exists()


def test_edit_config_reports_read_and_write_errors(env, monkeypatch, capsys):
    cfg = {"config_path": str(env.tmp / "missing.toml")}
    with pytest.raises(SystemExit) as exc:
        cli.edit_config(cfg, lambda t: t, lambda d: None)
    assert exc.value.code == 1 and "cannot read" in capsys.readouterr().err

    def deny(path, text):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(cli, "write_atomic", deny)
    with pytest.raises(SystemExit) as exc:
        cli.edit_config({"config_path": str(env.cfg)}, lambda t: t, lambda d: None)
    assert exc.value.code == 1 and f"cannot write {env.cfg}" in capsys.readouterr().err


def test_unknown_user_in_cli_args(env, monkeypatch, capsys):
    code, _, err = hive(monkeypatch, capsys, "roots", "add", "~nosuchuser_hive/x")
    assert code == 2 and "unknown user" in err
    code, _, err = hive(monkeypatch, capsys, "spawn", "~nosuchuser_hive/x")
    assert code == 2 and "unknown user" in err


# --- R7 ----------------------------------------------------------------------

class FakeTmux:
    def __init__(self):
        self.calls = []

    def __call__(self, cfg, *args, check=True):
        self.calls.append(args)
        rc = 1 if args[0] == "has-session" else 0
        return SimpleNamespace(returncode=rc, stdout="%1\n", stderr="")


@pytest.mark.parametrize("exc", [SystemExit(1), KeyboardInterrupt(), RuntimeError("boom")])
def test_up_removes_the_half_built_session_on_any_failure(env, monkeypatch, capsys, exc):
    fake = FakeTmux()
    monkeypatch.setattr(cli, "tmux", fake)

    def failing(cfg, **kw):
        raise exc
    monkeypatch.setattr(cli, "reconcile", failing)
    cfg = cli.load_config()
    with pytest.raises(type(exc)):
        cli.cmd_up(cfg, None)
    assert ("kill-session", "-t", "=t-agents") in fake.calls
    assert "removed session t-agents" in capsys.readouterr().err


def test_materialize_queen_dies_on_oserror(env, capsys):
    cfg = cli.load_config()
    qdir = env.xdg / "hive" / "queen"
    qdir.write_text("a file, not a dir")
    with pytest.raises(SystemExit) as exc:
        cli.materialize_queen(cfg)
    assert exc.value.code == 1 and "cannot write the queen dir" in capsys.readouterr().err


# --- R8 ----------------------------------------------------------------------

def test_discover_times_out(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "DISCOVER_TIMEOUT", 0.3)
    env.cfg.write_text(BASE + '\n[queen]\ndiscover = "sleep 5"\n')
    code, _, err = hive(monkeypatch, capsys, "spawn", "x", "--sandbox", "n")
    assert code == 1 and "discover command timed out" in err


def test_sandbox_discover_runs_before_the_lock(env, monkeypatch, capsys):
    order = []
    monkeypatch.setattr(cli, "launcher_record", lambda cfg, name: order.append("discover"))
    monkeypatch.setattr(cli, "hold_lock", lambda **_: order.append("lock"))
    monkeypatch.setattr(cli, "cmd_spawn", lambda cfg, args: 0)
    env.cfg.write_text(BASE + '\n[queen]\ndiscover = "true"\n')
    assert hive(monkeypatch, capsys, "spawn", "x", "--sandbox", "n")[0] == 0
    assert order == ["discover", "lock"]


# --- R12 ---------------------------------------------------------------------

def test_status_survives_a_tab_in_the_pane_title(env, monkeypatch, capsys):
    class Tmux(FakeTmux):
        def __call__(self, cfg, *args, check=True):
            if args[0] == "list-panes":
                return SimpleNamespace(returncode=0, stderr="",
                                       stdout="0\twin\t0\tsleep\ta\ttitle\twith\ttabs\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(cli, "tmux", Tmux())
    code, out, _ = hive(monkeypatch, capsys, "status")
    assert code == 0 and "pane 0: a\ttitle\twith\ttabs  (sleep)" in out


# --- R11, U9: the service unit -----------------------------------------------

def test_install_service_without_systemctl_writes_nothing(env, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(env.tmp / "empty"))
    code, _, err = hive(monkeypatch, capsys, "install-service")
    assert code == 1 and "systemctl not found" in err
    assert not (env.home / ".config" / "systemd").exists()
    assert not (env.xdg / "systemd").exists()


def test_install_service_path_env_and_next_steps(env, monkeypatch, capsys):
    log = fake_bin(env.tmp, monkeypatch, "systemctl")
    code, out, _ = hive(monkeypatch, capsys, "install-service")
    unit = env.home / ".config" / "systemd" / "user" / "hive.service"
    assert code == 0 and unit.exists() and not (env.xdg / "systemd").exists()
    text = unit.read_text()
    assert f"Environment=XDG_CONFIG_HOME={env.xdg}\n" in text
    assert "systemctl --user enable --now hive.service" in out
    assert "loginctl enable-linger $USER" in out
    assert "--user daemon-reload" in log.read_text()
    # the same unit path is used to uninstall; a second time it is a no-op
    assert hive(monkeypatch, capsys, "uninstall-service")[0] == 0
    assert not unit.exists()
    code, out, _ = hive(monkeypatch, capsys, "uninstall-service")
    assert code == 0 and "not installed" in out


def test_install_service_default_xdg_adds_no_environment_and_enable_skips_hint(
        env, monkeypatch, capsys):
    fake_bin(env.tmp, monkeypatch, "systemctl")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(env.home / ".config"))
    cfg = env.home / ".config" / "hive" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(BASE)
    code, out, _ = hive(monkeypatch, capsys, "install-service", "--enable")
    unit = env.home / ".config" / "systemd" / "user" / "hive.service"
    assert code == 0 and "XDG_CONFIG_HOME" not in unit.read_text() and "next:" not in out


def test_unit_exe_is_escaped_for_systemd(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/my tools/100%/hive")
    assert cli.service_exe() == '"/opt/my tools/100%%/hive"'
    assert cli.unit_quote("/usr/bin/hive") == "/usr/bin/hive"
    assert cli.unit_quote("a$b") == "a$$b"


# --- U1, R15: warnings when panes start ---------------------------------------

def pane(command):
    return SimpleNamespace(name="t-a-1", type="a", index=1, dir="/tmp", command=command)


def test_preflight_warns_about_missing_program_and_remote_control(env, monkeypatch, capsys):
    cfg = cli.load_config()
    cli.preflight(cfg, pane("nosuchprogram-hive FOO {name}"))
    err = capsys.readouterr().err
    assert "hive: warning: t-a-1: command has no --remote-control" in err
    assert "'nosuchprogram-hive' not found in PATH" in err
    cli.preflight(cfg, pane("FOO=1 sh {name} --remote-control"))
    assert capsys.readouterr().err == ""


def test_preflight_is_not_run_by_list(env, monkeypatch, capsys):
    env.cfg.write_text(BASE.replace("--remote-control", "") + '\n[[agents]]\ntype = "a"\ndir = "/tmp"\n')
    code, _, err = hive(monkeypatch, capsys, "list")
    assert code == 0 and "warning" not in err


# --- U3, U4: reconcile summary and untrusted warning ---------------------------

def test_reconcile_summarizes_skipped_agents(env, monkeypatch, capsys):
    fake = FakeTmux()
    monkeypatch.setattr(cli, "tmux", fake)
    monkeypatch.setattr(cli, "running_panes", lambda cfg: ({}, []))
    monkeypatch.setattr(cli, "session_running", lambda cfg: True)
    monkeypatch.setattr(cli, "warn_untrusted", lambda cfg, p: None)
    monkeypatch.setattr(cli, "start_pane", lambda *a: "%2")
    monkeypatch.setattr(cli, "window_ids", lambda cfg: {})
    monkeypatch.setattr(cli, "materialize_queen", lambda cfg: None)
    env.cfg.write_text(BASE + f'\n[[agents]]\ntype = "gone"\ndir = "{env.tmp / "gone"}"\ncount = 2\n')
    assert cli.reconcile(cli.load_config()) == 0
    err = capsys.readouterr().err
    assert "hive: warning: 1 agent(s) skipped: dir missing; see 'hive list'" in err


def test_untrusted_warning_has_prefix_and_remedy(env, capsys):
    cfg = cli.load_config()
    cli.warn_untrusted(cfg, pane("x").__class__(name="t-queen", type="queen", index=1,
                                                dir=str(env.tmp), command="x", config_dir=None))
    err = capsys.readouterr().err
    assert err.startswith(f"hive: warning: untrusted t-queen {env.tmp}")
    assert "trust prompt" in err and "--trust" in err


# --- U2, U5, U7, U8, R23 ---------------------------------------------------------

def test_init_prints_session_and_socket(env, monkeypatch, capsys):
    env.cfg.unlink()
    code, out, _ = hive(monkeypatch, capsys, "init")
    assert code == 0 and "session=" in out and "socket=hive" in out and "tmux_socket" in out


def test_stop_unknown_type_has_its_own_message(env, monkeypatch, capsys):
    code, _, err = hive(monkeypatch, capsys, "stop", "nope")
    assert code == 2 and "no agent type 'nope'; 'hive list' shows the types" in err
    assert "search_roots" not in err


def test_spawn_invalid_type_suggests_a_type(env, monkeypatch, capsys):
    proj = env.tmp / "my proj:x"
    proj.mkdir()
    code, _, err = hive(monkeypatch, capsys, "spawn", str(proj))
    assert code == 2 and "--type my-proj-x" in err


def test_help_texts(monkeypatch, capsys):
    out = hive(monkeypatch, capsys, "--help")[1]
    assert "exit codes:" in out


def test_queen_flags_are_added_once():
    base = "claude -n {name} --remote-control {name}"
    line = cli.queen_command_line(base)
    assert line == f"{base} --tools {cli.QUEEN_TOOLS} --strict-mcp-config"
    assert cli.queen_command_line(line) == line
    own = cli.queen_command_line("c --tools Read --strict-mcp-config")
    assert own == "c --tools Read --strict-mcp-config"
    assert cli.queen_command_line("c --tools=Read").count("--tools") == 1


def test_queen_tools_match_guard():
    """The queen's --tools list and the guard's allowlist must name the same tools."""
    import importlib.util
    from pathlib import Path
    path = Path(cli.__file__).parent / "queen" / "hooks" / "guard.py"
    spec = importlib.util.spec_from_file_location("hive_guard", path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    assert set(cli.QUEEN_TOOLS.split(",")) == guard.QUEEN_TOOLS
