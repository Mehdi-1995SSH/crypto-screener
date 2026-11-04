"""Self-check for multicall.py encoding and buyback_detector.py's strict rules (no network). Run: python test_buyback.py"""
from datetime import datetime
from types import SimpleNamespace

import buyback_detector as BB
import multicall as M

# ---- Multicall3 aggregate3: calldata layout + a hand-built return (ok / failed / empty)
cd = M.encode([("0x" + "11" * 20, "0x18160ddd"), ("0x" + "22" * 20, "0x70a08231" + "00" * 32)])
assert cd.startswith("0x82ad56cb" + format(32, "064x") + format(2, "064x"))
assert ("11" * 20) in cd and ("22" * 20) in cd and len(cd[10:]) % 64 == 0

w = lambda n: format(n, "064x")
def ret(results):
    """ABI-encode Result[] = (bool success, bytes returnData)[]."""
    tails, heads, off = [], [], 32 * len(results)
    for ok, data in results:
        b = bytes.fromhex(data)
        t = w(ok) + w(64) + w(len(b)) + b.hex().ljust(-(-len(b) // 32) * 64, "0")
        heads.append(w(off)); tails.append(t); off += len(t) // 2
    return "0x" + w(32) + w(len(results)) + "".join(heads) + "".join(tails)
assert M.decode(ret([(1, w(10 ** 24)), (0, w(5)), (1, ""), (1, w(18))])) == [10 ** 24, None, None, 18]

# ---- Proof 1: DefiLlama wording (real methodology texts from the 2026-11-04 probe)
assert BB.passes_defillama(154336, 4362401, "99% of ORE fees are used to buyback and burn ORE and distributed to ORE stakers.")
assert BB.passes_defillama(312, 9399, "DBR streamed to INV stakers and for INV buybacks.")
assert BB.passes_defillama(0, 336.38, "30% of the yield ... used to buy back and burn SPA, accruing value to SPA holders.")
assert not BB.passes_defillama(500, 9000, "No revenue share or token buybacks distributed to QI governance token holders.")
assert not BB.passes_defillama(500, 9000, "Holders revenue is sent to the vlPEAS wallet, of which 5% is burned.")
assert not BB.passes_defillama(0, 0, "99% of ORE fees are used to buyback and burn ORE.")
assert not BB.passes_defillama(10, 10, None)

# ---- Proof 3: 1-hop verdicts
DEAD, POOL, TEAM2, X = BB.B.BURN_WALLETS[0].lower(), "0xpool", "0xteam2", "0xstranger"
assert BB.team_verdict([], {POOL}, {}) == "RETAINED"
assert BB.team_verdict([{"to": DEAD}], {POOL}, {}) == "BURNED"
assert BB.team_verdict([{"to": DEAD}, {"to": POOL}], {POOL}, {}) is None              # sold part to the pool
assert BB.team_verdict([{"to": BB.POOL}], set(), {}) is None                           # Solana sale inside a swap
assert BB.team_verdict([{"to": X}], {POOL}, {X: "POOL"}) is None                       # 1-hop dump
assert BB.team_verdict([{"to": X}], {POOL}, {X: "HOLD"}) is None                       # unknown wallet = unverified
assert BB.team_verdict([{"to": X}], {POOL}, {X: "BURN"}) == "BURNED"                   # burned 1 hop later
assert BB.team_verdict([{"to": TEAM2}], {POOL}, {TEAM2: "TEAM"}) == "RETAINED"
assert BB.hop_kind([{"to": POOL}], X, {TEAM2}, {POOL}) == "POOL"
assert BB.hop_kind([{"to": POOL}], TEAM2, {TEAM2}, {POOL}) == "POOL"                   # a team wallet dumping is still a dump
assert BB.hop_kind([{"to": DEAD}], X, set()) == "BURN"
assert BB.hop_kind([], TEAM2, {TEAM2}) == "TEAM" and BB.hop_kind([], X, {TEAM2}) == "HOLD"

# ---- Solana: only swaps the team wallet SIGNED are buys (GOLD: fee crumbs in others' swaps)
MINT, TW = "MintAAA", "TeamWallet"
tx = lambda sig, ts, payer, typ, *tt: {"signature": sig, "timestamp": ts, "feePayer": payer, "type": typ, "tokenTransfers": list(tt)}
tr = lambda frm, to, amt, acc="acc", mint=MINT: {"mint": mint, "fromUserAccount": frm, "toUserAccount": to, "tokenAmount": amt, "toTokenAccount": acc}
buys, outs = BB.sol_moves(MINT, TW, [tx("a", 100, "someone", "SWAP", tr("pool", TW, 0.01))], 0)
assert buys == [] and outs == []
buys, outs = BB.sol_moves(MINT, TW, [tx("b", 100, TW, "SWAP", tr("pool", TW, 500), tr(TW, "", 500, acc="")),
                                     tx("c", 200, TW, "SWAP", tr("pool", TW, 50)),
                                     tx("d", 300, TW, "SWAP", tr(TW, "pool", 20)),
                                     tx("e", 400, TW, "TRANSFER", tr(TW, "Friend", 5)),
                                     tx("f", 50, TW, "SWAP", tr("pool", TW, 9))], 60)  # f is before `since`
assert [(b["tx"], b["burned"]) for b in buys] == [("b", True), ("c", False)]
assert [o["to"] for o in outs] == [BB.POOL, "Friend"]

# ---- volume + storage round trip
v = BB._volume("POOL_BURN", [{"value": 10 ** 18, "ts": 1000, "tx": "0x1"}, {"value": 2 * 10 ** 18, "ts": 90000, "tx": "0x2"}],
               10 ** 18, 2.0, "n", now=100000)
assert (v["usd_24h"], v["usd_30d"], v["last_tx"], v["last_at"]) == (4.0, 6.0, "0x2", 90000)
coin = SimpleNamespace(buyback_verified=None)
assert BB.for_coin(coin) is None
BB.store(coin, {"has_verified_buyback": True, "proofs": ["DEFILLAMA"], "note": "buyback", "usd_24h": 1.0,
                "usd_30d": 30.0, "last_tx": None, "last_at": 1_800_000_000}, datetime(2027, 1, 1))
d = BB.for_coin(coin)
assert d["has_verified_buyback"] and d["proofs"] == ["DEFILLAMA"] and d["last_at"] == "2027-01-15T08:00:00Z"
BB.store(coin, {"has_verified_buyback": False, "proofs": [], "note": None, "usd_24h": None, "usd_30d": None,
                "last_tx": None, "last_at": None}, datetime(2027, 1, 1))
assert BB.for_coin(coin) == {"has_verified_buyback": False, "proofs": [], "note": None, "usd_24h": None,
                             "usd_30d": None, "last_tx": None, "last_at": None}
print("buyback self-check: ok")
