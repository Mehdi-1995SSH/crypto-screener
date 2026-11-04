"""Self-check for the pre-listing / market-maker radar (pre_listing_radar.py) and its
seeder (seed_watched_entities.py): label filtering, the 3 rules, direct-transfer
filtering, dedupe, the 30-day prune (watched_entities never), snapshots, the
detail / badge payloads and Telegram only on new alerts.
In-memory SQLite, no network. Run: python test_pre_listing_radar.py"""
from datetime import date, datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database
import models as M
import pre_listing_radar as R
import seed_watched_entities as SEED

eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
database.Base.metadata.create_all(eng)
database.SessionLocal = sessionmaker(bind=eng)

# 1. seeder: keep hot / numbered wallets of the 25 entities, drop deposit addresses, hacks, routers, others
assert SEED.match("Binance: Hot Wallet 20") == ("Binance", "CEX")
assert SEED.match("Wintermute: Binance Deposit") == ("Wintermute", "MARKET_MAKER")
assert SEED.match("Gate.io 3") == ("Gate.io", "CEX") and SEED.match("Huobi 12") == ("HTX", "CEX")
for bad in ("Binance Dep: 0xabc", "Kucoin Hacker", "OKX DEX: Aggregation Router", "Binance US 2",
            "Bitget Wallet: Swap Router v2", "MEXC: Old Token", "Bybit Exploiter 3", "Coinbase Prime 7", "Uniswap V3", "", None):
    assert SEED.match(bad) is None, bad
csv_text = ("address,chainId,label,nameTag\n"
            "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,1,binance,Binance 14\n"
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,43114,binance,Binance 14\n"      # same EOA on another chain
            "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb,1,bilaxy,Binance Dep: 0x1234\n"
            "0xshort,1,okx,OKX 3\n")
assert SEED.rows_from_csv(csv_text) == {"0x" + "a" * 40: ("evm", "Binance", "CEX", "Binance 14")}
assert len(SEED.SOLANA) == len({a for a, _, _ in SEED.SOLANA}) == 22

# 2. classify: direct transfers only; listed CEX ignored; MM -> CEX names both sides
CEX, MM, MM2, TEAM, POOL, USER = "0x" + "c" * 40, "0x" + "e" * 40, "0x" + "f" * 40, "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40
W = {CEX: ("Gate.io", "CEX"), MM: ("Wintermute", "MARKET_MAKER"), MM2: ("GSR", "MARKET_MAKER")}
tr = lambda f, t, amount=10.0, ts=1, tx="0xt": {"from": f, "to": t, "amount": amount, "ts": ts, "tx": tx}
assert R.classify(tr(TEAM, CEX), W, {TEAM}, {POOL}, set()) == [("CEX_DEPOSIT", "Gate.io", "Gate.io", "direct")]
assert R.classify(tr(TEAM, CEX), W, {TEAM}, {POOL}, {"Gate.io"}) == []              # already listed there
assert R.classify(tr(USER, CEX), W, {TEAM}, {POOL}, set()) == []                     # a random user's deposit
assert R.classify(tr(MM, CEX), W, set(), {POOL}, set()) == [("MM_TO_CEX", "Wintermute", "Gate.io", "direct")]
assert R.classify(tr(TEAM, MM), W, {TEAM}, {POOL}, set()) == [("MM_ONBOARDING", "Wintermute", "", "direct")]
assert R.classify(tr(POOL, MM), W, set(), {POOL}, set()) == []                       # a DEX buy is not onboarding
assert R.classify(tr(MM, MM2), W, set(), set(), set()) == []                         # MM to MM: not onboarding
assert R.classify(tr(CEX, MM), W, {TEAM}, {POOL}, set()) == []                       # exchange withdrawal (Gate -> Jump)
assert R.classify(tr(USER, MM), W, {TEAM}, {POOL}, set()) == []                      # unlabeled exchange (Robinhood -> Wintermute)
assert R.classify(tr("0x" + "7a250d5630b4cf539739df2c5dacb4c659f2488d", MM), W, set(), R.ROUTERS, set()) == []

