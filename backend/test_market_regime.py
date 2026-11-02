"""Self-check for the Market Regime banner (market_regime.py): the 4 states,
the 0% boundary, averages, the 7-day refresh and latest()'s safety.
In-memory SQLite, no network. Run: python test_market_regime.py"""
import json
from datetime import date, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database
import models as M
import market_regime as R

row = lambda cg_id, d30, d7: {"id": cg_id, "current_price": 1.0, "price_change_percentage_30d_in_currency": d30,
                              "price_change_percentage_7d_in_currency": d7}
three = lambda d30, d7: [row("bitcoin", d30, d7), row("ethereum", d30, d7), row("solana", d30, d7)]

# 1. the 4 states, with 0% counting as up
for d30, d7, state in ((5, 2, "bullish"), (5, -2, "pullback"), (-5, 2, "recovery"), (-5, -2, "bearish"),
                       (0, 0, "bullish"), (0, -0.01, "pullback"), (-0.01, 0, "recovery")):
    assert R.from_markets(three(d30, d7))["state"] == state, (d30, d7)
d = R.from_markets(three(-5, -2))
assert (d["emoji"], d["label"], d["label_ar"]) == ("🔴", "Bearish", "هابط")

# 2. averages per window (a coin with no data is left out); none at all -> no state
d = R.from_markets([row("bitcoin", 10, -9), row("ethereum", -4, 3), row("solana", -3, 3)])
assert d["market"] == {"d30": 1.0, "d7": -1.0} and d["state"] == "pullback"
assert [c["symbol"] for c in d["coins"]] == ["BTC", "ETH", "SOL"] and d["coins"][0]["d7"] == -9
d = R.from_markets([row("bitcoin", 6, 2), row("ethereum", None, None)])
assert d["market"] == {"d30": 6.0, "d7": 2.0} and d["coins"][2]["d30"] is None
assert R.from_markets([])["state"] is None

# 3. refresh every 7 days: < 7 days -> stored row, no call; >= 7 days or force -> one call
eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
database.Base.metadata.create_all(eng)
database.SessionLocal = sessionmaker(bind=eng)
calls = []


class Resp:
    def __init__(self, body): self.body = body
    def raise_for_status(self): pass
    def json(self): return self.body


def fake_get(url, params=None, **kw):
    calls.append(params)
    return Resp(three(-5 + len(calls), 1))                      # changes every call


R.requests.get = fake_get
D0 = date(2026, 11, 2)
assert R.run_market_regime(D0)["market"]["d30"] == -4.0 and len(calls) == 1
assert calls[0]["price_change_percentage"] == "7d,30d" and "sparkline" not in calls[0]
for k in range(1, 7):                                          # days 1-6: no call
    assert R.run_market_regime(D0 + timedelta(days=k))["market"]["d30"] == -4.0
assert len(calls) == 1
assert R.run_market_regime(D0 + timedelta(days=7))["market"]["d30"] == -3.0 and len(calls) == 2
assert R.run_market_regime(D0 + timedelta(days=8), force=True)["market"]["d30"] == -2.0 and len(calls) == 3
R.run_market_regime(D0 + timedelta(days=8), force=True)          # same-day re-run: updated, not duplicated
with database.SessionLocal() as s:
    days = [r.day for r in s.query(M.MarketRegime).order_by(M.MarketRegime.day)]
    assert days == [D0, D0 + timedelta(days=7), D0 + timedelta(days=8)]
    assert R.latest(s)["market"]["d30"] == -1.0                    # the newest row

# a CoinGecko answer without changes never replaces the last good row
R.requests.get = lambda *a, **k: Resp([])
try:
    R.run_market_regime(D0 + timedelta(days=30))
    raise AssertionError("expected a failure")
except ValueError:
    pass
with database.SessionLocal() as s:
    assert R.latest(s)["market"]["d30"] == -1.0

# 4. latest() never raises (missing table) - /api/coins must still load
broken = sessionmaker(bind=create_engine("sqlite://"))()
assert R.latest(broken) is None
print("market regime self-check: ok")
