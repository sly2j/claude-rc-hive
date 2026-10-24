"""hive dirs: read-only listing of the directories under search_roots.

Output is one key=value record per line, like `hive list`. Hidden entries and
secrets/private directories are skipped; symlinks are listed only when they
resolve inside search_roots and are never followed for recursion.
"""

import os
import shlex
import sys
from pathlib import Path

from hive.paths import PathError, expanduser
from hive.reconcile import ResolveError, _canonical, _target_dir

MAX_DEPTH = 3
MAX_RECORDS = 500
PRIVATE_NAMES = {"secrets", "private"}


class _Cap(Exception):
    pass


def _roots(cfg):
    """(as written, resolved) per search root; the written form is what gets printed."""
    out = []
    for r in cfg["search_roots"]:
        p = Path(os.path.normpath(expanduser(r).absolute()))
        out.append((p, p.resolve()))
    return out


def _containing_root(path, roots):
    """The resolved root that contains resolved `path`, or None.

    On macOS a case-variant spelling of a root matches by file identity (case-insensitive
    APFS); the root is then returned in `path`'s spelling, so path.relative_to(root) works.
    """
    for _, resolved in roots:
        if path == resolved or resolved in path.parents:
            return resolved
        if sys.platform == "darwin" and str(path).lower().startswith(str(resolved).lower()):
            for p in (path, *path.parents):
                try:
                    if os.path.samefile(p, resolved):
                        return p
                except OSError:
                    pass
    return None


def _private_below(path, root):
    return any(p.lower() in PRIVATE_NAMES for p in path.relative_to(root).parts)


def _lister(cfg, roots, out):
    types = {}
    for a in cfg["agents"]:
        types.setdefault(_canonical(a), a["type"])
    count = 0

    def walk(directory, levels):
        nonlocal count
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as e:
            print(f"dir={shlex.quote(str(directory))} "
                  f"error={shlex.quote(e.strerror or str(e))}", file=out)
            return
        for entry in entries:
            if entry.name.startswith(".") or entry.name.lower() in PRIVATE_NAMES:
                continue
            try:
                if entry.is_symlink():
                    target = Path(entry.path).resolve()
                    root = _containing_root(target, roots)
                    if not target.is_dir() or root is None or _private_below(target, root):
                        continue
                    recurse = False
                elif entry.is_dir(follow_symlinks=False):
                    target = Path(entry.path).resolve()
                    recurse = levels > 1
                else:
                    continue
            except (OSError, RuntimeError):  # RuntimeError: symlink loop on 3.11/3.12
                continue
            if count == MAX_RECORDS:
                print("more=yes", file=out)
                raise _Cap
            count += 1
            print(f"dir={shlex.quote(entry.path)} name={shlex.quote(entry.name)} "
                  f"type={shlex.quote(types.get(str(target), 'none'))}", file=out)
            if recurse:
                walk(entry.path, levels - 1)

    return walk


def list_dirs(cfg, target, depth, out, err):
    """Print the directories under search_roots (or under TARGET). Returns the exit code."""
    if not cfg["search_roots"]:
        print("hive: error: search_roots is not set; set search_roots in config.toml "
              "to the directories hive dirs should list", file=err)
        return 2
    roots = _roots(cfg)
    names = ",".join(str(p) for p, _ in roots)

    if target is None:
        blocks = [p for p, _ in roots]
    else:
        if "/" in target or target.startswith(("~", ".")):
            try:
                path = expanduser(target).resolve()
            except PathError as e:
                print(f"hive: error: {e}", file=err)
                return 2
        else:
            try:
                path = Path(_target_dir(cfg, target)[0])
            except ResolveError as e:
                print(f"hive: error: {e}", file=err)
                return 2
        root = _containing_root(path, roots)
        if root is None:
            print(f"refused: {path} is not under search_roots ({names}); "
                  f"use `hive roots add {path}` after the user confirms", file=err)
            return 3
        if _private_below(path, root):
            print(f"refused: {path} is under a secrets/private path", file=err)
            return 3
        blocks = [path]

    walk = _lister(cfg, roots, out)
    listed = 0
    try:
        for block in blocks:
            if not block.is_dir():
                reason = "missing" if not block.exists() else "not a directory"
                print(f"root={shlex.quote(str(block))} error={shlex.quote(reason)}", file=out)
                continue
            try:
                os.scandir(block).close()
            except OSError as e:
                print(f"root={shlex.quote(str(block))} error={shlex.quote(e.strerror or str(e))}",
                      file=out)
                continue
            print(f"root={shlex.quote(str(block))}", file=out)
            listed += 1
            walk(block, depth)
    except _Cap:
        pass
    return 0 if listed else 1
