"""
http_retry.py
-------------
Retried HTTP for the 15-day chart's sources (Codex, GeckoTerminal), which run
on page views (detail page, Check a Coin) - kept short so a Vercel function
never times out: 2 tries, a 2 s wait between them, only on timeouts /
network errors / HTTP 429 / 5xx. Anything else (4xx, bad JSON) returns at once.
"""
import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

REQUEST_TIMEOUT = 6         # seconds per try


class Transient(Exception):
    """Timeout, network error, HTTP 429 or 5xx - worth retrying. Never carries the URL."""


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=10),
       retry=retry_if_exception_type(Transient), reraise=True)
def request(method, url, **kw):
    kw.setdefault("timeout", REQUEST_TIMEOUT)
    try:
        r = requests.request(method, url, **kw)
    except requests.exceptions.RequestException as e:
        raise Transient(type(e).__name__) from None
    if r.status_code == 429 or r.status_code >= 500:
        raise Transient(f"HTTP {r.status_code}")
    return r
