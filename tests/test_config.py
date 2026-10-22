import sys

import pytest

from hive import cli

BASE = """\
prefix = "t"

[defaults]
command = "claude -n {name} --remote-control {name}"
"""


QUEEN_FLAGS = f" --tools {cli.QUEEN_TOOLS} --strict-mcp-config"


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


def test_1_5_config_loads_with_queen_defaults(write, tmp_path):
    write(BASE.replace('prefix = "t"\n', 'prefix = "swarm"\nsession = "swarm-agents"\n')
          + f'\n[env]\n# X = "y"\n\n[service]\nafter = ["a.service"]\n\n'
          f'[[agents]]\ntype = "obsidian"\ndir = "{tmp_path}"\ncount = 4\n')
    cfg = cli.load_config()
    assert cfg["queen"] == {"type": "queen", "dir": str(tmp_path / "hive" / "queen"),
                            "command": "claude -n {name} --remote-control {name}" + QUEEN_FLAGS,
                            "args": [], "env": {},
                            "config_dir": None}
    assert cfg["queen"]["command"] == cfg["agents"][0]["command"] + QUEEN_FLAGS
    assert cfg["search_roots"] == []
    assert cfg["discover"] is None
    assert cfg["config_path"] == str(tmp_path / "hive" / "config.toml")


def test_queen_table_and_search_roots(write, tmp_path):
    write(BASE + '\n[queen]\ncommand = "q {name} --remote-control"\ndiscover = "echo hi"\n')
    cfg = cli.load_config()
    assert cfg["queen"]["command"] == "q {name} --remote-control" + QUEEN_FLAGS
    assert cfg["discover"] == "echo hi"


def test_search_roots_expanded(write):
    write('search_roots = ["~/work", "/srv"]\n' + BASE)
    roots = cli.load_config()["search_roots"]
    assert roots[1] == "/srv"
    assert roots[0].endswith("/work") and "~" not in roots[0]


def test_search_roots_bad_type(write, capsys):
    write('search_roots = "~/work"\n' + BASE)
    fails(capsys, "search_roots must be an array of strings")
    write('search_roots = [1]\n' + BASE)
    fails(capsys, "search_roots must be an array of strings")


def test_queen_not_a_table(write, capsys):
    write('queen = "x"\n' + BASE)
    fails(capsys, "'queen' must be a table")


def test_queen_unknown_key(write, capsys):
    write(BASE + '\n[queen]\ncolour = "red"\n')
    fails(capsys, "unknown key 'colour' in [queen]")


def test_queen_discover_must_be_str(write, capsys):
    write(BASE + "\n[queen]\ndiscover = 3\n")
    fails(capsys, "'queen.discover' must be a string")


def test_queen_command_must_be_str(write, capsys):
    write(BASE + "\n[queen]\ncommand = 3\n")
    fails(capsys, "'queen.command' must be a string")


def test_queen_command_falls_back_to_builtin(write):
    write('prefix = "t"\n')
    assert cli.load_config()["queen"]["command"] == cli.DEFAULT_QUEEN_COMMAND + QUEEN_FLAGS


def test_queen_command_without_defaults(write):
    write('prefix = "t"\n[queen]\ncommand = "q"\n')
    assert cli.load_config()["queen"]["command"] == "q" + QUEEN_FLAGS


def test_reserved_queen_type(write, capsys, tmp_path):
    write(BASE + f'\n[[agents]]\ntype = "queen"\ndir = "{tmp_path}"\n')
    fails(capsys, "agents[1].type 'queen' is reserved for the hive queen")


def test_count_zero_loads(write, tmp_path):
    write(BASE + f'\n[[agents]]\ntype = "a"\ndir = "{tmp_path}"\ncount = 0\n')
    assert cli.load_config()["agents"][0]["count"] == 0


def test_count_negative(write, capsys, tmp_path):
    write(BASE + f'\n[[agents]]\ntype = "a"\ndir = "{tmp_path}"\ncount = -1\n')
    fails(capsys, "agents[1].count must be >= 0")


def test_empty_agents_loads(write):
    write(BASE)
    assert cli.load_config()["agents"] == []
    write("agents = []\n" + BASE)
    assert cli.load_config()["agents"] == []


def test_all_zero_counts_load(write, tmp_path):
    write(BASE + f'\n[[agents]]\ntype = "a"\ndir = "{tmp_path}"\ncount = 0\n'
          f'\n[[agents]]\ntype = "b"\ndir = "{tmp_path}"\ncount = 0\n')
    assert [a["count"] for a in cli.load_config()["agents"]] == [0, 0]


