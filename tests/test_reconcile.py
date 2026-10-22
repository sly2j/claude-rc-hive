import pytest

from hive.reconcile import (Existing, New, Pane, ResolveError, RunningPane,
                            desired_panes, parse_records, plan, resolve_target,
                            sandbox_type)


def make_cfg(agents, roots=(), prefix="swarm"):
    return {
        "prefix": prefix,
        "queen": {"type": "queen", "dir": "/q", "command": "claude {name}"},
        "agents": [{"type": t, "dir": d, "count": c, "command": "run {name}"}
                   for t, d, c in agents],
        "search_roots": [str(r) for r in roots],
    }


def running(window, i, prefix="swarm"):
    name = f"{prefix}-{window}-{i}"
    return RunningPane(f"%{i}", "@1", f"{prefix}-{window}", name, window, i, False)


# desired_panes / plan

def test_desired_panes_order_and_counts():
    cfg = make_cfg([("a", "/a", 2), ("b", "/b", 0), ("c", "/c", 1)])
    panes = desired_panes(cfg)
    assert [p.name for p in panes] == [
        "swarm-queen", "swarm-a-1", "swarm-a-2", "swarm-c-1"]
    assert panes[0] == Pane("swarm-queen", "swarm-queen", "queen", 1, "/q", "claude {name}")
    assert panes[2] == Pane("swarm-a-2", "swarm-a", "a", 2, "/a", "run {name}")


def test_desired_panes_all_zero_is_queen_only():
    assert [p.name for p in desired_panes(make_cfg([("a", "/a", 0)]))] == ["swarm-queen"]


def test_plan_starts_missing_middle_indices_in_order():
    desired = desired_panes(make_cfg([("a", "/a", 3), ("b", "/b", 1)]))
    queen = RunningPane("%0", "@0", "swarm-queen", "swarm-queen", "queen", 1, False)
    run = {r.name: r for r in (queen, running("a", 1), running("a", 3))}
    to_start, to_stop = plan(desired, run, "swarm-queen")
    assert [p.name for p in to_start] == ["swarm-a-2", "swarm-b-1"]
    assert to_stop == []


def test_plan_stops_highest_index_first_and_never_the_queen():
    desired = desired_panes(make_cfg([("a", "/a", 1)]))
    panes = [running("a", i) for i in (1, 2, 3, 4)] + [running("b", 1), running("b", 2)]
    queen = RunningPane("%0", "@0", "swarm-queen", "swarm-queen", "queen", 1, False)
    run = {r.name: r for r in panes + [queen]}
    to_start, to_stop = plan(desired, run, "swarm-queen")
    assert to_start == []
    assert [r.name for r in to_stop] == [
        "swarm-a-4", "swarm-a-3", "swarm-a-2", "swarm-b-2", "swarm-b-1"]


def test_plan_queen_not_stopped_even_if_not_desired():
    queen = RunningPane("%0", "@0", "swarm-queen", "swarm-queen", "queen", 1, False)
    to_start, to_stop = plan([], {"swarm-queen": queen}, "swarm-queen")
    assert to_start == [] and to_stop == []


# resolve_target

QUEEN_MSG = "the queen is always running; use 'hive down' to stop the whole hive"


@pytest.fixture
def dirs(tmp_path):
    for n in ("work/obs", "work/proj", "other/proj", "elsewhere/x"):
        (tmp_path / n).mkdir(parents=True)
    return tmp_path


def test_rule1_queen(dirs):
    cfg = make_cfg([])
    for kwargs in ({}, {"type_override": "x"}):
        with pytest.raises(ResolveError, match="queen is always running") as e:
            resolve_target(cfg, "queen", **kwargs)
        assert str(e.value) == QUEEN_MSG
    with pytest.raises(ResolveError, match="queen is always running"):
        resolve_target(cfg, str(dirs / "work/obs"), "queen")


