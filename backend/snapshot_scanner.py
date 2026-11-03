"""
snapshot_scanner.py
-------------------
COIN DECISION page (website only, no Telegram): newly closed, PASSED,
high-impact DAO votes from Snapshot (hub.snapshot.org/graphql - free, no key).
Every 6 h (hooked into the hourly job, gated by dao_scan_state): closed
proposals ending since the last scan (-1 h overlap; first run: 30 days), then
  A tag     keyword in the title (or body[:500] when the title is just "AIP-12")
  B passed  quorum met, winner >= 50% and not no / against / abstain / ...
  C team    author is a space admin / moderator / member, or members-only space
  D quality verified space, or >= 1000 followers AND CoinGecko mcap >= $5M
A-C are in memory; only survivors cost calls. The token comes from the space's
voting strategies (contract confirmed by CoinGecko), else a DexScreener symbol
search pinned to the space's chain. Rows closed > 30 days ago are deleted on
every scan.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)
URL = "https://hub.snapshot.org/graphql"
QUERY = """query ClosedProposals($first: Int!, $skip: Int!, $end_gte: Int) {
  proposals(first: $first, skip: $skip, where: {state: "closed", end_gte: $end_gte},
            orderBy: "end", orderDirection: desc) {
    id title body choices scores scores_total quorum author created end
    space { id name symbol network verified followersCount admins moderators members
            validation { name } filters { onlyMembers } strategies { name network params } }
  }
}"""
CADENCE_H, BACKFILL_D, KEEP_D, OVERLAP = 6, 30, 30, timedelta(hours=1)
PAGE, MAX_SKIP = 100, 5000                               # Snapshot rejects skip > 5000
MIN_FOLLOWERS, MIN_MCAP, MIN_LIQ = 1000, 5_000_000, 10_000
TAGS = [("BURN", "🔥", ["burn", "supply reduction", "deflation", "deflationary"]),
        ("BUYBACK", "💰", ["buyback", "buy-back", "fee switch", "revenue share", "revenue distribution"]),
        ("TOKENOMICS", "⚙️", ["tokenomics", "halving", "emissions", "staking", "token split", "redenomination"]),
        ("UPGRADE", "🚀", ["mainnet launch", "token migration", "migration", "rebrand"])]
EMOJI = {t: e for t, e, _ in TAGS}
KEYWORDS = [(t, k, re.compile(r"\b" + re.escape(k) + r"(?:s|ed|ing)?\b", re.I)) for t, _, ks in TAGS for k in ks]
CODE_TITLE = re.compile(r"^\s*\[?[A-Za-z]{2,8}[- ]?\d+\]?\s*:?\s*$")       # "AIP-12", "[VIP 5]"
NEGATIVE = re.compile(r"^\s*(no|against|reject|abstain|do nothing|cancel)\b", re.I)
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
EVM_CHAINS = {"1": "ethereum", "56": "bsc", "8453": "base", "42161": "arbitrum", "137": "polygon",
              "10": "optimism", "43114": "avalanche", "59144": "linea", "81457": "blast",
              "324": "zksync", "25": "cronos", "999": "hyperevm"}


def tag(p):
    """Step A: (tag, keyword) or None."""
    title = p.get("title") or ""
    for text in [title] + ([(p.get("body") or "")[:500]] if CODE_TITLE.match(title) else []):
        for t, k, rx in KEYWORDS:
            if rx.search(text):
                return t, k
    return None


def passed(p):
    """Step B: (winning choice, approval %) or None."""
    sc, ch, tot = p.get("scores") or [], p.get("choices") or [], p.get("scores_total") or 0
    if tot <= 0 or not ch or len(sc) != len(ch) or (p.get("quorum") or 0) > tot:
        return None
    i = sc.index(max(sc))
    pct = sc[i] / tot * 100
    return None if pct < 50 or NEGATIVE.match(str(ch[i])) else (str(ch[i]), round(pct, 1))


def team_author(p):
    """Step C."""
    s = p["space"]
    team = {a.lower() for a in (s.get("admins") or []) + (s.get("moderators") or []) + (s.get("members") or [])}
    return (p.get("author") or "").lower() in team or bool((s.get("filters") or {}).get("onlyMembers"))


def fetch_closed(end_gte):
    out, skip = [], 0
    while skip <= MAX_SKIP:
        r = requests.post(URL, json={"query": QUERY, "variables": {"first": PAGE, "skip": skip, "end_gte": end_gte}},
                          timeout=60)
        r.raise_for_status()
        js = r.json()
        if js.get("errors"):
            raise RuntimeError(js["errors"][0].get("message"))
        page = js["data"]["proposals"]
        out += page
        if len(page) < PAGE:
            break
        skip += PAGE
    return out


def _symbol(space):
    s = re.sub(r"[^A-Za-z0-9]", "", space.get("symbol") or "")
    return re.sub(r"^(ve|vl|st|sd|v)(?=[A-Z])", "", s).upper()   # veCRV / vlMGP / stWCT / sdBAL / vILV -> the token


def strategy_contracts(space):
    """[(chain, address)] from the space's voting strategies, the space's own chain first."""
    home, seen, out = EVM_CHAINS.get(str(space.get("network"))), set(), []
    for s in space.get("strategies") or []:
        chain = EVM_CHAINS.get(str(s.get("network") or space.get("network")))
        addr = str((s.get("params") or {}).get("address") or "")
        if chain and ADDRESS.match(addr) and (chain, addr.lower()) not in seen:
            seen.add((chain, addr.lower()))
            out.append((chain, addr))
    return sorted(out, key=lambda c: c[0] != home)


def cg_mcaps(chain, addresses):
    """{address lower: CoinGecko market cap > 0} - one call for up to a few contracts."""
    from age_check import CHAIN_TO_CG_PLATFORM
    from discovery import CG_KEY
    js = requests.get(f"https://api.coingecko.com/api/v3/simple/token_price/{CHAIN_TO_CG_PLATFORM[chain]}",
                      params={"contract_addresses": ",".join(addresses), "vs_currencies": "usd",
                              "include_market_cap": "true"},
                      headers={"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}, timeout=20).json()
    return {a.lower(): v["usd_market_cap"] for a, v in (js.items() if isinstance(js, dict) else [])
            if isinstance(v, dict) and (v.get("usd_market_cap") or 0) > 0}   # CoinGecko says 0 when it doesn't know


def dex_search(chain, sym):
    """The most liquid pair (>= $10K) with exactly this symbol ON this chain, or None -
    an unpinned symbol search returns fakes (e.g. "AAVE" on Solana)."""
    pairs = requests.get("https://api.dexscreener.com/latest/dex/search", params={"q": sym}, timeout=20).json().get("pairs") or []
    liq = lambda p: (p.get("liquidity") or {}).get("usd") or 0
    ok = [p for p in pairs if p.get("chainId") == chain and (p.get("baseToken", {}).get("symbol") or "").upper() == sym
          and liq(p) >= MIN_LIQ]
    return max(ok, key=liq) if ok else None


def resolve(space):
    """{chain, contract, market_cap, mcap_source} or {}:
    1. a strategy contract on the space's chain that CoinGecko knows
    2. else the chain-pinned DexScreener search (CoinGecko mcap; DexScreener's only as a display fallback)
    3. else a strategy contract on another chain that CoinGecko knows."""
    try:
        cands = strategy_contracts(space)[:12]
        known = {}
        for chain in dict.fromkeys(c for c, _ in cands):
            known.update({(chain, a): m for a, m in cg_mcaps(chain, [a for c, a in cands if c == chain]).items()})
        confirmed = [(c, a) for c, a in cands if (c, a.lower()) in known]          # still home chain first
        found = lambda c, a: {"chain": c, "contract": a, "market_cap": known[(c, a.lower())], "mcap_source": "coingecko"}
        home = EVM_CHAINS.get(str(space.get("network")))
        if confirmed and confirmed[0][0] == home:
            return found(*confirmed[0])
        pair = dex_search(home, _symbol(space)) if home and _symbol(space) else None
        if pair:
            addr = pair["baseToken"]["address"]
            m = cg_mcaps(home, [addr]).get(addr.lower())
            return {"chain": home, "contract": addr, "market_cap": m or pair.get("marketCap"),
                    "mcap_source": "coingecko" if m else ("dexscreener" if pair.get("marketCap") else None)}
        if confirmed:
            return found(*confirmed[0])
    except Exception as e:
        log.warning("resolve %s failed (%s)", space.get("id"), type(e).__name__)
    return {}


def quality_ok(space, tok):
    """Step D (after resolve): verified, or the CoinGecko market cap >= $5M."""
    return bool(space.get("verified")) or (tok.get("mcap_source") == "coingecko" and (tok.get("market_cap") or 0) >= MIN_MCAP)


TICKER = re.compile(r"\$?\b[A-Z][A-Z0-9]{1,}\b|\$[A-Za-z]+\b")   # BEETS, $DRV, AIP, USDT - kept as is
TR_URL = "https://translate.googleapis.com/translate_a/single"
GLOSSARY = [(re.compile(r"\bstaking\b", re.I), "الستاكينغ"),          # Google: "spatial signing"
            (re.compile(r"\btokenomics\b", re.I), "اقتصاد التوكن")]


def translate_ar(title, keep_words=()):
    """English title -> Arabic, tickers / codes / the project name kept in Latin
    (else BEETS -> "beetroot"). None on failure (filled again next scan).
    shortcut: Google's keyless web endpoint (not an official API) - switch to a keyed API if it starts refusing."""
    keep = []
    def hold(m, put_back=None):
        keep.append(put_back or m.group(0))
        return f"[{len(keep) - 1}]"
    q = title or ""
    for rx, ar in GLOSSARY:
        q = rx.sub(lambda m: hold(m, ar), q)
    for w in sorted({w for w in keep_words if w and len(w) > 2}, key=len, reverse=True):
        q = re.sub(r"\b" + re.escape(w) + r"\b", hold, q, flags=re.I)
    q = TICKER.sub(hold, q)
    try:
        r = requests.get(TR_URL, params={"client": "gtx", "sl": "en", "tl": "ar", "dt": "t", "q": q}, timeout=20)
        r.raise_for_status()
        out = "".join(seg[0] for seg in r.json()[0] if seg and seg[0])
    except Exception as e:
        log.warning("translate failed (%s)", type(e).__name__)
        return None
    out = re.sub(r"\[\s*(\d+)\s*\]", lambda m: keep[int(m.group(1))] if int(m.group(1)) < len(keep) else "", out)
    return out.strip() or None


def fill_arabic(session, limit=20):
    """Arabic titles for rows that have none yet (new rows, earlier failures) -> count filled."""
    from models import DaoCatalystAlert as A
    n = 0
    for r in session.query(A).filter(A.title_ar.is_(None)).limit(limit):
        r.title_ar = translate_ar(r.title, [r.space_name, r.symbol])     # whole name: "Stake DAO BAL", not "Stake"
        n += bool(r.title_ar)
    return n


def scan(force=False, now=None):
    """Hourly hook: scans every CADENCE_H hours (or force) -> stats. Raises on a
    Snapshot failure (the gate does not move, so the next hour retries)."""
    from database import SessionLocal, utcnow
    from models import DaoCatalystAlert as A, DaoScanState
    now = now or utcnow()
    session = SessionLocal()
    try:
        state = session.get(DaoScanState, 1)
        if state and not force and now - state.last_scan_at < timedelta(hours=CADENCE_H):
            return {"skipped": True}
        since = state.last_scan_at - OVERLAP if state else now - timedelta(days=BACKFILL_D)
        props = fetch_closed(int(since.replace(tzinfo=timezone.utc).timestamp()))
        known = {pid for (pid,) in session.query(A.proposal_id)}
        stats, tokens = {"scanned": len(props), "kept": 0, "lookups": 0}, {}
        for p in props:
            sp = p["space"]
            if p["id"] in known:
                continue
            t, win = tag(p), passed(p)
            if not (t and win and team_author(p)):
                continue
            if not sp.get("verified") and (sp.get("followersCount") or 0) < MIN_FOLLOWERS:
                continue                                          # rejected before any call
            if sp["id"] not in tokens:
                tokens[sp["id"]] = resolve(sp)
                stats["lookups"] += 1
            tok = tokens[sp["id"]]
            if not quality_ok(sp, tok):
                continue
            session.add(A(proposal_id=p["id"], space_id=sp["id"], space_name=sp.get("name"), symbol=sp.get("symbol"),
                          verified=bool(sp.get("verified")), followers=sp.get("followersCount"), tag=t[0], keyword=t[1],
                          title=p.get("title") or "", winning_choice=win[0], approval_pct=win[1],
                          closed_at=datetime.fromtimestamp(p["end"], timezone.utc).replace(tzinfo=None),
                          chain=tok.get("chain"), contract=tok.get("contract"), market_cap=tok.get("market_cap"),
                          mcap_source=tok.get("mcap_source")))
            known.add(p["id"])
            stats["kept"] += 1
        stats["pruned"] = session.query(A).filter(A.closed_at < now - timedelta(days=KEEP_D)).delete()
        stats["arabic"] = fill_arabic(session)
        state = state or DaoScanState(id=1)
        state.last_scan_at = now
        session.add(state)
        session.commit()
        return stats
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def latest(session):
    """The page's rows, newest first; in_screener = the contract is an active coin (never raises)."""
    from models import Coin, DaoCatalystAlert as A
    try:
        active = {c.lower() for (c,) in session.query(Coin.contract).filter(Coin.status == "active")}
        return [{"proposal_id": r.proposal_id, "space_id": r.space_id, "space_name": r.space_name, "symbol": r.symbol,
                 "verified": r.verified, "tag": r.tag, "emoji": EMOJI.get(r.tag, ""), "keyword": r.keyword,
                 "title": r.title, "title_ar": r.title_ar, "winning_choice": r.winning_choice, "approval_pct": r.approval_pct,
                 "closed_at": r.closed_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "chain": r.chain, "contract": r.contract,
                 "market_cap": r.market_cap, "mcap_source": r.mcap_source,
                 "in_screener": (r.contract or "").lower() in active}
                for r in session.query(A).order_by(A.closed_at.desc())]
    except Exception:
        session.rollback()
        return []
