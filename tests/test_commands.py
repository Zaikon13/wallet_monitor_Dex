from __future__ import annotations

import telegram.commands as cmds


class DummyState(dict):
    def get(self, k, d=None):  # keep default behavior
        return super().get(k, d)


def test_handle_status_and_weekly(monkeypatch):
    dummy = DummyState(snapshot={}, snapshot_total_usd="0.05")
    monkeypatch.setattr(cmds, "get_state", lambda: dummy)
    monkeypatch.setattr(cmds, "build_weekly_report_text", lambda days, wallet: f"Weekly({days}) for {wallet}")

    out = cmds.handle_status()
    assert out == "Cronos DeFi Sentinel online"

    week = cmds.handle_weekly(3)
    assert week.startswith("Weekly(3)")


def test_handle_holdings(monkeypatch):
    monkeypatch.setattr(cmds, "holdings_snapshot", lambda: {"CRO": {"qty": "2.0", "usd": "0.10"}})
    text = cmds.handle_holdings(limit=10)
    assert "Holdings" in text
    assert "CRO" in text
