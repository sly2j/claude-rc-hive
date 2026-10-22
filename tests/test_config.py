import pytest

from hive import cli

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


def test_1_5_config_loads_with_queen_defaults(write, tmp_path):
    write(BASE.replace('prefix = "t"\n', 'prefix = "swarm"\nsession = "swarm-agents"\n')
          + f'\n[env]\n# X = "y"\n\n[service]\nafter = ["a.service"]\n\n'
          f'[[agents]]\ntype = "obsidian"\ndir = "{tmp_path}"\ncount = 4\n')
    cfg = cli.load_config()
    assert cfg["queen"] == {"type": "queen", "dir": str(tmp_path / "hive" / "queen"),
                            "command": "claude -n {name} --remote-control {name}", "env": {},
                            "config_dir": None}
    assert cfg["queen"]["command"] == cfg["agents"][0]["command"]
    assert cfg["search_roots"] == []
    assert cfg["discover"] is None
    assert cfg["config_path"] == str(tmp_path / "hive" / "config.toml")


def test_queen_table_and_search_roots(write, tmp_path):
    write(BASE + '\n[queen]\ncommand = "q {name} --remote-control"\ndiscover = "echo hi"\n')
    cfg = cli.load_config()
    assert cfg["queen"]["command"] == "q {name} --remote-control"
    assert cfg["discover"] == "echo hi"


def test_search_roots_expanded(write):
    write('search_roots = ["~/work", "/srv"]\n' + BASE)
    roots = cli.load_config()["search_roots"]
    assert roots[1] == "/srv"
    assert roots[0].endswith("/work") and "~" not in roots[0]


def test_search_roots_bad_type(write, capsys):
    write('search_roots = "~/work"\n' + BASE)
    fails(capsys, "search_roots must be a list of strings")
    write('search_roots = [1]\n' + BASE)
    fails(capsys, "search_roots must be a list of strings")


def test_queen_not_a_table(write, capsys):
    write('queen = "x"\n' + BASE)
    fails(capsys, "'queen' must be dict")


def test_queen_unknown_key(write, capsys):
    write(BASE + '\n[queen]\ncolour = "red"\n')
    fails(capsys, "unknown key 'colour' in [queen]")


def test_queen_discover_must_be_str(write, capsys):
    write(BASE + "\n[queen]\ndiscover = 3\n")
    fails(capsys, "'queen.discover' must be str")


def test_queen_command_must_be_str(write, capsys):
    write(BASE + "\n[queen]\ncommand = 3\n")
    fails(capsys, "'queen.command' must be str")


def test_queen_command_falls_back_to_builtin(write):
    write('prefix = "t"\n')
    assert cli.load_config()["queen"]["command"] == cli.DEFAULT_QUEEN_COMMAND


def test_queen_command_without_defaults(write):
    write('prefix = "t"\n[queen]\ncommand = "q"\n')
    assert cli.load_config()["queen"]["command"] == "q"


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
    fails(capsys, "agents[1].config_dir 'rel' must be absolute or start with ~")


def test_per_type_env_value_must_be_str(write, capsys, tmp_path):
    write(BASE + f'\n[[agents]]\ntype = "a"\ndir = "{tmp_path}"\nenv = {{ X = 1 }}\n')
    fails(capsys, "agents[1].env.X must be a string")
    write(BASE + '\n[queen]\nenv = "x"\n')
    fails(capsys, "'queen.env' must be dict")


def test_init_writes_a_config_that_up_accepts(tmp_path, monkeypatch, capsys):
    # A fresh deploy: no project dirs exist, so the example must not reference any.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cli.cmd_init(None) == 0
    cfg = cli.load_config()
    assert cfg["agents"] == []
    assert cfg["queen"]["command"] == cli.DEFAULT_QUEEN_COMMAND
    assert [p.name for p in cli.desired_panes(cfg)] == [f"{cfg['prefix']}-queen"]


def test_env_names_are_checked(write, capsys):
    write(BASE + '\n[env]\n"A B" = "x"\n')
    fails(capsys, "env.A B: env names must match")


def test_missing_agent_dir_is_not_a_load_error(write, tmp_path):
    write(BASE + f'\n[[agents]]\ntype = "gone"\ndir = "{tmp_path / "gone"}"\n')
    assert cli.load_config()["agents"][0]["type"] == "gone"
