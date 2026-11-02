"""
filter_eval.py
--------------
5-DAY FILTER SELF-EVALUATION (Telegram only - nothing on the website).

Every entry rejection by an EVALUABLE step (age_10d, buyers_24h, revenue,
volume_35, price_gain, security, liquidity) and every new entry is logged
with its DexScreener price (filter_evals). Market cap and the meme filter are
fixed rules and are never logged; "age unknown" isn't either (no DexScreener
price to measure).

  * Day 5: the row's price is read again (DexScreener, 30 per request, about
    2 requests a day) -> return_pct; no price -> "no_price".
  * A rejected coin that ENTERS active within its 5 days is removed
    (log_entry); a re-rejection while pending keeps the first row.
  * REPORT (in the daily Telegram report): every REPORT_EVERY_DAYS days, each
    covering exactly REPORT_EVERY_DAYS daily cohorts - it fires when the
    oldest un-reported measured cohort was logged EVAL_DAYS +
    REPORT_EVERY_DAYS - 1 days ago (cohorts D0..D4 -> report on D9, D5..D9
    -> D14, ...). Passed coins: win rate (>= +WIN_PCT after 5 days), average
    return, how many fell / were at the stop-loss level, losers vs winners
    context. Rejected coins, per filter: how many, missed winners, average
    return; the filter with the highest missed-winner rate is flagged.
  * Reported rows are pruned after KEEP_DAYS.
"""
from datetime import date, timedelta
from statistics import mean

EVAL_DAYS = 5
REPORT_EVERY_DAYS = 5
WIN_PCT = 10
STOP_LOSS_LEVEL = -15
KEEP_DAYS = 30
MIN_FLAG_REJECTED = 3        # a filter needs this many rejections to be flagged "too strict"
FILTERS = ["age_10d", "buyers_24h", "revenue", "volume_35", "price_gain", "security", "liquidity"]


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ctx(t, gain=None):
    g = gain or t.get("_price_gain") or {}
    return {"symbol": t.get("symbol"), "age_days": _num(t.get("age_days")), "buys_24h": t.get("buys_24h"),
            "sells_24h": t.get("sells_24h"), "gain_14d": g.get("gain_14d"), "gain_60d": g.get("gain_60d"),
            "liquidity_usd": _num(t.get("liquidity_usd")),
            "mcap": _num(t.get("discovery_mcap")) or _num(t.get("market_cap"))}


def _pending(session, chain, contract, **kw):
    """Pending rows of a coin (contracts keep their case - Solana addresses
    are case-sensitive for DexScreener - so match case-insensitively)."""
    from sqlalchemy import func
    from models import FilterEval
    return session.query(FilterEval).filter(FilterEval.chain == (chain or "").lower(),
                                            func.lower(FilterEval.contract) == (contract or "").lower(),
                                            FilterEval.status == "pending").filter_by(**kw)


def log_rejection(session, t, step, today, gain=None):
    """A candidate rejected by an evaluable step (the first pending rejection is kept)."""
    from models import FilterEval
    if _pending(session, t.get("chain"), t.get("contract")).first():
        return
    session.add(FilterEval(kind="rejected", rejected_by=step, chain=(t.get("chain") or "").lower(),
                           contract=t.get("contract"),
                           logged_on=today, price0=_num(t.get("price_usd")), **_ctx(t, gain)))


def log_entry(session, t, coin, today):
    """A new coin entered active: its pending rejections are cancelled (it
    entered within 5 days) and its own 5-day test starts."""
    from models import FilterEval
    _pending(session, coin.chain, coin.contract, kind="rejected").delete(synchronize_session=False)
    session.add(FilterEval(kind="passed", chain=(coin.chain or "").lower(), contract=coin.contract,
                           coin_id=coin.id, logged_on=today,
                           price0=coin.entry_price or _num(t.get("price_usd")), **_ctx(t)))


def _measure(session, today):
    """Day-5 price for every pending row that reached it. Returns rows measured."""
    from models import FilterEval
    from hourly_prices import fetch_live_prices
    due = (session.query(FilterEval).filter(FilterEval.status == "pending",
                                            FilterEval.logged_on <= today - timedelta(days=EVAL_DAYS)).all())
    if not due:
        return 0
    prices, _, _ = fetch_live_prices(sorted({(r.chain, r.contract) for r in due}))   # 30 per request
    for r in due:
        p = prices.get((r.contract or "").lower())     # fetch_live_prices keys are lowercased
        r.measured_on = today
        if p and r.price0:
            r.price5, r.return_pct, r.status = p, round((p / r.price0 - 1) * 100, 2), "measured"
        else:
            r.status = "no_price"
    return len(due)


def _avg(rows, col):
    vals = [getattr(r, col) for r in rows if getattr(r, col) is not None]
    return mean(vals) if vals else None


def summary(rows):
    """The report's numbers from measured rows (pure)."""
    out = {"passed": None, "rejected": {}, "too_strict": None}
    p = [r for r in rows if r.kind == "passed"]
    if p:
        win = [r for r in p if r.return_pct >= WIN_PCT]
        lose = [r for r in p if r.return_pct < 0]
        out["passed"] = {"n": len(p), "wins": len(win), "win_rate": round(len(win) / len(p) * 100),
                         "avg": round(mean(r.return_pct for r in p), 1), "fell": len(lose),
                         "stop_loss": sum(r.return_pct <= STOP_LOSS_LEVEL for r in p),
                         "losers_vs_winners": {c: (_avg(lose, c), _avg(win, c))
                                               for c in ("age_days", "gain_14d", "liquidity_usd")}}
    for f in FILTERS:
        rs = [r for r in rows if r.kind == "rejected" and r.rejected_by == f]
        if rs:
            w = [r for r in rs if r.return_pct >= WIN_PCT]
            out["rejected"][f] = {"n": len(rs), "wins": len(w), "win_rate": round(len(w) / len(rs) * 100),
                                  "avg": round(mean(r.return_pct for r in rs), 1)}
    strict = [(v["win_rate"], v["wins"], f) for f, v in out["rejected"].items()
              if v["n"] >= MIN_FLAG_REJECTED and v["wins"]]
    out["too_strict"] = max(strict)[2] if strict else None
    return out


def run_filter_eval(today=None):
    """Daily step: measure the rows that reached day 5; on a report day return
    the summary for Telegram (else report None); prune old rows."""
    from sqlalchemy import func
    from database import SessionLocal
    from models import FilterEval
    today = today or date.today()
    session = SessionLocal()
    try:
        measured = _measure(session, today)
        oldest = (session.query(func.min(FilterEval.logged_on))
                  .filter(FilterEval.status == "measured").scalar())
        report = None
        if oldest is not None and (today - oldest).days >= EVAL_DAYS + REPORT_EVERY_DAYS - 1:
            rows = session.query(FilterEval).filter_by(status="measured").all()
            report = summary(rows)
            unreported = session.query(FilterEval).filter_by(status="no_price", reported_on=None)
            report["no_price"] = unreported.count()
            unreported.update({"reported_on": today}, synchronize_session=False)
            for r in rows:
                r.status, r.reported_on = "reported", today
        session.query(FilterEval).filter(FilterEval.reported_on.isnot(None),
                                         FilterEval.reported_on < today - timedelta(days=KEEP_DAYS)
                                         ).delete(synchronize_session=False)
        session.commit()
        return {"measured": measured, "report": report}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
