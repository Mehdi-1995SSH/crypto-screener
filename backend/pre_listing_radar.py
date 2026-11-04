"""
pre_listing_radar.py
--------------------
PRE-LISTING / MARKET-MAKER RADAR (daily, Active coins only; display + Telegram
- no entry / score / drop rule reads it). Transfers of each active coin in the
last 24 h to / from the wallets in watched_entities (10 CEXs + 15 market
makers, seed_watched_entities.py). DIRECT transfers only: a transfer from or
to one of the coin's DEX pools / a router is a swap and never counts.
  CEX_DEPOSIT    deployer / owner / top holder -> a CEX wallet, the coin NOT
                 listed on that CEX. Also via an unlabeled wallet that got
                 >= 0.1% of supply from them and forwarded it to the CEX in
                 the window (via = deposit_sweep: teams deposit to their own,
                 unlabeled deposit address)
  MM_ONBOARDING  a market maker receives the coin by direct transfer from the
                 deployer / owner / a top holder, or is a top holder
  MM_TO_CEX      a market maker sends the coin to a CEX where it isn't listed
Rule 3: a wallet new in the top holders since the last snapshot, fresh (first
tx < 7 days; nonce <= 5 on BSC / Base / Avalanche - no free history there):
gas funded by a market maker, or sweeping the coin to a watched wallet ->
stored in watched_entities (1_HOP_DETECTION, permanent) + the matching alert.
(Gas from a CEX is a normal exchange withdrawal - not stored, not alerted.)
Sources: Etherscan V2 (its free chains), public RPC eth_getLogs (BSC / Base /
Avalanche), GoPlus top 10 holders (EVM), Helius + RugCheck (Solana).
One alert per (coin, rule, entity, cex). Alerts and top-holder snapshots older
than 30 days are deleted every run; watched_entities never.
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("pre_listing_radar")

WINDOW_H, KEEP_D, FRESH_DAYS, FRESH_NONCE = 24, 30, 7, 5
SWEEP_MIN_PCT = 0.1                      # % of supply an insider must send for deposit_sweep
ES_PAGE, ES_PAGES = 1000, 5              # Etherscan tokentx: page * offset <= 10000
RPC_CHUNK, RPC_GAP = 10, 0.3             # public RPCs accept <= 10 token contracts per eth_getLogs
SOL_PAGES = 3                            # Helius pages of 100 txs per mint
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BURN = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}
# Well-known routers / aggregators: tokens leaving them are swaps, never direct sends.
ROUTERS = {
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d", "0xe592427a0aece92de3edee1f18e0157c05861564",   # Uniswap V2 / V3
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45", "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",   # SwapRouter02 / Universal
    "0x66a9893cc07d91d95644aedd05d03f95e1dba8af",                                                 # Universal Router v4
    "0x10ed43c718714eb63d5aa57b78b54704e256024e", "0x13f4ea83d0bd40e75c8222255bc855a974568dd4",   # PancakeSwap V2 / Smart
    "0x1111111254eeb25477b68fb85ed929f73a960582", "0x111111125421ca6dc452d289314280a0f8842a65",   # 1inch v5 / v6
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff", "0x000000000022d473030f116ddee9f6b43ac78ba3",   # 0x / Permit2
    "BQ72nSv9f3PRyRKCBnHLVrerrv37CYTHm5h3s9VSGQDV",                                               # Jupiter authority
}
# Our CEX names -> exchange_listings slugs (DropsTab). Crypto.com isn't monitored -> CoinGecko.
SLUGS = {"Binance": "binance", "Coinbase": "gdax", "OKX": "okex", "Bybit": "bybit-spot", "KuCoin": "kucoin",
         "Bitget": "bitget", "Gate.io": "gate", "HTX": "huobi", "MEXC": "mxc"}
EXPLORERS = {"ethereum": "https://etherscan.io/tx/", "bsc": "https://bscscan.com/tx/", "base": "https://basescan.org/tx/",
             "polygon": "https://polygonscan.com/tx/", "arbitrum": "https://arbiscan.io/tx/",
             "avalanche": "https://snowtrace.io/tx/", "solana": "https://solscan.io/tx/"}


# ---------------------------------------------------------------- pure rules
def key(chain, addr):
    """watched_entities form of an address: EVM lowercase, Solana exact."""
    return addr if chain == "solana" else (addr or "").lower()


def classify(t, watched, insiders, pools, listed):
    """One direct transfer -> [(rule, entity, cex, via)] (pure). A transfer from or to a pool /
    router is a swap -> []."""
    frm, to = t["from"], t["to"]
    if frm in pools or to in pools:
        return []
    w_from, w_to = watched.get(frm), watched.get(to)
    out = []
    if w_to and w_to[1] == "CEX" and w_to[0] not in listed:
        if w_from and w_from[1] == "MARKET_MAKER":
            out.append(("MM_TO_CEX", w_from[0], w_to[0], "direct"))
        elif frm in insiders:
            out.append(("CEX_DEPOSIT", w_to[0], w_to[0], "direct"))
    # Onboarding = the project side (deployer / owner / top holder) hands tokens to the MM. Exchange
    # withdrawals to MMs (Gate.io -> Jump, Robinhood -> Wintermute) are routine inventory - not counted.
    if w_to and w_to[1] == "MARKET_MAKER" and frm in insiders:
        out.append(("MM_ONBOARDING", w_to[0], "", "direct"))
    return out


def deposit_sweeps(transfers, watched, insiders, pools, listed, supply):
    """Insider -> unlabeled wallet (>= SWEEP_MIN_PCT of supply) that forwards to a CEX where the coin
    isn't listed, later in the window -> [((rule, entity, cex, via), first_hop, sweep)] (pure)."""
    if not supply:
        return []
    hop = {}
    for t in transfers:
        if (t["from"] in insiders and t["to"] not in watched and t["to"] not in pools and t["to"] not in insiders
                and t["amount"] / supply * 100 >= SWEEP_MIN_PCT):
            hop.setdefault(t["to"], t)
    out = []
    for t in transfers:
        src, w = hop.get(t["from"]), watched.get(t["to"])
        if src and w and w[1] == "CEX" and w[0] not in listed and t["ts"] >= src["ts"]:
            out.append((("CEX_DEPOSIT", w[0], w[0], "deposit_sweep"), src, t))
    return out


