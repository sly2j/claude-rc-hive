#!/usr/bin/env python3
"""PreToolUse guard for the hive queen. Managed by hive; do not edit.

Bash and Monitor: only a single `hive ...` command, no chaining, redirection or substitution,
and no argument under a secrets/private path. Read, Grep, Glob, Edit, Write,
NotebookEdit: only paths inside the queen dir ($CLAUDE_PROJECT_DIR), never under
a secrets/private path, and no writes into its .claude dir. Exit 2 blocks the
tool call; any error blocks too (fail closed).
"""

import json
import os
import re
import shlex
import sys
from pathlib import Path

BASH_REASON = "guard: only `hive ...` commands are allowed in the queen; use hive subcommands"
CHAIN = (";", "&", "|", "`", "$(", ">", "<", "\n", "\r")
FILE_TOOLS = {"Read", "Grep", "Glob", "Edit", "Write", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "Monitor"}
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
GLOB_CHARS = set("*?[{")
PRIVATE_PARTS = {"secrets", "private"}


def block(reason):
    print(reason, file=sys.stderr)
    sys.exit(2)


def is_private(path):
    parts = [p.lower() for p in Path(path).parts]
    name = parts[-1] if parts else ""
    return (any(p in PRIVATE_PARTS for p in parts)
            or name == "secrets" or name.startswith("secrets."))


def resolve(text, base):
    """Resolve like the file tools do: ~ expanded, $VARS left literal."""
    p = Path(text).expanduser()
    return (p if p.is_absolute() else base / p).resolve()


def check_bash(command, qdir):
    if not isinstance(command, str):
        block("guard: shell tool input has no command string")
    if any(c in command for c in CHAIN) or "$'" in command or '$"' in command:
        block(BASH_REASON + " (no ; & | ` $( $' > < or newlines)")
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        block(f"guard: cannot parse command: {e}")
    if not tokens or not (tokens[0] == "hive"
                          or (os.path.isabs(tokens[0]) and Path(tokens[0]).name == "hive")):
        block(BASH_REASON)
    for token in tokens[1:]:
        for part in re.split(r"[\s=]+", token):
            if not ("/" in part or part.startswith("~")):
                continue
            if set("*?[") & set(part) or re.search(r"\{[^}]*(,|\.\.)[^}]*\}", part):
                block(f"guard: {part}: no globs or brace expansion in path arguments")
            if (is_private(os.path.expandvars(part))
                    or is_private(resolve(os.path.expandvars(part), qdir))):
                block(f"guard: {part} is under a secrets/private path")


def check_path(text, qdir, write):
    if not isinstance(text, str) or not text:
        block("guard: missing path")
    if is_private(os.path.expanduser(text)):
        block(f"guard: {text} is under a secrets/private path")
    path = resolve(text, qdir)
    if is_private(path):
        block(f"guard: {text} resolves under a secrets/private path ({path})")
    if path != qdir and qdir not in path.parents:
        block(f"guard: {text} is outside the queen dir {qdir}; the queen only reads its own dir")
    if write and (path == qdir / ".claude" or qdir / ".claude" in path.parents):
        block(f"guard: {text} is in the queen's .claude dir, which hive manages")


def check_file_tool(tool, tool_input, qdir):
    if tool in ("Read", "Edit", "Write"):
        check_path(tool_input.get("file_path"), qdir, tool in WRITE_TOOLS)
    elif tool == "NotebookEdit":
        check_path(tool_input.get("notebook_path"), qdir, True)
    else:
        base = tool_input.get("path") or str(qdir)
        check_path(base, qdir, False)
        if tool == "Glob":
            pattern = tool_input.get("pattern")
            if not isinstance(pattern, str):
                block("guard: Glob input has no pattern")
            parts = Path(pattern).parts
            if ".." in parts:
                block("guard: Glob pattern must not contain ..")
            static = []
            for part in parts:
                if GLOB_CHARS & set(part):
                    break
                static.append(part)
            prefix = str(Path(*static)) if static else "."
            check_path(prefix if Path(prefix).expanduser().is_absolute()
                       else str(resolve(base, qdir) / prefix), qdir, False)


def main():
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            block("guard: hook input is not a JSON object")
        tool = data.get("tool_name")
        tool_input = data.get("tool_input")
        if tool not in FILE_TOOLS and tool not in SHELL_TOOLS:
            return 0
        if not isinstance(tool_input, dict):
            block("guard: tool_input is not an object")
        qdir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
        if tool in SHELL_TOOLS:
            check_bash(tool_input.get("command"), qdir)
        else:
            check_file_tool(tool, tool_input, qdir)
        return 0
    except SystemExit:
        raise
    except Exception as e:  # fail closed
        block(f"guard: error, blocking: {type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
