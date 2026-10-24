"""Path helpers shared by the CLI and the library modules."""

import sys
from pathlib import Path


class PathError(Exception):
    """A user-supplied path cannot be used; the message names the path."""


def expanduser(text):
    """Path(text).expanduser(), but an unknown ~user raises PathError, not RuntimeError."""
    try:
        return Path(text).expanduser()
    except RuntimeError:
        raise PathError(f"{text}: unknown user in ~ path") from None


def secret_check_parts(path):
    """Path(path).parts for the secrets/private check.

    On macOS /tmp, /var and /etc live under /private, so a leading absolute /private is
    dropped; a deeper private component, or ~/private, still counts.
    """
    parts = Path(path).parts
    if sys.platform == "darwin" and parts[:2] == ("/", "private"):
        return parts[:1] + parts[2:]
    return parts