def fresh_entrants(today, yesterday, skip):
    """Top holders that weren't in the last snapshot (none on the first run) (pure)."""
    if yesterday is None:
        return []
    before = set(yesterday)
    return [a for a in today if a not in before and a not in skip]


def one_hop(info, watched, listed):
    """Rule 3 for one fresh wallet -> (entity, type, rule, cex, via) or None (pure).
    info: {"fresh", "direct_in", "funder", "sweeps": [to, ...]}."""
    if not info or not info.get("fresh") or not info.get("direct_in"):
        return None
    for to in info.get("sweeps") or []:
        w = watched.get(to)
        if w and w[1] == "CEX" and w[0] not in listed:
            return w[0], "CEX", "CEX_DEPOSIT", w[0], "1_hop_sweep"
        if w and w[1] == "MARKET_MAKER":
            return w[0], "MARKET_MAKER", "MM_ONBOARDING", "", "1_hop_sweep"
    w = watched.get(info.get("funder"))
    if w and w[1] == "MARKET_MAKER":                          # gas from a CEX = a normal withdrawal
        return w[0], "MARKET_MAKER", "MM_ONBOARDING", "", "1_hop_gas"
    return None


def describe(a, coin_name):
    """{"en", "ar"} text of an alert dict (rule, entity_name, cex_name, via) (pure)."""
    rule, ent, cex, via = a["rule"], a["entity_name"], a["cex_name"], a.get("via")
    if rule == "CEX_DEPOSIT":
        how = {"deposit_sweep": ("through an intermediate deposit address", "عبر عنوان إيداع وسيط"),
               "1_hop_sweep": ("from a new top holder's wallet", "من محفظة جديدة ضمن كبار الحاملين")}.get(
            via, ("by direct transfer from the team / a top holder", "بتحويل مباشر من الفريق أو كبار الحاملين"))
        return {"en": f"🚨 Possible upcoming listing: {coin_name} was sent to a {cex} wallet {how[0]} - "
                      f"it is not listed on {cex} yet.",
                "ar": f"🚨 تنبيه إدراج قريب: عملة {coin_name} أُرسلت إلى محفظة منصة {cex} {how[1]} "
                      f"وهي غير مدرجة فيها بعد!"}
    if rule == "MM_TO_CEX":
        return {"en": f"🐋 Market maker {ent} sent {coin_name} to {cex} - not listed there yet.",
                "ar": f"🐋 صانع السوق {ent} أرسل عملة {coin_name} إلى منصة {cex} وهي غير مدرجة فيها بعد!"}
    how = {"top_holder": ("is now one of the top holders of", "أصبح ضمن أكبر حاملي عملة"),
           "1_hop_gas": ("funded a new top holder of", "موّل محفظة جديدة ضمن كبار حاملي عملة"),
           "1_hop_sweep": ("received from a new top holder of", "استلم من محفظة جديدة ضمن كبار حاملي عملة")}.get(
        via, ("received by direct transfer (not a DEX buy)", "استلم بتحويل مباشر (ليس شراء من DEX) عملة"))
    return {"en": f"🐋 Market maker {ent} {how[0]} {coin_name}.", "ar": f"🐋 صانع السوق {ent} {how[1]} {coin_name}."}


