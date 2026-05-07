from __future__ import annotations

import pytest


def test_get_node_exe_respects_openlap_node(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "node.fake"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENLAP_NODE", str(fake))
    from node_paths import get_node_exe

    assert get_node_exe() == str(fake)


def test_get_node_exe_returns_str(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENLAP_NODE", raising=False)
    from node_paths import get_node_exe

    p = get_node_exe()
    assert isinstance(p, str)
