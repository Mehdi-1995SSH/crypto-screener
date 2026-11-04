"""
seed_watched_entities.py
------------------------
Seeder for watched_entities (pre_listing_radar.py): the 15 market makers +
10 CEXs only.
  EVM:    dawsbot/eth-labels accounts.csv (MIT), pinned commit. Per-user
          deposit addresses ("Binance Dep: 0x..."), hacks, routers, bridges,
          deployers etc. are dropped so the table stays ~1.2K rows. Stored
          once as chain='evm' (CEX / MM wallets are reused across EVM chains).
  Solana: SOLANA below - each address checked by hand against its Solscan
          public name (2026-11-03). No market-maker wallet could be verified;
          the radar's 1-hop detection adds them.
Only adds rows - never changes or deletes one (the table is permanent).
Run: python seed_watched_entities.py
"""
import csv
import io
import re

import requests

SRC = ("https://raw.githubusercontent.com/dawsbot/eth-labels/"
       "d9b21aefeded56f7fce7208d6fa173d128526c1b/data/csv/accounts.csv")
MARKET_MAKERS = {"Wintermute": r"wintermute", "DWF Labs": r"\bdwf\b", "GSR": r"^gsr\b",
                 "Jump Trading": r"\bjump (trading|crypto)\b", "Amber Group": r"^amber group\b",
                 "Cumberland": r"^cumberland\b", "Flow Traders": r"^flow traders", "Auros": r"^auros\b",
                 "B2C2": r"^b2c2\b", "Manifold": r"^manifold trading", "Kronos Research": r"^kronos research",
                 "QCP Capital": r"^qcp\b", "Selini Capital": r"^selini", "Tokka Labs": r"^tokka", "Keyrock": r"^keyrock"}
CEXS = {"Binance": r"^binance\b(?! us)", "Bybit": r"^bybit\b", "KuCoin": r"^kucoin\b", "Gate.io": r"^gate(\.io)?\b",
        "Bitget": r"^bitget\b(?! wallet)", "MEXC": r"^mexc\b", "OKX": r"^(okx\b(?! dex)|okex\b)",
        "Coinbase": r"^coinbase\b", "HTX": r"^(htx|huobi)\b", "Crypto.com": r"^crypto\.com\b"}
DROP = re.compile(r"\bdep: 0x|hack|exploit|scam|phish|old (token|address)|pegged|charity|commerce|token$|swap|"
                  r"router|\bdex\b|bridge|avs operator|deployer|prime", re.I)
ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
# (address, entity, Solscan public name) - verified 2026-11-03
SOLANA = [
    ("2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S", "Binance", "Binance 1"),
    ("5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9", "Binance", "Binance 2"),
    ("9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "Binance", "Binance 3"),
    ("3yFwqXBfZY4jBVUafQ1YEXw189y2dN3V5KQq9uzBDy1E", "Binance", "Binance 8"),
    ("HXsKP7wrBWaQ8T2Vtjry3Nj3oUgwYcqq9vrHDM12G664", "Binance", "Binance 12"),
    ("H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS", "Coinbase", "Coinbase 1"),
    ("2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm", "Coinbase", "Coinbase 2"),
    ("GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE", "Coinbase", "Coinbase Hot Wallet 2"),
    ("5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD", "OKX", "OKX Hot Wallet (5VCwK)"),
    ("9un5wqE3q4oCjyrDkwsdD48KteCJitQX5978Vh7KKxHo", "OKX", "OKX Hot Wallet (9un5w)"),
    ("AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2", "Bybit", "Bybit Hot Wallet (AC5RD)"),
    ("42brAgAVNzMBP7aaktPvAmBSPEkehnFQejiZc53EpJFd", "Bybit", "Bybit Cold Wallet 1"),
    ("BmFdpraQhkiDQE6SnfG5omcA1VwzqfXrwtNYBwWTymy6", "KuCoin", "KuCoin Hot Wallet (BmFdp)"),
    ("u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w", "Gate.io", "Gate Hot Wallet (u6PJ8)"),
    ("HiRpdAZifEsZGdzQ5Xo5wcnaH3D2Jj9SoNsUzcYNK78J", "Gate.io", "Gate 2"),
    ("ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ", "MEXC", "MEXC"),
    ("5PAhQiYdLBd6SVdjzBQDxUAEFyDdF5ExNPQfcscnPRj5", "MEXC", "MEXC #2"),
    ("A77HErqtfN1hLLpvZ9pCtu66FEtM8BveoaKbbMoZ4RiR", "Bitget", "Bitget Exchange"),
    ("88xTWZMeKfiTgbfEmPLdsUCQcZinwUfk25EBQZ21XMAZ", "HTX", "Huobi"),
    ("BY4StcU9Y2BpgH8quZzorg31EGE4L1rjomN8FNsCBEcx", "HTX", "HTX Hot Wallet (BY4St)"),
    ("6FEVkH17P9y8Q9aCkDdPcMDjvj7SVxrTETaYEm8f51Jy", "Crypto.com", "Crypto.com Hot Wallet 1"),
    ("AobVSwdW9BbpMdJvTqeCN4hPAmh4rHm7vwLnQ5ATSyrS", "Crypto.com", "Crypto.com Hot Wallet 2"),
]


def match(name_tag):
    """(entity_name, entity_type) for a dataset name tag, or None (pure)."""
    if not name_tag or DROP.search(name_tag):
        return None
    for group, kind in ((MARKET_MAKERS, "MARKET_MAKER"), (CEXS, "CEX")):
        for entity, rx in group.items():
            if re.search(rx, name_tag, re.I):
                return entity, kind
    return None


def rows_from_csv(text):
    """{address: (chain, entity, type, label)} from the dataset CSV (pure; first label wins)."""
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        a, hit = (r.get("address") or "").lower(), match(r.get("nameTag"))
        if hit and ADDRESS.match(a) and a not in out:
            out[a] = ("evm", *hit, (r.get("nameTag") or "")[:120])
    return out


def main():
    from database import SessionLocal, utcnow
    from models import WatchedEntity
    r = requests.get(SRC, timeout=120)
    r.raise_for_status()
    rows = rows_from_csv(r.text)
    rows.update({a: ("solana", e, "CEX", f"Solscan: {label}") for a, e, label in SOLANA})
    s = SessionLocal()
    try:
        have = {a for (a,) in s.query(WatchedEntity.address)}
        new = [WatchedEntity(address=a, chain=chain, entity_name=entity, entity_type=kind, is_root_wallet=True,
                             discovered_via="OPEN_LABELS", source_label=label, created_at=utcnow())
               for a, (chain, entity, kind, label) in rows.items() if a not in have]
        s.add_all(new)
        s.commit()
        print(f"watched_entities: {len(new)} added, {len(have)} already there")
    finally:
        s.close()


if __name__ == "__main__":
    main()