# ---------------------------------------------------------------- listings
def listed_on(session, coin_ids):
    """{coin_id: {CEX names it trades on}} from exchange_listings (0 calls)."""
    from models import ExchangeListing
    by_slug = {v: k for k, v in SLUGS.items()}
    out = {}
    for cid, ex in (session.query(ExchangeListing.coin_id, ExchangeListing.exchange)
                    .filter(ExchangeListing.coin_id.in_(coin_ids), ExchangeListing.delisted_on.is_(None))):
        if ex in by_slug:
            out.setdefault(cid, set()).add(by_slug[ex])
    return out


def on_crypto_com(chain, contract):
    """True / False from CoinGecko's tickers, None when unknown (1 call, only when needed)."""
    from age_check import CHAIN_TO_CG_PLATFORM
    from discovery import CG_KEY
    plat = CHAIN_TO_CG_PLATFORM.get(chain)
    if not plat:
        return None
    try:
        r = requests.get(f"https://api.coingecko.com/api/v3/coins/{plat}/contract/{contract}",
                         params={"localization": "false", "tickers": "true", "market_data": "false"},
                         headers={"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}, timeout=20)
        if r.status_code != 200:
            return None
        return any("crypto_com" in ((t.get("market") or {}).get("identifier") or "") for t in r.json().get("tickers") or [])
    except Exception:
        return None


# ---------------------------------------------------------------- EVM sources
def _es(cid, **params):
    from burn_detector import _es_get
    return _es_get(cid, **params)


def _rpc(url, method, params):
    from burn_detector import BurnError, _rpc_post
    time.sleep(RPC_GAP)
    r = _rpc_post(url, [{"jsonrpc": "2.0", "id": 1, "method": method, "params": params}])[1]
    if "error" in r:
        raise BurnError(f"RPC {method}: {str(r['error'])[:80]}")
    return r["result"]


def goplus_holders(chain, contract):
    """(top 10 holder addresses, pool addresses) from GoPlus (1 call); ([], set()) on failure."""
    from security_check import CHAIN_IDS, GOPLUS_EVM, _goplus
    cid = CHAIN_IDS.get(chain)
    data = _goplus(f"{GOPLUS_EVM}/{cid}", {"contract_addresses": contract}) if cid else None
    info = next(iter(((data or {}).get("result") or {}).values()), None) or {}
    pools = {(d.get("pair") or "").lower() for d in info.get("dex") or [] if d.get("pair")}
    pools |= {(h.get("address") or "").lower() for h in info.get("holders") or [] if h.get("tag")}   # tagged = DEX / locker
    top = [(h.get("address") or "").lower() for h in info.get("holders") or []]
    return [a for a in top if a and a not in pools and a not in BURN], pools


def es_transfers(cid, contract, start_block):
    out = []
    for page in range(1, ES_PAGES + 1):
        rows = _es(cid, module="account", action="tokentx", contractaddress=contract, startblock=start_block,
                   endblock=99999999, page=page, offset=ES_PAGE, sort="desc") or []
        out += [{"from": r["from"].lower(), "to": r["to"].lower(), "tx": r["hash"], "ts": int(r["timeStamp"]),
                 "amount": int(r["value"]) / 10 ** int(r.get("tokenDecimal") or 0)} for r in rows]
        if len(rows) < ES_PAGE:
            break
    return out


def es_wallet(cid, wallet, contract, pools, now_ts):
    """Rule 3 facts for a fresh-candidate wallet on an Etherscan chain (2 calls)."""
    first = (_es(cid, module="account", action="txlist", address=wallet, page=1, offset=1, sort="asc") or [None])[0]
    if not first:
        return None
    fresh = now_ts - int(first["timeStamp"]) < FRESH_DAYS * 86400
    funder = first["from"].lower() if first.get("to", "").lower() == wallet else None
    toks = _es(cid, module="account", action="tokentx", contractaddress=contract, address=wallet,
               page=1, offset=50, sort="desc") or []
    return {"fresh": fresh, "funder": funder,
            "direct_in": any(t["to"].lower() == wallet and t["from"].lower() not in pools for t in toks),
            "sweeps": [t["to"].lower() for t in toks if t["from"].lower() == wallet]}


def rpc_window(url):
    """(head, first block of the last WINDOW_H hours, seconds per block) from 2 block headers."""
    head = int(_rpc(url, "eth_blockNumber", []), 16)
    t1 = int(_rpc(url, "eth_getBlockByNumber", [hex(head), False])["timestamp"], 16)
    t0 = int(_rpc(url, "eth_getBlockByNumber", [hex(head - 10000), False])["timestamp"], 16)
    spb = max(t1 - t0, 1) / 10000
    return head, max(0, head - int(WINDOW_H * 3600 / spb)), spb, t1


def rpc_transfers(url, step, tokens, decimals, watched_evm, insiders, head, start, spb, head_ts):
    """Transfers of up to 10 tokens TO a watched wallet or FROM an insider, start..head (public RPC)."""
    pad = lambda a: "0x" + a[2:].rjust(64, "0")
    out = []
    for topics in ([TRANSFER, None, [pad(a) for a in watched_evm]],
                   [TRANSFER, [pad(a) for a in insiders]] if insiders else None):
        if not topics:
            continue
        for lo in range(start, head + 1, step):
            for lg in _rpc(url, "eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(min(lo + step - 1, head)),
                                                 "address": tokens, "topics": topics}]):
                if len(lg.get("topics") or []) < 3:
                    continue
                blk = int(lg["blockNumber"], 16)
                out.append({"token": lg["address"].lower(), "from": "0x" + lg["topics"][1][-40:].lower(),
                            "to": "0x" + lg["topics"][2][-40:].lower(), "tx": lg["transactionHash"], "ts": blk,
                            "time": head_ts - (head - blk) * spb,
                            "amount": int(lg["data"], 16) / 10 ** decimals.get(lg["address"].lower(), 18)})
    seen, uniq = set(), []                       # an insider -> watched transfer matches both filters
    for t in out:
        if (t["tx"], t["from"], t["to"], t["amount"]) not in seen:
            seen.add((t["tx"], t["from"], t["to"], t["amount"]))
            uniq.append(t)
    return uniq


