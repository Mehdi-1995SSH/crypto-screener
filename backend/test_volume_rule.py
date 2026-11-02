"""Self-check for the daily volume rule (drop_logic.volume_problem) and the
45-day snapshot pruning (tracking.prune_snapshots). In-memory SQLite, no
network. Run: python test_volume_rule.py"""
import json
from datetime import date, datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import database
import models as M
import drop_logic as D
import tracking as T

eng = create_engine("sqlite://")
database.Base.metadata.create_all(eng)
s = sessionmaker(bind=eng)()
TODAY = date(2026, 11, 2)


def coin(name, cg=(), codex=()):
    """A coin with CoinGecko readings (oldest first, ending today) and stored
    Codex daily bars (complete days, ending yesterday)."""
    c = M.Coin(contract=f"0x{name}", chain="ethereum", name=name, status="active")
    s.add(c)
    s.flush()
    for i, v in enumerate(cg):
        s.add(M.DailySnapshot(coin_id=c.id, date=TODAY - timedelta(days=len(cg) - 1 - i), total_volume=v))
    if codex:
        bars = [[(TODAY - timedelta(days=len(codex) - i)).isoformat(), 1.0, v, None, None] for i, v in enumerate(codex)]
        s.add(M.EntryZoneBars(chain="ethereum", contract=c.contract.lower(), data=json.dumps({"bars": bars})))
    s.commit()
    return c


check = lambda c: D.volume_problem(s, c, TODAY)

assert check(coin("flat", cg=[5000] * 10)) is None                               # steady, above the floor
assert check(coin("tiny", cg=[1500] * 10)).startswith("Volume too low (5-day average $1,500/day")   # zombie, no exemption
assert check(coin("tinyflat", cg=[50] * 10)).startswith("Volume too low")         # flat zombie (never "declines")
r = check(coin("falling", cg=[10_000] * 5 + [6_000] * 5))                        # -40%
assert r == "Volume falling (5-day average -40% vs the previous 5 days)"
assert check(coin("edge", cg=[10_000] * 5 + [6_500] * 5)) is not None            # exactly -35% -> falling
assert check(coin("mild", cg=[10_000] * 5 + [7_000] * 5)) is None                # -30% -> healthy
assert check(coin("rising", cg=[3_000] * 5 + [9_000] * 5)) is None
# new coin: 3 CoinGecko readings -> its Codex daily bars decide
assert check(coin("new_ok", cg=[9_000] * 3, codex=[8_000] * 10)) is None
assert check(coin("new_dead", cg=[9_000] * 3, codex=[300] * 10)).startswith("Volume too low")
assert check(coin("new_fall", cg=[9_000] * 3, codex=[10_000] * 5 + [5_000] * 5)).startswith("Volume falling")
# floor needs only 5 readings: CoinGecko (6 readings) decides, decline then falls back to Codex
assert check(coin("six_low", cg=[100] * 6, codex=[50_000] * 10)).startswith("Volume too low")
assert check(coin("nodata")) is None                                             # nothing -> unknown = healthy
assert check(coin("few", cg=[100] * 3, codex=[100] * 3)) is None

# --- pruning: > 45 days deleted, except each coin's entry-day rows
old = M.Coin(contract="0xold", chain="ethereum", name="old", status="active", price_usd=1.0,
             active_since=datetime(2026, 9, 1, 12))
s.add(old)
s.flush()
HIGH_DAY, LOW_DAY = date(2026, 9, 10), date(2026, 9, 15)                    # both older than 45 days (cutoff Sep 18)
for k in range(70):
    d = TODAY - timedelta(days=k)
    s.add(M.DailySnapshot(coin_id=old.id, date=d, price_usd=9.0 if d == HIGH_DAY else 0.2 if d == LOW_DAY else 1.0))
s.commit()
gone = T.prune_snapshots(s, TODAY)
s.commit()
left = {r.date for r in s.query(M.DailySnapshot).filter_by(coin_id=old.id)}
cutoff = TODAY - timedelta(days=T.SNAPSHOT_KEEP_DAYS)
assert min(d for d in left if d >= cutoff) == cutoff and max(left) == TODAY   # last 45 days all kept
entry_rows = {date(2026, 9, 1) + timedelta(days=k) for k in (-2, -1, 0, 1, 2)}
assert {d for d in left if d < cutoff} == entry_rows | {HIGH_DAY, LOW_DAY}     # entry, max- and min-price rows only
assert gone == 70 - len(left) and gone > 0
# Performance Since Caught is unchanged by the pruning: same entry, high and low
import performance
perf = performance.compute(s, [old])[old.id]
assert perf["entry_price"] == 1.0 and perf["highest_since"] == {"price": 9.0, "date": HIGH_DAY.isoformat()}
assert perf["lowest_since"] == {"price": 0.2, "date": LOW_DAY.isoformat()}
print("volume rule self-check: ok")
