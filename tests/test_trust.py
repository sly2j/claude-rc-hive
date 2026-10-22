"""trust.py with HOME pointed at tmp_path: never the real ~/.claude.json."""

import json
import os
import subprocess

import pytest

from hive.trust import (TrustError, is_trusted, looks_like_trust_prompt, seed_trust,
                        trust_file, trust_root)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return tmp_path


def test_trust_file_resolution(home, tmp_path, monkeypatch):
    assert trust_file() == home / ".claude.json"
    assert trust_file(str(tmp_path / "cc")) == tmp_path / "cc" / ".claude.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "proc"))
    assert trust_file() == tmp_path / "proc" / ".claude.json"     # hive's own env
    assert trust_file(str(tmp_path / "cc")) == tmp_path / "cc" / ".claude.json"  # config_dir wins


def test_trust_root_plain_dir(tmp_path):
    d = tmp_path / "plain" / "sub"
    d.mkdir(parents=True)
    assert trust_root(d) == d.resolve()


def test_trust_root_git_toplevel(tmp_path):
    repo = tmp_path / "repo"
    (repo / "a" / "b").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert trust_root(repo / "a" / "b") == repo.resolve()


def test_is_trusted(home):
    root = home / "p"
    assert not is_trusted(root)                      # no file
    f = home / ".claude.json"
    f.write_text("{not json")
    assert not is_trusted(root)                      # unreadable JSON
    f.write_text(json.dumps({"projects": {str(root): {"hasTrustDialogAccepted": False}}}))
    assert not is_trusted(root)
    f.write_text(json.dumps({"projects": {str(root): {"hasTrustDialogAccepted": True}}}))
    assert is_trusted(root)
    assert not is_trusted(home / "other")


def test_seed_creates_file(home):
    root = home / "p"
    assert seed_trust(root) == home / ".claude.json"
    data = json.loads((home / ".claude.json").read_text())
    assert data == {"projects": {str(root): {"hasTrustDialogAccepted": True}}}
    assert is_trusted(root)
    assert oct((home / ".claude.json").stat().st_mode & 0o777) == "0o600"


def test_seed_preserves_unrelated_keys_and_mode(home):
    root = home / "p"
    original = {"oauthAccount": {"x": 1}, "numStartups": 7,
                "projects": {"/elsewhere": {"allowedTools": ["a"]},
                             str(root): {"history": ["h"], "hasTrustDialogAccepted": False}}}
    f = home / ".claude.json"
    f.write_text(json.dumps(original))
    os.chmod(f, 0o640)
    seed_trust(root)
    data = json.loads(f.read_text())
    original["projects"][str(root)]["hasTrustDialogAccepted"] = True
    assert data == original
    assert f.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in home.iterdir()] == [".claude.json"]  # no temp file left


def test_seed_refuses_corrupt_json(home):
    f = home / ".claude.json"
    f.write_text('{"projects": {')
    with pytest.raises(TrustError, match="not valid JSON"):
        seed_trust(home / "p")
    assert f.read_text() == '{"projects": {'
    f.write_text('{"projects": []}')
    with pytest.raises(TrustError, match="not an object"):
        seed_trust(home / "p")


def test_seed_into_config_dir(home, tmp_path):
    cc = str(tmp_path / "cc")
    root = home / "p"
    assert seed_trust(root, cc) == tmp_path / "cc" / ".claude.json"
    assert is_trusted(root, cc)
    assert not is_trusted(root)
    assert not (home / ".claude.json").exists()


def test_looks_like_trust_prompt():
    assert looks_like_trust_prompt("Do you trust the files in this folder?")
    assert looks_like_trust_prompt("Quick safety check: Is this a project you created or one"
                                   " you trust?\n \u276f 1. Yes, I trust this folder\n   2. No, exit")
    assert not looks_like_trust_prompt("> how can I help\n")
    # a session that only talks about trust (e.g. the queen) is not waiting
    assert not looks_like_trust_prompt("trusting enables that folder's .claude/settings.json "
                                       "allow rules\n trusted=yes  Read 3 files")


def test_seed_waits_for_and_respects_claude_lock(home, monkeypatch):
    import hive.trust as trust
    monkeypatch.setattr(trust, "LOCK_TIMEOUT", 0.3)
    lock = home / ".claude.json.lock"
    lock.mkdir()
    with pytest.raises(TrustError, match="is held"):
        seed_trust(home / "p")
    assert not (home / ".claude.json").exists()
    old = os.stat(lock).st_mtime - 60
    os.utime(lock, (old, old))                       # stale lock: taken over
    seed_trust(home / "p")
    assert is_trusted(home / "p") and not lock.exists()


def test_seed_refuses_non_utf8(home):
    (home / ".claude.json").write_bytes(b"\xff\xfe{")
    with pytest.raises(TrustError, match="not valid JSON"):
        seed_trust(home / "p")


def test_unknown_user_in_config_dir_and_root():
    from hive.paths import PathError
    with pytest.raises(PathError, match="unknown user"):
        trust_file("~nosuchuser_hive/x")
    with pytest.raises(PathError, match="unknown user"):
        trust_root("~nosuchuser_hive/x")


def test_lock_rmdir_missing_does_not_mask_result(home):
    from hive.trust import _claude_lock
    path = home / ".claude.json"
    with _claude_lock(path):
        os.rmdir(f"{path}.lock")      # vanished (e.g. judged stale by another process)
    assert not os.path.exists(f"{path}.lock")