def rpc_decimals(url, tokens):
    out = {}
    for a in tokens:
        try:
            out[a] = int(_rpc(url, "eth_call", [{"to": a, "data": "0x313ce567"}, "latest"]), 16)
        except Exception:
            out[a] = 18
    return out


# ---------------------------------------------------------------- Solana sources
def _helius(method, url, **kw):
    from burn_detector import _helius as h
    return h(method, url, **kw)


def sol_context(mint):
    """(top 20 owner wallets, pools, creator) - Helius largest accounts + RugCheck known accounts."""
    from burn_detector import HELIUS_RPC
    from security_check import RUGCHECK
    pools, creator = set(), None
    try:
        rc = requests.get(RUGCHECK.format(mint=mint), timeout=30).json()
        for a, v in (rc.get("knownAccounts") or {}).items():
            if v.get("type") in ("AMM", "LOCKER", "CLOB"):
                pools.add(a)
            elif v.get("type") == "CREATOR":
                creator = a
        creator = creator or rc.get("creator")
    except Exception:
        pass
    rpc = lambda m, p: (_helius("POST", HELIUS_RPC, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}) or {}).get("result") or {}
    accts = [x["address"] for x in (rpc("getTokenLargestAccounts", [mint]).get("value") or [])]
    owners = []
    if accts:
        for v in rpc("getMultipleAccounts", [accts, {"encoding": "jsonParsed"}]).get("value") or []:
            o = ((((v or {}).get("data") or {}).get("parsed") or {}).get("info") or {}).get("owner")
            if o:
                owners.append(o)
    pools |= {o for o, a in zip(owners, accts) if a in pools}      # pool vaults are owned by the pool authority
    return [o for o in owners if o not in pools], pools, creator


