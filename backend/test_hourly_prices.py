"""Self-check: the hourly price update survives a coin deleted mid-run (the
daily job removing it while prices are being fetched) - the other coins are
still updated, no StaleDataError. In-memory SQLite, no network.
Run: python test_hourly_prices.py"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database
import models as M
import price_sources
import hourly_prices as H

eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
database.Base.metadata.create_all(eng)
database.SessionLocal = sessionmaker(bind=eng)             # run() imports it from database
with database.SessionLocal() as s:
    for i in range(3):
        s.add(M.Coin(contract=f"0x{i}", chain="ethereum", name=f"c{i}", status="active", price_usd=1.0, cmc_id=100 + i))
    s.commit()


def quotes_while_daily_deletes(ids):
    """CMC answers - and meanwhile the daily job deletes coin 0x1 (own session)."""
    with database.SessionLocal() as other:
        other.query(M.Coin).filter_by(contract="0x1").delete()
        other.commit()
    return {i: 1.1 for i in ids}, 1, False


price_sources.cmc_quotes = quotes_while_daily_deletes
H.fetch_live_prices = lambda keys: ({}, 0, 0)               # CMC covered everyone

stats = H.run()
assert stats["error"] is None, stats["error"]
with database.SessionLocal() as s:
    left = {c.contract: c for c in s.query(M.Coin)}
assert sorted(left) == ["0x0", "0x2"]
assert all(c.live_price == 1.1 and c.live_source == "cmc" and c.cmc_live_price == 1.1 for c in left.values())
print("hourly prices self-check: ok")
