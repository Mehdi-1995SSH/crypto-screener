"""Self-check for Phase 2: the dual price gain entry step (volume_check.
check_price_gain) and the -15% stop-loss with its smart recovery
(drop_logic). In-memory SQLite, no network. Run: python test_entry_rules.py"""
import time
from datetime import date, datetime, timedelta, timezone
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import database
import models as M
import volume_check as V
import drop_logic as D
import liquidity_verify as L
import holders_check

# ---------------------------------------------------------------- 1. dual price gain
NOW = time.time()


def prices(days, cur, low14=100.0, low60=None, base=100.0):
    """Hourly [ms, price] points over `days` days ending now: `base`, one dip
    to low60 40 days ago, one dip to low14 5 days ago, `cur` as the last point."""
    pts = []
    for h in range(days * 24, 0, -1):
        ts = NOW - h * 3600
        p = base
        if low60 is not None and abs(h - 40 * 24) < 1:
            p = low60
        if abs(h - 5 * 24) < 1:
            p = low14
        pts.append([ts * 1000, p])
    pts.append([NOW * 1000, cur])
    return pts


def gain(pts):
    key = ("ethereum", "0xabc")
    V._memo[key], V._prices[key] = [1.0] * 5, pts         # as if step 5 fetched it
    return V.check_price_gain("0xABC", "ethereum")


r = gain(prices(60, cur=129, low60=90))                  # +29% vs 14d, +43% vs 60d
assert r["verdict"] == "pass" and r["gain_14d"] == 29.0 and r["gain_60d"] == 43.33 and r["young"] is False
assert r["low_14d"] == 100 and r["low_60d"] == 90 and r["current"] == 129
assert gain(prices(60, cur=130, low60=90))["verdict"] == "pass"                 # exactly +30% passes
assert gain(prices(60, cur=131, low60=90))["verdict"] == "fail"                 # +31% vs the 14-day low
assert gain(prices(60, cur=129, low60=85))["verdict"] == "fail"                 # +51.8% vs the 60-day low
r = gain(prices(40, cur=129, low60=50))                  # young: 40 days, the old dip doesn't count
assert r["verdict"] == "pass" and r["young"] is True and r["low_60d"] is None and r["gain_60d"] is None
assert gain([])["verdict"] == "unknown"

# no extra CoinGecko call: data from step 5 is reused; a transient step-5 failure isn't retried here
calls = []
requests.get = lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("no call expected"))
gain(prices(60, cur=110, low60=95))
V._transient.add(("ethereum", "0xdead"))
assert V.check_price_gain("0xdead", "ethereum")["verdict"] == "unknown"
assert not calls

# ---------------------------------------------------------------- 2. stop-loss (unit)
c = M.Coin(name="x", entry_price=1.0, price_usd=0.8501)
assert D.stop_loss_reason(c) is None                                             # -14.99%
c.price_usd = 0.85
reason = D.stop_loss_reason(c)
assert reason == "Stop-loss - price -15.0% vs entry $1"
assert D.stop_loss_reason(M.Coin(name="y", entry_price=None, price_usd=0.1)) is None
# plain decimals, never scientific notation (Telegram + the Arabic regex)
tiny = D.stop_loss_reason(M.Coin(name="t", entry_price=7.703e-05, price_usd=6e-05))
assert tiny == "Stop-loss - price -22.1% vs entry $0.00007703", tiny
assert D.plain_price(28132.0) == "28132" and D.plain_price(1.13) == "1.13" and D.plain_price(0.0000001234567) == "0.000000123457"
import main, daily_report                                                        # Telegram + Arabic wording
assert daily_report._reason_kind(reason) == "ستوب لوس (-15%)"
assert main.reason_ar(reason) != reason and "وقف الخسارة" in main.reason_ar(reason)
assert "0.00007703" in main.reason_ar(tiny) and "وقف الخسارة" in main.reason_ar(tiny)

# ---------------------------------------------------------------- 3. run_drop_logic: day 1 + smart recovery
eng = create_engine("sqlite://")
database.Base.metadata.create_all(eng)
D.SessionLocal = sessionmaker(bind=eng)
s = D.SessionLocal()
now = datetime.now(timezone.utc).replace(tzinfo=None)
today = date.today()
dropped_at = now - timedelta(days=2)