def sol_transfers(mint, since_ts):
    """The mint's token transfers since since_ts (Helius parsed txs; swaps left out)."""
    from burn_detector import HELIUS_TXS
    out, before = [], None
    for _ in range(SOL_PAGES):
        txs = _helius("GET", HELIUS_TXS.format(mint=mint), params={"limit": 100, **({"before": before} if before else {})}) or []
        for tx in txs:
            if tx.get("type") == "SWAP" or (tx.get("timestamp") or 0) < since_ts:
                continue
            for t in tx.get("tokenTransfers") or []:
                if t.get("mint") == mint and t.get("fromUserAccount") and t.get("toUserAccount"):
                    out.append({"from": t["fromUserAccount"], "to": t["toUserAccount"], "tx": tx["signature"],
                                "ts": tx["timestamp"], "amount": float(t.get("tokenAmount") or 0)})
        if not txs or (txs[-1].get("timestamp") or 0) < since_ts:
            break
        before = txs[-1]["signature"]
    return out


def sol_wallet(wallet, mint, pools, now_ts):
    """Rule 3 facts for a fresh-candidate Solana wallet (1 Helius call)."""
    from burn_detector import HELIUS_TXS
    txs = _helius("GET", HELIUS_TXS.format(mint=wallet), params={"limit": 100}) or []
    if not txs:
        return None
    oldest = txs[-1]
    fresh = len(txs) < 100 and now_ts - (oldest.get("timestamp") or 0) < FRESH_DAYS * 86400
    funder = next((n["fromUserAccount"] for n in oldest.get("nativeTransfers") or [] if n.get("toUserAccount") == wallet), None)
    tt = [t for tx in txs if tx.get("type") != "SWAP" for t in tx.get("tokenTransfers") or [] if t.get("mint") == mint]
    return {"fresh": fresh, "funder": funder,
            "direct_in": any(t.get("toUserAccount") == wallet and t.get("fromUserAccount") not in pools for t in tt),
            "sweeps": [t["toUserAccount"] for t in tt if t.get("fromUserAccount") == wallet and t.get("toUserAccount")]}


