"""
buyback_detector.py
-------------------
Verified buybacks (Supply card "Buyback" row, under Burned). A coin is marked
ONLY on hard evidence - one of 4 proofs. A whale buying on a DEX never counts.
  1 DEFILLAMA  holders revenue > 0 (24h or 30d) AND methodology.HoldersRevenue
               says buyback - and not "no ... buyback" (BENQI's wording).
  2 POOL_BURN  DEX pool -> burn wallet in a tx SIGNED by a team wallet, or sent
               to the token contract itself (its own auto buyback). Solana: a
               swap + burn in one tx whose fee payer is a team wallet.
  3 TEAM_BUY   pool -> team wallet in a tx the team wallet SIGNED (>= $100),
               then 1 hop forward from the first buy: every outflow went to a
               burn wallet (BURNED), to another team wallet / nowhere
               (RETAINED), or to a wallet that burned it. Any outflow to a
               pool - direct or 1 hop - or to an unknown wallet = reject.
  4 DAO_VOTE   a passed Snapshot BUYBACK proposal for this contract
               (dao_catalyst_alerts, already pruned to 30 days).
Team wallets = creator / owner stored from GoPlus (EVM) / RugCheck (Solana).
DefiLlama /treasuries is paid (402) - not used.
Windows: Etherscan chains 30 days; public-RPC chains only their log window
(BSC ~1 h, Base / Avalanche ~1 day); Solana one Helius page (100 txs).
Daily: run_buyback_update(). Check a Coin: check().
"""
import re
import time
from datetime import datetime, timezone

import burn_detector as B

BUYBACK = re.compile(r"\bbuy[- ]?backs?\b|\bbought back\b", re.I)
NO_BUYBACK = re.compile(r"\bno\b[^.]*\bbuy[- ]?backs?\b", re.I)
WINDOW = 30 * 86400
MIN_USD = 100                       # smaller team buys = fee crumbs, never a buyback
MAX_CHECKS = 20                     # signer lookups per proof / 1-hop wallets per team wallet
BURNS = {w.lower() for w in B.BURN_WALLETS}
POOL = "POOL"                       # Solana: an outflow inside a swap = sold to a pool
ORDER = ["POOL_BURN", "TEAM_BUY_BURNED", "TEAM_BUY_RETAINED", "DEFILLAMA", "DAO_VOTE"]
HELIUS_TXS = "https://api.helius.xyz/v0/addresses/{a}/transactions"
_blk30 = {}


# ---------------------------------------------------------------- pure rules
def passes_defillama(t24, t30, note):
    note = note or ""
    return ((t24 or 0) > 0 or (t30 or 0) > 0) and bool(BUYBACK.search(note)) and not NO_BUYBACK.search(note)


def team_verdict(outs, pools, hop):
    """1-hop rule over a team wallet's outflows after its first signed buy.
    hop = {recipient: "POOL" | "BURN" | "TEAM" | "HOLD"}. -> "BURNED" / "RETAINED" / None (reject)."""
    kinds = set()
    for o in outs:
        to = o["to"]
        if to in BURNS:
            kinds.add("BURNED")
        elif to in pools or to == POOL or hop.get(to) in ("POOL", "HOLD", None):
            return None
        else:
            kinds.add("BURNED" if hop[to] == "BURN" else "RETAINED")
    return "BURNED" if "BURNED" in kinds else "RETAINED"


def hop_kind(outs, wallet, team, pools=()):
    """What a 1-hop recipient did with the tokens (its outflows after the buy)."""
    if any(o["to"] in pools or o["to"] == POOL for o in outs):
        return "POOL"
    if outs and all(o["to"] in BURNS for o in outs):
        return "BURN"
    return "TEAM" if wallet in team else "HOLD"


def _volume(proof, hits, scale, price, note, now=None):
    now = now or time.time()
    usd = lambda xs: round(sum(h["value"] for h in xs) / scale * price, 2) if price else None
    last = hits[-1]
    return {"proof": proof, "note": note, "usd_24h": usd([h for h in hits if h["ts"] >= now - 86400]),
            "usd_30d": usd(hits), "last_tx": last["tx"], "last_at": last["ts"]}


