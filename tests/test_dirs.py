import os
import sys

import pytest

from hive import cli, dirs

BASE = '[defaults]\ncommand = "claude -n {name} --remote-control {name}"\n'


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """Two search roots with nested, hidden, secret, private and escaping entries."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    work, notes, outside = tmp_path / "work", tmp_path / "notes", tmp_path / "outside"
    for d in ("api/src/deep/deeper", "api/docs", "web", ".hidden/x", "secrets/k",
              "Private/p", "api/secrets/s"):
        (work / d).mkdir(parents=True)
    (work / "README").write_text("file")
    (notes / "journal").mkdir(parents=True)
    outside.mkdir()
    (work / "escape").symlink_to(outside)
    (work / "jnl").symlink_to(notes / "journal")
    return tmp_path


def write_config(tmp_path, text):
    path = tmp_path / "cfg" / "hive" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def roots_config(tmp_path, extra=""):
    return write_config(tmp_path, f'search_roots = ["{tmp_path}/work", "{tmp_path}/notes"]\n'
                        + BASE + extra)


def run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["hive", *argv])
    try:
        code = cli.main()
    except SystemExit as e:
        code = e.code
    out, err = capsys.readouterr()
    return code, out.splitlines(), err


def names(lines):
    return [line.split()[0] for line in lines]


def test_depth_1_lists_both_roots_with_type(tree, monkeypatch, capsys):
    roots_config(tree, f'\n[[agents]]\ntype = "backend"\ndir = "{tree}/work/api"\n')
    code, out, err = run(monkeypatch, capsys, "dirs")
    w = tree / "work"
    assert (code, err) == (0, "")
    assert out == [
        f"root={w}",
        f"dir={w}/api name=api type=backend",
        f"dir={w}/jnl name=jnl type=none",
        f"dir={w}/web name=web type=none",
        f"root={tree}/notes",
        f"dir={tree}/notes/journal name=journal type=none",
    ]


def test_depth_2_skips_private_and_does_not_follow_symlinks(tree, monkeypatch, capsys):
    roots_config(tree)
    (tree / "notes" / "journal" / "2026").mkdir()
    code, out, _ = run(monkeypatch, capsys, "dirs", "--depth", "2")
    w = tree / "work"
    assert code == 0
    assert names(out) == [
        f"root={w}", f"dir={w}/api", f"dir={w}/api/docs", f"dir={w}/api/src",
        f"dir={w}/jnl", f"dir={w}/web",
        f"root={tree}/notes", f"dir={tree}/notes/journal", f"dir={tree}/notes/journal/2026",
    ]


def test_bare_name_resolves_like_spawn(tree, monkeypatch, capsys):
    roots_config(tree)
    code, out, _ = run(monkeypatch, capsys, "dirs", "api", "--depth", "3")
    a = tree / "work" / "api"
    assert code == 0
    assert names(out) == [f"root={a}", f"dir={a}/docs", f"dir={a}/src", f"dir={a}/src/deep",
                          f"dir={a}/src/deep/deeper"]


def test_refused_outside_roots(tree, monkeypatch, capsys):
    roots_config(tree)
    code, out, err = run(monkeypatch, capsys, "dirs", str(tree / "outside"))
    assert (code, out) == (3, [])
    assert err.startswith(f"refused: {tree / 'outside'} is not under search_roots (")
    assert err.rstrip().endswith(f"use `hive roots add {tree / 'outside'}` after the user confirms")
    code, _, err = run(monkeypatch, capsys, "dirs", str(tree / "work" / "escape"))
    assert code == 3 and "not under search_roots" in err


def test_refused_private_root(tree, monkeypatch, capsys):
    roots_config(tree)
    code, _, err = run(monkeypatch, capsys, "dirs", str(tree / "work" / "Private"))
    assert code == 3 and "secrets/private" in err


def test_unset_search_roots(tree, monkeypatch, capsys):
    write_config(tree, BASE)
    code, out, err = run(monkeypatch, capsys, "dirs")
    assert (code, out) == (2, [])
    assert "set search_roots" in err


def test_missing_root_reported_and_continues(tree, monkeypatch, capsys):
    write_config(tree, f'search_roots = ["{tree}/gone", "{tree}/notes"]\n' + BASE)
    code, out, _ = run(monkeypatch, capsys, "dirs")
    assert code == 0
    assert out[0] == f"root={tree}/gone error=missing"
    assert out[1] == f"root={tree}/notes"


def test_depth_max_3(tree, monkeypatch, capsys):
    roots_config(tree)
    code, _, err = run(monkeypatch, capsys, "dirs", "--depth", "4")
    assert code == 2 and "must be <= 3" in err


def test_cap(tree, monkeypatch, capsys):
    roots_config(tree)
    monkeypatch.setattr(dirs, "MAX_RECORDS", 2)
    code, out, _ = run(monkeypatch, capsys, "dirs")
    assert code == 0
    assert names(out) == [f"root={tree}/work", f"dir={tree}/work/api", f"dir={tree}/work/jnl",
                          "more=yes"]


# --- hive roots -----------------------------------------------------------

ROOTS_TEXT = """\
# my hive
prefix = "t"   # short
search_roots = ["~/work", '/srv']   # where projects live

