"""
tx_bars.py
----------
Codex data for a coin's DETAIL PAGE (dashboard only - nothing here keeps or
drops a coin).

1. Daily bars - Codex getBars, resolution "1D", by contract + networkId.
   ONE request returns every day back to MAX_HISTORY_DAYS (Codex's limit is
   1,500 data points per request), with per-day buys, sells, buyers,
   sellers, buyVolume, sellVolume, volume and transactions. Codex sends the
   volume fields as STRINGS - converted to numbers here. From that single
   response we keep:
     * the Transactions chart: the last WINDOW_DAYS (15) COMPLETE UTC days -
       today's partial day is left out, like the volume check; a younger
       coin has fewer bars. The window rolls by itself each day.
     * 7d volume  = the last 7 complete days
     * Total volume since the coin's first trading day on Codex (capped at
       the last 1,500 days for coins older than ~4 years - `since` says
       from when)
     * liquidity now (latest bar) and 7 days ago - the Liquidity section's
       total, 7d growth and weekly Liquidity / Market cap. Same level as the
       liquidity tracking.py stores (both Codex), unlike
       getDetailedTokenStats' liquidity, which counts other pools - so the
       two are never mixed in one comparison.
   Cached per coin per UTC day (tx_bars_cache) - it only changes once a day.

2. Live 4h / 24h volume and 4h / 24h liquidity CHANGE - Codex
   getDetailedTokenStats (durations hour4, day1; it has no 7-day duration).
   The liquidity change is its own now-vs-then (never compared with the
   getBars level). These roll every minute, so they're cached per coin for
   LIVE_TTL_MINUTES (vol_stats_cache).

3. Days Codex has as 0 / missing (a dead or wrong pool) are filled from
   GeckoTerminal's top pools (volume only - see GT_POOLS); a day that comes
   back missing keeps the value saved earlier (keep_saved_days). Missing
   stays None, never a fake 0. Requests: 2 tries, 6 s each (http_retry).

Cost: only when someone opens a coin's page - at most 1 getBars request per
coin per day plus 1 getDetailedTokenStats request per coin per hour (+ up to
3 keyless GeckoTerminal calls when Codex has 0 / missing days).
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from http_retry import request, Transient

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
CODEX_KEY = os.getenv("CODEX_API_KEY")
CODEX_URL = "https://graph.codex.io/graphql"

WINDOW_DAYS = 15            # Transactions chart
MAX_HISTORY_DAYS = 1499     # one getBars request (Codex max 1,500 points)
LIVE_TTL_MINUTES = 60       # 4h / 24h volume cache

# FALLBACK for the days Codex has as 0 / missing (a dead or wrong pool, e.g.
# Coinweb, TREAD): GeckoTerminal's daily OHLCV of the token's top GT_POOLS
# pools, summed per day (DEX volume only - no CEX mixing; keyless, ~30 calls /
# min). It has volume but no buy / sell counts, so those days carry volume only.
GT_BASE = "https://api.geckoterminal.com/api/v2"
GT_HEADERS = {"accept": "application/json;version=20230302"}
GT_POOLS = 2
# Our chain name -> GeckoTerminal network id.
GT_NETWORKS = {"ethereum": "eth", "bsc": "bsc", "base": "base", "solana": "solana",
               "arbitrum": "arbitrum", "optimism": "optimism", "polygon": "polygon_pos",
               "avalanche": "avax", "zksync": "zksync", "cronos": "cro", "linea": "linea",
               "blast": "blast", "sui": "sui-network", "hyperevm": "hyperevm"}

BARS_QUERY = """
query ($symbol: String!, $from: Int!, $to: Int!) {
  getBars(symbol: $symbol, from: $from, to: $to, resolution: "1D",
          removeLeadingNullValues: true) {
    t buys sells buyers sellers buyVolume sellVolume volume transactions liquidity
  }
}"""

STATS_QUERY = """
query ($addr: String!, $net: Int!) {
  getDetailedTokenStats(tokenAddress: $addr, networkId: $net,
                        durations: [hour4, day1], bucketCount: 1) {
    stats_hour4 { statsUsd { volume { currentValue } liquidity { currentValue previousValue } } }
    stats_day1  { statsUsd { volume { currentValue } liquidity { currentValue previousValue } } }
  }
}"""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _opt(v):
    """float, or None when the source has no value (never a fake 0)."""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(v):
    v = _opt(v)
    return int(v) if v is not None else None


def _sum(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def _now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _today_utc():
    return datetime.now(timezone.utc).date()


def _codex(query, variables):
    """(data, error) for one Codex GraphQL request. Never raises."""
    if not CODEX_KEY:
        return None, "no Codex key configured"
    try:
        body = request("POST", CODEX_URL, json={"query": query, "variables": variables},
                       headers={"Authorization": CODEX_KEY}).json()
    except Transient as e:
        return None, f"Codex request failed ({e})"
    except ValueError:
        return None, "Codex sent a bad answer"
    if body.get("errors"):
        return None, "Codex: " + (body["errors"][0].get("message") or "error")[:120]
    return body.get("data") or {}, None


def _network(chain):
    from liquidity_check import CHAIN_TO_NETWORK
    return CHAIN_TO_NETWORK.get((chain or "").lower())


def fetch_from_codex(contract, chain):
    """(daily, error). daily = {"bars": last WINDOW_DAYS complete days (oldest
    first), "vol_7d", "vol_total", "since"} from ONE getBars request."""
    network = _network(chain)
    if not network:
        return None, f"Codex doesn't cover the {chain} chain"

    today = _today_utc()
    day0 = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    frm = int((day0 - timedelta(days=MAX_HISTORY_DAYS)).timestamp())
    data, error = _codex(BARS_QUERY, {"symbol": f"{contract}:{network}",
                                      "from": frm, "to": int(time.time())})
    if error:
        return None, error
    b = data.get("getBars") or {}

    days, liq_by_day = [], {}
    for i, t in enumerate(b.get("t") or []):
        day = datetime.fromtimestamp(t, tz=timezone.utc).date()

        def at(field):
            vals = b.get(field) or []
            return vals[i] if i < len(vals) else None
        if at("liquidity") is not None:
            liq_by_day[day] = _num(at("liquidity"))
        if day >= today:
            continue                       # today's partial day - left out
        days.append({
            "date": day.isoformat(),
            "buys": _int(at("buys")),
            "sells": _int(at("sells")),
            "buyers": _int(at("buyers")),
            "sellers": _int(at("sellers")),
            "buy_volume": _opt(at("buyVolume")),
            "sell_volume": _opt(at("sellVolume")),
            "volume": _opt(at("volume")),
            "transactions": _int(at("transactions")),
            "source": "codex",
        })
    # Liquidity now = the latest bar (today's, so far); 7 days ago = the bar
    # dated a week before today (or the nearest earlier one).
    liq_now = liq_by_day[max(liq_by_day)] if liq_by_day else None
    week_ago = [d for d in liq_by_day if d <= today - timedelta(days=7)]
    liq_7d_ago = liq_by_day[max(week_ago)] if week_ago else None
    return {
        "liq_now": liq_now,
        "liq_7d_ago": liq_7d_ago,
        "liq_7d_ago_date": max(week_ago).isoformat() if week_ago else None,
        "bars": days[-WINDOW_DAYS:],
        "vol_7d": _sum(d["volume"] for d in days[-7:]),
        "vol_total": _sum(d["volume"] for d in days),
        "since": days[0]["date"] if days else None,
    }, None


def geckoterminal_daily(contract, chain):
    """{"YYYY-MM-DD": USD volume} - the token's top GT_POOLS GeckoTerminal
    pools' daily volume, summed per day (1 + GT_POOLS keyless calls), or {}
    (unsupported chain, no pool, an error)."""
    net = GT_NETWORKS.get((chain or "").lower())
    if not net or not contract:
        return {}
    try:
        pools = request("GET", f"{GT_BASE}/networks/{net}/tokens/{contract}/pools",
                        params={"page": 1}, headers=GT_HEADERS).json().get("data") or []
    except (Transient, ValueError, AttributeError):
        return {}
    out = {}
    for p in pools[:GT_POOLS]:
        try:
            body = request("GET", f"{GT_BASE}/networks/{net}/pools/{p['attributes']['address']}/ohlcv/day",
                           params={"limit": WINDOW_DAYS + 1, "currency": "usd", "token": contract},
                           headers=GT_HEADERS).json()
        except (Transient, ValueError, KeyError, TypeError):
            continue                       # one pool failing keeps the other's days
        for r in ((body.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []:
            v = _opt(r[5]) if len(r) >= 6 else None
            if v is not None:
                d = datetime.fromtimestamp(r[0], tz=timezone.utc).date().isoformat()
                out[d] = out.get(d, 0.0) + v
    return out


def fill_gaps(daily, fallback, source):
    """Fill the days `daily` (fetch_from_codex) has as 0 / missing with a
    fallback's volume ({date: USD}) - VOLUME only, buy / sell counts None,
    "source" says where it came from. A fallback 0 never replaces Codex; no
    Codex bars at all -> the fallback's own last WINDOW_DAYS complete days."""
    blank = {"buys": None, "sells": None, "buyers": None, "sellers": None,
             "buy_volume": None, "sell_volume": None, "transactions": None}
    bars = daily["bars"]
    if not bars:
        today = _today_utc().isoformat()
        bars = [{"date": d, **blank, "volume": v, "source": source}
                for d, v in sorted(fallback.items()) if d < today][-WINDOW_DAYS:]
        added = _sum(b["volume"] for b in bars)
    else:
        added = 0.0
        for b in bars:
            v = fallback.get(b["date"])
            if not b.get("volume") and v:
                b.update(blank, volume=v, source=source)
                added += v
    daily.update(bars=bars, vol_7d=_sum(b["volume"] for b in bars[-7:]),
                 vol_total=_sum([daily.get("vol_total"), added]),
                 since=daily.get("since") or (bars[0]["date"] if bars else None))
    return daily