# 3. deposit_sweep: insider -> unlabeled (>= 0.1% supply) -> CEX later in the window
DEP = "0x" + "4" * 40
ts = [tr(TEAM, DEP, amount=2000, ts=10, tx="0xa"), tr(DEP, CEX, amount=2000, ts=20, tx="0xb")]
hits = R.deposit_sweeps(ts, W, {TEAM}, {POOL}, set(), 1_000_000)
assert [(h[0], h[1]["tx"], h[2]["tx"]) for h in hits] == [(("CEX_DEPOSIT", "Gate.io", "Gate.io", "deposit_sweep"), "0xa", "0xb")]
assert R.deposit_sweeps(ts, W, {TEAM}, {POOL}, set(), 10_000_000) == []             # 0.02% of supply: too small
assert R.deposit_sweeps(ts, W, {TEAM}, {POOL}, {"Gate.io"}, 1_000_000) == []        # listed there
assert R.deposit_sweeps(list(reversed([tr(TEAM, DEP, 2000, ts=30), tr(DEP, CEX, 2000, ts=20)])), W, {TEAM}, set(), set(), 1e6) == []

# 4. rule 3: fresh entrants (none on the first run) and the 1-hop match
assert R.fresh_entrants(["a", "b", "c"], None, set()) == []
assert R.fresh_entrants(["a", "b", "c"], ["a"], {"c"}) == ["b"]
info = lambda **kw: {"fresh": True, "direct_in": True, "funder": None, "sweeps": [], **kw}
assert R.one_hop(info(sweeps=[USER, CEX]), W, set()) == ("Gate.io", "CEX", "CEX_DEPOSIT", "Gate.io", "1_hop_sweep")
assert R.one_hop(info(sweeps=[CEX]), W, {"Gate.io"}) is None
assert R.one_hop(info(sweeps=[MM]), W, set()) == ("Wintermute", "MARKET_MAKER", "MM_ONBOARDING", "", "1_hop_sweep")
assert R.one_hop(info(funder=MM), W, set()) == ("Wintermute", "MARKET_MAKER", "MM_ONBOARDING", "", "1_hop_gas")
assert R.one_hop(info(funder=CEX), W, set()) is None                                 # CEX gas = a withdrawal
assert R.one_hop(info(funder=MM, fresh=False), W, set()) is None
assert R.one_hop(info(funder=MM, direct_in=False), W, set()) is None and R.one_hop(None, W, set()) is None

# 5. texts: English + Arabic, both names on MM -> CEX
d = R.describe({"rule": "MM_TO_CEX", "entity_name": "Wintermute", "cex_name": "Gate.io"}, "XYZ")
assert "Wintermute" in d["en"] and "Gate.io" in d["en"] and "صانع السوق Wintermute" in d["ar"] and "Gate.io" in d["ar"]
d = R.describe({"rule": "CEX_DEPOSIT", "entity_name": "Binance", "cex_name": "Binance", "via": "deposit_sweep"}, "XYZ")
assert "Binance" in d["en"] and "عنوان إيداع وسيط" in d["ar"] and "غير مدرجة" in d["ar"]