# ---------------------------------------------------------------- the run
def _pairs(coins):
    from fetch_dexscreener import fetch_pairs_for_addresses
    try:
        pairs = fetch_pairs_for_addresses([c.contract for c in coins])
    except Exception:
        return {}
    out = {}
    for p in pairs:
        tok = ((p.get("baseToken") or {}).get("address") or "")
        out.setdefault(tok.lower(), set()).add(key(p.get("chainId"), p.get("pairAddress") or ""))
    return out


def save_alert(session, coin, found, rule, ent, cex, via, t=None, chain=None, now=None):
    """Insert one alert unless (coin, rule, entity, cex) already exists -> the new row or None."""
    from models import PreListingAlert as P
    if (rule, ent, cex) in found or session.query(P.id).filter_by(coin_id=coin.id, rule=rule, entity_name=ent,
                                                                  cex_name=cex).first():
        return None
    found.add((rule, ent, cex))
    t = t or {}
    when = t.get("time", t.get("ts"))
    supply = coin.total_supply or 0
    row = P(coin_id=coin.id, rule=rule, entity_name=ent, cex_name=cex, via=via, from_address=t.get("from"),
            to_address=t.get("to"), amount=t.get("amount"), chain=chain or coin.chain, tx_hash=t.get("tx"),
            pct_supply=round(t["amount"] / supply * 100, 4) if supply and t.get("amount") else None,
            tx_time=datetime.fromtimestamp(when, timezone.utc).replace(tzinfo=None) if when else None, created_at=now)
    session.add(row)
    return row


def analyze(session, coin, ctx, transfers, watched, listed, now, wallet_info, add_watched):
    """All rules for one coin -> new alert rows. wallet_info(addr) -> Rule 3 facts (calls)."""
    from database import utcnow
    now = now or utcnow()
    found, rows = set(), []
    pools, insiders = ctx["pools"] | ROUTERS, ctx["insiders"]
    for t in transfers:
        for rule, ent, cex, via in classify(t, watched, insiders, pools, listed):
            rows.append(save_alert(session, coin, found, rule, ent, cex, via, t, now=now))
    for (rule, ent, cex, via), src, sweep in deposit_sweeps(transfers, watched, insiders, pools, listed, coin.total_supply):
        rows.append(save_alert(session, coin, found, rule, ent, cex, via, {**sweep, "from": src["from"]}, now=now))
    for a in ctx["top"]:
        w = watched.get(a)
        if w and w[1] == "MARKET_MAKER":
            rows.append(save_alert(session, coin, found, "MM_ONBOARDING", w[0], "", "top_holder", {"to": a}, now=now))
    for a in fresh_entrants(ctx["top"], ctx["yesterday"], set(watched) | pools):
        hit = one_hop(wallet_info(a), watched, listed)
        if hit:
            ent, kind, rule, cex, via = hit
            add_watched(a, ent, kind)
            rows.append(save_alert(session, coin, found, rule, ent, cex, via, {"to": a}, now=now))
    return [r for r in rows if r is not None]


def prune(session, now):
    from models import PreListingAlert, TopHolderSnapshot
    cut = now - timedelta(days=KEEP_D)
    return (session.query(PreListingAlert).filter(PreListingAlert.created_at < cut).delete()
            + session.query(TopHolderSnapshot).filter(TopHolderSnapshot.day < cut.date()).delete())


def snapshot(session, coin_id, top, today):
    """Store today's top holders; return the previous snapshot's list (None if there is none)."""
    from models import TopHolderSnapshot as S
    prev = (session.query(S).filter(S.coin_id == coin_id, S.day < today).order_by(S.day.desc()).first())
    row = session.query(S).filter_by(coin_id=coin_id, day=today).first() or S(coin_id=coin_id, day=today)
    row.holders = json.dumps(top)
    session.add(row)
    return json.loads(prev.holders) if prev else None


