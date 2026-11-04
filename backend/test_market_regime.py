"""Self-check for the Market Regime top bar (market_regime.py): BTC 24h / 5d /
30d from an hourly chart, the 3 states and their boundaries, the daily refresh
and latest()'s safety. In-memory SQLite, no network. Run: python test_market_regime.py"""
import json
from datetime import date, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database
import models as M
import market_regime as R

H = 3600 * 1000
def chart(d1=0.0, d5=0.0, d30=0.0, days=31, now=100.0):
    """Hourly [[ms, usd]] ending at `now`, with the price 24 h / 5 d / 30 d ago set so the changes are d1 / d5 / d30 %."""
    hours = days * 24
    pts = [[i * H, now] for i in range(hours + 1)]
    for back, chg in ((30 * 24, d30), (5 * 24, d5), (24, d1)):
        if back <= hours:
            pts[hours - back][1] = now / (1 + chg / 100)
    return pts

# 1. 24h / 5d / 30d vs the newest point at least that old
b = R.from_chart(chart(d1=2.0, d5=-3.0, d30=5.0))["btc"]
assert (b["price"], b["d1"], b["d5"], b["d30"]) == (100.0, 2.0, -3.0, 5.0)
assert R.from_chart(chart(days=20))["state"] is None and R.from_chart(chart(days=20))["btc"]["d30"] is None
assert R.from_chart([]) == {"btc": None, "state": None} and R.from_chart(None)["state"] is None

# 2. the 3 states and their boundaries (24h never decides)
for d5, d30, state in ((0, 0, "bullish"), (-2, 0, "bullish"), (-2.01, 0, "pullback"), (5, -0.01, "pullback"),
                       (-7.99, -9.99, "pullback"), (-8, 5, "bearish"), (5, -10, "bearish"), (4.24, -5.92, "pullback")):
    assert R.classify(d5, d30) == state, (d5, d30)
d = R.from_chart(chart(d1=-9.0, d5=1.0, d30=1.0))
assert (d["state"], d["emoji"], d["label_ar"]) == ("bullish", "🟢", "إيجابي / مستقر")
d = R.from_chart(chart(d5=-9.0, d30=1.0))
assert (d["state"], d["emoji"], d["label_ar"]) == ("bearish", "🔴", "هبوط وضغط بيعي (حذر)")
assert R.from_chart(chart(d5=-3.0, d30=1.0))["label_ar"] == "تصحيح صحي (فرصة انتقاء)"

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
    return Resp({"prices": chart(d30=-5.0 + len(calls))})          # changes every call


R.requests.get = fake_get
D0 = date(2026, 11, 2)
assert R.run_market_regime(D0)["btc"]["d30"] == -4.0 and len(calls) == 1
assert calls[0][0].endswith("/coins/bitcoin/market_chart") and calls[0][1]["days"] == 31
assert R.run_market_regime(D0)["btc"]["d30"] == -4.0 and len(calls) == 1          # same day: no call
assert R.run_market_regime(D0 + timedelta(days=1))["btc"]["d30"] == -3.0 and len(calls) == 2
assert R.run_market_regime(D0 + timedelta(days=1), force=True)["btc"]["d30"] == -2.0 and len(calls) == 3
with database.SessionLocal() as s:
    days = [r.day for r in s.query(M.MarketRegime).order_by(M.MarketRegime.day)]
    assert days == [D0, D0 + timedelta(days=1)]                                   # same-day re-run: updated, not duplicated
    assert R.latest(s)["btc"]["d30"] == -2.0

# a CoinGecko answer without history never replaces the last good row
R.requests.get = lambda *a, **k: Resp({"prices": []})
try:
    R.run_market_regime(D0 + timedelta(days=30))
    raise AssertionError("expected a failure")
except ValueError:
    pass
with database.SessionLocal() as s:
    assert R.latest(s)["btc"]["d30"] == -2.0

# 4. latest() never raises (missing table) - /api/coins must still load
broken = sessionmaker(bind=create_engine("sqlite://"))()
assert R.latest(broken) is None
print("market regime self-check: ok")
