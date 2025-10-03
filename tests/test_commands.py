from __future__ import annotations

from types import SimpleNamespace

import telegram.commands as cmds


class DummyState(dict):
    def get(self, k, d=None):  # keep default behavior
        return super().get(k, d)


def test_handle_status_and_weekly(monkeypatch):
    # Stub state getters
    dummy = DummyState(snapshot={"CRO": {"qty": "1", "usd": "0.05"}}, snapshot_total_usd="0.05")
    monkeypatch.setattr(cmds, "get_state", lambda: dummy)

    # Stub weekly builder to avoid IO
    monkeypatch.setattr(cmds, "build_weekly_report_text", lambda days, wallet: f"Weekly({days}) for {wallet}")

    # Environment wallet not required; builder can handle empty
    out = cmds.handle_status()
    assert "Status:" in out
    assert "Estimated" in out

    week = cmds.handle_weekly(3)
    assert week.startswith("Weekly(3)")


def test_handle_holdings(monkeypatch):
    # Stub snapshot getter used by holdings/status
    monkeypatch.setattr(cmds, "get_snapshot", lambda: {"CRO": {"qty": "2.0", "usd": "0.10"}})
    text = cmds.handle_holdings(limit=10)
    assert "Holdings" in text
    assert "CRO" in text