def test_rule3_existing_type(dirs):
    cfg = make_cfg([("obs", str(dirs / "work/obs"), 1)])
    assert resolve_target(cfg, "obs") == Existing(cfg["agents"][0])


def test_rule4_path_matches_existing_dir(dirs):
    cfg = make_cfg([("notes", str(dirs / "work/obs"), 1)])
    assert resolve_target(cfg, str(dirs / "work/obs")) == Existing(cfg["agents"][0])


def test_rule4_symlinked_dir_canonicalized(dirs):
    link = dirs / "link"
    link.symlink_to(dirs / "work/obs")
    cfg = make_cfg([("notes", str(dirs / "work/obs"), 1)])
    assert resolve_target(cfg, str(link)) == Existing(cfg["agents"][0])
    # entry dir stored as a symlink also matches the real path
    cfg = make_cfg([("notes", str(link), 1)])
    assert resolve_target(cfg, str(dirs / "work/obs")) == Existing(cfg["agents"][0])


def test_rule4_new_absolute(dirs):
    cfg = make_cfg([])
    real = str((dirs / "work/proj").resolve())
    assert resolve_target(cfg, str(dirs / "work/proj")) == New("proj", real, real)


def test_rule4_tilde_keeps_stored_text(dirs, monkeypatch):
    monkeypatch.setenv("HOME", str(dirs))
    cfg = make_cfg([])
    real = str((dirs / "work/proj").resolve())
    assert resolve_target(cfg, "~/work/proj") == New("proj", real, "~/work/proj")


def test_rule4_relative_and_missing(dirs):
    cfg = make_cfg([])
    for t in ("./x", "../x", "sub/dir"):
        with pytest.raises(ResolveError) as e:
            resolve_target(cfg, t)
        assert str(e.value) == f"'{t}' is relative; give an absolute path or ~/..."
    missing = str(dirs / "nope")
    with pytest.raises(ResolveError) as e:
        resolve_target(cfg, missing)
    assert str(e.value) == f"'{missing}' is not a directory"


def test_rule4_basename_collision(dirs):
    cfg = make_cfg([("proj", str(dirs / "work/proj"), 1)])
    with pytest.raises(ResolveError) as e:
        resolve_target(cfg, str(dirs / "other/proj"))
    assert str(e.value) == (f"type 'proj' already uses '{dirs / 'work/proj'}'; "
                            "give --type")


def test_rule5_search_roots_hit(dirs):
    cfg = make_cfg([], roots=[dirs / "work", dirs / "missing"])
    real = str((dirs / "work/obs").resolve())
    assert resolve_target(cfg, "obs") == New("obs", real, real)


def test_rule5_hit_matching_existing_dir(dirs):
    cfg = make_cfg([("notes", str(dirs / "work/obs"), 1)], roots=[dirs / "work"])
    assert resolve_target(cfg, "obs") == Existing(cfg["agents"][0])


def test_rule5_ambiguous(dirs):
    cfg = make_cfg([], roots=[dirs / "work", dirs / "other"])
    with pytest.raises(ResolveError) as e:
        resolve_target(cfg, "proj")
    assert str(e.value) == (f"ambiguous: proj matches {dirs / 'work/proj'}, "
                            f"{dirs / 'other/proj'}; give the full path")


def test_rule5_miss(dirs):
    cfg = make_cfg([], roots=[dirs / "work", dirs / "other"])
    with pytest.raises(ResolveError) as e:
        resolve_target(cfg, "zzz")
    assert str(e.value) == (f"no agent type 'zzz' and no directory 'zzz' under "
                            f"search_roots [{dirs / 'work'}, {dirs / 'other'}]")
    with pytest.raises(ResolveError) as e:
        resolve_target(make_cfg([]), "zzz")
    assert str(e.value) == ("no agent type 'zzz' and no directory 'zzz' "
                            "and search_roots is not set")


