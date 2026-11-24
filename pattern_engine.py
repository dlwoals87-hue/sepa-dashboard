"""Pattern Engine: VCP & Flat Base Detection with Explicit Validation States."""

from typing import Dict, List, Optional
import pandas as pd
from config import VCPConfig


def detect_vcp_pattern(df: Optional[pd.DataFrame], cfg: VCPConfig) -> Dict:
    """
    Detects Volatility Contraction Pattern (VCP) structure:
      1. Prior uptrend
      2. Contracting depth sequence (T1 > T2 > ...)
      3. Tight final contraction
      4. Volume dry-up in final contraction
      5. Clear Pivot Resistance
    Returns status in:
      {'VCP_VALIDATED', 'VCP_REVIEW_REQUIRED', 'VCP_CANDIDATE', 'NO_SUPPORTED_PATTERN', 'INSUFFICIENT_DATA'}
    """
    if df is None or len(df) < 120:
        return {"pattern_status": "INSUFFICIENT_DATA", "pattern_type": "NONE", "pivot": None, "contractions": []}

    recent_90 = df.tail(90).copy()
    prior_120 = df.tail(210).head(120)
    if prior_120.empty:
        return {"pattern_status": "INSUFFICIENT_DATA", "pattern_type": "NONE", "pivot": None, "contractions": []}

    prior_low = float(prior_120["Low"].min())
    base_high = float(recent_90["High"].max())
    prior_uptrend_pct = ((base_high - prior_low) / prior_low) * 100.0 if prior_low > 0 else 0.0

    # Split 60D base into 3 sequential windows (20D each) to measure contraction depths
    base_60 = df.tail(60)
    windows = [base_60.iloc[0:20], base_60.iloc[20:40], base_60.iloc[40:60]]
    contractions: List[Dict] = []
    for idx, w in enumerate(windows, start=1):
        hi = float(w["High"].max())
        lo = float(w["Low"].min())
        depth_pct = round(((hi - lo) / hi) * 100.0, 1) if hi > 0 else 0.0
        avg_vol = float(w["Volume"].mean())
        contractions.append({"stage": f"T{idx}", "high": round(hi, 2), "low": round(lo, 2), "depth_pct": depth_pct, "avg_vol": round(avg_vol, 0)})

    depths = [c["depth_pct"] for c in contractions]
    contracting = depths[0] > depths[1] and depths[1] >= depths[2]
    tight_final = depths[2] <= cfg.max_final_contraction_pct
    vol_50 = float(df["Volume"].tail(50).mean())
    vol_dryup = contractions[2]["avg_vol"] <= (vol_50 * cfg.volume_dryup_ratio) if vol_50 > 0 else False
    pivot = round(float(base_60["High"].max()), 2)

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
    elif contracting and tight_final:
        status = "VCP_REVIEW_REQUIRED"
        ptype = "VCP"
    elif tight_final:
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
