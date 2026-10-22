"""hive inspect-launcher: read-only look at a launcher for the Claude config dir it uses.

Only files under the configured roots are read, never executed. Output is one
key=value record per line, like `hive list`.
"""

import os
import re
import shlex
import shutil
from pathlib import Path

from hive.paths import PathError, expanduser

DEFAULT_ROOTS = ["~/.local/bin", "/usr/local/bin", "/usr/bin", "/bin", "/opt"]
HEAD_BYTES = 4096
MAX_BYTES = 1 << 20
MAX_LINES = 200
MATCH_RE = re.compile(r"CLAUDE_CONFIG_DIR|claude|exec |env |export |HOME=|XDG_CONFIG_HOME",
                      re.IGNORECASE)
SECRET_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*=")
PRIVATE_PARTS = {"secrets", "private"}


def is_private(path):
    """True for a path under a secrets/private dir, or a file named secrets or secrets.*."""
    parts = [p.lower() for p in Path(path).parts]
    name = parts[-1] if parts else ""
    return (any(p in PRIVATE_PARTS for p in parts)
            or name == "secrets" or name.startswith("secrets."))


def inside(path, roots):
    return any(path == r or r in path.parents for r in roots)


def root_paths(roots):
    """Each root as written (~ expanded) and resolved, so a symlinked root still matches."""
    out = []
    for r in roots:
        p = Path(os.path.normpath(expanduser(r)))
        out += [p, p.resolve()]
    return out


class LauncherError(Exception):
    """A launcher command that cannot be resolved. str(e) is the message without a prefix."""
    code = 1


class LauncherNotFound(LauncherError):
    code = 2


class LauncherRefused(LauncherError):
    code = 3


def locate_launcher(command, environ=os.environ):
    """Return (path, resolved, shown) for COMMAND's token 0, without executing it."""
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        raise LauncherNotFound(f"cannot parse command: {e}")
    if not tokens:
        raise LauncherNotFound("empty command")
    token = tokens[0]
    if "/" in token:
        try:
            found = expanduser(token)
        except PathError as e:
            raise LauncherNotFound(str(e)) from None
    else:
        which = shutil.which(token, path=environ.get("PATH"))
        found = Path(which) if which else None
    if found is None or not found.exists():
        raise LauncherNotFound(token)

    path = Path(os.path.normpath(found.absolute()))
    resolved = found.resolve()
    shown = str(path) if path == resolved else f"{path} -> {resolved}"
    if is_private(path) or is_private(resolved):
        raise LauncherRefused(f"{shown} is under a secrets/private path")
    return path, resolved, shown


def resolve_launcher(command, environ=os.environ):
    """Resolve COMMAND's token 0 (PATH lookup or path) to an absolute path, never executing it.

    Raises LauncherNotFound (exit 2) or LauncherRefused for secrets/private paths (exit 3).
    No inspect_roots check: a plain `claude` symlink may point anywhere.
    """
    return locate_launcher(command, environ)[0]


def inspect_launcher(command, roots, out, err, environ=os.environ):
    """Print what COMMAND's launcher reveals about CLAUDE_CONFIG_DIR. Returns the exit code."""
    try:
        path, resolved, shown = locate_launcher(command, environ)
    except LauncherNotFound as e:
        print(f"not found: {e}", file=err)
        return 2
    except LauncherRefused as e:
        print(f"refused: {e}", file=err)
        return 3
    allowed = root_paths(roots)
    if not (inside(resolved, allowed) and inside(path, allowed)):
        print(f"refused: {shown} is outside inspect_roots ({','.join(roots)})", file=err)
        return 3
    if not resolved.is_file():
        print(f"not found: {shlex.split(command)[0]} (not a regular file)", file=err)
        return 2

    record = f"path={shlex.quote(str(path))} resolved={shlex.quote(str(resolved))}"
    try:
        with open(resolved, "rb") as f:
            head = f.read(HEAD_BYTES)
            binary = b"\0" in head or not head.startswith(b"#!")
            data = b"" if binary else head + f.read(MAX_BYTES + 1 - HEAD_BYTES)
    except OSError as e:
        print(f"not found: {shlex.split(command)[0]} (unreadable: {e.strerror or e})", file=err)
        return 2
    if binary:
        print(f"{record} kind=binary", file=out)
    else:
        truncated = len(data) > MAX_BYTES
        lines = data[:MAX_BYTES].decode("utf-8", errors="replace").splitlines()
        shebang = lines[0]
        if SECRET_RE.search(shebang):
            shebang = "redacted"
        print(f"{record} kind=script shebang={shlex.quote(shebang)} lines={len(lines)}"
              + (" truncated=yes" if truncated else ""), file=out)
        shown_lines = 0
        for n, line in enumerate(lines, 1):
            secret = SECRET_RE.search(line)
            if not secret and not MATCH_RE.search(line):
                continue
            if shown_lines == MAX_LINES:
                print("more=yes", file=out)
                break
            shown_lines += 1
            if secret:
                print(f"line={n} redacted=secret-like", file=out)
            else:
                print(f"line={n} text={shlex.quote(line)}", file=out)

    value = environ.get("CLAUDE_CONFIG_DIR")
    print(f"env CLAUDE_CONFIG_DIR={shlex.quote(value) if value is not None else 'unset'}", file=out)
    return 0
