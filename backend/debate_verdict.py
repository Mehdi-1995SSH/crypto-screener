"""
debate_verdict.py
-----------------
Bull vs Bear debate + Risk Manager verdict (Arabic), built ONLY from the
numbers already in a coin's API payload (detail page or Check a Coin): no
API call, no LLM, nothing stored. Deterministic - same payload, same text.

Each rule adds a weighted Arabic point to the bull or bear side; a side's
score is the sum of its weights (capped at 100). Categories with no data
(e.g. futures on a spot-only coin) add nothing to either side. The coin's
own screener score is never read or mentioned - only market / on-chain data.
Not available in the app, so never used: CEX orderbook depth (bid / ask
walls) and top-10 holder % (only top-holder addresses are stored).
"""
from datetime import datetime, timezone

from relative_strength import _ltr

BUYBACK_AR = {"POOL_BURN": "شراء من المجمّع ثم حرق", "TEAM_BUY_BURNED": "شراء الفريق ثم الحرق",
              "TEAM_BUY_RETAINED": "شراء الفريق والاحتفاظ", "DEFILLAMA": "من إيرادات البروتوكول - DefiLlama",
              "DAO_VOTE": "تصويت DAO ناجح"}
GREEN, YELLOW, RED = "🟢 مرشحة للصعود", "🟡 محايد - انتظار تأكيد", "🔴 مرشحة للهبوط - مخاطر مرتفعة"


def _p(x, d=1, sign=True):
    return _ltr(format(x, f"{'+' if sign else ''}.{d}f") + "%")


def _usd(v):
    a = abs(v)
    s = f"${v / 1e9:.2f}B" if a >= 1e9 else f"${v / 1e6:.2f}M" if a >= 1e6 else f"${v / 1e3:.1f}K" if a >= 1e3 else f"${v:,.0f}"
    return _ltr(s)


