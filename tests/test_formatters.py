from __future__ import annotations

from telegram.formatters import escape_md, format_holdings


def test_escape_md_basic():
    s = "*bold* _i_ [x](y)"
    e = escape_md(s)
    # Should contain backslashes added
    assert "\\*bold\\*" in e
    assert "\\_i\\_" in e


def test_format_holdings_snapshot():
    snap = {
        "CRO": {"qty": "10", "price_usd": "0.05", "usd": "0.5"},
        "USDT": {"qty": "5", "price_usd": "1", "usd": "5"},
    }
    text = format_holdings(snap)
    assert "Holdings snapshot:" in text
    assert "Total" in text
