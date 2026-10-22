"""Pure parts of hive: desired pane set, start/stop plan, spawn/stop target resolution.

No tmux and no process exits here; errors are raised as ResolveError.
"""

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from hive.paths import PathError, expanduser


@dataclass(frozen=True)
class Pane:
    name: str
    window: str
    type: str
    index: int
    dir: str
    command: str  # unexpanded template
    env: dict = field(default_factory=dict)  # the type's own env, exported after [env]
    config_dir: str | None = None  # Claude config dir of the command; locates the trust file
    args: tuple = ()  # extra flags for the command, placed by cli.expand_command


@dataclass(frozen=True)
class RunningPane:
    pane_id: str
    window_id: str
    window_name: str
    name: str
    type: str
    index: int
    adopted: bool


def queen_name(prefix):
    return f"hive-queen-{prefix}"


def desired_panes(cfg):
    """The queen first, then each agent type in config order, indices 1..count."""
    prefix = cfg["prefix"]
    queen = cfg["queen"]
    name = queen_name(prefix)
    panes = [Pane(name, name, "queen", 1, queen["dir"], queen["command"],
                  queen.get("env", {}), queen.get("config_dir"),
                  tuple(queen.get("args", ())))]
    for agent in cfg["agents"]:
        window = f"{agent['type']}-{prefix}"
        for i in range(1, agent["count"] + 1):
            panes.append(Pane(f"{window}-{i}", window, agent["type"], i,
                              agent["dir"], agent["command"], agent.get("env", {}),
                              agent.get("config_dir"), tuple(agent.get("args", ()))))
    return panes


def plan(desired, running, queen_name):
    """Return (to_start, to_stop); the queen is never stopped."""
    names = {p.name for p in desired}
    to_start = [p for p in desired if p.name not in running]
    to_stop = sorted((r for r in running.values()
                      if r.name not in names and r.name != queen_name),
                     key=lambda r: (r.window_name, -r.index))
    return to_start, to_stop


class ResolveError(Exception):
    """Message is the user-facing error text."""


@dataclass(frozen=True)
class Existing:
    entry: dict


@dataclass(frozen=True)
class New:
    type: str
    dir: str         # canonical absolute path
    stored_dir: str  # what to write to the config: as typed if it starts with "~"


RESERVED_TYPES = ("queen", "hive-queen")
QUEEN_ERROR = "the queen is always running; use 'hive down' to stop the whole hive"


def _canonical(entry):
    return str(expanduser(entry["dir"]).resolve())


def _entry(cfg, type_name):
    return next((a for a in cfg["agents"] if a["type"] == type_name), None)


def _path_dir(text):
    """Rule 4: a path must be absolute (or ~) and an existing directory."""
    try:
        path = expanduser(text)
    except PathError as e:
        raise ResolveError(str(e)) from None
    if not path.is_absolute():
        raise ResolveError(f"'{text}' is relative; give an absolute path or ~/...")
    if not path.is_dir():
        raise ResolveError(f"'{path}' is not a directory")
    return str(path.resolve())


def _search_hit(cfg, target):
    """Rule 5: the single directory named `target` under search_roots."""
    roots = cfg["search_roots"]
    hits = [str(Path(r) / target) for r in roots if (Path(r) / target).is_dir()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ResolveError(f"ambiguous: {target} matches {', '.join(hits)}; "
                           "give the full path")
    where = (f"under search_roots [{', '.join(roots)}]" if roots
             else "and search_roots is not set")
    raise ResolveError(f"no agent type '{target}' and no directory '{target}' {where}")


def _target_dir(cfg, target):
    """Resolve a TARGET that is an existing type, a path, or a bare word to
    (canonical dir, dir to store)."""
    entry = _entry(cfg, target)
    if entry:
        return _canonical(entry), entry["dir"]
    if "/" in target or target.startswith(("~", ".")):
        d = _path_dir(target)
        return d, target if target.startswith("~") else d
    d = _path_dir(_search_hit(cfg, target))
    return d, d


def sandbox_type(cfg, target, sandbox):
    """Default type for --sandbox: TARGET's type, else its directory name, plus -<sandbox>."""
    if target in RESERVED_TYPES:
        raise ResolveError(QUEEN_ERROR)
    entry = _entry(cfg, target)
    base = entry["type"] if entry else Path(_target_dir(cfg, target)[0]).name
    return f"{base}-{sandbox}"


RECORD_KEYS = ("name", "config_dir", "command")


def parse_records(text):
    """Parse `[queen] discover` output: one record per line of key=value tokens.

    Values with spaces are double-quoted (shell rules). Unknown keys are kept but
    ignored by callers; blank lines are skipped.
    """
    records = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            tokens = shlex.split(line)
        except ValueError as e:
            raise ResolveError(f"discover line {n}: {e}: {line}")
        record = {}
        for token in tokens:
            key, sep, value = token.partition("=")
            if not sep or not key:
                raise ResolveError(f"discover line {n}: '{token}' is not key=value: {line}")
            record[key] = value
        missing = [k for k in RECORD_KEYS if k not in record]
        if missing:
            raise ResolveError(f"discover line {n}: missing {', '.join(missing)}: {line}")
        records.append(record)
    return records


def resolve_target(cfg, target, type_override=None):
    """Map a spawn/stop TARGET to Existing(entry) or New(type, dir, stored_dir)."""
    if target in RESERVED_TYPES or type_override in RESERVED_TYPES:
        raise ResolveError(QUEEN_ERROR)

    if type_override:
        entry = _entry(cfg, type_override)
        if entry:
            if target == type_override:
                return Existing(entry)
            if _target_dir(cfg, target)[0] != _canonical(entry):
                raise ResolveError(f"type '{type_override}' uses dir '{entry['dir']}'")
            return Existing(entry)
        d, stored = _target_dir(cfg, target)
        return New(type_override, d, stored)

    entry = _entry(cfg, target)
    if entry:
        return Existing(entry)

    d, stored = _target_dir(cfg, target)
    for a in cfg["agents"]:
        if _canonical(a) == d:
            return Existing(a)
    name = Path(d).name
    clash = _entry(cfg, name)
    if clash:
        raise ResolveError(f"type '{name}' already uses '{clash['dir']}'; give --type")
    return New(name, d, stored)