def test_per_type_env_and_config_dir(write, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    write(BASE + '\n[env]\nA = "g"\nB = "g"\n\n[queen]\nenv = { Q = "1" }\n'
          'config_dir = "~/.claude-q"\n'
          f'\n[[agents]]\ntype = "a"\ndir = "{tmp_path}"\nenv = {{ B = "t", C = "$HOME/x" }}\n'
          'config_dir = "/sandbox/cc"\n')
    cfg = cli.load_config()
    assert cfg["queen"]["env"] == {"Q": "1"}
    assert cfg["queen"]["config_dir"] == str(tmp_path / ".claude-q")
    agent = cfg["agents"][0]
    assert agent["env"] == {"B": "t", "C": "$HOME/x"}
    assert agent["config_dir"] == "/sandbox/cc"
    line = cli.shell_line(cfg, "/d", "run", agent["env"])
    assert line == ('export A="g" && export B="g" && export B="t" && export C="$HOME/x"'
                    ' && cd /d && run')
    # config_dir only locates the trust file; it is never exported
    assert "CLAUDE_CONFIG_DIR" not in line


def test_per_type_env_rejects_claude_config_dir(write, capsys, tmp_path):
    write(BASE + f'\n[[agents]]\ntype = "a"\ndir = "{tmp_path}"\n'
          'env = { CLAUDE_CONFIG_DIR = "/x" }\n')
    fails(capsys, "agents[1].env.CLAUDE_CONFIG_DIR: hive never sets CLAUDE_CONFIG_DIR")
    write(BASE + '\n[queen]\nenv = { CLAUDE_CONFIG_DIR = "/x" }\n')
    fails(capsys, "queen.env.CLAUDE_CONFIG_DIR")
    write(BASE + '\n[env]\nCLAUDE_CONFIG_DIR = "/x"\n')
    fails(capsys, "env.CLAUDE_CONFIG_DIR: hive never sets CLAUDE_CONFIG_DIR")


def test_config_dir_must_be_absolute(write, capsys, tmp_path):
    write(BASE + f'\n[[agents]]\ntype = "a"\ndir = "{tmp_path}"\nconfig_dir = "rel"\n')
    fails(capsys, "agents[1].config_dir 'rel' must be an absolute path or start with ~")


def test_per_type_env_value_must_be_str(write, capsys, tmp_path):
    write(BASE + f'\n[[agents]]\ntype = "a"\ndir = "{tmp_path}"\nenv = {{ X = 1 }}\n')
    fails(capsys, "agents[1].env.X must be a string")
    write(BASE + '\n[queen]\nenv = "x"\n')
    fails(capsys, "'queen.env' must be a table")


def test_init_writes_a_config_that_up_accepts(tmp_path, monkeypatch, capsys):
    # A fresh deploy: no project dirs exist, so the example must not reference any.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cli.cmd_init(None) == 0
    cfg = cli.load_config()
    assert cfg["agents"] == []
    assert cfg["queen"]["command"] == cli.DEFAULT_QUEEN_COMMAND + QUEEN_FLAGS
    assert [p.name for p in cli.desired_panes(cfg)] == [f"hive-queen-{cfg['prefix']}"]
    assert cfg["launchers"] == [] and cfg["default_launcher"] is None


def test_env_names_are_checked(write, capsys):
    write(BASE + '\n[env]\n"A B" = "x"\n')
    fails(capsys, "env.A B: env names must match")


def test_missing_agent_dir_is_not_a_load_error(write, tmp_path):
    write(BASE + f'\n[[agents]]\ntype = "gone"\ndir = "{tmp_path / "gone"}"\n')
    assert cli.load_config()["agents"][0]["type"] == "gone"


def test_parse_name_hyphenated_type():
    assert cli.parse_name("t", "t-x-t-1") == ("t-x", 1)
    assert cli.parse_name("t", "hive-queen-t") == ("queen", 1)
    # a user type `hive` does not collide with the queen
    assert cli.parse_name("t", "hive-t-1") == ("hive", 1)
    # the queen is matched exactly, never by prefix
    assert cli.parse_name("gpu07", "hive-queen-gpu07") == ("queen", 1)
    assert cli.parse_name("gpu07", "hive-queen-gpu07-1") != ("queen", 1)
    # numeric and dotted prefixes
    assert cli.parse_name("1", "a-1-2") == ("a", 2)
    assert cli.parse_name("1", "hive-queen-1") == ("queen", 1)
    assert cli.parse_name("g.1-x", "a-g.1-x-3") == ("a", 3)
    assert cli.parse_name("t", "a-u-1") is None
    assert cli.parse_name("t", "-t-1") is None


def test_parse_legacy_name():
    assert cli.parse_legacy_name("t", "t-queen") == ("queen", 1)
    assert cli.parse_legacy_name("t", "t-a-2") == ("a", 2)
    assert cli.parse_legacy_name("t", "t-b-api-1") == ("b-api", 1)
    assert cli.parse_legacy_name("t", "x-a-1") is None


def test_hive_type_names_distinct_from_queen(write, tmp_path):
    write(BASE + f'\n[[agents]]\ntype = "hive"\ndir = "{tmp_path}"\n')
    names = [p.name for p in cli.desired_panes(cli.load_config())]
    assert names == ["hive-queen-t", "hive-t-1"]


def test_hive_queen_type_reserved(write, capsys, tmp_path):
    write(BASE + f'\n[[agents]]\ntype = "hive-queen"\ndir = "{tmp_path}"\n')
    fails(capsys, "type 'hive-queen' is reserved for the hive queen")


def test_bad_prefix(write, capsys):
    write('prefix = "a b"\n')
    fails(capsys, "prefix")


LAUNCHERS = BASE + """
[[launchers]]
name = "a"
command = "claude -n {name} --remote-control {name}"

[[launchers]]
name = "b"
command = "cb -n {name} --remote-control {name}"
config_dir = "/cc/b"
description = "the b one"
"""


def run_hive(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["hive", *argv])
    try:
        code = cli.main()
    except SystemExit as e:
        code = e.code
    out, err = capsys.readouterr()
    return code, out.splitlines(), err


def test_launchers_parsed(write):
    write(LAUNCHERS)
    cfg = cli.load_config()
    assert [l["name"] for l in cfg["launchers"]] == ["a", "b"]
    assert cfg["launchers"][1]["config_dir"] == "/cc/b"
    assert cfg["launchers"][1]["description"] == "the b one"
    assert cfg["launchers"][0]["config_dir"] is None
    assert cfg["default_launcher"] is None


def test_launcher_bad_name(write, capsys):
    write(BASE + '\n[[launchers]]\nname = "Bad Name"\ncommand = "c {name} --remote-control"\n')
    fails(capsys, "launchers[1].name 'Bad Name' must match")


def test_launcher_duplicate_name(write, capsys):
    block = '\n[[launchers]]\nname = "x"\ncommand = "c {name} --remote-control"\n'
    write(BASE + block + block)
    fails(capsys, "duplicate launcher name 'x'")


def test_launcher_command_needs_placeholders(write, capsys):
    write(BASE + '\n[[launchers]]\nname = "x"\ncommand = "c --remote-control"\n')
    fails(capsys, "launchers[1].command must contain {name} and --remote-control")
    write(BASE + '\n[[launchers]]\nname = "x"\ncommand = "c {name}"\n')
    fails(capsys, "must contain {name} and --remote-control")
    write(BASE + '\n[[launchers]]\nname = "x"\ncommand = "c {name} --remote-control"\nbogus = 1\n')
    fails(capsys, "bogus")


def test_default_launcher_unknown_registry_ok(write):
    write(BASE.replace("[defaults]\n", '[defaults]\nlauncher = "elsewhere"\n'))
    assert cli.load_config()["default_launcher"] == "elsewhere"


def test_launchers_list_default_first(write, monkeypatch, capsys):
    write(LAUNCHERS.replace("[defaults]\n", '[defaults]\nlauncher = "b"\n'))
    for argv in (("launchers",), ("launchers", "list")):
        code, out, _ = run_hive(monkeypatch, capsys, *argv)
        assert code == 0
        assert out[0].startswith('launcher name="b" source=config default=yes config_dir="/cc/b" ')
        assert out[0].endswith('description="the b one"')
        assert out[1].startswith('launcher name="a" source=config default=no config_dir="default" ')


def discover_config(tmp_path, script):
    path = tmp_path / "discover.sh"
    path.write_text(f"#!/bin/sh\n{script}\n")
    path.chmod(0o755)
    return f'\n[queen]\ndiscover = "{path}"\n'


def test_launchers_list_includes_discover(write, tmp_path, monkeypatch, capsys):
    queen = discover_config(
        tmp_path, "echo 'name=x config_dir=/cc/x command=\"d {name} --remote-control\"'\n"
                  "echo 'name=a config_dir=/cc/a command=\"d2 {name} --remote-control\"'")
    write(LAUNCHERS + queen)
    code, out, _ = run_hive(monkeypatch, capsys, "launchers")
    assert code == 0
    assert [l.split()[1] for l in out] == ['name="a"', 'name="b"', 'name="x"', 'name="a"']
    assert "source=discover" in out[2] and "shadowed=yes" not in out[2]
    assert "source=discover" in out[3] and out[3].endswith("shadowed=yes")


def test_launchers_list_discover_error(write, tmp_path, monkeypatch, capsys):
    write(LAUNCHERS + discover_config(tmp_path, "echo boom >&2; exit 3"))
    code, out, _ = run_hive(monkeypatch, capsys, "launchers", "list")
    assert code == 0
    assert [l.split()[0] for l in out] == ["launcher", "launcher", "discover"]
    assert out[2].startswith('discover error="discover command failed (exit 3): boom')


def make_launcher(directory, name="mycl"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


CMD = "{} -n {{name}} --remote-control {{name}}"


def test_launchers_add_ok(write, tmp_path, monkeypatch, capsys):
    path = write(BASE)
    tool = make_launcher(tmp_path / "bin")
    code, out, _ = run_hive(monkeypatch, capsys, "launchers", "add", "mine", CMD.format(tool),
                            "--config-dir", "/cc/mine", "--description", "my one")
    assert code == 0 and out[0].startswith('launcher added name="mine" default=yes')
    cfg = cli.load_config()
    assert cfg["default_launcher"] == "mine"
    assert cfg["launchers"][0]["config_dir"] == "/cc/mine"
    code, out, _ = run_hive(monkeypatch, capsys, "launchers", "add", "second", CMD.format(tool))
    assert code == 0 and "default=no" in out[0]
    assert cli.load_config()["default_launcher"] == "mine"
    code, out, _ = run_hive(monkeypatch, capsys, "launchers")
    assert [l.split()[1] for l in out] == ['name="mine"', 'name="second"']
    assert path.read_text().startswith(BASE.replace("[defaults]\n", '[defaults]\nlauncher = "mine"\n'))


def test_launchers_add_symlink_outside_roots(write, tmp_path, monkeypatch, capsys):
    write(BASE)
    target = make_launcher(tmp_path / "elsewhere", "real-claude")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").symlink_to(target)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    code, out, err = run_hive(monkeypatch, capsys, "launchers", "add", "viapath",
                              "claude -n {name} --remote-control {name}")
    assert code == 0, err
    assert [l["name"] for l in cli.load_config()["launchers"]] == ["viapath"]


def test_launchers_add_duplicate(write, tmp_path, monkeypatch, capsys):
    queen = discover_config(
        tmp_path, "echo 'name=x config_dir=/cc/x command=\"d {name} --remote-control\"'")
    path = write(LAUNCHERS + queen)
    before = path.read_text()
    tool = make_launcher(tmp_path / "bin")
    for name in ("a", "x"):
        code, _, err = run_hive(monkeypatch, capsys, "launchers", "add", name, CMD.format(tool))
        assert code == 2 and "already exists" in err
    assert path.read_text() == before


def test_launchers_add_not_found(write, tmp_path, monkeypatch, capsys):
    path = write(BASE)
    before = path.read_text()
    code, _, err = run_hive(monkeypatch, capsys, "launchers", "add", "ghost",
                            CMD.format(tmp_path / "nope" / "claude"))
    assert code == 2 and "not found" in err
    assert path.read_text() == before


def test_launchers_add_private_refused(write, tmp_path, monkeypatch, capsys):
    path = write(BASE)
    before = path.read_text()
    tool = make_launcher(tmp_path / "private" / "bin")
    code, _, err = run_hive(monkeypatch, capsys, "launchers", "add", "secret", CMD.format(tool))
    assert code == 3 and "refused" in err
    assert path.read_text() == before


def test_launchers_default_set(write, tmp_path, monkeypatch, capsys):
    queen = discover_config(
        tmp_path, "echo 'name=x config_dir=/cc/x command=\"d {name} --remote-control\"'")
    write(LAUNCHERS + queen)
    for name in ("b", "x"):
        code, out, _ = run_hive(monkeypatch, capsys, "launchers", "default", name)
        assert code == 0 and out[0].startswith(f'default launcher="{name}"')
        assert cli.load_config()["default_launcher"] == name


def test_launchers_default_unknown(write, monkeypatch, capsys):
    path = write(LAUNCHERS)
    before = path.read_text()
    code, _, err = run_hive(monkeypatch, capsys, "launchers", "default", "nope")
    assert code == 2 and "available: a, b" in err
    assert path.read_text() == before