# ---------------------------------------------------------------- proofs 1 + 4
def proof_defillama(contract):
    from revenue_detail import resolve_slugs, _get, FEES_URL
    for slug in resolve_slugs(contract):
        d, ok = _get(FEES_URL.format(slug=slug), {"dataType": "dailyHoldersRevenue"})
        if not ok:
            raise B.BurnError("DefiLlama unreachable")
        m = (d or {}).get("methodology")
        note = m.get("HoldersRevenue") if isinstance(m, dict) else None
        if d and passes_defillama(d.get("total24h"), d.get("total30d"), note):
            return {"proof": "DEFILLAMA", "note": note, "usd_24h": d.get("total24h"), "usd_30d": d.get("total30d")}
    return None


def proof_dao(session, contract):
    from sqlalchemy import func
    from models import DaoCatalystAlert
    a = (session.query(DaoCatalystAlert).filter(DaoCatalystAlert.tag == "BUYBACK",
                                                func.lower(DaoCatalystAlert.contract) == contract.lower())
         .order_by(DaoCatalystAlert.closed_at.desc()).first())
    return a and {"proof": "DAO_VOTE", "note": a.title, "last_at": a.closed_at.replace(tzinfo=timezone.utc).timestamp()}


# ---------------------------------------------------------------- EVM
def _es_logs(cid, token, side, addr):
    if cid not in _blk30:
        _blk30[cid] = int(B._es_get(cid, module="block", action="getblocknobytime",
                                     timestamp=int(time.time()) - WINDOW, closest="after"))
    try:
        return B._es_get(cid, module="logs", action="getLogs", address=token, fromBlock=_blk30[cid], toBlock="latest",
                         topic0=B.TRANSFER_TOPIC, **{side: B._pad(addr), f"topic0_{side[-1]}_opr": "and"}) or []
    except B.BurnError as e:
        if "No records" in str(e):
            return []
        raise