def _n(v):
    return _ltr(f"{v:,.0f}")


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ---------------------------------------------------------------- metrics
def metrics(p, drop=None, dao=None):
    """Flatten a detail-page or Check a Coin payload into the numbers the agents read."""
    ez, pr, rs = p.get("entry_zone") or {}, p.get("price_range") or {}, p.get("rel_strength") or {}
    sp = p.get("supply") or {}
    bars = [b for b in p.get("tx_bars") or [] if b.get("volume") is not None]
    check = "verdict" in p                                      # Check a Coin payload
    price = p.get("live_price") if p.get("price_is_live") else p.get("price")
    series = [c for _, c in ((p.get("price_chart") or {}).get("series") or []) if c]
    low = lambda n: min(series[-n:]) if len(series) >= n else None
    pc = p.get("price_chart") or {}
    low14, low60 = pc.get("low_14d") or low(14), pc.get("low_60d") or low(60)
    liq = (p.get("liquidity") or {}).get("now") if check else (p.get("liquidity_now") or p.get("liquidity"))
    liq_chg = (p.get("liquidity") or {}).get("pct") if check else (p.get("liq_growth_pct") or {}).get("7d")
    fut = p.get("futures") or {}
    fh = fut.get("history") or [] if fut.get("matched") else []
    lst = p.get("listings") or {}
    rev = p.get("revenue") or {}
    unl = (p.get("unlocks") or {}).get("next") or {}
    sec = p.get("security_info") or {}
    last7 = bars[-7:]
    rk = p.get("ranks") or {}                     # detail: {"rank", "was", "change"}; Check a Coin: a bare rank
    rank = lambda k: (rk.get(k) or {}).get("rank") if isinstance(rk.get(k), dict) else rk.get(k)
    ls = p.get("liquidity_sources") or {}
    perf = p.get("performance") or {}
    return {
        "check": check, "status": "check" if check else p.get("status"), "drop": drop,
        "verdict": p.get("verdict"), "mcap": p.get("mcap"), "price": price,
        "ez_pos": ez.get("position_pct"), "ez_above_low": ez.get("above_low_pct"),
        "above_14": (price / low14 - 1) * 100 if price and low14 else None,
        "above_60": (price / low60 - 1) * 100 if price and low60 else None,
        "ath_chg": pr.get("ath_change_pct"), "atl_chg": pr.get("atl_change_pct"), "ath_pos": pr.get("position_pct"),
        "rs": rs.get("verdict"), "rs_line": rs.get("line"), "coin_24h": rs.get("coin_24h"),
        # Check a Coin: the filter's own 5-day volume change (all exchanges); detail page: DEX bars only
        "vol_trend": (p.get("volume") or {}).get("pct") if check else
                     ((_avg([b["volume"] for b in bars[-5:]]) / _avg([b["volume"] for b in bars[-10:-5]]) - 1) * 100
                      if len(bars) >= 10 and _avg([b["volume"] for b in bars[-10:-5]]) else None),
        "vol_src": "حجم التداول" if check else "حجم التداول على DEX",
        "turnover": bars[-1]["volume"] / p["mcap"] * 100 if bars and p.get("mcap") else None,
        "vol_liq": bars[-1]["volume"] / liq if bars and liq else None,
        "buyers": sum(b.get("buyers") or 0 for b in last7), "sellers": sum(b.get("sellers") or 0 for b in last7),
        "buys": sum(b.get("buys") or 0 for b in last7), "sells": sum(b.get("sells") or 0 for b in last7),
        "holders": (p.get("holders") or {}).get("now"), "holders_chg": (p.get("holders") or {}).get("week_change_pct"),
        "honeypot": p.get("honeypot") or p.get("security"), "verified": sec.get("verified"),
        "mint_off": sec.get("mint_disabled"), "tax": max(sec.get("buy_tax") or 0, sec.get("sell_tax") or 0),
        "liq": liq, "liq_chg": liq_chg, "abandoned": ls.get("abandoned"),
        "src_above": ls.get("n_above"), "src_count": ls.get("sources_count"), "src_min": ls.get("threshold"),
        "cg_rank": rank("coingecko"), "cmc_rank": rank("cmc"),
        "rank_chg": _avg([(rk.get(k) or {}).get("change") for k in ("coingecko", "cmc") if isinstance(rk.get(k), dict)]),
        "cex": len(lst.get("listed") or []), "cex_of": lst.get("monitored"),
        "delisted": len(lst.get("delisted") or []), "scheduled": len(lst.get("scheduled") or []),
        "funding": fh[-1].get("funding") if fh else None, "long_pct": fh[-1].get("long_pct") if fh else None,
        "oi_chg": (fh[-1]["oi"] / fh[0]["oi"] - 1) * 100 if len(fh) > 1 and fh[0].get("oi") else None,
        "burn": (sp.get("burned") or {}).get("status"), "buyback": sp.get("buyback") or {},
        "circ_pct": sp.get("circulating_pct"), "uncapped": sp.get("uncapped"),
        "unlocks_ok": (p.get("unlocks") or {}).get("status") == "ok",
        "unlock_mcap_pct": unl.get("mcap_pct"), "unlock_usd": unl.get("usd"),
        "unlock_days": ((datetime.fromisoformat(unl["at"]) - datetime.now(timezone.utc)).days if unl.get("at") else None),
        "pre_listing": p.get("pre_listing") or [], "dao": dao or [],
        "revenue_30d": rev.get("d30") if check else rev.get("revenue_30d"),
        "perf": perf.get("change_pct"), "perf_days": perf.get("days_held"),
    }


