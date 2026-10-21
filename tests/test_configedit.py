import difflib
import os
import stat
from pathlib import Path

import pytest

from hive import configedit
from hive.configedit import EditError, append_agent, set_count, verify, write_atomic

EXAMPLE = (Path(configedit.__file__).parent / "config.example.toml").read_text()

NO_NEWLINE = '[[agents]]\ntype = "a"\ndir = "/a"\ncount = 1'

COMMENTS = """\
# count = 9
[[agents]]   # first
type = 'single'   # type = "other"
# count = 9
dir = "/s"
count = 3   # three of them

[[agents]]
type = "other"
dir = "/o"
count = 1
"""

SERVICE = """\
[[agents]]
type = "a"
dir = "/a"

[service]
after = ["x.service"]
"""

INLINE = 'agents = [{type = "a", dir = "/a", count = 1}]\n'


def changes(old, new):
    """Removed and added lines, as '-line' / '+line' (newline stripped)."""
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
    return [d for d in diff if d[:1] in "+-" and d[:3] not in ("+++", "---")]


def count_mutation(type_, n):
    def mutate(doc):
        for agent in doc["agents"]:
            if agent["type"] == type_:
                agent["count"] = n
    return mutate


def append_mutation(**agent):
    return lambda doc: doc.setdefault("agents", []).append(agent)


def edit_count(text, type_, n):
    new = set_count(text, type_, n)
    verify(text, new, count_mutation(type_, n))
    return new


def test_set_count_example_keeps_comment():
    new = edit_count(EXAMPLE, "notes", 5)
    old_line = next(l for l in EXAMPLE.splitlines() if l.startswith("count = 2"))
    assert "#" in old_line
    assert changes(EXAMPLE, new) == ["-" + old_line, "+" + old_line.replace("2", "5", 1)]


def test_set_count_example_second_block_to_zero():
    new = edit_count(EXAMPLE, "project", 0)
    assert changes(EXAMPLE, new) == ["-count = 1", "+count = 0"]


def test_set_count_no_trailing_newline():
    new = edit_count(NO_NEWLINE, "a", 12)
    assert changes(NO_NEWLINE, new) == ["-count = 1", "+count = 12"]
    assert not new.endswith("\n")


def test_set_count_comments_and_single_quotes():
    new = edit_count(COMMENTS, "single", 4)
    assert changes(COMMENTS, new) == [
        "-count = 3   # three of them",
        "+count = 4   # three of them",
    ]
    assert new.count("# count = 9") == 2


def test_set_count_type_is_matched_literally():
    text = '[[agents]]\ntype = "axb"\ndir = "/x"\ncount = 1\n'
    with pytest.raises(EditError, match="no \\[\\[agents\\]\\] block with type 'a.b'"):
        set_count(text, "a.b", 2)
    with pytest.raises(EditError):
        set_count(text, "ax", 2)


def test_set_count_missing_count_before_service():
    new = edit_count(SERVICE, "a", 3)
    assert changes(SERVICE, new) == ["+count = 3"]
    assert new.index("count = 3") < new.index("[service]")


def test_set_count_missing_count_at_eof_without_newline():
    text = '[[agents]]\ntype = "a"\ndir = "/a"'
    new = edit_count(text, "a", 3)
    assert new == text + "\ncount = 3"


def test_set_count_preserves_crlf():
    text = EXAMPLE.replace("\n", "\r\n")
    new = edit_count(text, "notes", 7)
    assert new.replace("\r\n", "\n") == set_count(EXAMPLE, "notes", 7)
    assert new.count("\r\n") == text.count("\r\n")
    missing = SERVICE.replace("\n", "\r\n")
    assert "count = 3\r\n" in edit_count(missing, "a", 3)


def test_set_count_unknown_type():
    with pytest.raises(EditError, match="no \\[\\[agents\\]\\] block with type 'nope'"):
        set_count(EXAMPLE, "nope", 1)
    with pytest.raises(EditError):
        set_count("", "a", 1)


def test_inline_agents_array_is_refused():
    msg = "inline agents array"
    with pytest.raises(EditError, match=msg):
        set_count(INLINE, "a", 2)
    with pytest.raises(EditError, match=msg):
        append_agent(INLINE, "b", "/b", 1)


