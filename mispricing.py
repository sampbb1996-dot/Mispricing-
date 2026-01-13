# mispricing.py
# "Adverse-to-zero" signal engine (no EV hype; zero/inaction is treated as a liability when safe action exists)
#
# What this DOES:
# - Maintains a conservative, bounded decision rule:
#       ACT if (worst_cost_of_inaction > worst_cost_of_action) AND (worst_cost_of_action <= HARD_MAX_LOSS)
# - Stores observations in SQLite
# - Emits BUY/SELL/FLAT signals (signal-only by default)
#
# What this DOES NOT do:
# - No guarantees, no martingale, no averaging down, no "make it back"
# - No custody, no auto-trading unless you explicitly wire an execution adapter
#
# You can plug any price source by implementing fetch_market_snapshot().

import time
import math
import json
import sqlite3
import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

DB = "field.db"

# -------------------- cadence --------------------
POLL_SECONDS = 30

# -------------------- hard safety bounds --------------------
HARD_MAX_LOSS_FRAC = 0.006  # 0.6% worst-case tolerated loss per action (conservative)
FEE_FRAC = 0.0015           # combined fees estimate (edit for your venue)
SLIPPAGE_FRAC = 0.0010      # conservative slippage estimate (edit)
ADVERSE_MOVE_PAD = 0.0025   # worst plausible move against you during entry/exit window

# -------------------- "zero is a liability" strength --------------------
# If there's a clean edge and the window is short, inaction can be "more costly" than bounded action.
WINDOW_SECONDS = 180        # how quickly opportunities decay / disappear
ZERO_LIABILITY_GAIN = 1.0   # 1.0 = literal; >1 increases aversion to staying at 0

# -------------------- signal thresholding --------------------
# We still need a minimal edge detection so we don't act randomly.
MIN_EDGE_FRAC = 0.004       # 0.4% raw edge before we even compare costs

# -------------------- persistence --------------------

def _connect():
    return sqlite3.connect(DB, timeout=30)

