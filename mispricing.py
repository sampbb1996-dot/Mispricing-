import requests
import time

# ---- CONFIG (safe defaults) ----
PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
ASSET_ID = "bitcoin"
VS_CURRENCY = "usd"

# Reference is a slow EMA maintained locally (not market-driven)
EMA_ALPHA = 0.01
_ref_price = None
_last_ts = 0


def get_market_mispricing_evidence():
    """
    Returns:
        float | None
        +ve  => uncorrected error (cheap)
        -ve  => correction pressure
        None => no update / no evidence
    """
    global _ref_price, _last_ts

    # throttle implicitly (no chunking logic needed)
    if time.time() - _last_ts < 10:
        return None

    try:
        r = requests.get(
            PRICE_URL,
            params={
                "ids": ASSET_ID,
                "vs_currencies": VS_CURRENCY
            },
            timeout=5
        )
        r.raise_for_status()
        price = r.json()[ASSET_ID][VS_CURRENCY]
    except Exception:
        return None

    _last_ts = time.time()

    if _ref_price is None:
        _ref_price = price
        return None

    # update slow reference (human-latency proxy)
    _ref_price = (1 - EMA_ALPHA) * _ref_price + EMA_ALPHA * price

    # signed relative mispricing
    evidence = (_ref_price - price) / _ref_price

    return evidence