def notify(alerts, names):
    """One Arabic Telegram message for the run's NEW alerts; nothing when there are none."""
    if not alerts:
        return False
    from daily_report import send_message
    lines = ["📡 رادار الإدراج وصنّاع السوق - تنبيهات جديدة:"]
    for a in alerts:
        lines.append(describe({"rule": a.rule, "entity_name": a.entity_name, "cex_name": a.cex_name, "via": a.via},
                              names.get(a.coin_id, "?"))["ar"])
    chunks, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 2 > 4000:
            chunks.append(cur)
            cur = ""
        cur += ln + "\n\n"
    chunks.append(cur)
    return all([send_message(c.strip()) for c in chunks])


def run_pre_listing_radar(now=None):
    """Daily step. Returns stats; per-coin failures are counted, never raised."""
    from burn_detector import ES_FREE, RPCS, CHAIN_IDS as ES_IDS
    from database import SessionLocal, utcnow
    from models import Coin, WatchedEntity
    now = now or utcnow()
    now_ts = int(now.replace(tzinfo=timezone.utc).timestamp())
    since_ts = now_ts - WINDOW_H * 3600
    stats = {"coins": 0, "alerts": 0, "new_watched": 0, "errors": 0, "pruned": 0}
    session = SessionLocal()
    try:
        watched = {w.address: (w.entity_name, w.entity_type) for w in session.query(WatchedEntity)}
        watched_evm = [a for a in watched if a.startswith("0x")]
        coins = [c for c in session.query(Coin).filter(Coin.status == "active")
                 if c.chain == "solana" or ES_IDS.get(c.chain) in ES_FREE | set(RPCS)]
        listed = listed_on(session, [c.id for c in coins])
        pairs = _pairs([c for c in coins if c.chain != "solana"])
        stats["coins"] = len(coins)

        def add_watched(addr, ent, kind):
            if addr not in watched:
                watched[addr] = (ent, kind)
                session.add(WatchedEntity(address=addr, chain="solana" if not addr.startswith("0x") else "evm",
                                          entity_name=ent, entity_type=kind, is_root_wallet=False,
                                          discovered_via="1_HOP_DETECTION", source_label="radar 1-hop", created_at=now))
                stats["new_watched"] += 1

        ctxs = {}
        for c in coins:
            try:
                if c.chain == "solana":
                    top, pools, creator = sol_context(c.contract)
                    ins = {a for a in (creator, c.owner_address) if a}
                else:
                    top, pools = goplus_holders(c.chain, c.contract)
                    pools |= pairs.get(c.contract.lower(), set())
                    ins = {a.lower() for a in (c.creator_address, c.owner_address) if a}
                ins = (ins | set(top)) - set(watched) - pools - BURN
                ctxs[c.id] = {"top": top, "pools": pools, "insiders": ins,
                              "yesterday": snapshot(session, c.id, top, now.date())}
            except Exception as e:
                stats["errors"] += 1
                log.warning("radar context %s (%s): %s", c.name, c.chain, type(e).__name__)

        transfers = {}
        es_start = {}
        for c in coins:
            cid = ES_IDS.get(c.chain)
            if c.id not in ctxs or c.chain == "solana" or cid not in ES_FREE:
                continue
            try:
                if cid not in es_start:
                    es_start[cid] = int(_es(cid, module="block", action="getblocknobytime", timestamp=since_ts, closest="after"))
                transfers[c.id] = es_transfers(cid, c.contract, es_start[cid])
            except Exception as e:
                stats["errors"] += 1
                log.warning("radar transfers %s: %s", c.name, e)
        for chain in {c.chain for c in coins if ES_IDS.get(c.chain) in RPCS and ES_IDS.get(c.chain) not in ES_FREE}:
            url, step = RPCS[ES_IDS[chain]]
            group = [c for c in coins if c.chain == chain and c.id in ctxs]
            try:
                head, start, spb, head_ts = rpc_window(url)
            except Exception as e:
                stats["errors"] += len(group)
                log.warning("radar %s window: %s", chain, e)
                continue
            for i in range(0, len(group), RPC_CHUNK):
                part = group[i:i + RPC_CHUNK]
                toks = [c.contract.lower() for c in part]
                ins = sorted(set().union(*(ctxs[c.id]["insiders"] for c in part)))
                try:
                    got = rpc_transfers(url, step, toks, rpc_decimals(url, toks), watched_evm, ins, head, start, spb, head_ts)
                except Exception as e:
                    stats["errors"] += len(part)
                    log.warning("radar %s logs: %s", chain, e)
                    continue
                for c in part:
                    transfers[c.id] = [t for t in got if t["token"] == c.contract.lower()]
        for c in coins:
            if c.chain == "solana" and c.id in ctxs:
                try:
                    transfers[c.id] = sol_transfers(c.contract, since_ts)
                except Exception as e:
                    stats["errors"] += 1
                    log.warning("radar solana %s: %s", c.name, e)

        new = []
        for c in coins:
            if c.id not in ctxs or c.id not in transfers:
                continue
            ctx, ts = ctxs[c.id], transfers[c.id]
            lst = set(listed.get(c.id, set()))
            if any((watched.get(t["to"]) or ("",))[0] == "Crypto.com" for t in ts) and on_crypto_com(c.chain, c.contract):
                lst.add("Crypto.com")
            cid = ES_IDS.get(c.chain)

            def wallet_info(a, c=c, cid=cid, ctx=ctx, ts=ts):
                try:
                    if c.chain == "solana":
                        return sol_wallet(a, c.contract, ctx["pools"] | ROUTERS, now_ts)
                    if cid in ES_FREE:
                        return es_wallet(cid, a, c.contract, ctx["pools"] | ROUTERS, now_ts)
                    url = RPCS[cid][0]
                    nonce = int(_rpc(url, "eth_getTransactionCount", [a, "latest"]), 16)
                    # no free history here: fresh = low nonce; the sweep comes from the fetched transfers
                    return {"fresh": nonce <= FRESH_NONCE, "funder": None, "direct_in": True,
                            "sweeps": [t["to"] for t in ts if t["from"] == a]}
                except Exception:
                    return None
            try:
                new += analyze(session, c, ctx, ts, watched, lst, now, wallet_info, add_watched)
            except Exception as e:
                stats["errors"] += 1
                log.warning("radar analyze %s: %s", c.name, e)
        stats["alerts"] = len(new)
        stats["pruned"] = prune(session, now)
        session.commit()
        if notify(new, {c.id: c.name for c in coins}):
            for a in new:
                a.notified_at = utcnow()
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    print(f"  pre-listing radar: {stats}")
    return stats


