"""
debate_verdict.py
-----------------
Bull vs Bear debate + Risk Manager verdict (Arabic), built ONLY from the
numbers already in a coin's API payload (detail page or Check a Coin): no
API call, no LLM, nothing stored. Deterministic - same payload, same text.

Each rule adds a weighted Arabic point to the bull or bear side; a side's
score is the sum of its weights (capped at 100). Categories with no data
(e.g. futures on a spot-only coin) add nothing to either side.
Not available in the app, so never used: CEX orderbook depth (bid / ask
walls) and top-10 holder % (only top-holder addresses are stored).
"""
from datetime import datetime, timezone

from relative_strength import _ltr

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
def metrics(p, entry_score=None, drop=None, dao=None):
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
    hist = p.get("score_history") or []
    score = (p.get("score") or {}).get("total") if check else (p.get("score") if p.get("score") is not None
                                                             else (hist[0]["score"] if hist else None))  # dropped: last recorded
    liq = (p.get("liquidity") or {}).get("now") if check else (p.get("liquidity_now") or p.get("liquidity"))
    liq_chg = (p.get("liquidity") or {}).get("pct") if check else (p.get("liq_growth_pct") or {}).get("7d")
    fut = p.get("futures") or {}
    fh = fut.get("history") or [] if fut.get("matched") else []
    lst = p.get("listings") or {}
    rev = p.get("revenue") or {}
    unl = (p.get("unlocks") or {}).get("next") or {}
    sec = p.get("security_info") or {}
    last7 = bars[-7:]
    return {
        "check": check, "status": "check" if check else p.get("status"), "drop": drop,
        "verdict": p.get("verdict"), "mcap": p.get("mcap"), "price": price,
        "score": score, "score_entry": entry_score,
        "score_week": hist[-1]["score"] if len(hist) > 1 else None,
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
        "buyers": sum(b.get("buyers") or 0 for b in last7), "sellers": sum(b.get("sellers") or 0 for b in last7),
        "buys": sum(b.get("buys") or 0 for b in last7), "sells": sum(b.get("sells") or 0 for b in last7),
        "holders": (p.get("holders") or {}).get("now"), "holders_chg": (p.get("holders") or {}).get("week_change_pct"),
        "honeypot": p.get("honeypot") or p.get("security"), "verified": sec.get("verified"),
        "mint_off": sec.get("mint_disabled"), "tax": max(sec.get("buy_tax") or 0, sec.get("sell_tax") or 0),
        "liq": liq, "liq_chg": liq_chg, "abandoned": (p.get("liquidity_sources") or {}).get("abandoned"),
        "cex": len(lst.get("listed") or []), "cex_of": lst.get("monitored"),
        "delisted": len(lst.get("delisted") or []), "scheduled": len(lst.get("scheduled") or []),
        "funding": fh[-1].get("funding") if fh else None, "long_pct": fh[-1].get("long_pct") if fh else None,
        "oi_chg": (fh[-1]["oi"] / fh[0]["oi"] - 1) * 100 if len(fh) > 1 and fh[0].get("oi") else None,
        "burn": (sp.get("burned") or {}).get("status"), "buyback": sp.get("buyback") or {},
        "circ_pct": sp.get("circulating_pct"),
        "unlock_mcap_pct": unl.get("mcap_pct"), "unlock_usd": unl.get("usd"),
        "unlock_days": ((datetime.fromisoformat(unl["at"]) - datetime.now(timezone.utc)).days if unl.get("at") else None),
        "pre_listing": p.get("pre_listing") or [], "dao": dao or [],
        "revenue_30d": rev.get("d30") if check else rev.get("revenue_30d"),
    }