# ---------------------------------------------------------------- the two agents
def _cases(m):
    bull, bear = [], []
    add = lambda side, w, text: side.append((w, text))
    # 1 entry zone / 14d + 60d price action / all-time range
    if m["ez_pos"] is not None:
        if m["ez_pos"] <= 35: add(bull, 10, f"السعر داخل منطقة الدخول: موقعه {_p(m['ez_pos'], 0, False)} من نطاق 60 يوماً ({_p(m['ez_above_low'])} فوق القاع).")
        elif m["ez_pos"] >= 80: add(bear, 10, f"السعر ممتد في أعلى نطاق 60 يوماً (موقعه {_p(m['ez_pos'], 0, False)}، {_p(m['ez_above_low'])} فوق القاع).")
    if m["above_14"] is not None:
        if m["above_14"] >= 40: add(bear, 6, f"صعد {_p(m['above_14'])} فوق قاع 14 يوماً — تمدد قصير المدى قد يتبعه جني أرباح.")
        elif m["above_14"] <= 10: add(bull, 4, f"قريب من قاع 14 يوماً ({_p(m['above_14'])} فقط).")
    # the Entry Zone is the same 60-day range: only say it here when the zone point above didn't
    if m["above_60"] is not None and not (m["ez_pos"] is not None and (m["ez_pos"] <= 35 or m["ez_pos"] >= 80)):
        if m["above_60"] >= 80: add(bear, 6, f"صعد {_p(m['above_60'], 0)} فوق قاع 60 يوماً — صعود كبير سبق ومخاطر جني أرباح.")
        elif m["above_60"] <= 15: add(bull, 4, f"قريب من قاع 60 يوماً ({_p(m['above_60'])} فقط).")
    if m["ath_pos"] is not None:
        if m["ath_pos"] <= 15: add(bull, 6, f"قرب القاع التاريخي: {_p(m['ath_chg'], 0)} من القمة و{_p(m['atl_chg'], 0)} فوق القاع التاريخي.")
        elif m["ath_pos"] >= 80: add(bear, 8, f"قريب من القمة التاريخية ({_p(m['ath_chg'], 0)} فقط) — مساحة صعود محدودة ومخاطرة أعلى.")
    # 2 relative strength
    if m["rs"] == "STRONG": add(bull, 8, f"قوة نسبية: {m['rs_line']}")
    elif m["rs"] == "WEAK": add(bear, 8, f"ضعف نسبي: {m['rs_line']}")
    # 3 volume / turnover / transactions
    if m["vol_trend"] is not None:
        if m["vol_trend"] >= 20: add(bull, 6, f"{m['vol_src']} يرتفع ({_p(m['vol_trend'], 0)} متوسط آخر 5 أيام مقابل الخمسة السابقة).")
        elif m["vol_trend"] <= -30: add(bear, 8, f"{m['vol_src']} ينخفض ({_p(m['vol_trend'], 0)} متوسط آخر 5 أيام مقابل الخمسة السابقة).")
    if m["turnover"] is not None and m["turnover"] >= 5:
        add(bull, 4, f"تداول نشط: حجم آخر يوم {_p(m['turnover'], 1, False)} من القيمة السوقية.")
    if m["vol_liq"] is not None:
        x = _ltr(format(m["vol_liq"], ".1f") + "x")
        if m["vol_liq"] >= 1: add(bull, 4, f"حجم آخر يوم يساوي {x} السيولة — طلب قوي مقارنة بعمق السوق.")
        elif m["vol_liq"] < 0.05: add(bear, 3, f"نشاط ضعيف: حجم آخر يوم {_p(m['vol_liq'] * 100, 1, False)} فقط من السيولة.")
    b, sl = m["buyers"], m["sellers"]
    if b and sl:
        r = b / sl
        if r >= 1.15: add(bull, 8, f"المشترون أكثر من البائعين خلال 7 أيام ({_n(b)} مقابل {_n(sl)} محفظة، صفقات شراء {_n(m['buys'])} مقابل بيع {_n(m['sells'])}).")
        elif r <= 0.87: add(bear, 8, f"البائعون أكثر من المشترين خلال 7 أيام ({_n(sl)} مقابل {_n(b)} محفظة، صفقات بيع {_n(m['sells'])} مقابل شراء {_n(m['buys'])}).")
    # 4 holders + contract security
    if m["holders_chg"] is not None:
        if m["holders_chg"] >= 2: add(bull, 6, f"عدد الحاملين يزداد ({_p(m['holders_chg'])} خلال أسبوع، الآن {_n(m['holders'])}).")
        elif m["holders_chg"] <= -2: add(bear, 6, f"عدد الحاملين يتناقص ({_p(m['holders_chg'])} خلال أسبوع).")
    if m["honeypot"] == "unsafe": add(bear, 25, "العقد غير آمن أو غير قابل للبيع (GoPlus / RugCheck).")
    elif m["honeypot"] == "safe" and m["mint_off"] and m["verified"]: add(bull, 5, "العقد موثّق وآمن والسكّ مُعطّل (لا تضخم مفاجئ).")
    if m["mint_off"] is False: add(bear, 5, "يمكن سكّ عملات جديدة (صلاحية السكّ مفعّلة).")
    if m["verified"] is False: add(bear, 4, "كود العقد غير موثّق (لا يمكن مراجعته).")
    if m["holders"] is not None and m["holders"] < 1000: add(bear, 3, f"قاعدة حاملين صغيرة ({_n(m['holders'])} محفظة فقط).")
    if m["tax"] > 5: add(bear, 6, f"ضريبة تداول مرتفعة ({_p(m['tax'], 0, False)}).")
    # 5 liquidity + sources, market-cap rank, CEX listings (orderbook depth: not in the app)
    if m["abandoned"]: add(bear, 10, "السيولة مهجورة: لا تحديث للسعر منذ أكثر من 48 ساعة (Birdeye).")
    if m["liq_chg"] is not None:
        if m["liq_chg"] >= 10: add(bull, 6, f"السيولة تنمو ({_p(m['liq_chg'])}، الآن {_usd(m['liq'])}).")
        elif m["liq_chg"] <= -20: add(bear, 8, f"السيولة تتراجع ({_p(m['liq_chg'])}، الآن {_usd(m['liq'])}).")
    if m["liq"] and m["mcap"] and m["liq"] / m["mcap"] * 100 < 0.5:
        add(bear, 4, f"سيولة DEX رقيقة مقابل القيمة السوقية ({_p(m['liq'] / m['mcap'] * 100, 2, False)}).")
    if m["src_count"] and m["src_above"] is not None:
        src = f"{_ltr(m['src_above'])} من {_ltr(m['src_count'])} مصادر فوق {_usd(m['src_min'] or 5000)}"
        if m["src_above"] >= 3: add(bull, 3, f"السيولة مؤكدة من عدة مصادر ({src}).")
        elif m["src_above"] < 2: add(bear, 6, f"السيولة غير مؤكدة ({src} فقط).")
    if m["rank_chg"] is not None and abs(m["rank_chg"]) >= 20:
        where = " · ".join(k + " " + _ltr("#" + format(v, ",")) for k, v in (("CoinGecko", m["cg_rank"]), ("CMC", m["cmc_rank"])) if v)
        moved = _ltr(format(m["rank_chg"], "+.0f"))
        if m["rank_chg"] > 0: add(bull, 5, f"ترتيب القيمة السوقية يتحسّن: {where} (متوسط {moved} مركزاً خلال أسبوع).")
        else: add(bear, 5, f"ترتيب القيمة السوقية يتراجع: {where} (متوسط {moved} مركزاً خلال أسبوع).")
    if m["cex_of"]:
        if m["cex"] >= 5: add(bull, 6, f"مدرجة على {_ltr(m['cex'])} من {_ltr(m['cex_of'])} منصات مركزية نراقبها.")
        elif m["cex"] == 0: add(bear, 4, f"غير مدرجة على أي من المنصات المركزية الـ{_ltr(m['cex_of'])} التي نراقبها.")
        if m["scheduled"]: add(bull, 6, f"إدراج مجدول على {_ltr(m['scheduled'])} منصة مركزية.")
        if m["delisted"]: add(bear, 8, f"شُطبت من {_ltr(m['delisted'])} منصة مركزية.")
    # 6 futures - only when the coin has a futures market
    if m["funding"] is not None:
        if m["funding"] <= -0.01: add(bull, 6, f"تمويل العقود الآجلة سلبي ({_p(m['funding'], 3)}) — البائعون على المكشوف يدفعون، احتمال ضغط صعودي (Short Squeeze).")
        elif m["funding"] >= 0.03: add(bear, 6, f"تمويل موجب مرتفع ({_p(m['funding'], 3)}) — رافعة شرائية مفرطة قد تُصفّى.")
        if m["oi_chg"] is not None and m["oi_chg"] >= 20: add(bull, 4, f"الاهتمام المفتوح (OI) يرتفع {_p(m['oi_chg'], 0)} خلال 7 أيام.")
        elif m["oi_chg"] is not None and m["oi_chg"] <= -25: add(bear, 3, f"الاهتمام المفتوح (OI) ينخفض {_p(m['oi_chg'], 0)} خلال 7 أيام.")
        if m["long_pct"] and m["long_pct"] >= 65: add(bear, 4, f"ازدحام في المراكز الشرائية ({_p(m['long_pct'], 0, False)} لونغ).")
    # 7 supply: burn / buyback vs unlocks / inflation
    if m["buyback"].get("has_verified_buyback"):
        how = "، ".join(BUYBACK_AR.get(x, x) for x in m["buyback"].get("proofs") or [])
        vol = m["buyback"].get("usd_30d")
        add(bull, 8, f"إعادة شراء موثّقة ({how})" + (f" — {_usd(vol)} خلال 30 يوماً." if vol else "."))
    if m["burn"] in ("ACTIVE_BURN", "BUYBACK_AND_BURN"): add(bull, 5, "حرق نشط ومستمر للمعروض.")
    if m["unlock_mcap_pct"] is not None and m["unlock_days"] is not None and m["unlock_days"] <= 30:
        txt = f"فتح توكنات بعد {_ltr(max(m['unlock_days'], 0))} يوم بقيمة {_usd(m['unlock_usd'] or 0)} ({_p(m['unlock_mcap_pct'], 2, False)} من القيمة السوقية)."
        if m["unlock_mcap_pct"] >= 1: add(bear, 10, txt + " ضغط بيع محتمل.")
        elif m["unlock_mcap_pct"] >= 0.2: add(bear, 4, txt)
    big_unlock = m["unlock_days"] is not None and m["unlock_days"] <= 30 and (m["unlock_mcap_pct"] or 0) >= 0.2
    if m["unlocks_ok"] and not big_unlock:
        add(bull, 3, "لا يوجد فتح توكنات مؤثر خلال 30 يوماً.")
    if m["circ_pct"] is not None and m["circ_pct"] < 50:
        add(bear, 5, f"أقل من نصف المعروض متداول ({_p(m['circ_pct'], 0, False)}) — تخفيف مستقبلي.")
    if m["uncapped"]: add(bear, 4, "المعروض بلا سقف أقصى — احتمال تضخم مستمر.")
    # 8 catalysts + performance since caught
    for a in m["pre_listing"][:2]:
        add(bull, 8, f"رادار ما قبل الإدراج: {a.get('text_ar') or a.get('rule')}")
    for d in m["dao"][:2]:
        add(bull, 6, f"قرار حوكمة ناجح ({d['tag']}): {d['title'][:90]}")
    if m["revenue_30d"]:
        add(bull, 6, f"إيرادات فعلية {_usd(m['revenue_30d'])} خلال آخر 30 يوماً (DefiLlama).")
    if m["perf"] is not None and m["perf_days"]:
        since = f"منذ دخولها المتابعة قبل {_ltr(m['perf_days'])} يوم"
        if m["perf"] >= 15: add(bull, 4, f"أداء قوي {since}: {_p(m['perf'])}.")
        elif m["perf"] <= -15: add(bear, 6, f"أداء ضعيف {since}: {_p(m['perf'])} تحت سعر الدخول.")
    # 9 status: drop reason / Check a Coin verdict
    if m["status"] == "dropped" and m["drop"]:
        add(bear, 15, f"استُبعدت من المتابعة في {_ltr(m['drop']['date'])}: {m['drop']['reason_ar']}")
    if m["check"] and m["verdict"]:                      # passes None = check not finished -> no point
        if m["verdict"].get("passes") is True: add(bull, 8, "تجتاز جميع خطوات الفلتر.")
        elif m["verdict"].get("passes") is False: add(bear, 10, f"لا تجتاز الفلتر: {m['verdict'].get('reason')}")
    return bull, bear


