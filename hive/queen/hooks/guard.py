#!/usr/bin/env python3
"""PreToolUse guard for the hive queen. Managed by hive; do not edit.

Only the QUEEN_TOOLS are allowed; any other tool (WebFetch, Agent, mcp__*, ...) is denied. Bash
and Monitor: only a single `hive ...` command, no chaining, redirection or substitution, and no
argument under a secrets/private path. Of those, `hive spawn --command X` (also `--command=X`
and argparse abbreviations such as `--comm`) runs without a prompt only when X is a command
string from ../config.toml ([defaults] command or an [[agents]] command). For any other
--command, for any spawn `--env` or `--trust` (same abbreviations), for `hive roots add PATH`,
for `hive launchers add` and for `hive down`/`restart`, the guard answers "ask" so the user confirms (`hive stop` needs no
prompt). Read, Grep, Glob, Edit, Write, NotebookEdit: only paths inside the queen dir
($CLAUDE_PROJECT_DIR), never under a secrets/private path, no `..` in glob patterns, and no
writes into its .claude dir, .mcp.json, .claude.json or CLAUDE.md. AskUserQuestion is allowed
as is. Exit 2 blocks the tool call
(deny wins over ask); "ask" is JSON on stdout with exit 0; any error blocks (fail closed).
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
# Asks the user a question; no input the guard needs to check.
DIALOG_TOOLS = {"AskUserQuestion"}
QUEEN_TOOLS = {"Bash", "Monitor", "Read", "Grep", "Glob", "Edit", "Write", "NotebookEdit"} | DIALOG_TOOLS
PROTECTED_NAMES = (".claude", ".mcp.json", ".claude.json", "CLAUDE.md")
PRIVATE_PARTS = {"secrets", "private"}


def block(reason):
    print(reason, file=sys.stderr)
    sys.exit(2)


def ask(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": reason}}))
    sys.exit(0)


def config_commands(qdir):
    """Command strings in ../config.toml; empty when it cannot be read or parsed."""
    try:
        import tomllib
        with open(qdir.parent / "config.toml", "rb") as f:
            cfg = tomllib.load(f)
        found = [(cfg.get("defaults") or {}).get("command")]
        found += [a.get("command") for a in cfg.get("agents") or [] if isinstance(a, dict)]
        return {c for c in found if isinstance(c, str)}
    except Exception:
        return set()


def spawn_options(args, option):
    """Values of `option` (e.g. --command, or an abbreviation of it) in `hive spawn` arguments."""
    values = []
    for i, token in enumerate(args):
        name, eq, value = token.partition("=")
        if name.startswith("--") and len(name) >= 3 and option.startswith(name):
            if eq:
                values.append(value)
            elif i + 1 < len(args):
                values.append(args[i + 1])
    return values


def has_flag(args, flag):
    """True if `flag` (e.g. --trust, or an abbreviation of it) is among `hive spawn` arguments."""
    for token in args:
        if token == "--":
            return False
        name = token.partition("=")[0]
        if name.startswith("--") and len(name) >= 3 and flag.startswith(name):
            return True
    return False


SPAWN_VALUE_OPTIONS = ("--type", "--command", "--config-dir", "--launcher", "--sandbox", "--env")


def spawn_target(args):
    """The TARGET positional of `hive spawn`, skipping options and their values."""
    skip = False
    for i, token in enumerate(args):
        if skip:
            skip = False
            continue
        if token == "--":
            return args[i + 1] if i + 1 < len(args) else "the target"
        if token.startswith("-") and token != "-":
            name = token.partition("=")[0]
            skip = "=" not in token and (name == "-n" or (
                name.startswith("--") and len(name) >= 3
                and any(o.startswith(name) for o in SPAWN_VALUE_OPTIONS)))
            continue
        return token
    return "the target"


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
    first = tokens[1] if len(tokens) > 1 else ""
    if first.startswith("-") and first not in ("--version", "-h", "--help"):
        # `hive -- down` parses as `hive down`; the checks below key on the subcommand.
        block(BASH_REASON + " (no options before the subcommand)")
    if len(tokens) > 1 and tokens[1] == "spawn":
        known = config_commands(qdir)
        reasons = [f"hive spawn --command is not a command from config.toml: {cmd}"
                   for cmd in spawn_options(tokens[2:], "--command") if cmd not in known]
        env = spawn_options(tokens[2:], "--env")
        if env:
            reasons.append(f"hive spawn --env sets environment for the new agent: {' '.join(env)}")
        if has_flag(tokens[2:], "--trust"):
            target = spawn_target(tokens[2:])
            reasons.append(f"hive spawn --trust grants workspace trust to {target}")
        if reasons:
            ask("; ".join(reasons) + "; approve to run it")
    if len(tokens) > 1 and tokens[1] == "down":
        ask("hive down stops the whole hive, including this queen; approve to run it")
    if len(tokens) > 1 and tokens[1] == "restart":
        ask("hive restart stops and restarts the whole hive, including this queen; "
            "approve to run it")
    if len(tokens) > 2 and tokens[1] == "roots" and tokens[2] == "add":
        ask(f"hive roots add changes the search roots: {' '.join(tokens[3:])}; approve to run it")
    if len(tokens) > 2 and tokens[1] == "launchers" and tokens[2] == "add":
        # A registered launcher later runs through `spawn --launcher` without a --command prompt.
        ask(f"hive launchers add registers a launch command: {' '.join(tokens[3:])}; "
            "approve to run it")


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
    if write:
        for name in PROTECTED_NAMES:
            if path == qdir / name or qdir / name in path.parents:
                block(f"guard: {text} is {name} in the queen dir, which hive manages")


def check_glob_parts(pattern, what):
    for part in pattern.split("/"):
        if ".." in part:
            block(f"guard: {what} must not contain ..")
        if ("{" in part or "[" in part) and "." in part:
            block(f"guard: {what} part {part!r} mixes {{ or [ with a dot")


def check_file_tool(tool, tool_input, qdir):
    if tool in ("Read", "Edit", "Write"):
        check_path(tool_input.get("file_path"), qdir, tool in WRITE_TOOLS)
    elif tool == "NotebookEdit":
        check_path(tool_input.get("notebook_path"), qdir, True)
    else:
        base = tool_input.get("path") or str(qdir)
        check_path(base, qdir, False)
        if tool == "Grep" and tool_input.get("glob") is not None:
            if not isinstance(tool_input["glob"], str):
                block("guard: Grep glob is not a string")
            check_glob_parts(tool_input["glob"], "Grep glob")
        if tool == "Glob":
            pattern = tool_input.get("pattern")
            if not isinstance(pattern, str):
                block("guard: Glob input has no pattern")
            check_glob_parts(pattern, "Glob pattern")
            parts = Path(pattern).parts
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
        if tool not in QUEEN_TOOLS:
            block(f"guard: tool {tool!r} is not available to the queen")
        if not isinstance(tool_input, dict):
            block("guard: tool_input is not an object")
        qdir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
        if tool in DIALOG_TOOLS:
            return 0
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