# ---------------------------------------------------------------- the two agents
def _cases(m):
    bull, bear = [], []
    add = lambda side, w, text: side.append((w, text))
    s, se = m["score"], m["score_entry"] if m["score_entry"] is not None else m["score_week"]
    # 1 score + trajectory
    if s is not None:
        if s >= 60: add(bull, 10, f"السكور الحالي {_ltr(f'{s:.0f}/100')} — من الأعلى بين العملات.")
        elif s >= 40: add(bull, 5, f"السكور الحالي {_ltr(f'{s:.0f}/100')} — فوق المتوسط.")
        elif s < 30: add(bear, 8, f"السكور الحالي ضعيف {_ltr(f'{s:.0f}/100')}.")
        if se is not None and abs(s - se) >= 8:
            since = "منذ دخولها المتابعة" if m["score_entry"] is not None else "خلال آخر 7 أيام"
            txt = f"السكور {'تحسّن' if s > se else 'تراجع'} {since} من {_ltr(f'{se:.0f}')} إلى {_ltr(f'{s:.0f}')} ({_ltr(f'{s - se:+.0f}')})."
            add(bull if s > se else bear, 8, txt)
    # 2 entry zone / ranges / all-time
    if m["ez_pos"] is not None:
        if m["ez_pos"] <= 35: add(bull, 10, f"السعر داخل منطقة الدخول: موقعه {_p(m['ez_pos'], 0, False)} من نطاق 60 يوماً ({_p(m['ez_above_low'])} فوق القاع).")
        elif m["ez_pos"] >= 80: add(bear, 10, f"السعر ممتد في أعلى نطاق 60 يوماً (موقعه {_p(m['ez_pos'], 0, False)}، {_p(m['ez_above_low'])} فوق القاع).")
    if m["above_14"] is not None:
        if m["above_14"] >= 40: add(bear, 6, f"صعد {_p(m['above_14'])} فوق قاع 14 يوماً — تمدد قصير المدى قد يتبعه جني أرباح.")
        elif m["above_14"] <= 10: add(bull, 4, f"قريب من قاع 14 يوماً ({_p(m['above_14'])} فقط).")
    if m["ath_pos"] is not None:
        if m["ath_pos"] <= 15: add(bull, 6, f"قرب القاع التاريخي: {_p(m['ath_chg'], 0)} من القمة و{_p(m['atl_chg'], 0)} فوق القاع التاريخي.")
        elif m["ath_pos"] >= 80: add(bear, 8, f"قريب من القمة التاريخية ({_p(m['ath_chg'], 0)} فقط) — مساحة صعود محدودة ومخاطرة أعلى.")
    # 3 relative strength
    if m["rs"] == "STRONG": add(bull, 8, f"قوة نسبية: {m['rs_line']}")
    elif m["rs"] == "WEAK": add(bear, 8, f"ضعف نسبي: {m['rs_line']}")
    # 4 volume + buyers vs sellers
    if m["vol_trend"] is not None:
        if m["vol_trend"] >= 20: add(bull, 6, f"{m['vol_src']} يرتفع ({_p(m['vol_trend'], 0)} متوسط آخر 5 أيام مقابل الخمسة السابقة).")
        elif m["vol_trend"] <= -30: add(bear, 8, f"{m['vol_src']} ينخفض ({_p(m['vol_trend'], 0)} متوسط آخر 5 أيام مقابل الخمسة السابقة).")
    if m["turnover"] is not None and m["turnover"] >= 5:
        add(bull, 4, f"تداول نشط: حجم آخر يوم {_p(m['turnover'], 1, False)} من القيمة السوقية.")
    b, sl = m["buyers"], m["sellers"]
    if b and sl:
        r = b / sl
        if r >= 1.15: add(bull, 8, f"المشترون أكثر من البائعين خلال 7 أيام ({_n(b)} مقابل {_n(sl)} محفظة، صفقات شراء {_n(m['buys'])} مقابل بيع {_n(m['sells'])}).")
        elif r <= 0.87: add(bear, 8, f"البائعون أكثر من المشترين خلال 7 أيام ({_n(sl)} مقابل {_n(b)} محفظة، صفقات بيع {_n(m['sells'])} مقابل شراء {_n(m['buys'])}).")
    # 5 holders + security
    if m["holders_chg"] is not None:
        if m["holders_chg"] >= 2: add(bull, 6, f"عدد الحاملين يزداد ({_p(m['holders_chg'])} خلال أسبوع، الآن {_n(m['holders'])}).")
        elif m["holders_chg"] <= -2: add(bear, 6, f"عدد الحاملين يتناقص ({_p(m['holders_chg'])} خلال أسبوع).")
    if m["honeypot"] == "unsafe": add(bear, 25, "العقد غير آمن أو غير قابل للبيع (GoPlus / RugCheck).")
    elif m["honeypot"] == "safe" and m["mint_off"] and m["verified"]: add(bull, 5, "العقد موثّق وآمن والسكّ مُعطّل (لا تضخم مفاجئ).")
    if m["mint_off"] is False: add(bear, 5, "يمكن سكّ عملات جديدة (صلاحية السكّ مفعّلة).")
    if m["tax"] > 5: add(bear, 6, f"ضريبة تداول مرتفعة ({_p(m['tax'], 0, False)}).")
    # 6 liquidity + CEX listings (orderbook depth: not in the app)
    if m["abandoned"]: add(bear, 10, "السيولة مهجورة: لا تحديث للسعر منذ أكثر من 48 ساعة (Birdeye).")
    if m["liq_chg"] is not None:
        if m["liq_chg"] >= 10: add(bull, 6, f"السيولة تنمو ({_p(m['liq_chg'])}، الآن {_usd(m['liq'])}).")
        elif m["liq_chg"] <= -20: add(bear, 8, f"السيولة تتراجع ({_p(m['liq_chg'])}، الآن {_usd(m['liq'])}).")
    if m["liq"] and m["mcap"] and m["liq"] / m["mcap"] * 100 < 0.5:
        add(bear, 4, f"سيولة DEX رقيقة مقابل القيمة السوقية ({_p(m['liq'] / m['mcap'] * 100, 2, False)}).")
    if m["cex_of"]:
        if m["cex"] >= 5: add(bull, 6, f"مدرجة على {_ltr(m['cex'])} من {_ltr(m['cex_of'])} منصات مركزية نراقبها.")
        elif m["cex"] == 0: add(bear, 4, f"غير مدرجة على أي من المنصات المركزية الـ{_ltr(m['cex_of'])} التي نراقبها.")
        if m["scheduled"]: add(bull, 6, f"إدراج مجدول على {_ltr(m['scheduled'])} منصة مركزية.")
        if m["delisted"]: add(bear, 8, f"شُطبت من {_ltr(m['delisted'])} منصة مركزية.")
    # 7 futures - only when the coin has a futures market
    if m["funding"] is not None:
        if m["funding"] <= -0.01: add(bull, 6, f"تمويل العقود الآجلة سلبي ({_p(m['funding'], 3)}) — البائعون على المكشوف يدفعون، احتمال ضغط صعودي (Short Squeeze).")
        elif m["funding"] >= 0.03: add(bear, 6, f"تمويل موجب مرتفع ({_p(m['funding'], 3)}) — رافعة شرائية مفرطة قد تُصفّى.")
        if m["oi_chg"] is not None and m["oi_chg"] >= 20: add(bull, 4, f"الاهتمام المفتوح (OI) يرتفع {_p(m['oi_chg'], 0)} خلال 7 أيام.")
        elif m["oi_chg"] is not None and m["oi_chg"] <= -25: add(bear, 3, f"الاهتمام المفتوح (OI) ينخفض {_p(m['oi_chg'], 0)} خلال 7 أيام.")
        if m["long_pct"] and m["long_pct"] >= 65: add(bear, 4, f"ازدحام في المراكز الشرائية ({_p(m['long_pct'], 0, False)} لونغ).")
    # 8 supply: burn / buyback vs unlocks
    if m["buyback"].get("has_verified_buyback"): add(bull, 8, "إعادة شراء موثّقة (Verified Buyback).")
    if m["burn"] in ("ACTIVE_BURN", "BUYBACK_AND_BURN"): add(bull, 5, "حرق نشط ومستمر للمعروض.")
    if m["unlock_mcap_pct"] is not None and m["unlock_days"] is not None and m["unlock_days"] <= 30:
        txt = f"فتح توكنات بعد {_ltr(max(m['unlock_days'], 0))} يوم بقيمة {_usd(m['unlock_usd'] or 0)} ({_p(m['unlock_mcap_pct'], 2, False)} من القيمة السوقية)."
        if m["unlock_mcap_pct"] >= 1: add(bear, 10, txt + " ضغط بيع محتمل.")
        elif m["unlock_mcap_pct"] >= 0.2: add(bear, 4, txt)
    if m["circ_pct"] is not None and m["circ_pct"] < 50:
        add(bear, 5, f"أقل من نصف المعروض متداول ({_p(m['circ_pct'], 0, False)}) — تخفيف مستقبلي.")
    # 9 catalysts
    for a in m["pre_listing"][:2]:
        add(bull, 8, f"رادار ما قبل الإدراج: {a.get('text_ar') or a.get('rule')}")
    for d in m["dao"][:2]:
        add(bull, 6, f"قرار حوكمة ناجح ({d['tag']}): {d['title'][:90]}")
    if m["revenue_30d"]:
        add(bull, 6, f"إيرادات فعلية {_usd(m['revenue_30d'])} خلال آخر 30 يوماً (DefiLlama).")
    # 10 stage / status
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
    s, se = m["score"], m["score_entry"]
    if s is None:
        score_txt = "لا يوجد سكور محفوظ لهذه العملة."
    elif se is not None:
        trend = "تحسّن" if s - se >= 8 else "تراجع" if se - s >= 8 else "استقر"
        score_txt = f"السكور {trend} منذ دخولها المتابعة: من {_ltr(f'{se:.0f}')} إلى {_ltr(f'{s:.0f}')}."
    elif m["score_week"] is not None:
        sw = m["score_week"]
        trend = "تحسّن" if s - sw >= 8 else "تراجع" if sw - s >= 8 else "استقر"
        score_txt = f"السكور {trend} خلال آخر 7 أيام: من {_ltr(f'{sw:.0f}')} إلى {_ltr(f'{s:.0f}')}."
    else:
        score_txt = f"السكور الحالي {_ltr(f'{s:.0f}/100')}."
    side = "الصعود" if net >= 20 else "الهبوط" if net <= -15 else "الحياد"
    strip = lambda t: t.rstrip(".")
    parts = [f"الكفة تميل نحو {side}: الوكيل الإيجابي {_ltr(bull_score)} مقابل مخاطر {_ltr(risk_score)}.", score_txt]
    top = lambda pts: [strip(t) for t in pts if not t.startswith("السكور")][:2]     # the score line is already said
    if top(bull_points): parts.append("أقوى ما يدعم العملة: " + "؛ ".join(top(bull_points)) + ".")
    if top(bear_points): parts.append("أبرز المخاطر: " + "؛ ".join(top(bear_points)) + ".")
    if dropped:
        parts.append("بما أنها مستبعدة، لا تعود مرشحة إلا إذا عادت لاجتياز الفلتر." if net < 25
                     else "رغم استبعادها، الأرقام الحالية إيجابية — تستحق المراقبة حتى تعود لاجتياز الفلتر.")
    parts.append("هذا تحليل آلي للأرقام المعروضة وليس نصيحة مالية.")
    return {"confidence_score": confidence, "stage_ar": _stage(m), "direction_outlook_ar": outlook,
            "final_verdict_ar": " ".join(parts)}


def debate(p, entry_score=None, drop=None, dao=None):
    """The card's dict for a detail-page / Check a Coin payload. Never raises (-> None)."""
    try:
        m = metrics(p, entry_score, drop, dao)
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
    """Detail page (active or dropped): + entry score, drop reason, DAO decisions (3 small reads)."""
    from main import reason_ar
    from models import DroppedCoin, ScoreHistory
    try:
        e = (session.query(ScoreHistory.score).filter(ScoreHistory.coin_id == coin.id,
             ScoreHistory.recorded_at >= coin.active_since.date()).order_by(ScoreHistory.recorded_at).first()
             if coin.active_since else None)
        d = (session.query(DroppedCoin).filter(DroppedCoin.coin_id == coin.id)
             .order_by(DroppedCoin.dropped_date.desc()).first() if coin.status == "dropped" else None)
        drop = {"date": d.dropped_date.strftime("%Y-%m-%d"), "reason_ar": reason_ar(d.reason)} if d else None
        return debate(payload, e[0] if e else None, drop, _dao(session, coin.contract))
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
