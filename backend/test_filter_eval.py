"""Self-check for the 5-day filter self-evaluation (filter_eval.py).
In-memory SQLite, no network. Run: python test_filter_eval.py"""
from datetime import date, timedelta
from types import SimpleNamespace as NS
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database
import models as M
import hourly_prices
import filter_eval as FE
import daily_report

eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
database.Base.metadata.create_all(eng)
database.SessionLocal = sessionmaker(bind=eng)             # run_filter_eval imports it from database
D0 = date(2026, 11, 1)
tok = lambda addr, price=1.0, **kw: {"chain": "Solana", "contract": addr, "symbol": addr[:4], "price_usd": str(price), **kw}
rows = lambda **kw: database.SessionLocal().query(M.FilterEval).filter_by(**kw).all()

# 1. logging: first pending rejection kept, no duplicates; entry within 5 days cancels it
with database.SessionLocal() as s:
    FE.log_rejection(s, tok("AbCdSoLaNa", age_days=7), "age_10d", D0)
    FE.log_rejection(s, tok("abcdsolana"), "buyers_24h", D0 + timedelta(days=2))       # same coin, other case
    s.commit()
r = rows(kind="rejected")
assert len(r) == 1 and r[0].rejected_by == "age_10d" and r[0].contract == "AbCdSoLaNa"   # case kept (Solana)
assert r[0].chain == "solana" and r[0].age_days == 7 and r[0].price0 == 1.0
with database.SessionLocal() as s:
    coin = M.Coin(contract="AbCdSoLaNa", chain="solana", name="x", status="active", entry_price=1.2)
    s.add(coin)
    s.flush()
    FE.log_entry(s, tok("AbCdSoLaNa", liquidity_usd=50_000), coin, D0 + timedelta(days=3))
    s.commit()
assert not rows(kind="rejected") and len(rows(kind="passed")) == 1
p = rows(kind="passed")[0]
assert p.price0 == 1.2 and p.liquidity_usd == 50_000 and p.status == "pending"
with database.SessionLocal() as s:                             # reset for the cadence test
    s.query(M.FilterEval).delete()
    s.commit()

# 2. summary maths: +10% boundary, -15% level, per-filter missed winners, too_strict
R = lambda kind, ret, by=None, **kw: NS(kind=kind, return_pct=ret, rejected_by=by,
                                       age_days=kw.get("age"), gain_14d=kw.get("g14"), liquidity_usd=kw.get("liq"))
sm = FE.summary([R("passed", 10.0, age=40), R("passed", 9.99), R("passed", -15.0, age=12), R("passed", -3),
                 R("rejected", 25, "volume_35"), R("rejected", 10, "volume_35"), R("rejected", -5, "volume_35"),
                 R("rejected", 50, "price_gain"), R("rejected", 1, "age_10d")])
p = sm["passed"]
assert (p["n"], p["wins"], p["win_rate"], p["fell"], p["stop_loss"]) == (4, 1, 25, 2, 1)
assert p["losers_vs_winners"]["age_days"] == (12, 40)
assert sm["rejected"]["volume_35"] == {"n": 3, "wins": 2, "win_rate": 67, "avg": 10.0}
assert sm["rejected"]["price_gain"]["win_rate"] == 100 and sm["too_strict"] == "volume_35"   # price_gain: < 3 rejections
txt = "\n".join(daily_report.filter_eval_lines(sm))
assert "volume_35: رفض 3 · فاتونا 2 رابحين (67%)" in txt and "صارم بزاف: volume_35" in txt
assert daily_report.filter_eval_lines(None) == []

# 3. day-5 measurement + the report cadence (every 5 days, 5 cohorts each)
calls = []


def prices(keys):
    calls.append(len(keys))
    return {c.lower(): 1.2 for _, c in keys if not c.startswith("nop")}, 1, 0


hourly_prices.fetch_live_prices = prices
for k in range(10):                                            # one rejected cohort a day, D0..D9
    with database.SessionLocal() as s:
        FE.log_rejection(s, tok(f"Coin{k}"), "volume_35", D0 + timedelta(days=k))
        if k == 0:
            FE.log_rejection(s, tok("nop0"), "liquidity", D0)     # no DexScreener price
        s.commit()
reports = {}
for d in range(0, 16):
    out = FE.run_filter_eval(D0 + timedelta(days=d))
    if out["report"]:
        reports[d] = out["report"]
    if d == 4:
        assert out["measured"] == 0 and not rows(status="measured")    # nothing reaches day 5 before D5
    if d == 5:
        assert out["measured"] == 2 and len(rows(status="measured")) == 1 and len(rows(status="no_price")) == 1
assert sorted(reports) == [9, 14]                              # every 5 days
assert reports[9]["rejected"]["volume_35"]["n"] == 5 and reports[9]["no_price"] == 1      # cohorts D0..D4
assert reports[14]["rejected"]["volume_35"]["n"] == 5 and reports[14]["no_price"] == 0    # cohorts D5..D9
assert reports[9]["rejected"]["volume_35"]["avg"] == 20.0 and reports[9]["rejected"]["volume_35"]["wins"] == 5
assert max(calls) <= 2                                        # only the coins due that day

# 4. pruning: reported rows older than 30 days are deleted
FE.run_filter_eval(D0 + timedelta(days=9 + FE.KEEP_DAYS + 1))      # > 30 days after its report
assert len(rows()) == 5                                        # the D14 batch is kept
FE.run_filter_eval(D0 + timedelta(days=14 + FE.KEEP_DAYS + 1))
assert rows() == []
# 5. the real save_tokens logs each evaluable rejection with its step (age unknown: not logged)
import save_to_db as S
S.SessionLocal = database.SessionLocal
S.remember_queue_display = lambda *a, **k: None
S.check_revenue = lambda *a, **k: {"verdict": "NO_REVENUE"}
AGES = {"0xyoung": (False, 4, "dexscreener"), "0xnoage": (False, None, "dexscreener")}
S.decide_age = lambda t, session: AGES.get(t["contract"], (True, 30, "dexscreener"))
cand = lambda addr, b=5, sl=1: {"chain": "base", "contract": addr, "name": addr, "symbol": addr, "price_usd": "2",
                                "liquidity_usd": 9_000, "age_days": 30, "buys_24h": b, "sells_24h": sl}
S.save_tokens([cand("0xyoung"), cand("0xnoage"), cand("0xsellers", 1, 9), cand("0xnorev")])
got = {r.contract: r.rejected_by for r in rows(kind="rejected")}
assert got == {"0xyoung": "age_10d", "0xsellers": "buyers_24h", "0xnorev": "revenue"}, got
assert rows(contract="0xyoung")[0].age_days == 4 and rows(contract="0xnorev")[0].price0 == 2.0
print("filter eval self-check: ok")