def test_append_agent_example():
    new = append_agent(EXAMPLE, "docs", "~/docs", 2)
    assert new.startswith(EXAMPLE)
    assert changes(EXAMPLE, new) == [
        "+", "+[[agents]]", '+type = "docs"', '+dir = "~/docs"', "+count = 2",
    ]
    verify(EXAMPLE, new, append_mutation(type="docs", dir="~/docs", count=2))


def test_append_agent_normalizes_trailing_newlines():
    new = append_agent(NO_NEWLINE + "\n\n\n", "b", "/b", 1)
    assert new == NO_NEWLINE + '\n\n[[agents]]\ntype = "b"\ndir = "/b"\ncount = 1\n'
    assert append_agent(NO_NEWLINE, "b", "/b", 1) == new


def test_append_agent_after_service_table():
    new = append_agent(SERVICE, "b", "/b", 1)
    verify(SERVICE, new, append_mutation(type="b", dir="/b", count=1))


def test_append_agent_empty_file():
    for text in ("", "\n\n"):
        new = append_agent(text, "b", "/b", 0)
        assert new == '[[agents]]\ntype = "b"\ndir = "/b"\ncount = 0\n'
        verify(text, new, append_mutation(type="b", dir="/b", count=0))


def test_append_agent_command_is_escaped():
    command = 'sh -c "echo \\n {name}"'
    new = append_agent(EXAMPLE, "c", "/c", 1, command)
    assert changes(EXAMPLE, new)[-1] == '+command = "sh -c \\"echo \\\\n {name}\\""'
    verify(EXAMPLE, new, append_mutation(type="c", dir="/c", count=1, command=command))


@pytest.mark.parametrize("bad", ["a\nb", "a\tb", "a\x00b", "a\x7fb"])
def test_control_characters_are_refused(bad):
    with pytest.raises(EditError, match="control characters"):
        append_agent(EXAMPLE, bad, "/d", 1)
    with pytest.raises(EditError, match="control characters"):
        append_agent(EXAMPLE, "t", "/d", 1, bad)


def test_verify_raises_on_mismatch(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE)
    wrong = set_count(EXAMPLE, "notes", 5)
    with pytest.raises(EditError, match=f"unexpected result; {path} left unchanged"):
        verify(EXAMPLE, wrong, count_mutation("notes", 6), path=str(path))
    with pytest.raises(EditError, match="unexpected result; config left unchanged"):
        verify(EXAMPLE, wrong, lambda doc: None)
    assert path.read_text() == EXAMPLE


def test_verify_raises_on_invalid_toml():
    with pytest.raises(EditError, match="unexpected result"):
        verify(EXAMPLE, EXAMPLE + "count = = 1\n", lambda doc: None)


def test_verify_catches_misread_layout():
    # a multi-line string with a line starting with '[' ends the block early
    text = '[[agents]]\ntype = "a"\ndir = "/a"\nnote = """\n[x]\n"""\n'
    new = set_count(text, "a", 3)
    with pytest.raises(EditError):
        verify(text, new, count_mutation("a", 3))


def test_write_atomic_preserves_mode(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("old\n")
    path.chmod(0o640)
    write_atomic(path, "new\r\ntext")
    assert path.read_bytes() == b"new\r\ntext"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert os.listdir(tmp_path) == ["config.toml"]


def test_write_atomic_keeps_symlink(tmp_path):
    real_dir = tmp_path / "dotfiles"
    real_dir.mkdir()
    real = real_dir / "config.toml"
    real.write_text("old\n")
    real.chmod(0o600)
    link = tmp_path / "config.toml"
    link.symlink_to(real)
    write_atomic(link, "new\n")
    assert link.is_symlink()
    assert real.read_text() == "new\n"
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert os.listdir(real_dir) == ["config.toml"]


def test_write_atomic_cleans_up_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("old\n")

    def boom(*args):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_atomic(path, "new\n")
    assert path.read_text() == "old\n"
    assert os.listdir(tmp_path) == ["config.toml"]