# below
[defaults]
command = "claude -n {name} --remote-control {name}"
"""


def test_roots_list(tree, monkeypatch, capsys):
    roots_config(tree)
    code, out, _ = run(monkeypatch, capsys, "roots")
    assert (code, out) == (0, [f"root={tree}/work", f"root={tree}/notes"])
    write_config(tree, BASE)
    assert run(monkeypatch, capsys, "roots")[:2] == (0, ["roots=none"])


def test_roots_add_present_key_keeps_comments_and_spellings(tree, monkeypatch, capsys):
    path = write_config(tree, ROOTS_TEXT)
    code, out, _ = run(monkeypatch, capsys, "roots", "add", str(tree / "notes"))
    assert (code, out) == (0, [f"added {tree}/notes"])
    assert path.read_text() == ROOTS_TEXT.replace(
        """["~/work", '/srv']""", f'["~/work", "/srv", "{tree}/notes"]')


def test_roots_add_absent_key_and_tilde_spelling(tree, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tree))
    text = '# top\nprefix = "t"\n\n' + BASE
    path = write_config(tree, text)
    code, out, _ = run(monkeypatch, capsys, "roots", "add", "~/work/")
    assert (code, out) == (0, ["added ~/work"])
    assert path.read_text() == '# top\nprefix = "t"\nsearch_roots = ["~/work"]\n\n' + BASE
    code, out, _ = run(monkeypatch, capsys, "roots", "add", str(tree / "work"))
    assert (code, out) == (0, ["unchanged"])


def test_roots_add_rejects_non_dir_and_secrets(tree, monkeypatch, capsys):
    path = write_config(tree, ROOTS_TEXT)
    code, _, err = run(monkeypatch, capsys, "roots", "add", str(tree / "work" / "README"))
    assert code == 2 and "not an existing directory" in err
    code, _, err = run(monkeypatch, capsys, "roots", "add", str(tree / "work" / "secrets"))
    assert code == 3 and err.startswith("refused: ")
    code, _, err = run(monkeypatch, capsys, "roots", "add", str(tree / "work" / "Private" / "p"))
    assert code == 3
    assert path.read_text() == ROOTS_TEXT


def test_roots_remove(tree, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tree))
    path = write_config(tree, ROOTS_TEXT)
    code, out, _ = run(monkeypatch, capsys, "roots", "remove", str(tree / "work"))
    assert (code, out) == (0, ["removed ~/work"])
    assert path.read_text() == ROOTS_TEXT.replace("""["~/work", '/srv']""", '["/srv"]')
    code, _, err = run(monkeypatch, capsys, "roots", "remove", str(tree / "work"))
    assert code == 2 and err.startswith("not found: ")


def test_roots_multiline_array_left_alone(tree, monkeypatch, capsys):
    text = 'search_roots = [\n  "/srv",\n]\n' + BASE
    path = write_config(tree, text)
    code, _, err = run(monkeypatch, capsys, "roots", "add", str(tree / "notes"))
    assert code == 2 and "one-line array" in err
    assert path.read_text() == text


def test_roots_edit_keeps_bracket_comment_and_skips_multiline_key(tree, monkeypatch, capsys):
    text = f'search_roots = ["{tree}/work"]  # see [docs]\n' + BASE
    path = write_config(tree, text)
    assert run(monkeypatch, capsys, "roots", "add", str(tree / "notes"))[0] == 0
    assert path.read_text() == text.replace('"]  #', f'", "{tree}/notes"]  #')
    text = 'prefix = "t"\ninspect_roots = [\n  "/usr/bin",\n]\n' + BASE
    path = write_config(tree, text)
    assert run(monkeypatch, capsys, "roots", "add", str(tree / "notes"))[0] == 0
    assert path.read_text() == text.replace('prefix = "t"\n', f'prefix = "t"\nsearch_roots = ["{tree}/notes"]\n')


def test_unlistable_subdir_reports_error(tree, monkeypatch, capsys):
    if os.geteuid() == 0:
        pytest.skip("root can list mode-000 dirs")
    roots_config(tree)
    bad = tree / "work" / "web"
    bad.chmod(0)
    try:
        code, lines, _ = run(monkeypatch, capsys, "dirs", "--depth", "2")
    finally:
        bad.chmod(0o755)
    assert code == 0
    assert any(l.startswith(f"dir={bad} error=") for l in lines)


def test_unknown_user_target_and_root(tree, monkeypatch, capsys):
    roots_config(tree)
    code, _, err = run(monkeypatch, capsys, "dirs", "~nosuchuser_hive/x")
    assert code == 2 and "unknown user" in err


def test_unknown_user_in_search_root_raises_patherror():
    from hive.paths import PathError
    with pytest.raises(PathError, match="unknown user"):
        dirs._roots({"search_roots": ["~nosuchuser_hive/r"]})
