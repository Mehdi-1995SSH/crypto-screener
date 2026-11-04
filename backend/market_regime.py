"""
market_regime.py
----------------
MARKET REGIME (website top status bar) - OBSERVATION ONLY: no entry filter,
score, drop or stop-loss rule reads it. BTC 24h / 5d / 30d change from ONE
CoinGecko call a day (/coins/bitcoin/market_chart, 31 days, hourly):
  🔴 bearish   30d <= -10% or 5d <= -8%
  🟡 pullback  otherwise, 5d < -2% or 30d < 0%
  🟢 bullish   otherwise                (24h is shown, not used)
One row in market_regime per day; /api/coins serves the latest (0 calls per page view).
"""
import json
from datetime import date

import requests

CG_CHART_URL = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
REFRESH_DAYS = 1
STATES = {"bullish": ("🟢", "Positive / stable", "إيجابي / مستقر"),
          "pullback": ("🟡", "Healthy pullback", "تصحيح صحي (فرصة انتقاء)"),
          "bearish": ("🔴", "Downtrend, selling pressure", "هبوط وضغط بيعي (حذر)")}


def classify(d5, d30):
    if d30 <= -10 or d5 <= -8:
        return "bearish"
    return "pullback" if d5 < -2 or d30 < 0 else "bullish"


def from_chart(prices):
    """The bar's dict from market_chart prices [[ms, usd], ...] (pure): the change
    vs the newest point at least 24 h / 5 d / 30 d before the latest one."""
    pts = sorted(p for p in prices or [] if p and p[1])
    if not pts:
        return {"btc": None, "state": None}
    t, now = pts[-1]

    def chg(sec):
        past = [p for p in pts if p[0] <= t - sec * 1000]
        return round((now / past[-1][1] - 1) * 100, 2) if past else None
    btc = {"price": now, "d1": chg(86400), "d5": chg(5 * 86400), "d30": chg(30 * 86400)}
    out = {"btc": btc, "state": None}
    if btc["d5"] is not None and btc["d30"] is not None:
        key = classify(btc["d5"], btc["d30"])
        emoji, label, label_ar = STATES[key]
        out.update(state=key, emoji=emoji, label=label, label_ar=label_ar)
    return out


def run_market_regime(today=None, force=False):
    """Daily step: every REFRESH_DAYS days (or force), one CoinGecko call ->
    a new market_regime row; in between, the stored row is returned as is."""
    from discovery import CG_KEY
    from database import SessionLocal, utcnow
    from models import MarketRegime
    today = today or date.today()
    session = SessionLocal()
    try:
        row = session.query(MarketRegime).order_by(MarketRegime.day.desc()).first()
        if row and not force and (today - row.day).days < REFRESH_DAYS:
            return json.loads(row.data)                     # still fresh - no call
        r = requests.get(CG_CHART_URL, params={"vs_currency": "usd", "days": 31},
                         headers={"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}, timeout=30)
        r.raise_for_status()
        data = {**from_chart(r.json().get("prices")), "as_of": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}
        if not data["state"]:
            raise ValueError("CoinGecko returned no BTC 5d / 30d history")   # keep the last good row
        new = session.query(MarketRegime).filter_by(day=today).first() or MarketRegime(day=today)
        new.data, new.computed_at = json.dumps(data), utcnow()
        session.add(new)
        session.commit()
        return data
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def latest(session):
    """The newest stored banner, or None (never raises - the Active table must load)."""
    from models import MarketRegime
    try:
        row = session.query(MarketRegime).order_by(MarketRegime.day.desc()).first()
        return json.loads(row.data) if row else None
    except Exception:
        session.rollback()
        return None
