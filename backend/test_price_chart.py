"""Self-check for the price chart's data: Check a Coin's daily closes from the
CoinGecko response in memory (volume_check.daily_closes) and the detail
page's series from the stored Codex closes (entry_zone.load_bars). No network.
Run: python test_price_chart.py"""
import json
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import database
import models as M
import volume_check as V
from entry_zone import load_bars

ms = lambda y, m, d, h: datetime(y, m, d, h, tzinfo=timezone.utc).timestamp() * 1000

# 1. daily_closes: the LAST price of each UTC day, sorted; no data -> []
V._prices[("base", "0xabc")] = [[ms(2026, 11, 2, 23), 1.5], [ms(2026, 11, 1, 1), 1.0],
                                [ms(2026, 11, 1, 23), 1.2], [ms(2026, 11, 2, 1), 0.0],   # a 0 price is skipped
                                [ms(2026, 11, 3, 5), 7.703e-05]]
assert V.daily_closes("0xABC", "base") == [["2026-11-01", 1.2], ["2026-11-02", 1.5], ["2026-11-03", 7.703e-05]]
assert V.daily_closes("0xnone", "base") == [] and V.daily_closes("0xabc", "tron") == []

# 2. the detail page's series = the stored Codex closes (what main.py serves), no API call
eng = create_engine("sqlite://")
database.Base.metadata.create_all(eng)
s = sessionmaker(bind=eng)()
coin = M.Coin(contract="0xDEF", chain="ethereum", name="d", status="dropped", entry_price=2.0)
s.add(coin)
s.add(M.EntryZoneBars(chain="ethereum", contract="0xdef", data=json.dumps(
    {"bars": [["2026-10-30", 2.1, 10, None, None], ["2026-10-31", None, 10, None, None], ["2026-11-01", 1.9, 10, None, None]]})))
s.commit()
series = [[d.isoformat(), c] for d, c, _, _ in (load_bars(s, coin)[0] or []) if c]    # = main.py's expression
assert series == [["2026-10-30", 2.1], ["2026-11-01", 1.9]]                            # missing closes skipped
assert load_bars(s, M.Coin(contract="0xnone", chain="ethereum"))[0] is None               # -> [] in main.py
# 3. Check a Coin's day cache: when a parallel check of the same coin saved
#    its row first (unique contract), this one still returns - no 500
import coin_check
coin_check.run_check = lambda c: {"found": True, "contract": c}
assert coin_check.get_check(sessionmaker(bind=eng)(), "0xRACE")["found"] is True       # saves 0xrace


class LateSession:
    """Didn't see the row when it looked, so it inserts a duplicate."""
    def __init__(self): self.s, self.rolled = sessionmaker(bind=eng)(), False
    def query(self, *a): return self.s.query(*a).filter_by(contract="nobody")
    def add(self, row): self.s.add(row)
    def commit(self): self.s.commit()
    def rollback(self): self.rolled = True; self.s.rollback()


late = LateSession()
assert coin_check.get_check(late, "0xRACE")["found"] is True and late.rolled

# 4. the candles' pool on Check a Coin = the most liquid DexScreener pair
assert coin_check.top_pool([{"pairAddress": "0xsmall", "liquidity": {"usd": 5}},
                            {"pairAddress": "0xbig", "liquidity": {"usd": 900}}, {"pairAddress": "0xnone"}]) == "0xbig"
assert coin_check.top_pool([]) is None and coin_check.top_pool(None) is None
from tx_bars import GT_NETWORKS
assert GT_NETWORKS["ethereum"] == "eth" and GT_NETWORKS["solana"] == "solana"   # the network the browser asks for
print("price chart self-check: ok")
