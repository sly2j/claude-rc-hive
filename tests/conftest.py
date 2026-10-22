"""Isolation for every test: a temp HOME and XDG_CONFIG_HOME, no Claude config dir, no tmux client env.

hive's default tmux socket is the user's live hive; tests must never reach it or the real
config. Tests that need their own paths override these with monkeypatch or an env dict.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("isolated")
    (base / "home").mkdir()
    monkeypatch.setenv("HOME", str(base / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "xdg"))
    for name in ("CLAUDE_CONFIG_DIR", "TMUX", "TMUX_PANE"):
        monkeypatch.delenv(name, raising=False)