def evaluate_bull_case(m):
    pts = sorted(_cases(m)[0], key=lambda x: -x[0])
    return {"bull_points": [t for _, t in pts], "bull_score": min(100, sum(w for w, _ in pts))}


def evaluate_bear_case(m):
    pts = sorted(_cases(m)[1], key=lambda x: -x[0])
    return {"bear_points": [t for _, t in pts], "risk_score": min(100, sum(w for w, _ in pts))}


def _stage(m):
    if m["status"] == "dropped":
        return "عملة مستبعدة من المتابعة"
    near_bottom = (m["ez_pos"] is not None and m["ez_pos"] <= 35) or (m["ath_pos"] is not None and m["ath_pos"] <= 10)
    if m["ath_pos"] is not None and m["ath_pos"] >= 80:
        return "مرحلة تمدد قرب القمة التاريخية"
    if m["rs"] == "STRONG" and (m["above_14"] or 0) >= 25:
        return "مرحلة صعود زخم"
    if m["ez_pos"] is not None and m["ez_pos"] >= 70 and m["rs"] == "WEAK":
        return "مرحلة تصحيح بعد صعود"
    if near_bottom:
        return "مرحلة تجميع قرب القاع"
    if (m["coin_24h"] or 0) < -5 or (m["vol_trend"] or 0) <= -30:
        return "مرحلة تصحيح / ضعف زخم"
    return "مرحلة تذبذب عرضي"