def fetch_daily(contract, chain):
    """fetch_from_codex + the GeckoTerminal fallback for the days Codex has as
    0 / missing. A failed Codex request is returned as is (the cache keeps the
    last bars - no fallback calls during a Codex outage)."""
    daily, error = fetch_from_codex(contract, chain)
    if error and _network(chain):
        return daily, error
    daily = daily or {"liq_now": None, "liq_7d_ago": None, "liq_7d_ago_date": None,
                      "bars": [], "vol_7d": None, "vol_total": None, "since": None}
    if daily["bars"] and all(b.get("volume") for b in daily["bars"]):
        return daily, None
    fallback = geckoterminal_daily(contract, chain)
    if not any(fallback.values()):
        return daily, (None if daily["bars"] else error)
    return fill_gaps(daily, fallback, "geckoterminal"), None


def keep_saved_days(new_bars, saved_bars):
    """Zero protection: a day that comes back missing (volume None) keeps the
    value saved earlier for that date - never replaced by None / a fake 0."""
    old = {b["date"]: b for b in saved_bars or []}
    return [old[b["date"]] if b.get("volume") is None and (old.get(b["date"]) or {}).get("volume") else b
            for b in new_bars]


def get_tx_bars(session, contract, chain):
    """Daily data, cached per coin per UTC day. Returns
    {"bars", "vol_7d", "vol_total", "since", "error"}."""
    from models import TxBarsCache

    key_chain, key_contract = (chain or "").lower(), (contract or "").lower()
    today = _today_utc()
    row = (session.query(TxBarsCache)
           .filter_by(chain=key_chain, contract=key_contract).first())
    cached = json.loads(row.bars) if row is not None else None
    stale_format = cached is not None and not (isinstance(cached, dict) and "liq_now" in cached)
    if isinstance(cached, list):           # stored before 7d / total existed
        cached = {"bars": cached}
    empty = {"bars": [], "vol_7d": None, "vol_total": None, "since": None,
             "liq_now": None, "liq_7d_ago": None, "liq_7d_ago_date": None}
    if cached is not None:
        cached = {**empty, **cached}
    if row is not None and row.day == today and not stale_format:
        return {**cached, "error": None}

    daily, error = fetch_daily(contract, chain)
    if error:
        # Keep showing the last saved data rather than nothing if Codex failed.
        return {**(cached or empty), "error": error}
    daily["bars"] = keep_saved_days(daily["bars"], (cached or {}).get("bars"))
    daily["vol_7d"] = _sum(b["volume"] for b in daily["bars"][-7:])

    if row is None:
        row = TxBarsCache(chain=key_chain, contract=key_contract)
        session.add(row)
    row.day = today
    row.bars = json.dumps(daily)
    row.fetched_at = _now_naive()
    session.commit()
    return {**daily, "error": None}


