"""Self-check for the Market Regime top bar (market_regime.py): BTC / ETH / SOL
price + 24h / 7d / 30d, the 4 overall states (0% counts as up), averages, the
daily refresh and latest()'s safety. In-memory SQLite, no network.
Run: python test_market_regime.py"""
import json
from datetime import date, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database
import models as M
import market_regime as R

row = lambda cg_id, d30, d7, d1=0.5, price=1.0: {"id": cg_id, "current_price": price,
    "price_change_percentage_30d_in_currency": d30, "price_change_percentage_7d_in_currency": d7,
    "price_change_percentage_24h_in_currency": d1}
three = lambda d30, d7: [row("bitcoin", d30, d7), row("ethereum", d30, d7), row("solana", d30, d7)]

# 1. the 4 states from the market's 30d / 7d (0% counts as up); 24h never decides
for d30, d7, state in ((5, 2, "bullish"), (5, -2, "pullback"), (-5, 2, "recovery"), (-5, -2, "bearish"),
                       (0, 0, "bullish"), (0, -0.01, "pullback"), (-0.01, 0, "recovery")):
    assert R.from_markets(three(d30, d7))["state"] == state, (d30, d7)
d = R.from_markets(three(-5, -2))
assert (d["emoji"], d["label_ar"]) == ("🔴", "هبوط وضغط بيعي (حذر)")
assert [R.from_markets(three(*x))["label_ar"] for x in ((5, 2), (5, -2), (-5, 2))] ==        ["صاعد / إيجابي", "تصحيح صحي (فرصة انتقاء)", "ارتداد وتعافي"]
assert R.from_markets([row("bitcoin", 5, 2, d1=-30), row("ethereum", 5, 2, d1=-30)])["state"] == "bullish"

# 2. each coin keeps its price + 24h / 7d / 30d; the market averages the coins that have data
d = R.from_markets([row("bitcoin", 10, -9, d1=3.456, price=80272.5), row("ethereum", -4, 3), row("solana", -3, 3)])
assert d["market"] == {"d30": 1.0, "d7": -1.0} and d["state"] == "pullback"
assert [c["symbol"] for c in d["coins"]] == ["BTC", "ETH", "SOL"]
assert d["coins"][0] == {"symbol": "BTC", "price": 80272.5, "d1": 3.46, "d7": -9, "d30": 10}
d = R.from_markets([row("bitcoin", 6, 2), row("ethereum", None, None)])
assert d["market"] == {"d30": 6.0, "d7": 2.0} and d["coins"][2]["d30"] is None and d["coins"][2]["price"] is None
assert R.from_markets([])["state"] is None and R.from_markets(None)["state"] is None

# 3. refresh once a day: same day -> stored row, no call; next day or force -> one call
eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
database.Base.metadata.create_all(eng)
database.SessionLocal = sessionmaker(bind=eng)
calls = []


class Resp:
    def __init__(self, body): self.body = body
    def raise_for_status(self): pass
    def json(self): return self.body


def fake_get(url, params=None, **kw):
    calls.append((url, params))
    return Resp(three(-5 + len(calls), 1))                      # changes every call


R.requests.get = fake_get
D0 = date(2026, 11, 2)
assert R.run_market_regime(D0)["market"]["d30"] == -4.0 and len(calls) == 1
assert calls[0][0].endswith("/coins/markets") and calls[0][1]["ids"] == "bitcoin,ethereum,solana"
assert calls[0][1]["price_change_percentage"] == "24h,7d,30d"
assert R.run_market_regime(D0)["market"]["d30"] == -4.0 and len(calls) == 1         # same day: no call
assert R.run_market_regime(D0 + timedelta(days=1))["market"]["d30"] == -3.0 and len(calls) == 2
assert R.run_market_regime(D0 + timedelta(days=1), force=True)["market"]["d30"] == -2.0 and len(calls) == 3
with database.SessionLocal() as s:
    days = [r.day for r in s.query(M.MarketRegime).order_by(M.MarketRegime.day)]
    assert days == [D0, D0 + timedelta(days=1)]                                  # same-day re-run: updated, not duplicated
    assert R.latest(s)["market"]["d30"] == -2.0

# a CoinGecko answer without changes never replaces the last good row
R.requests.get = lambda *a, **k: Resp([])
try:
    R.run_market_regime(D0 + timedelta(days=30))
    raise AssertionError("expected a failure")
except ValueError:
    pass
with database.SessionLocal() as s:
    assert R.latest(s)["market"]["d30"] == -2.0

# 4. latest() never raises (missing table) - /api/coins must still load
broken = sessionmaker(bind=create_engine("sqlite://"))()
assert R.latest(broken) is None
print("market regime self-check: ok")