def for_coin(session, coin):
    """Detail page: the coin's alerts, newest first ([] = render nothing)."""
    from models import PreListingAlert as P
    try:
        rows = session.query(P).filter_by(coin_id=coin.id).order_by(P.created_at.desc()).all()
    except Exception:
        session.rollback()
        return []
    out = []
    for r in rows:
        d = {"rule": r.rule, "entity_name": r.entity_name, "cex_name": r.cex_name, "via": r.via}
        out.append({**d, **describe(d, coin.name), "amount": r.amount, "pct_supply": r.pct_supply,
                    "tx_url": EXPLORERS.get(r.chain, "") + r.tx_hash if r.tx_hash and r.chain in EXPLORERS else None,
                    "time": (r.tx_time or r.created_at).strftime("%Y-%m-%dT%H:%M:%SZ")})
    return out


def rules_by_coin(session, coin_ids):
    """Active table badge: {coin_id: [rule, ...]} for coins with alerts (one query)."""
    from models import PreListingAlert as P
    try:
        out = {}
        for cid, rule in session.query(P.coin_id, P.rule).filter(P.coin_id.in_(coin_ids)).distinct():
            out.setdefault(cid, []).append(rule)
        return out
    except Exception:
        session.rollback()
        return {}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_pre_listing_radar()