def coin(name, status, price, entry=1.0, since=None):
    c = M.Coin(contract=f"0x{name}", chain="ethereum", name=name, status=status, price_usd=price,
               entry_price=entry, active_since=since or now - timedelta(days=30), ever_qualified=True)
    s.add(c)
    s.flush()
    return c


fresh = coin("fresh", "active", 0.80, since=now - timedelta(days=1))   # in grace, -20%
ok = coin("ok", "active", 0.90)                                         # -10% -> stays
for name, price, buys, sells in (("small", 0.83, 9, 1),     # +3.75% from the drop price -> stays dropped
                                 ("back", 0.85, 9, 1),      # +6.25%, buyers -> RECOVERS
                                 ("sellers", 0.85, 1, 9),   # +6.25%, sellers dominate -> stays dropped
                                 ("sick", 0.85, 9, 1)):     # +6.25%, buyers, but volume unhealthy -> stays
    c = coin(name, "dropped", price)
    s.add(M.DroppedCoin(coin_id=c.id, dropped_date=dropped_at, reason="Stop-loss - price -20.0% vs entry $1"))
    s.add(M.DailySnapshot(coin_id=c.id, date=dropped_at.date(), price_usd=0.80))
    s.add(M.TradeFlow(chain="ethereum", contract=c.contract, day=today, buy_volume=float(buys),
                      sell_volume=float(sells), buy_count=buys, sell_count=sells))
# Codex-blind coins (no trade_flows row, or 0 / 0): DexScreener 24h txns decide
for name, zero_row in (("blind_buy", False), ("blind_sell", False), ("zero_buy", True)):
    c = coin(name, "dropped", 0.85)
    s.add(M.DroppedCoin(coin_id=c.id, dropped_date=dropped_at, reason="Stop-loss - price -20.0% vs entry $1"))
    s.add(M.DailySnapshot(coin_id=c.id, date=dropped_at.date(), price_usd=0.80))
    if zero_row:
        s.add(M.TradeFlow(chain="ethereum", contract=c.contract, day=today, buy_volume=0.0,
                          sell_volume=0.0, buy_count=0, sell_count=0))
s.commit()
DEX_TXNS = {"0xblind_buy": (30, 10), "0xblind_sell": (10, 30), "0xzero_buy": (12, 5)}
dex_calls = []


def fake_pairs(addresses):
    dex_calls.extend(addresses)
    out = []
    for a in addresses:
        b, sl = DEX_TXNS[a]
        out += [{"chainId": "ethereum", "baseToken": {"address": a}, "txns": {"h24": {"buys": b, "sells": sl}}},
                {"chainId": "bsc", "baseToken": {"address": a}, "txns": {"h24": {"buys": 0, "sells": 999}}}]  # other chain: ignored
    return out


import fetch_dexscreener
fetch_dexscreener.fetch_pairs_for_addresses = fake_pairs

L.coin_evaluation = lambda session, coin, day: {"verdict": L.HEALTHY}           # liquidity healthy
L.store_verdict = L.log_decision = lambda *a, **k: None
D._remove_for_good = lambda *a, **k: False
D.crash_check = lambda *a, **k: (None, None)
D.unhealthy_reason = lambda coin, session, day: "Volume too low (5-day average $10/day, under $2,000)" \
    if coin.name == "sick" else None
D._print_summary = lambda *a, **k: None
holders_check.get_holder_count = lambda *a, **k: 100

stats = D.run_drop_logic()
s = D.SessionLocal()
st = {c.name: c for c in s.query(M.Coin)}
assert st["fresh"].status == "dropped" and stats["stop_loss"] == 1                 # day 1, grace bypassed
assert s.query(M.DroppedCoin).filter_by(coin_id=st["fresh"].id).one().reason == "Stop-loss - price -20.0% vs entry $1"
assert st["ok"].status == "active"
assert st["small"].status == "dropped" and st["sellers"].status == "dropped" and st["sick"].status == "dropped"
assert st["back"].status == "active" and st["back"].entry_price == 0.85          # fresh baseline
assert st["blind_buy"].status == "active" and st["zero_buy"].status == "active"   # DexScreener 24h buys > sells
assert st["blind_sell"].status == "dropped"                                        # DexScreener sellers win
assert sorted(dex_calls) == ["0xblind_buy", "0xblind_sell", "0xzero_buy"]          # only when Codex has no day
assert stats["recovered"] == 3
print("entry rules self-check: ok")
