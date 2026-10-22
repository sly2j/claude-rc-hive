"""Path helpers shared by the CLI and the library modules."""

from pathlib import Path


class PathError(Exception):
    """A user-supplied path cannot be used; the message names the path."""


def expanduser(text):
    """Path(text).expanduser(), but an unknown ~user raises PathError, not RuntimeError."""
    try:
        return Path(text).expanduser()
    except RuntimeError:
        raise PathError(f"{text}: unknown user in ~ path") from None
