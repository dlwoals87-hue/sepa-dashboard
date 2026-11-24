"""System Relative Strength (RS) Engine across Full Eligible Universe."""

from typing import Dict, Optional
import pandas as pd
from config import RSConfig


def rs_tier(rank: Optional[int]) -> str:
    if rank is None:
        return "DATA_ERROR"
    if rank >= 90:
        return "Elite"
    if rank >= 80:
        return "Leader"
    if rank >= 70:
        return "Watch"
    return "Non-Leader"


def compute_universe_rs(ohlcv_map: Dict[str, pd.DataFrame], spy_df: Optional[pd.DataFrame], cfg: RSConfig) -> Dict[str, Dict]:
    """
    Computes System RS Rank (1~99) across the entire eligible universe.
    Never rank only among final Top-20 candidates.
    """
    rows = []
    for ticker, df in ohlcv_map.items():
        if ticker in ("SPY", "QQQ", "IWM"):
            continue
        if df is None or len(df) < cfg.days_12m + 1:
            continue
        c = df["Close"]
        r3 = (float(c.iloc[-1]) / float(c.iloc[-1 - cfg.days_3m])) - 1.0
        r6 = (float(c.iloc[-1]) / float(c.iloc[-1 - cfg.days_6m])) - 1.0
        r12 = (float(c.iloc[-1]) / float(c.iloc[-1 - cfg.days_12m])) - 1.0
        rows.append({"ticker": ticker, "r3": r3, "r6": r6, "r12": r12})

    if not rows:
        return {}

    rdf = pd.DataFrame(rows)
    rdf["rs_3m_pctl"] = rdf["r3"].rank(pct=True) * 100.0
    rdf["rs_6m_pctl"] = rdf["r6"].rank(pct=True) * 100.0
    rdf["rs_12m_pctl"] = rdf["r12"].rank(pct=True) * 100.0
    rdf["rs_raw"] = (
        cfg.weight_3m * rdf["rs_3m_pctl"]
        + cfg.weight_6m * rdf["rs_6m_pctl"]
        + cfg.weight_12m * rdf["rs_12m_pctl"]
    )
    # Convert RS_RAW to 1~99 integer System RS Rank
    pctl = rdf["rs_raw"].rank(pct=True)
    rdf["system_rs_rank"] = (pctl * 98 + 1).round().clip(1, 99).astype(int)
    rdf["rs_accel"] = (rdf["rs_3m_pctl"] - rdf["rs_12m_pctl"]).round(1)

    results = {}
    for _, row in rdf.iterrows():
        t = row["ticker"]
        df = ohlcv_map[t]
        rs_line_new_high = False
        if spy_df is not None and not spy_df.empty:
            joined = pd.concat([df["Close"].rename("s"), spy_df["Close"].rename("spy")], axis=1).dropna().tail(252)
            if len(joined) >= 20:
                rs_line = joined["s"] / joined["spy"]
                rs_line_new_high = bool(rs_line.iloc[-1] >= rs_line.max() * 0.999)

        rank_val = int(row["system_rs_rank"])
        emerging = bool(row["rs_3m_pctl"] >= cfg.emerging_3m_pctl and row["rs_accel"] >= cfg.emerging_accel_min)
        results[t] = {
            "system_rs_rank": rank_val,
            "rs_tier": rs_tier(rank_val),
            "rs_3m_pct": round(float(row["r3"]) * 100, 1),
            "rs_6m_pct": round(float(row["r6"]) * 100, 1),
            "rs_12m_pct": round(float(row["r12"]) * 100, 1),
            "rs_3m_pctl": round(float(row["rs_3m_pctl"]), 1),
            "rs_12m_pctl": round(float(row["rs_12m_pctl"]), 1),
            "rs_accel": float(row["rs_accel"]),
            "rs_line_52w_high": rs_line_new_high,
            "emerging_leader_flag": emerging,
        }
    return results
