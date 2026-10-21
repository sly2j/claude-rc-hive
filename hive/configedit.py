"""Line-based edits of hive's config.toml, stdlib only.

Every edit is text -> text; `verify` checks the result with tomllib before
`write_atomic` puts it on disk.
"""

import os
import re
import shutil
import tempfile
import tomllib

HEADER_RE = re.compile(r"^\s*\[\[\s*agents\s*\]\]\s*(#.*)?$")
COUNT_RE = re.compile(r"^(\s*count\s*=\s*)\d+")
KEY_RE = re.compile(r"^\s*[A-Za-z0-9_\"'-]+\s*=")

INLINE_MSG = "config uses an inline agents array; hive can only edit [[agents]] tables"


class EditError(Exception):
    pass


def toml_string(s):
    """Return s as a TOML basic string."""
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in s):
        raise EditError(f"control characters are not allowed in {s!r}")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def check_inline(lines):
    """Raise when agents are defined, but not as [[agents]] tables."""
    if not any(HEADER_RE.match(line.rstrip("\r\n")) for line in lines):
        if "agents" in tomllib.loads("".join(lines)):
            raise EditError(INLINE_MSG)


def agent_blocks(lines):
    """Yield (start, end) line ranges of [[agents]] blocks, header included."""
    start = None
    for i, line in enumerate(lines):
        line = line.rstrip("\r\n")
        if start is not None and re.match(r"^\s*\[", line):
            yield start, i
            start = None
        if HEADER_RE.match(line):
            start = i
    if start is not None:
        yield start, len(lines)


def set_count(text, type_, n):
    lines = text.splitlines(keepends=True)
    check_inline(lines)
    type_re = re.compile(r"^\s*type\s*=\s*([\"'])" + re.escape(type_) + r"\1")
    for start, end in agent_blocks(lines):
        if not any(type_re.match(line) for line in lines[start:end]):
            continue
        for i in range(start, end):
            if COUNT_RE.match(lines[i]):
                lines[i] = COUNT_RE.sub(lambda m: m.group(1) + str(n), lines[i], count=1)
                return "".join(lines)
        last = max(i for i in range(start, end) if KEY_RE.match(lines[i]))
        if lines[last].endswith(("\n", "\r")):
            eol = lines[last][len(lines[last].rstrip("\r\n")):]
            lines.insert(last + 1, f"count = {n}{eol}")
        else:
            # last line of a file without trailing newline: keep it that way
            lines[last] += "\n"
            lines.insert(last + 1, f"count = {n}")
        return "".join(lines)
    raise EditError(f"no [[agents]] block with type '{type_}'")


def append_agent(text, type_, dir_, count, command=None):
    check_inline(text.splitlines(keepends=True))
    block = f"[[agents]]\ntype = {toml_string(type_)}\ndir = {toml_string(dir_)}\ncount = {count}\n"
    if command is not None:
        block += f"command = {toml_string(command)}\n"
    head = text.rstrip("\n")
    return head + "\n\n" + block if head.strip() else block


def verify(old_text, new_text, mutate, path="config"):
    """Require new_text to parse to old_text's document with `mutate` applied."""
    expected = tomllib.loads(old_text)
    mutate(expected)
    try:
        ok = tomllib.loads(new_text) == expected
    except tomllib.TOMLDecodeError:
        ok = False
    if not ok:
        raise EditError(f"config edit produced an unexpected result; {path} left unchanged")


def write_atomic(path, text):
    real = os.path.realpath(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        shutil.copymode(real, tmp)
        os.replace(tmp, real)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
