"""Self-check for debate_verdict.py (no network, no DB). Run: python test_debate.py"""
import copy

import debate_verdict as D

LTR = dict.fromkeys(map(ord, "⁦⁩"))            # strip the LTR isolates for readable asserts
plain = lambda xs: [x.translate(LTR) for x in xs]
bar = lambda v, b=50, s=40: {"volume": v, "buyers": b, "sellers": s, "buys": b * 2, "sells": s * 2}
BASE = {"status": "active", "price": 1.0, "mcap": 10_000_000, "score": 62.0,
        "score_history": [{"score": 62.0}, {"score": 50.0}],
        "entry_zone": {"position_pct": 20, "above_low_pct": 12.0},
        "price_range": {"position_pct": 50, "ath_change_pct": -60, "atl_change_pct": 80},
        "rel_strength": {"verdict": "STRONG", "line": "صاعد +12% بينما BTC -1%", "coin_24h": 12},
        "tx_bars": [bar(100)] * 5 + [bar(200)] * 5,
        "security": "safe", "security_info": {"verified": True, "mint_disabled": True, "buy_tax": 0, "sell_tax": 0},
        "listings": {"monitored": 12, "listed": [{}] * 6, "delisted": [], "scheduled": []},
        "futures": {"matched": False}, "supply": {"circulating_pct": 90}}

# 1 futures: unmatched adds nothing; matched + negative funding adds a bull point
spot = D.debate(BASE)
assert spot and not any("تمويل" in x or "OI" in x for x in spot["bull_points"] + spot["bear_points"])
assert spot == D.debate({**BASE, "futures": {"matched": False, "history": [{"funding": -0.5, "oi": 1}]}})
fut = D.debate({**BASE, "futures": {"matched": True, "history": [{"funding": 0.0, "oi": 100, "long_pct": 50},
                                                                 {"funding": -0.8, "oi": 210, "long_pct": 45}]}})
assert any("Short Squeeze" in x for x in fut["bull_points"]) and fut["bull_score"] == spot["bull_score"] + 10   # funding 6 + OI 4

# 2 score trajectory: entry score wins over the 7-day window; dropped coins fall back to their last recorded score
d = D.debate(BASE, entry_score=40.0)
assert "من 40 إلى 62 (+22)" in " ".join(plain(d["bull_points"])) and "منذ دخولها المتابعة" in d["final_verdict_ar"]
assert D.metrics({**BASE, "score": None})["score"] == 62.0

# 3 dropped: drop-reason point, stage, red outlook (unless clearly positive)
dropped = D.debate({**BASE, "status": "dropped"}, drop={"date": "2026-11-04", "reason_ar": "الحجم يتراجع"})
assert any(x.startswith("استُبعدت من المتابعة في") for x in dropped["bear_points"])
assert dropped["stage_ar"] == "عملة مستبعدة من المتابعة"
net = dropped["bull_score"] - dropped["risk_score"]
assert dropped["direction_outlook_ar"] == (D.YELLOW if net >= 25 else D.RED)

# 4 Check a Coin payload: the filter's own volume number, verdict as a point (None = not finished -> no point)
check = {**BASE, "verdict": {"passes": False, "reason": "الحجم ينخفض"}, "volume": {"pct": -48.0},
         "score": {"total": 17.0}, "liquidity": {"now": 200_000, "pct": -38.6}}
c = D.debate(check)
bp = plain(c["bear_points"])
assert any(x.startswith("حجم التداول ينخفض (-48%") for x in bp) and "لا تجتاز الفلتر: الحجم ينخفض" in bp
assert not any("على DEX" in x for x in plain(c["bull_points"] + c["bear_points"]))
assert not any("الفلتر" in x for x in D.debate({**check, "verdict": {"passes": None, "reason": "?"}})["bear_points"])
assert any("على DEX" in x for x in plain(spot["bull_points"]))       # detail page: DEX bars, labelled

# 5 security: an unsafe honeypot is a heavy risk
assert D.debate({**BASE, "security": "unsafe"})["risk_score"] >= 25

# 6 direction thresholds + confidence cap
m = D.metrics(BASE)
assert D.risk_manager_verdict(40, 20, m)["direction_outlook_ar"] == D.GREEN
assert D.risk_manager_verdict(30, 20, m)["direction_outlook_ar"] == D.YELLOW
assert D.risk_manager_verdict(10, 25, m)["direction_outlook_ar"] == D.RED
assert D.risk_manager_verdict(100, 0, m)["confidence_score"] == 95
assert D.risk_manager_verdict(0, 0, m)["confidence_score"] == 0

# 7 deterministic, sorted by weight, never raises
assert D.debate(copy.deepcopy(BASE)) == D.debate(copy.deepcopy(BASE))
assert D.debate({}) is not None and D.debate({})["bull_points"] == []
assert "ليس نصيحة مالية" in spot["final_verdict_ar"]
print("debate self-check: ok")
