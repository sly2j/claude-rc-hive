"""Pure parts of hive: desired pane set, start/stop plan, spawn/stop target resolution.

No tmux and no process exits here; errors are raised as ResolveError.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Pane:
    name: str
    window: str
    type: str
    index: int
    dir: str
    command: str  # unexpanded template


@dataclass(frozen=True)
class RunningPane:
    pane_id: str
    window_id: str
    window_name: str
    name: str
    type: str
    index: int
    adopted: bool


def desired_panes(cfg):
    """The queen first, then each agent type in config order, indices 1..count."""
    prefix = cfg["prefix"]
    queen = cfg["queen"]
    name = f"{prefix}-queen"
    panes = [Pane(name, name, "queen", 1, queen["dir"], queen["command"])]
    for agent in cfg["agents"]:
        window = f"{prefix}-{agent['type']}"
        for i in range(1, agent["count"] + 1):
            panes.append(Pane(f"{window}-{i}", window, agent["type"], i,
                              agent["dir"], agent["command"]))
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


QUEEN_ERROR = "the queen is always running; use 'hive down' to stop the whole hive"


def _canonical(entry):
    return str(Path(entry["dir"]).expanduser().resolve())


def _entry(cfg, type_name):
    return next((a for a in cfg["agents"] if a["type"] == type_name), None)


def _path_dir(text):
    """Rule 4: a path must be absolute (or ~) and an existing directory."""
    path = Path(text).expanduser()
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


def resolve_target(cfg, target, type_override=None):
    """Map a spawn/stop TARGET to Existing(entry) or New(type, dir, stored_dir)."""
    if target == "queen" or type_override == "queen":
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
