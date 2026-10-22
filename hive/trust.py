"""Claude Code workspace trust: where it is keyed and whether it is recorded.

Trust is projects["<root>"].hasTrustDialogAccepted in <Claude config dir>/.claude.json.
Claude Code rewrites that file itself, so hive only ever sets that one key,
under Claude's lock, and never in a file it cannot parse.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager, suppress
from pathlib import Path

from hive.paths import expanduser

LOCK_TIMEOUT = 5.0   # seconds to wait for Claude Code's lock on the trust file
LOCK_STALE = 10.0    # Claude Code (proper-lockfile) treats older lock dirs as stale


class TrustError(Exception):
    """Message is the user-facing error text."""


def trust_file(config_dir=None):
    config_dir = config_dir or os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return expanduser(config_dir) / ".claude.json"
    return Path.home() / ".claude.json"


def trust_root(directory):
    """The git toplevel containing `directory`, else the directory; absolute."""
    d = expanduser(directory).resolve()
    try:
        out = subprocess.run(["git", "-C", str(d), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
    except (FileNotFoundError, NotADirectoryError):
        return d
    top = out.stdout.strip()
    return Path(top).resolve() if out.returncode == 0 and top else d


def is_trusted(root, config_dir=None):
    try:
        data = json.loads(trust_file(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    projects = data.get("projects") if isinstance(data, dict) else None
    entry = projects.get(str(root)) if isinstance(projects, dict) else None
    return isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True


def _child(parent, key, path):
    value = parent.setdefault(key, {})
    if not isinstance(value, dict):
        raise TrustError(f"{path}: '{key}' is not an object; not touching it")
    return value


@contextmanager
def _claude_lock(path):
    """Hold Claude Code's lock on its config file: a `<file>.lock` directory."""
    lock = f"{path}.lock"
    Path(lock).parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            try:
                if time.time() - os.stat(lock).st_mtime > LOCK_STALE:
                    os.rmdir(lock)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise TrustError(f"{lock} is held (a Claude session is writing {path}); "
                                 "try again")
            time.sleep(0.1)
    try:
        yield
    finally:
        with suppress(FileNotFoundError):
            os.rmdir(lock)


def seed_trust(root, config_dir=None):
    """Set projects[root].hasTrustDialogAccepted = true; return the trust file."""
    path = trust_file(config_dir)
    with _claude_lock(path):
        _seed_locked(path, str(root))
    return path


def _seed_locked(path, root):
    real = Path(os.path.realpath(path))
    try:
        text = real.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    except UnicodeError as e:
        raise TrustError(f"{path} is not valid JSON ({e}); not touching it")
    if text is None:
        data = {}
    else:
        try:
            data = json.loads(text)
        except ValueError as e:
            raise TrustError(f"{path} is not valid JSON ({e}); not touching it")
        if not isinstance(data, dict):
            raise TrustError(f"{path} is not a JSON object; not touching it")
    _child(_child(data, "projects", path), root, path)["hasTrustDialogAccepted"] = True

    new = json.dumps(data, indent=2, ensure_ascii=False)
    if json.loads(new) != data:
        raise TrustError(f"re-encoding {path} changed its content; not touching it")

    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=".claude.json-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
        if text is not None:
            shutil.copymode(real, tmp)
        # mkstemp already created tmp as 0600, which suits a new trust file.
        os.replace(tmp, real)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


TRUST_PROMPT_PHRASES = ("i trust this folder", "trust the files in this folder")


def looks_like_trust_prompt(screen):
    """Match Claude Code's trust dialog on a captured pane screen.

    Dialog phrases only: a bare "trust" + "folder" also matches a session that
    merely talks about trust, such as the queen's own pane.
    """
    text = " ".join(screen.lower().split())
    return any(phrase in text for phrase in TRUST_PROMPT_PHRASES)