def init_db():
    con = _connect()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ticks (
        ts INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        price REAL NOT NULL,
        PRIMARY KEY (ts, symbol)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS signals (
        ts INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        action TEXT NOT NULL, -- BUY/SELL/FLAT
        edge REAL NOT NULL,
        cost_zero REAL NOT NULL,
        cost_act REAL NOT NULL,
        reason TEXT NOT NULL
    )
    """)
    con.commit()
    con.close()

def put_tick(ts: int, symbol: str, price: float):
    con = _connect()
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO ticks (ts, symbol, price) VALUES (?, ?, ?)", (ts, symbol, price))
    con.commit()
    con.close()

def recent_prices(symbol: str, limit: int = 200) -> List[Tuple[int, float]]:
    con = _connect()
    cur = con.cursor()
    cur.execute("SELECT ts, price FROM ticks WHERE symbol=? ORDER BY ts DESC LIMIT ?", (symbol, limit))
    rows = cur.fetchall()
    con.close()
    return list(reversed(rows))  # chronological

def log_signal(ts: int, symbol: str, action: str, edge: float, cost_zero: float, cost_act: float, reason: str):
    con = _connect()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO signals (ts, symbol, action, edge, cost_zero, cost_act, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (ts, symbol, action, edge, cost_zero, cost_act, reason))
    con.commit()
    con.close()

# -------------------- model --------------------

@dataclass
class Opportunity:
    symbol: str
    price: float
    ref_price: float   # your "reference" (fair) price, computed however you like
    side: str          # "BUY" if cheap vs ref, "SELL" if expensive vs ref

    def edge(self) -> float:
        # signed edge from perspective of taking the action side
        # BUY: positive when price < ref
        # SELL: positive when price > ref (overpriced to sell)
        if self.ref_price <= 0:
            return 0.0
        if self.side == "BUY":
            return (self.ref_price - self.price) / self.ref_price
        else:  # SELL
            return (self.price - self.ref_price) / self.ref_price

# -------------------- robust volatility estimate (conservative) --------------------

def robust_vol_frac(symbol: str) -> float:
    """
    Returns a conservative per-window volatility estimate (fraction),
    derived from median absolute log return, scaled.
    """
    pts = recent_prices(symbol, limit=120)
    if len(pts) < 20:
        return 0.0

    rets = []
    for i in range(1, len(pts)):
        p0 = pts[i-1][1]
        p1 = pts[i][1]
        if p0 <= 0 or p1 <= 0:
            continue
        rets.append(abs(math.log(p1 / p0)))

    if len(rets) < 10:
        return 0.0

    rets.sort()
    med = rets[len(rets)//2]
    # Convert log-return magnitude to approximate fractional move; pad conservatively
    frac = (math.exp(med) - 1.0)
    return max(0.0, frac)

# -------------------- adverse-to-zero decision rule --------------------

def worst_cost_of_action(symbol: str) -> float:
    """
    Conservative worst-case cost (fraction) of taking the action now.
    Includes fees, slippage, and an adverse move pad; also respects observed vol.
    """
    vol = robust_vol_frac(symbol)
    # take the worse of (fixed pad) and (vol-based pad)
    adverse = max(ADVERSE_MOVE_PAD, 2.0 * vol)
    return FEE_FRAC + SLIPPAGE_FRAC + adverse

def worst_cost_of_inaction(opp: Opportunity) -> float:
    """
    Conservative worst-case cost of staying at zero (doing nothing),
    modeled as "edge decays / opportunity vanishes."
    """
    e = max(0.0, opp.edge())

    # If the opportunity is time-sensitive, assume you can lose the whole edge window.
    # Scale by ZERO_LIABILITY_GAIN (adverse-to-zero pressure).
    decay_penalty = min(1.0, POLL_SECONDS / max(1.0, WINDOW_SECONDS))
    # Worst case: you miss the move that captures the edge; treat as lost edge.
    return ZERO_LIABILITY_GAIN * e * (1.0 + decay_penalty)

def decide(opp: Opportunity) -> Tuple[str, float, float, str]:
    """
    Returns (action, cost_zero, cost_act, reason)
    action is BUY/SELL/FLAT
    """
    e = opp.edge()

    # Hard gate: if edge isn't present, no reason to escape zero.
    if e < MIN_EDGE_FRAC:
        return ("FLAT", 0.0, 0.0, f"edge<{MIN_EDGE_FRAC:.4f}")

    cost_act = worst_cost_of_action(opp.symbol)

    # Hard safety: never exceed bound
    if cost_act > HARD_MAX_LOSS_FRAC:
        return ("FLAT", 0.0, cost_act, f"cost_act>{HARD_MAX_LOSS_FRAC:.4f} (hard bound)")

    cost_zero = worst_cost_of_inaction(opp)

    # Core rule: escape zero when staying at zero is worse than bounded action
    if cost_zero > cost_act:
        return (opp.side, cost_zero, cost_act, "escape_zero(cost_zero>cost_act)")
    else:
        return ("FLAT", cost_zero, cost_act, "stay_zero(cost_zero<=cost_act)")

# -------------------- reference price model (plug your own) --------------------

def compute_ref_prices(snapshot: Dict[str, float]) -> Dict[str, float]:
    """
    Default reference model: trailing median over recent prices.
    Replace this with your actual predictor/ref model.
    """
    refs: Dict[str, float] = {}
    for sym, px in snapshot.items():
        pts = recent_prices(sym, limit=60)
        if len(pts) < 15:
            refs[sym] = px
            continue
        vals = [p for _, p in pts]
        vals.sort()
        refs[sym] = vals[len(vals)//2]
    return refs

# -------------------- market data adapter --------------------

def fetch_market_snapshot() -> Dict[str, float]:
    """
    IMPLEMENT THIS.
    Return mapping: symbol -> last price.
    For now, supports a local file 'prices.json' like:
      {"BTC-AUD": 65000.0, "ETH-AUD": 3500.0}
    """
    try:
        with open("prices.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        out = {}
        for k, v in data.items():
            try:
                out[str(k)] = float(v)
            except Exception:
                continue
        return out
    except FileNotFoundError:
        return {}

# -------------------- main loop --------------------

def main():
    init_db()
    print("mispricing.py started (adverse-to-zero)", flush=True)

    while True:
        ts = int(time.time())
        snap = fetch_market_snapshot()
        if not snap:
            # No data: staying at zero is forced, log nothing.
            time.sleep(POLL_SECONDS)
            continue

        # store ticks
        for sym, px in snap.items():
            if px > 0:
                put_tick(ts, sym, px)

        refs = compute_ref_prices(snap)

        # evaluate opportunities
        for sym, px in snap.items():
            ref = refs.get(sym, px)
            if ref <= 0 or px <= 0:
                continue

            # Cheap -> BUY opportunity; Expensive -> SELL opportunity
            if px < ref:
                opp = Opportunity(sym, px, ref, "BUY")
            elif px > ref:
                opp = Opportunity(sym, px, ref, "SELL")
            else:
                opp = Opportunity(sym, px, ref, "FLAT")

            if opp.side == "FLAT":
                continue

            action, cost_zero, cost_act, reason = decide(opp)

            # Log only meaningful decisions (edge present or constraint info)
            if action != "FLAT":
                print(f"[{ts}] {sym} {action} px={px:.8f} ref={ref:.8f} edge={opp.edge():.4f} "
                      f"cost0={cost_zero:.4f} costA={cost_act:.4f} :: {reason}", flush=True)
            log_signal(ts, sym, action, float(opp.edge()), float(cost_zero), float(cost_act), reason)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
