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

# 2 the coin's own score is never read or mentioned (any score / history -> the same debate)
assert spot == D.debate({**BASE, "score": 5.0, "score_history": [{"score": 5.0}, {"score": 95.0}]})
assert spot == D.debate({k: v for k, v in BASE.items() if k not in ("score", "score_history")})
assert "السكور" not in str(spot) and "score" not in D.metrics(BASE)
assert "المرحلة الحالية:" in spot["final_verdict_ar"]

# 2b the newer rules: 60d extension, volume / liquidity, sources, rank, holders, contract, inflation, unlocks, performance
NOZONE = {**BASE, "entry_zone": {}}                       # no Entry Zone point -> the 60d rule speaks
ext = D.debate({**NOZONE, "price": 2.0, "price_chart": {"series": [["d", 1.0]] * 60}})          # +100% over the 60d low
assert any(x.startswith("صعد") and "فوق قاع 60 يوماً" in x for x in plain(ext["bear_points"]))
near = D.debate({**NOZONE, "price_chart": {"series": [["d", 0.95]] * 60}})                      # +5.3% over the 60d low
assert any(x.startswith("قريب من قاع 60 يوماً") for x in plain(near["bull_points"]))
dup = D.debate({**BASE, "price_chart": {"series": [["d", 0.95]] * 60}})                         # zone point already said it
assert not any("قاع 60 يوماً" in x for x in plain(dup["bull_points"])) and any("منطقة الدخول" in x for x in dup["bull_points"])
liq = lambda v: plain(D.debate({**BASE, "liquidity_now": v})["bull_points"] + D.debate({**BASE, "liquidity_now": v})["bear_points"])
assert any("طلب قوي" in x for x in liq(150)) and any(x.startswith("نشاط ضعيف") for x in liq(10_000))
src = lambda n: D.debate({**BASE, "liquidity_sources": {"n_above": n, "sources_count": 3, "threshold": 5000}})
assert any("السيولة مؤكدة" in x for x in src(3)["bull_points"]) and any("غير مؤكدة" in x for x in src(1)["bear_points"])
rk = lambda c: D.debate({**BASE, "ranks": {"coingecko": {"rank": 783, "was": 783 + c, "change": c},
                                           "cmc": {"rank": 623, "was": 623 + c, "change": c}}})
assert any("ترتيب القيمة السوقية يتحسّن" in x and "#783" in x for x in plain(rk(120)["bull_points"]))
assert any("ترتيب القيمة السوقية يتراجع" in x for x in rk(-40)["bear_points"]) and rk(10) == spot   # < 20 places: no point
assert D.debate({**BASE, "ranks": {"coingecko": 783, "cmc": 623}}) == spot                         # Check a Coin: rank only, no change
risky = D.debate({**BASE, "holders": {"now": 400}, "security_info": {"verified": False, "mint_disabled": False},
                  "supply": {"circulating_pct": 30, "uncapped": True}})
rb = plain(risky["bear_points"])
for frag in ("قاعدة حاملين صغيرة", "غير موثّق", "صلاحية السكّ", "أقل من نصف المعروض", "بلا سقف"):
    assert any(frag in x for x in rb), frag
unl = lambda nxt: D.debate({**BASE, "unlocks": {"status": "ok", "next": nxt}})
assert any("لا يوجد فتح توكنات" in x for x in unl(None)["bull_points"])
soon = {"at": "2099-01-01T00:00:00+00:00", "mcap_pct": 3.0, "usd": 1e6}                         # far future: not imminent
assert any("لا يوجد فتح توكنات" in x for x in unl(soon)["bull_points"])
bb = D.debate({**BASE, "supply": {"circulating_pct": 90, "buyback": {"has_verified_buyback": True, "proofs": ["DEFILLAMA"], "usd_30d": 9399}}})
assert any("إعادة شراء موثّقة (من إيرادات البروتوكول - DefiLlama)" in x and "$9.4K" in x for x in plain(bb["bull_points"]))
pf = lambda c: D.debate({**BASE, "performance": {"change_pct": c, "days_held": 5}})
assert any(x.startswith("أداء قوي") for x in pf(20)["bull_points"]) and any(x.startswith("أداء ضعيف") for x in pf(-20)["bear_points"])

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