# 6. analyze + save: dedupe per (coin, rule, entity, cex), 1-hop wallet stored, prune keeps watched_entities
NOW = datetime(2026, 11, 3, 12, 0)
FRESH = "0x" + "5" * 40
with database.SessionLocal() as s:
    coin = M.Coin(contract="0x" + "9" * 40, chain="ethereum", name="XYZ", status="active", total_supply=1_000_000)
    s.add(coin)
    s.add(M.WatchedEntity(address=CEX, chain="evm", entity_name="Gate.io", entity_type="CEX", discovered_via="OPEN_LABELS",
                          created_at=NOW - timedelta(days=400)))
    s.commit()
    added = []
    ctx = {"top": [TEAM, FRESH, MM2], "pools": {POOL}, "insiders": {TEAM}, "yesterday": [TEAM]}
    transfers = [tr(TEAM, CEX, ts=1_793_700_000, tx="0x1"), tr(TEAM, CEX, ts=1_793_700_100, tx="0x2"), tr(MM, CEX, tx="0x3")]
    rows = R.analyze(s, coin, ctx, transfers, W, set(), NOW,
                     wallet_info=lambda a: info(funder=MM) if a == FRESH else None,
                     add_watched=lambda a, e, k: added.append((a, e, k)))
    got = sorted((r.rule, r.entity_name, r.cex_name, r.via) for r in rows)
    assert got == [("CEX_DEPOSIT", "Gate.io", "Gate.io", "direct"), ("MM_ONBOARDING", "GSR", "", "top_holder"),
                   ("MM_ONBOARDING", "Wintermute", "", "1_hop_gas"), ("MM_TO_CEX", "Wintermute", "Gate.io", "direct")], got
    assert added == [(FRESH, "Wintermute", "MARKET_MAKER")]
    dep = next(r for r in rows if r.rule == "CEX_DEPOSIT")
    assert dep.tx_hash == "0x1" and dep.pct_supply == 0.001 and dep.tx_time == datetime(2026, 11, 3, 10, 0)
    s.commit()
    again = R.analyze(s, coin, ctx, transfers, W, set(), NOW, lambda a: None, lambda *a: None)
    assert again == []                                                              # already stored -> no new alert
    s.add(M.PreListingAlert(coin_id=coin.id, rule="CEX_DEPOSIT", entity_name="OKX", cex_name="OKX",
                            created_at=NOW - timedelta(days=31)))
    s.add(M.TopHolderSnapshot(coin_id=coin.id, day=date(2026, 10, 1), holders="[]"))
    s.commit()
    assert R.prune(s, NOW) == 2
    s.commit()
    assert s.query(M.WatchedEntity).count() == 1                                    # never pruned
    assert s.query(M.PreListingAlert).count() == 4

    # 7. snapshots: the previous day's list, None on the first one
    assert R.snapshot(s, 777, ["a", "b"], date(2026, 11, 2)) is None
    s.commit()
    assert R.snapshot(s, 777, ["b", "c"], date(2026, 11, 3)) == ["a", "b"]

    # 8. payloads: [] / no badge for a coin without alerts; EN + AR + explorer link otherwise
    other = M.Coin(contract="0x" + "8" * 40, chain="bsc", name="Quiet", status="active")
    s.add(other)
    s.commit()
    assert R.for_coin(s, other) == [] and R.rules_by_coin(s, [other.id]) == {}
    pl = R.for_coin(s, coin)
    assert len(pl) == 4 and all(p["en"] and p["ar"] for p in pl)
    assert next(p for p in pl if p["rule"] == "CEX_DEPOSIT")["tx_url"] == "https://etherscan.io/tx/0x1"
    assert sorted(R.rules_by_coin(s, [coin.id, other.id])[coin.id]) == ["CEX_DEPOSIT", "MM_ONBOARDING", "MM_TO_CEX"]

    # 9. listings: exchange_listings slugs -> CEX names (delisted ignored)
    s.add_all([M.ExchangeListing(coin_id=coin.id, exchange="gate", first_seen=date(2026, 1, 1), last_seen=date(2026, 11, 3)),
               M.ExchangeListing(coin_id=coin.id, exchange="mxc", first_seen=date(2026, 1, 1), last_seen=date(2026, 5, 1),
                                 delisted_on=date(2026, 5, 2)),
               M.ExchangeListing(coin_id=coin.id, exchange="upbit", first_seen=date(2026, 1, 1), last_seen=date(2026, 11, 3))])
    s.commit()
    assert R.listed_on(s, [coin.id]) == {coin.id: {"Gate.io"}}

# 10. Telegram only when there is something new; long lists split under 4000 chars
import daily_report
sent = []
daily_report.send_message = lambda text: sent.append(text) or True
assert R.notify([], {}) is False and sent == []
A = type("A", (), {})
alerts = []
for i in range(60):
    a = A()
    a.coin_id, a.rule, a.entity_name, a.cex_name, a.via = 1, "MM_TO_CEX", "Wintermute", "Gate.io", "direct"
    alerts.append(a)
assert R.notify(alerts, {1: "XYZ"}) is True and len(sent) >= 2 and all(len(m) <= 4000 for m in sent)
assert sent[0].startswith("📡") and "Wintermute" in sent[0]
print("pre-listing radar self-check: ok")
