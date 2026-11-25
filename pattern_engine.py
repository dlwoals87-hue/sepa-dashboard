"""Pattern Engine: VCP & Tight Breakout Detection with Peak-to-Subsequent-Low Drawdown."""

from typing import Dict, List, Optional
import pandas as pd
from config import VCPConfig


def _max_pullback_pct(w: pd.DataFrame) -> float:
    """Measures max peak-to-subsequent-low drawdown (%) inside a window so rallies are not miscounted as contractions."""
    if w.empty:
        return 0.0
    running_hi = -1.0
    max_dd = 0.0
    for _, row in w.iterrows():
        hi = float(row["High"])
        lo = float(row["Low"])
        if hi > running_hi:
            running_hi = hi
        if running_hi > 0:
            dd = ((running_hi - lo) / running_hi) * 100.0
            if dd > max_dd:
                max_dd = dd
    return round(max_dd, 1)


def detect_vcp_pattern(df: Optional[pd.DataFrame], cfg: VCPConfig) -> Dict:
    """
    Detects Volatility Contraction Pattern (VCP) & Tight-Base Breakouts:
      1. Prior uptrend (>=25%)
      2. Exclude today's breakout bar from base measurement if today breaks above prior 60D high
      3. Contracting pullback depth sequence (T1 > T2 >= T3) using peak-to-subsequent-low drawdown
      4. Tight final contraction (<=12%) or tight pre-breakout 10D shelf (<=12%)
      5. Volume dry-up in final contraction or volume surge (>=1.40x) on breakout
    """
    if df is None or len(df) < 120:
        return {"pattern_status": "INSUFFICIENT_DATA", "pattern_type": "NONE", "pivot": None, "contractions": []}

    # Check if today's bar is breaking out above the prior 59-day high
    prior_59_hi = float(df.iloc[-60:-1]["High"].max()) if len(df) >= 60 else float(df["High"].max())
    today_close = float(df.iloc[-1]["Close"])
    today_vol = float(df.iloc[-1]["Volume"])
    vol_50 = float(df["Volume"].iloc[-51:-1].mean()) if len(df) >= 51 else float(df["Volume"].tail(50).mean())
    is_today_breakout = today_close > prior_59_hi and (vol_50 > 0 and (today_vol / vol_50) >= 1.40)

    # Measure 60D base excluding today's breakout bar if a breakout just occurred
    base_df = df.iloc[-61:-1] if (is_today_breakout and len(df) >= 61) else df.tail(60)
    ref_end_idx = -1 if is_today_breakout else len(df)
    recent_90 = df.iloc[max(0, len(df) + ref_end_idx - 90) : (len(df) + ref_end_idx)] if ref_end_idx < 0 else df.tail(90)
    prior_120 = df.iloc[max(0, len(df) + ref_end_idx - 210) : max(0, len(df) + ref_end_idx - 90)]
    if prior_120.empty or recent_90.empty:
        return {"pattern_status": "INSUFFICIENT_DATA", "pattern_type": "NONE", "pivot": None, "contractions": []}

    prior_low = float(prior_120["Low"].min())
    base_high = float(recent_90["High"].max())
    prior_uptrend_pct = ((base_high - prior_low) / prior_low) * 100.0 if prior_low > 0 else 0.0

    windows = [base_df.iloc[0:20], base_df.iloc[20:40], base_df.iloc[40:60]]
    contractions: List[Dict] = []
    for idx, w in enumerate(windows, start=1):
        hi = float(w["High"].max())
        lo = float(w["Low"].min())
        depth_pct = _max_pullback_pct(w)
        avg_vol = float(w["Volume"].mean())
        contractions.append({"stage": f"T{idx}", "high": round(hi, 2), "low": round(lo, 2), "depth_pct": depth_pct, "avg_vol": round(avg_vol, 0)})

    depths = [c["depth_pct"] for c in contractions]
    contracting = depths[0] > depths[1] and depths[1] >= depths[2]
    tight_final = depths[2] <= cfg.max_final_contraction_pct
    tight_10d = _max_pullback_pct(base_df.tail(10)) <= cfg.max_final_contraction_pct
    vol_dryup = contractions[2]["avg_vol"] <= (vol_50 * cfg.volume_dryup_ratio) if vol_50 > 0 else False
    pivot = round(float(base_df["High"].max()), 2)

    if prior_uptrend_pct < cfg.min_prior_uptrend_pct:
        return {
            "pattern_status": "NO_SUPPORTED_PATTERN",
            "pattern_type": "NONE",
            "pivot": pivot,
            "contractions": contractions,
            "vol_dryup": vol_dryup,
            "reason": "선행 상승추세(+25%) 부족",
        }

    if contracting and tight_final and vol_dryup:
        status = "VCP_VALIDATED"
        ptype = "VCP"
    elif (contracting and tight_final) or (is_today_breakout and (tight_final or tight_10d)):
        status = "VCP_REVIEW_REQUIRED"
        ptype = "VCP" if contracting else "TIGHT_BREAKOUT"
    elif tight_final or tight_10d:
        status = "VCP_CANDIDATE"
        ptype = "FLAT_BASE_OR_VCP"
    else:
        status = "NO_SUPPORTED_PATTERN"
        ptype = "NONE"

    return {
        "pattern_status": status,
        "pattern_type": ptype,
        "pivot": pivot,
        "contractions": contractions,
        "vol_dryup": vol_dryup,
        "prior_uptrend_pct": round(prior_uptrend_pct, 1),
    }
