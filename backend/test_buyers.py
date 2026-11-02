"""Self-check for entry step 3b (24h buys > sells, pre-queue). In-memory
SQLite, no network. Run: python test_buyers.py"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import database
import models as M
import fetch_dexscreener as F
import save_to_db as S

# 1. build_token_records: 24h txns summed over the token's pools on its chain
pair = lambda chain, addr, b, s, liq=1000: {"chainId": chain, "baseToken": {"address": addr, "name": "X", "symbol": "X"},
                                           "liquidity": {"usd": liq}, "txns": {"h24": {"buys": b, "sells": s}}}
recs = {(r["chain"], r["contract"]): r for r in F.build_token_records(
    [pair("base", "0xa", 30, 10), pair("base", "0xa", 21, 40), pair("bsc", "0xa", 0, 999)])}
assert (recs[("base", "0xa")]["buys_24h"], recs[("base", "0xa")]["sells_24h"]) == (51, 50)
assert recs[("bsc", "0xa")]["sells_24h"] == 999                                   # its own record, not mixed in

# 2. the rule: buys > sells (even by 1); ties / more sells -> skipped; no data passes
ok = lambda b, s: F.buyers_ok({"buys_24h": b, "sells_24h": s})
assert ok(51, 50) and not ok(50, 50) and not ok(49, 50) and ok(0, 0) and F.buyers_ok({})
assert not ok(0, 1) and ok(1, 0)

# 3. save_tokens: a seller-heavy coin is skipped BEFORE the queue (not analyzed),
#    and enters the queue on a later run once buys > sells
eng = create_engine("sqlite://")
database.Base.metadata.create_all(eng)
S.SessionLocal = sessionmaker(bind=eng)
S.decide_age = lambda t, session: (True, 30, "dexscreener")
S.check_revenue = lambda *a, **k: {"verdict": "NO_REVENUE"}                       # stop right after the queue
S.remember_queue_display = lambda *a, **k: None


def token(addr, b, s):
    return {"chain": "base", "contract": addr, "name": addr, "symbol": addr.upper(), "price_usd": "1",
            "liquidity_usd": 50_000, "volume_24h": 10_000, "age_days": 30, "buys_24h": b, "sells_24h": s}


def queued():
    with S.SessionLocal() as s:
        return {q.contract: q.last_checked is not None for q in s.query(M.CoinQueue)}


stats = S.save_tokens([token("0xsell", 50, 50), token("0xbuy", 51, 50), token("0xnone", 0, 0)])
assert stats["skipped_sellers"] == 1 and stats["passed_age"] == 3
q = queued()
assert "0xsell" not in q                                    # skipped today: not queued, not analyzed
assert q.get("0xbuy") is True and q.get("0xnone") is True   # went on to the analyzed steps

stats = S.save_tokens([token("0xsell", 80, 20)])            # "tomorrow": buyers now dominate
assert stats["skipped_sellers"] == 0 and queued().get("0xsell") is True
print("buyers self-check: ok")