def get_live_volume(session, contract, chain):
    """4h / 24h volume + 4h / 24h liquidity change in % (getDetailedTokenStats),
    cached LIVE_TTL_MINUTES per coin. Returns
    {"vol_4h", "vol_24h", "liq_chg_4h", "liq_chg_24h", "error"}."""
    from models import VolStatsCache

    key_chain, key_contract = (chain or "").lower(), (contract or "").lower()
    row = (session.query(VolStatsCache)
           .filter_by(chain=key_chain, contract=key_contract).first())
    fresh_after = _now_naive() - timedelta(minutes=LIVE_TTL_MINUTES)
    def out(r, error=None):
        return {"vol_4h": r.vol_4h, "vol_24h": r.vol_24h, "liq_chg_4h": r.liq_chg_4h,
                "liq_chg_24h": r.liq_chg_24h, "error": error}

    if row is not None and row.fetched_at >= fresh_after:
        return out(row)

    network = _network(chain)
    error = None if network else f"Codex doesn't cover the {chain} chain"
    data = None
    if network:
        data, error = _codex(STATS_QUERY, {"addr": contract, "net": network})
    if error:
        if row is not None:
            return out(row, error)
        return {"vol_4h": None, "vol_24h": None, "liq_chg_4h": None,
                "liq_chg_24h": None, "error": error}

    s = data.get("getDetailedTokenStats") or {}

    def vol(key):
        v = (((s.get(key) or {}).get("statsUsd") or {}).get("volume") or {}).get("currentValue")
        return None if v is None else _num(v)

    def liq_change(key):
        lq = ((s.get(key) or {}).get("statsUsd") or {}).get("liquidity") or {}
        cur, prev = lq.get("currentValue"), lq.get("previousValue")
        if cur is None or not _num(prev):
            return None
        return (_num(cur) - _num(prev)) / _num(prev) * 100

    if row is None:
        row = VolStatsCache(chain=key_chain, contract=key_contract)
        session.add(row)
    row.vol_4h, row.vol_24h = vol("stats_hour4"), vol("stats_day1")
    row.liq_chg_4h, row.liq_chg_24h = liq_change("stats_hour4"), liq_change("stats_day1")
    row.fetched_at = _now_naive()
    session.commit()
    return out(row)
