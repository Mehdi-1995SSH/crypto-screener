"""Self-check for the Coin Decision scanner (snapshot_scanner.py): steps A-D, the
token resolution order, the 6-hour gate, dedupe, the 30-day prune and latest().
In-memory SQLite, Snapshot / DexScreener / CoinGecko mocked. Run: python test_snapshot_scanner.py"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database
import models as M
import snapshot_scanner as S

eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
database.Base.metadata.create_all(eng)
database.SessionLocal = sessionmaker(bind=eng)

# 1. Step A: tags, word endings, word boundaries, code-title body fallback, no bare v2 / mainnet
p = lambda title, body="": {"title": title, "body": body}
assert S.tag(p("Burn the 10M BEETS")) == ("BURN", "burn")
assert S.tag(p("Weekly buybacks resume")) == ("BUYBACK", "buyback")
assert S.tag(p("Tokens burned last epoch")) == ("BURN", "burn")
assert S.tag(p("New Staking APY Formula")) == ("TOKENOMICS", "staking")
assert S.tag(p("Founder burnout")) is None and S.tag(p("Rebalance")) is None
assert S.tag(p("Morpho Vault v2 - raise cap")) is None and S.tag(p("Add gauge (Ethereum mainnet)")) is None
assert S.tag(p("Mainnet launch date")) == ("UPGRADE", "mainnet launch")
assert S.tag(p("AIP-12", "This enables the fee switch for ...")) == ("BUYBACK", "fee switch")
assert S.tag(p("Grant for the team", "burn")) is None                 # body only read for code titles

# 2. Step B: quorum, >= 50%, affirmative winner, malformed scores
v = lambda ch, sc, q=0: {"choices": ch, "scores": sc, "scores_total": sum(sc), "quorum": q}
assert S.passed(v(["For", "Against"], [94.2, 5.8])) == ("For", 94.2)
assert S.passed(v(["For", "Against"], [20, 80])) is None
assert S.passed(v(["Burn 50%", "Burn 25%", "Abstain"], [55, 30, 15])) == ("Burn 50%", 55.0)
assert S.passed(v(["Abstain", "Yes"], [60, 40])) is None
assert S.passed(v(["For", "Against"], [49, 1], q=100)) is None        # quorum not met
assert S.passed(v(["A", "B", "C"], [40, 35, 25])) is None              # no option >= 50%
assert S.passed({"choices": ["For"], "scores": [1, 2], "scores_total": 3, "quorum": 0}) is None
assert S.passed(v(["For", "Against"], [0, 0])) is None

# 3. Step C
sp = lambda **kw: {"admins": ["0xAA"], "moderators": [], "members": [], "filters": {"onlyMembers": False}, **kw}
assert S.team_author({"author": "0xaa", "space": sp()})
assert not S.team_author({"author": "0xbb", "space": sp()})
assert S.team_author({"author": "0xbb", "space": sp(filters={"onlyMembers": True})})

# 4. resolve(): home-chain strategy contract (CoinGecko-confirmed) > chain-pinned search > other-chain strategy
A1, A2, A3 = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40
calls = []
def fake_cg(chain, addrs):
    calls.append((chain, tuple(addrs)))
    return {a.lower(): m for a, m in CG.items() if a.lower() in {x.lower() for x in addrs} and m}
def fake_dex(chain, sym):
    calls.append(("dex", chain, sym))
    return DEX.get((chain, sym))
S.cg_mcaps, S.dex_search = fake_cg, fake_dex
space = {"id": "x.eth", "network": "1", "symbol": "veXYZ", "strategies": [
    {"network": "137", "params": {"address": A3}},                        # other chain
    {"network": "1", "params": {"address": A1}},                          # wrapper, unknown to CoinGecko
    {"network": "1", "params": {"address": A2}},                          # the real token
    {"network": "1", "params": {"address": "not-an-address"}}, {"network": "1", "params": {}}]}
assert [S._symbol({"symbol": s}) for s in ("veCRV", "vlMGP", "stWCT", "sdBAL", "$NGP", "VELO")] == ["CRV", "MGP", "WCT", "BAL", "NGP", "VELO"]
assert S.strategy_contracts(space) == [("ethereum", A1), ("ethereum", A2), ("polygon", A3)]
CG, DEX = {A2: 9e6, A3: 9e6}, {}
assert S.resolve(space) == {"chain": "ethereum", "contract": A2, "market_cap": 9e6, "mcap_source": "coingecko"}
assert not any(c[0] == "dex" for c in calls)                             # no search needed
CG, DEX = {A3: 9e6, A1.replace("1", "4"): 0}, {("ethereum", "XYZ"): {"baseToken": {"address": A1.replace("1", "5")}, "marketCap": 7e6}}
r = S.resolve(space)                                                     # no home-chain strategy hit -> search (symbol veXYZ -> XYZ)
assert r == {"chain": "ethereum", "contract": A1.replace("1", "5"), "market_cap": 7e6, "mcap_source": "dexscreener"}, r
CG, DEX = {A3: 9e6}, {}
assert S.resolve(space)["contract"] == A3                                 # last resort: other-chain strategy
CG, DEX = {}, {}
assert S.resolve(space) == {} and S.resolve({"id": "y", "network": "solana", "symbol": "Y"}) == {}

# 5. Step D: verified passes without a market cap; unverified needs a CoinGecko mcap >= $5M
assert S.quality_ok({"verified": True}, {})
assert S.quality_ok({"verified": False}, {"market_cap": 6e6, "mcap_source": "coingecko"})
assert not S.quality_ok({"verified": False}, {"market_cap": 6e6, "mcap_source": "dexscreener"})
assert not S.quality_ok({"verified": False}, {"market_cap": 4e6, "mcap_source": "coingecko"})

# 6. scan(): gate, end_gte window, dedupe, followers gate before any lookup, prune, gate unmoved on failure
NOW = datetime(2026, 11, 3, 12, 0)
ts = lambda dt: int(dt.replace(tzinfo=timezone.utc).timestamp())
def prop(pid, space_id, verified, followers, title="Burn 5% of supply", end=NOW - timedelta(hours=5)):
    return {"id": pid, "title": title, "body": "", "choices": ["For", "Against"], "scores": [90, 10], "scores_total": 100,
            "quorum": 0, "author": "0xaa", "end": ts(end),
            "space": {"id": space_id, "name": space_id, "symbol": "TKN", "network": "1", "verified": verified,
                      "followersCount": followers, "admins": ["0xAA"], "moderators": [], "members": [],
                      "filters": {"onlyMembers": False}, "strategies": []}}
PROPS, seen_gte, resolved = [], [], []
def fake_fetch(end_gte):
    seen_gte.append(end_gte)
    if PROPS is None:
        raise RuntimeError("snapshot down")
    return PROPS
def fake_resolve(space):
    resolved.append(space["id"])
    return {"chain": "ethereum", "contract": A2, "market_cap": 8e6 if space["id"] == "big" else 1e6, "mcap_source": "coingecko"}
S.fetch_closed, S.resolve = fake_fetch, fake_resolve
with database.SessionLocal() as s:
    s.add(M.DaoCatalystAlert(proposal_id="old", space_id="z", tag="BURN", keyword="burn", title="t",
                             closed_at=NOW - timedelta(days=31)))
    s.add(M.Coin(contract=A2.upper().replace("0X", "0x"), chain="ethereum", name="TKN", status="active"))
    s.commit()
PROPS = [prop("p1", "verified", True, 5), prop("p2", "big", False, 2000), prop("p3", "small", False, 2000),
         prop("p4", "nofollow", False, 10), prop("p5", "verified", True, 5, title="Grant program"), prop("p1", "verified", True, 5)]
st = S.scan(now=NOW)
assert st == {"scanned": 6, "kept": 2, "lookups": 3, "pruned": 1}, st
assert seen_gte == [ts(NOW - timedelta(days=30))]                       # first run: 30-day backfill
assert "nofollow" not in resolved                                       # < 1000 followers: no API call
with database.SessionLocal() as s:
    assert sorted(r.proposal_id for r in s.query(M.DaoCatalystAlert)) == ["p1", "p2"]
assert S.scan(now=NOW + timedelta(hours=5)) == {"skipped": True}       # 6-hour gate
st = S.scan(now=NOW + timedelta(hours=5), force=True)                  # force; p1/p2 already stored
assert st["kept"] == 0 and seen_gte[-1] == ts(NOW - timedelta(hours=1))   # since last scan - 1 h overlap
PROPS = None
try:
    S.scan(now=NOW + timedelta(hours=12))
    raise AssertionError("should raise")
except RuntimeError:
    pass
with database.SessionLocal() as s:
    assert s.get(M.DaoScanState, 1).last_scan_at == NOW + timedelta(hours=5)   # gate not moved -> retried

# 7. latest(): newest first, in_screener by contract (case-insensitive)
with database.SessionLocal() as s:
    rows = S.latest(s)
assert [r["proposal_id"] for r in rows] == ["p1", "p2"] or [r["proposal_id"] for r in rows] == ["p2", "p1"]
assert all(r["in_screener"] and r["emoji"] == "🔥" and r["closed_at"].endswith("Z") for r in rows)
print("snapshot scanner self-check: ok")
