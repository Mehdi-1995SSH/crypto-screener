"""
multicall.py
------------
Multicall3 batch reader: totalSupply / decimals / balanceOf(holders) for many
ERC-20s on one EVM chain in ONE eth_call (CHUNK tokens per call). Multicall3 is
at the same address on every chain we use (probed 2026-11-04: ethereum, bsc,
base, polygon, arbitrum, avalanche - 87 coins read in 6 calls, ~1-2 s each).
Hand-rolled aggregate3 ABI over requests - no web3.py.
"""
import requests

MC3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
# Public RPCs for the Etherscan chains; the others are in burn_detector.RPCS
EXTRA_RPCS = {1: "https://ethereum-rpc.publicnode.com", 42161: "https://arbitrum-one-rpc.publicnode.com",
              137: "https://polygon-bor-rpc.publicnode.com"}
CHUNK = 100
SUPPLY, DECIMALS, BALANCE = "0x18160ddd", "0x313ce567", "0x70a08231"


def _w(n):
    return format(n, "064x")


def encode(calls):
    """aggregate3((address, bool allowFailure=true, bytes)[]) calldata for [(target, data_hex)]."""
    heads, tails, off = [], [], 32 * len(calls)
    for target, data in calls:
        b = bytes.fromhex(data[2:])
        t = (target[2:].lower().rjust(64, "0") + _w(1) + _w(96) + _w(len(b))
             + b.hex().ljust(-(-len(b) // 32) * 64, "0"))
        heads.append(_w(off))
        tails.append(t)
        off += len(t) // 2
    return "0x82ad56cb" + _w(32) + _w(len(calls)) + "".join(heads) + "".join(tails)


def decode(hexdata):
    """aggregate3 return data -> [int or None] (None = that sub-call failed / returned nothing)."""
    d = bytes.fromhex(hexdata[2:])
    word = lambda o: int.from_bytes(d[o:o + 32], "big")
    base, out = word(0), []
    for i in range(word(base)):
        e = base + 32 + word(base + 32 + 32 * i)
        bo = e + word(e + 32)
        n = word(bo)
        out.append(int.from_bytes(d[bo + 32:bo + 32 + n], "big") if word(e) == 1 and n else None)
    return out


def rpc_url(cid):
    from burn_detector import RPCS
    return EXTRA_RPCS.get(cid) or (RPCS.get(cid) or (None,))[0]


def read_tokens(cid, contracts, holders=()):
    """{contract_lower: {"supply", "decimals", "balances": {holder: int}}}. Raises on RPC
    failure - callers fall back to their per-token path."""
    url = rpc_url(cid)
    if not url:
        raise ValueError(f"no public RPC for chain {cid}")
    per, out = 2 + len(holders), {}
    for i in range(0, len(contracts), CHUNK):
        part = contracts[i:i + CHUNK]
        calls = [(c, x) for c in part
                 for x in [SUPPLY, DECIMALS] + [BALANCE + h[2:].lower().rjust(64, "0") for h in holders]]
        r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                     "params": [{"to": MC3, "data": encode(calls)}, "latest"]}, timeout=30).json()
        vals = decode(r["result"])
        for j, c in enumerate(part):
            v = vals[j * per:(j + 1) * per]
            out[c.lower()] = {"supply": v[0], "decimals": v[1],
                              "balances": {h: v[2 + k] or 0 for k, h in enumerate(holders)}}
    return out
