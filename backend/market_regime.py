"""
market_regime.py
----------------
MARKET REGIME banner (website, above the Active table) - OBSERVATION ONLY: no
entry filter, score, drop or stop-loss rule reads it. BTC, ETH, SOL:
  30d / 7d = CoinGecko's price_change_percentage_30d / _7d_in_currency
  market   = the average of the three, per window; >= 0% = up, < 0% = down
States (market 30d / 7d): up/up 🟢 Bullish, up/down 🟡 Pullback in uptrend,
down/up 🔵 Recovery / bounce, down/down 🔴 Bearish.
ONE CoinGecko call (/coins/markets) every REFRESH_DAYS days (the daily run
skips it in between) -> one row in market_regime; /api/coins serves the
latest (0 calls per page view).
"""
import json
from datetime import date

import requests

CG_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
MAJORS = [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")]
REFRESH_DAYS = 7
STATES = {(True, True): ("bullish", "🟢", "Bullish", "صاعد"),
          (True, False): ("pullback", "🟡", "Pullback in uptrend", "تصحيح مؤقت في اتجاه صاعد"),
          (False, True): ("recovery", "🔵", "Recovery / bounce", "ارتداد وتعافي في اتجاه هابط"),
          (False, False): ("bearish", "🔴", "Bearish", "هابط")}


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _pct(v):
    return None if v is None else round(v, 2)


def from_markets(rows):
    """The banner's dict from a /coins/markets response (pure)."""
    by_id = {r.get("id"): r for r in rows or []}
    coins = []
    for cg_id, sym in MAJORS:
        r = by_id.get(cg_id) or {}
        coins.append({"symbol": sym, "price": r.get("current_price"),
                      "d30": _pct(r.get("price_change_percentage_30d_in_currency")),
                      "d7": _pct(r.get("price_change_percentage_7d_in_currency"))})
    m30, m7 = _avg(c["d30"] for c in coins), _avg(c["d7"] for c in coins)
    out = {"coins": coins, "market": {"d30": m30, "d7": m7}, "state": None}
    if m30 is not None and m7 is not None:
        key, emoji, label, label_ar = STATES[(m30 >= 0, m7 >= 0)]
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
        r = requests.get(CG_MARKETS_URL, params={"vs_currency": "usd", "ids": ",".join(i for i, _ in MAJORS),
                                                 "price_change_percentage": "7d,30d"},
                         headers={"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}, timeout=30)
        r.raise_for_status()
        data = {**from_markets(r.json()), "as_of": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}
        if not data["state"]:
            raise ValueError("CoinGecko returned no 30d / 7d changes")   # keep the last good row
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
