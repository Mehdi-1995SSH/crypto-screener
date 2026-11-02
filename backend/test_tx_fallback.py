"""Self-check for the 15-day chart fallback (tx_bars): GeckoTerminal fills
Codex's 0 / missing days (volume only), a fallback 0 never replaces Codex,
saved days are never overwritten by None, missing stays None, and requests
are retried (2 tries) only on transient errors. No network. Run:
python test_tx_fallback.py"""
from datetime import timedelta
import requests
import http_retry
import tx_bars as X

GT_REAL = X.geckoterminal_daily
today = X._today_utc()
D = lambda k: (today - timedelta(days=k)).isoformat()            # k days ago
bar = lambda k, vol, tx=5: {"date": D(k), "buys": tx, "sells": tx, "buyers": 1, "sellers": 1,
                            "buy_volume": vol, "sell_volume": vol, "volume": vol, "transactions": tx,
                            "source": "codex"}
daily = lambda bars: {"liq_now": 1, "liq_7d_ago": 1, "liq_7d_ago_date": None, "bars": bars,
                      "vol_7d": X._sum(b["volume"] for b in bars[-7:]),
                      "vol_total": X._sum(b["volume"] for b in bars), "since": bars[0]["date"] if bars else None}

# 1. only the 0 / missing days are filled, volume only; a fallback 0 doesn't replace Codex
d = X.fill_gaps(daily([bar(3, 100.0), bar(2, 0.0, 0), bar(1, None, None)]), {D(3): 999, D(2): 50.0, D(1): 0.0}, "geckoterminal")
b3, b2, b1 = d["bars"]
assert b3["volume"] == 100.0 and b3["source"] == "codex"                       # good Codex day untouched
assert b2["volume"] == 50.0 and b2["source"] == "geckoterminal" and b2["buys"] is None and b2["transactions"] is None
assert b1["volume"] is None and b1["source"] == "codex"                        # fallback 0 -> stays missing, not 0
assert d["vol_7d"] == 150.0 and d["vol_total"] == 150.0

# 2. Codex has nothing -> the fallback's own last 15 complete days (today's partial left out)
fb = {D(k): float(k) for k in range(0, 20)}
d = X.fill_gaps(daily([]), fb, "geckoterminal")
assert [b["date"] for b in d["bars"]] == [D(k) for k in range(15, 0, -1)] and d["since"] == D(15)
assert all(b["source"] == "geckoterminal" and b["buys"] is None for b in d["bars"])

# 3. zero protection: a day back as None keeps the saved value; a real new value wins
saved = [bar(2, 70.0), bar(1, 80.0)]
new = [bar(2, None, None), bar(1, 90.0)]
kept = X.keep_saved_days(new, saved)
assert kept[0]["volume"] == 70.0 and kept[1]["volume"] == 90.0

# 4. fetch_daily: no fallback calls when Codex is complete; a Codex outage is returned as is
calls = []
X.geckoterminal_daily = lambda c, ch: calls.append(1) or {D(1): 5.0}
X.fetch_from_codex = lambda c, ch: (daily([bar(2, 10.0), bar(1, 20.0)]), None)
assert X.fetch_daily("0xa", "ethereum")[0]["bars"][1]["volume"] == 20.0 and not calls
X.fetch_from_codex = lambda c, ch: (None, "Codex request failed (HTTP 500)")
assert X.fetch_daily("0xa", "ethereum") == (None, "Codex request failed (HTTP 500)") and not calls
X.fetch_from_codex = lambda c, ch: (daily([bar(2, 10.0), bar(1, 0.0, 0)]), None)  # dead-pool day -> filled
r, err = X.fetch_daily("0xa", "ethereum")
assert err is None and r["bars"][1]["volume"] == 5.0 and r["bars"][1]["source"] == "geckoterminal" and calls

# 5. retries: 2 tries on a timeout / 5xx, none on a 4xx
tries = []
def fake(status=None, exc=None):
    def go(method, url, **kw):
        tries.append(kw.get("timeout"))
        if exc:
            raise exc
        r = requests.Response()
        r.status_code = status
        return r
    return go
http_retry.request.retry.sleep = lambda s: None                               # no real waiting in the test
for f, n in ((fake(exc=requests.exceptions.Timeout()), 2), (fake(503), 2), (fake(404), 1)):
    tries.clear()
    requests.request = f
    try:
        http_retry.request("GET", "https://x")
    except http_retry.Transient:
        pass
    assert len(tries) == n and tries[0] == http_retry.REQUEST_TIMEOUT == 6, (n, tries)

# 6. GeckoTerminal: top 2 pools summed per day
class R:
    def __init__(self, body): self.body = body
    def json(self): return self.body
from datetime import datetime, timezone
ts = int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp()) - 86400   # yesterday 00:00 UTC
seen = []
def gt(method, url, **kw):
    seen.append(url)
    if url.endswith("/pools"):
        return R({"data": [{"attributes": {"address": a}} for a in ("p1", "p2", "p3")]})
    return R({"data": {"attributes": {"ohlcv_list": [[ts, 1, 1, 1, 1, 100.0 if "/p1/" in url else 30.0]]}}})
X.request = gt
assert GT_REAL("0xa", "ethereum") == {D(1): 130.0}                            # p1 + p2, not p3
assert sum("/ohlcv/" in u for u in seen) == 2 and "/networks/eth/" in seen[0]
assert GT_REAL("0xa", "tron") == {}                                           # no GeckoTerminal network
def broken(method, url, **kw):
    raise http_retry.Transient("HTTP 503")
X.request = broken
assert GT_REAL("0xa", "ethereum") == {}                                       # outage -> {} (never raises)
print("tx fallback self-check: ok")