def test_type_override_existing_entry(dirs):
    cfg = make_cfg([("obs", str(dirs / "work/obs"), 1)], roots=[dirs / "work"])
    entry = cfg["agents"][0]
    assert resolve_target(cfg, "obs", "obs") == Existing(entry)
    assert resolve_target(cfg, str(dirs / "work/obs"), "obs") == Existing(entry)
    with pytest.raises(ResolveError) as e:
        resolve_target(cfg, str(dirs / "work/proj"), "obs")
    assert str(e.value) == f"type 'obs' uses dir '{dirs / 'work/obs'}'"


def test_type_override_existing_entry_other_type_name(dirs):
    cfg = make_cfg([("obs", str(dirs / "work/obs"), 1),
                    ("proj", str(dirs / "work/proj"), 1)])
    with pytest.raises(ResolveError, match="type 'obs' uses dir"):
        resolve_target(cfg, "proj", "obs")


def test_type_override_new_type_same_dir_as_existing_type(dirs):
    cfg = make_cfg([("obs", str(dirs / "work/obs"), 1)])
    real = str((dirs / "work/obs").resolve())
    assert resolve_target(cfg, "obs", "obs-sandbox") == New("obs-sandbox", real, str(dirs / "work/obs"))


def test_type_override_new_type_from_path_ignores_basename_collision(dirs):
    cfg = make_cfg([("proj", str(dirs / "work/proj"), 1)])
    real = str((dirs / "other/proj").resolve())
    assert resolve_target(cfg, str(dirs / "other/proj"), "proj2") == New("proj2", real, real)


def test_type_override_new_type_tilde_and_search_roots(dirs, monkeypatch):
    monkeypatch.setenv("HOME", str(dirs))
    cfg = make_cfg([], roots=[dirs / "elsewhere"])
    assert resolve_target(cfg, "~/work/proj", "t") == New(
        "t", str((dirs / "work/proj").resolve()), "~/work/proj")
    real = str((dirs / "elsewhere/x").resolve())
    assert resolve_target(cfg, "x", "t") == New("t", real, real)
    with pytest.raises(ResolveError, match="no agent type 'zzz'"):
        resolve_target(cfg, "zzz", "t")


# parse_records / sandbox_type

def test_parse_records_quoted_values_and_unknown_keys():
    text = ('name=api config_dir=~/.claude-api extra=1 '
            'command="claude-supervisor run api -- -n {name} --remote-control {name}"\n'
            '\n'
            "name=b config_dir=/b command='x {name} --remote-control' url=a=b\n")
    records = parse_records(text)
    assert records[0] == {"name": "api", "config_dir": "~/.claude-api", "extra": "1",
                          "command": "claude-supervisor run api -- -n {name} --remote-control {name}"}
    assert records[1]["command"] == "x {name} --remote-control"
    assert records[1]["url"] == "a=b"


@pytest.mark.parametrize("line, msg", [
    ("name=a command=x", "discover line 1: missing config_dir"),
    ("name=a config_dir=/c command=\"x", "discover line 1: No closing quotation"),
    ("name=a config_dir=/c command=x stray", "discover line 1: 'stray' is not key=value"),
])
def test_parse_records_errors_name_the_line(line, msg):
    with pytest.raises(ResolveError, match=msg):
        parse_records(line + "\n")


def test_sandbox_type(tmp_path):
    (tmp_path / "notes").mkdir()
    cfg = make_cfg([("obsidian", str(tmp_path), 1)], roots=[tmp_path])
    assert sandbox_type(cfg, "obsidian", "api") == "obsidian-api"
    assert sandbox_type(cfg, "notes", "api") == "notes-api"
    assert sandbox_type(cfg, str(tmp_path / "notes"), "x") == "notes-x"
    with pytest.raises(ResolveError, match="the queen is always running"):
        sandbox_type(cfg, "queen", "x")


def test_unknown_user_in_target_is_resolve_error():
    cfg = make_cfg([])
    with pytest.raises(ResolveError, match="unknown user"):
        resolve_target(cfg, "~nosuchuser_hive/x")