def risk_manager_verdict(bull_score, risk_score, m, bull_points=(), bear_points=()):
    net = bull_score - risk_score
    dropped = m["status"] == "dropped"
    outlook = (YELLOW if net >= 25 else RED) if dropped else GREEN if net >= 20 else RED if net <= -15 else YELLOW
    confidence = min(95, 40 + abs(net)) if bull_score + risk_score else 0
    side = "الصعود" if net >= 20 else "الهبوط" if net <= -15 else "الحياد"
    strip = lambda t: t.rstrip(".")
    stage = _stage(m)
    parts = [f"الكفة تميل نحو {side}: الوكيل الإيجابي {_ltr(bull_score)} مقابل مخاطر {_ltr(risk_score)}.",
             f"المرحلة الحالية: {stage}."]
    top = lambda pts: [strip(t) for t in pts][:2]
    if top(bull_points): parts.append("أقوى ما يدعم العملة: " + "؛ ".join(top(bull_points)) + ".")
    if top(bear_points): parts.append("أبرز المخاطر: " + "؛ ".join(top(bear_points)) + ".")
    if dropped:
        parts.append("بما أنها مستبعدة، لا تعود مرشحة إلا إذا عادت لاجتياز الفلتر." if net < 25
                     else "رغم استبعادها، الأرقام الحالية إيجابية — تستحق المراقبة حتى تعود لاجتياز الفلتر.")
    parts.append("هذا تحليل آلي للأرقام المعروضة وليس نصيحة مالية.")
    return {"confidence_score": confidence, "stage_ar": stage, "direction_outlook_ar": outlook,
            "final_verdict_ar": " ".join(parts)}