def transfers(cid, token, frm=None, to=None):
    """Transfers of `token` from any of `frm` / to any of `to` (lowercase sets, None = any), oldest first."""
    if cid in B.ES_FREE:                        # one call per address on the given side, other side filtered here
        side, keys = ("topic1", frm) if frm is not None and (to is None or len(frm) <= len(to)) else ("topic2", to)
        logs = [l for k in keys for l in _es_logs(cid, token, side, k)]
    else:
        url, span = B.RPCS[cid]
        head = B._hex(B._rpc_post(url, [{"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}]).get(1))
        logs = B._rpc_post(url, [{"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": [{
            "address": token, "fromBlock": hex(max(0, head - span)), "toBlock": hex(head),
            "topics": [B.TRANSFER_TOPIC, [B._pad(a) for a in frm] if frm else None,
                       [B._pad(a) for a in to] if to else None]}]}]).get(1, {}).get("result") or []
    out = []
    for l in logs:
        t = l.get("topics") or []
        if len(t) != 3:
            continue
        f, r = "0x" + t[1][-40:], "0x" + t[2][-40:]
        if (frm is None or f in frm) and (to is None or r in to):
            ts = l.get("timeStamp") or l.get("blockTimestamp")
            # shortcut: RPC logs without a timestamp count as "now" - those windows are <= 1 day anyway
            out.append({"from": f, "to": r, "value": int(l["data"], 16), "tx": l["transactionHash"],
                        "ts": int(ts, 16) if ts else int(time.time()), "block": int(l["blockNumber"], 16)})
    return sorted(out, key=lambda x: x["block"])


def _signer(cid, tx):
    import multicall
    r = B._rpc_post(multicall.rpc_url(cid), [{"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionByHash",
                                              "params": [tx]}]).get(1, {}).get("result") or {}
    return (r.get("from") or "").lower(), (r.get("to") or "").lower()


def proof_pool_burn_evm(cid, token, pools, team, scale, price):
    ok = []
    for h in transfers(cid, token, frm=pools, to=BURNS)[-MAX_CHECKS:]:
        frm, to = _signer(cid, h["tx"])
        if frm in team or to == token.lower():
            ok.append(h)
    return ok and _volume("POOL_BURN", ok, scale, price, "Bought on the DEX and sent straight to a burn wallet")


def proof_team_buy_evm(cid, token, pools, team, scale, price):
    best = None
    for w in team:
        buys = [b for b in transfers(cid, token, frm=pools, to={w})[-MAX_CHECKS:] if _signer(cid, b["tx"])[0] == w]
        if not buys or (price and sum(b["value"] for b in buys) / scale * price < MIN_USD):
            continue
        start = buys[0]["block"]
        outs = [o for o in transfers(cid, token, frm={w}) if o["block"] >= start]
        targets = {o["to"] for o in outs} - BURNS - pools
        if len(targets) > MAX_CHECKS:
            return None
        hop = {x: hop_kind([o for o in transfers(cid, token, frm={x}) if o["block"] >= start], x, team, pools)
               for x in targets}
        v = team_verdict(outs, pools, hop)
        if v is None:
            return None                         # one team wallet dumping rejects the coin's team-buy proof
        best = _volume("TEAM_BUY_" + v, buys, scale, price,
                       "Tokens burned after buyback" if v == "BURNED" else "Tokens retained in team treasury")
    return best


# ---------------------------------------------------------------- Solana (Helius)
def sol_moves(mint, wallet, txs, since):
    """Pure: the wallet's own buys of `mint` (SWAP, fee payer = wallet) and its outflows, oldest first.
    -> (buys [{tx, ts, value, burned}], outs [{to, ts}]). A sale inside a swap goes to POOL."""
    buys, outs = [], []
    for t in sorted(txs, key=lambda t: t.get("timestamp", 0)):
        if t.get("transactionError") or t.get("timestamp", 0) < since:
            continue
        tt = [x for x in t.get("tokenTransfers") or [] if x.get("mint") == mint]
        got = sum(x.get("tokenAmount") or 0 for x in tt
                  if x.get("toUserAccount") == wallet and x.get("fromUserAccount") != wallet)
        burned = lambda x: not x.get("toTokenAccount") or x.get("toUserAccount") == B.INCINERATOR
        if t.get("type") == "SWAP" and t.get("feePayer") == wallet and got > 0:
            buys.append({"tx": t["signature"], "ts": t["timestamp"], "value": got,
                         "burned": any(x.get("fromUserAccount") == wallet and burned(x) for x in tt)})
            continue
        for x in tt:
            if x.get("fromUserAccount") == wallet:
                outs.append({"ts": t["timestamp"], "to": B.BURN_WALLETS[0].lower() if burned(x)
                             else POOL if t.get("type") == "SWAP" else x.get("toUserAccount")})
    return buys, outs


def _sol_txs(addr):
    return B._helius("GET", HELIUS_TXS.format(a=addr), params={"limit": 100}) or []


def proof_solana(mint, team, price):
    since, found = time.time() - WINDOW, []
    for w in team:
        buys, outs = sol_moves(mint, w, _sol_txs(w), since)
        if not buys or (price and sum(b["value"] for b in buys) * price < MIN_USD):
            continue
        burned_in_tx = [b for b in buys if b["burned"]]
        if burned_in_tx:
            found.append(_volume("POOL_BURN", burned_in_tx, 1, price, "Bought on the DEX and burned in the same transaction"))
            continue
        start = buys[0]["ts"]
        outs = [o for o in outs if o["ts"] >= start]
        targets = {o["to"] for o in outs} - BURNS - {POOL}
        if len(targets) > MAX_CHECKS:
            return None
        hop = {x: hop_kind(sol_moves(mint, x, _sol_txs(x), start)[1], x, team) for x in targets}
        v = team_verdict(outs, set(), hop)
        if v is None:
            return None
        found.append(_volume("TEAM_BUY_" + v, buys, 1, price,
                             "Tokens burned after buyback" if v == "BURNED" else "Tokens retained in team treasury"))
    return found and min(found, key=lambda f: ORDER.index(f["proof"]))


# ---------------------------------------------------------------- run all 4
def evaluate(session, contract, chain, pairs, team, price, decimals=None):
    """-> {"has_verified_buyback", "proofs", "note", "usd_24h", "usd_30d", "last_tx", "last_at"}.
    A proof that errors is skipped (never a false YES); raises only when every proof errored."""
    sol, cid = (chain or "").lower() == "solana", B.chain_id(chain)
    pools = {p["pairAddress"].lower() for p in pairs or []
             if p.get("chainId") == chain and re.fullmatch(r"0x[0-9a-fA-F]{40}", p.get("pairAddress") or "")}
    team = {a for a in team if a} if sol else {a.lower() for a in team if a} - BURNS - pools
    checks = [lambda: proof_defillama(contract), lambda: proof_dao(session, contract)]
    if sol and team:
        checks.append(lambda: proof_solana(contract, team, price))
    elif cid and pools:
        def scale():
            if decimals is not None:
                return 10 ** decimals
            import multicall
            return 10 ** multicall.read_tokens(cid, [contract])[contract.lower()]["decimals"]
        checks += [lambda: proof_pool_burn_evm(cid, contract, pools, team, scale(), price),
                   lambda: team and proof_team_buy_evm(cid, contract, pools, team, scale(), price)]
    found, errors = [], 0
    for f in checks:
        try:
            r = f()
            if r:
                found.append(r)
        except Exception:
            errors += 1
    if not found and errors == len(checks):
        raise B.BurnError("every buyback check failed")
    found.sort(key=lambda f: ORDER.index(f["proof"]))
    top = found[0] if found else {}
    tx = next((f for f in found if f.get("last_tx")), {})
    return {"has_verified_buyback": bool(found), "proofs": [f["proof"] for f in found], "note": top.get("note"),
            "usd_24h": top.get("usd_24h"), "usd_30d": top.get("usd_30d"),
            "last_tx": tx.get("last_tx"), "last_at": tx.get("last_at") or top.get("last_at")}


# ---------------------------------------------------------------- storage / display
def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def store(coin, r, now):
    coin.buyback_verified, coin.buyback_proofs = r["has_verified_buyback"], ",".join(r["proofs"]) or None
    coin.buyback_note, coin.buyback_usd_24h, coin.buyback_usd_30d = r["note"], r["usd_24h"], r["usd_30d"]
    coin.buyback_last_tx = r["last_tx"]
    coin.buyback_last_at = datetime.fromtimestamp(r["last_at"], timezone.utc).replace(tzinfo=None) if r["last_at"] else None
    coin.buyback_checked_at = now


def for_coin(coin):
    if coin.buyback_verified is None:
        return None
    return {"has_verified_buyback": coin.buyback_verified,
            "proofs": coin.buyback_proofs.split(",") if coin.buyback_proofs else [],
            "note": coin.buyback_note, "usd_24h": coin.buyback_usd_24h, "usd_30d": coin.buyback_usd_30d,
            "last_tx": coin.buyback_last_tx,
            "last_at": coin.buyback_last_at.strftime("%Y-%m-%dT%H:%M:%SZ") if coin.buyback_last_at else None}


def check(contract, chain, pairs, team, price):
    """Check a Coin: live result for an untracked EVM / Solana coin; None when unavailable."""
    from database import SessionLocal
    if not B.supported(chain):
        return None
    s = SessionLocal()
    try:
        r = evaluate(s, contract, chain, pairs, team, price)
        return {**r, "last_at": _iso(r["last_at"])}
    except Exception:
        return None
    finally:
        s.close()


def run_buyback_update(session=None):
    """Daily job / backfill: the 4 proofs for every active EVM / Solana coin."""
    from database import SessionLocal, utcnow
    from models import Coin
    from fetch_dexscreener import fetch_pairs_for_addresses
    own, session = session is None, session or SessionLocal()
    stats = {"checked": 0, "verified": 0, "errors": 0}
    try:
        coins = [c for c in session.query(Coin).filter(Coin.status == "active").all() if B.supported(c.chain)]
        try:
            pairs = fetch_pairs_for_addresses([c.contract for c in coins])
        except Exception:
            pairs = []                                  # no pools -> proofs 2/3 skipped, 1/4 still run
        by_token = {}
        for p in pairs:
            by_token.setdefault(((p.get("baseToken") or {}).get("address") or "").lower(), []).append(p)
        pre = B.prefetch(coins)
        now = utcnow()
        for c in coins:
            stats["checked"] += 1
            try:
                r = evaluate(session, c.contract, c.chain, by_token.get(c.contract.lower()),
                             (c.creator_address, c.owner_address), c.live_price or c.price_usd,
                             (pre.get(c.contract.lower()) or {}).get("decimals"))
            except Exception as e:
                stats["errors"] += 1
                print(f"  buyback check failed for {c.name} ({c.chain}): {str(e)[:80]}")
                continue
            store(c, r, now)
            stats["verified"] += r["has_verified_buyback"]
        session.commit()
    finally:
        if own:
            session.close()
    print(f"  buybacks: {stats['verified']} verified of {stats['checked']} checked, {stats['errors']} errors")
    return stats


if __name__ == "__main__":
    # One-time backfill: python buyback_detector.py
    run_buyback_update()