def debate(p, drop=None, dao=None):
    """The card's dict for a detail-page / Check a Coin payload. Never raises (-> None)."""
    try:
        m = metrics(p, drop, dao)
        bull, bear = evaluate_bull_case(m), evaluate_bear_case(m)
        return {**bull, **bear, **risk_manager_verdict(bull["bull_score"], bear["risk_score"], m,
                                                      bull["bull_points"], bear["bear_points"])}
    except Exception:
        return None


def _dao(session, contract):
    from sqlalchemy import func
    from models import DaoCatalystAlert
    return [{"tag": a.tag, "title": a.title} for a in session.query(DaoCatalystAlert)
            .filter(func.lower(DaoCatalystAlert.contract) == (contract or "").lower())
            .order_by(DaoCatalystAlert.closed_at.desc()).limit(2)]


def for_coin(session, coin, payload):
    """Detail page (active or dropped): + drop reason, DAO decisions (2 small reads)."""
    from main import reason_ar
    from models import DroppedCoin
    try:
        d = (session.query(DroppedCoin).filter(DroppedCoin.coin_id == coin.id)
             .order_by(DroppedCoin.dropped_date.desc()).first() if coin.status == "dropped" else None)
        drop = {"date": d.dropped_date.strftime("%Y-%m-%d"), "reason_ar": reason_ar(d.reason)} if d else None
        return debate(payload, drop, _dao(session, coin.contract))
    except Exception:
        session.rollback()
        return debate(payload)


def for_check(result):
    """Check a Coin: + DAO decisions for the contract (1 read)."""
    from database import SessionLocal
    s = SessionLocal()
    try:
        return debate(result, dao=_dao(s, result.get("contract")))
    except Exception:
        return debate(result)
    finally:
        s.close()
