"""Pivot & EOD Entry Engine: Separates Pivot, EOD Breakout, and D+1 Executable Entry."""

from typing import Dict, Optional
import pandas as pd
from config import EntryConfig


def evaluate_entry_status(
    df: Optional[pd.DataFrame],
    tt_status: str,
    pattern_status: str,
    pivot: Optional[float],
    market_regime: str,
    cfg: EntryConfig,
    next_open: Optional[float] = None,
) -> Dict:
    """
    Distinguishes Pivot Resistance, EOD_CONFIRMED Breakout, and D+1 Open Executable Entry.
    """
    if df is None or len(df) < 51 or pivot is None or pivot <= 0 or tt_status in ("DATA_ERROR", "INSUFFICIENT_DATA"):
        return {
            "entry_status": "DATA UNAVAILABLE",
            "eod_confirmed": False,
            "d1_open_status": "UNAVAILABLE",
            "pivot_distance_pct": None,
            "vol_ratio": None,
            "reason": "필수 주가·거래량 또는 피벗 데이터 부족",
        }

    last = df.iloc[-1]
    close_now = float(last["Close"])
    high_now = float(last["High"])
    vol_now = float(last["Volume"])
    avg_vol_50 = float(df["Volume"].iloc[-51:-1].mean())
    vol_ratio = round(vol_now / avg_vol_50, 2) if avg_vol_50 > 0 else 0.0
    pivot_dist_pct = round(((close_now - pivot) / pivot) * 100.0, 2)

    if market_regime == "RISK_OFF":
        return {
            "entry_status": "BLOCKED BY MARKET",
            "eod_confirmed": False,
            "d1_open_status": "MARKET_PAUSE",
            "pivot_distance_pct": pivot_dist_pct,
            "vol_ratio": vol_ratio,
            "reason": "시장 상태 RISK_OFF(신규 매수 중단)로 진입 차단",
        }

    pivot_valid = pattern_status in ("VCP_VALIDATED", "VCP_REVIEW_REQUIRED")
    eod_confirmed = (
        pivot_valid
        and tt_status == "PASS"
        and high_now > pivot
        and close_now > pivot
        and vol_ratio >= cfg.breakout_volume_mult
    )

    if eod_confirmed:
        d1_status = "PENDING_D1_OPEN"
        entry_status = "EOD CONFIRMED"
        if next_open is not None:
            gap_pct = ((next_open - pivot) / pivot) * 100.0
            if next_open < pivot:
                d1_status = "OPEN_BELOW_PIVOT_WAIT"
                entry_status = "WATCH"
            elif gap_pct <= cfg.max_chase_pct:
                d1_status = "ACTIONABLE_AT_OPEN"
                entry_status = "ACTIONABLE"
            else:
                d1_status = "GAP_EXTENDED_NO_ENTRY"
                entry_status = "EXTENDED / NO CHASE"
        return {
            "entry_status": entry_status,
            "eod_confirmed": True,
            "d1_open_status": d1_status,
            "pivot_distance_pct": pivot_dist_pct,
            "vol_ratio": vol_ratio,
            "reason": f"EOD 돌파 확인(거래량 {vol_ratio}x >= {cfg.breakout_volume_mult}x)",
        }

    # Not EOD confirmed
    if close_now > pivot * (1 + cfg.max_chase_pct / 100.0):
        entry_status = "EXTENDED / NO CHASE"
        reason = f"피벗 대비 +{pivot_dist_pct}% 이격 과다 (추격 매수 금지)"
    elif high_now > pivot and close_now < pivot:
        entry_status = "FAILED BREAKOUT"
        reason = "장중 피벗 돌파 후 종가 이탈 (실패 돌파)"
    elif pivot_dist_pct >= -3.0 and tt_status == "PASS" and pivot_valid:
        entry_status = "NEAR PIVOT"
        reason = f"피벗({pivot})까지 {abs(pivot_dist_pct)}% 이내 접근, 거래량 {cfg.breakout_volume_mult}x 돌파 대기"
    elif tt_status == "PASS" and pattern_status in ("VCP_CANDIDATE", "VCP_REVIEW_REQUIRED", "VCP_VALIDATED"):
        entry_status = "SETUP FORMING"
        reason = "Stage 2 추세 충족 및 베이스 수축 형성 중"
    else:
        entry_status = "WATCH"
        reason = "추세 또는 패턴 조건 충족 대기"

    return {
        "entry_status": entry_status,
        "eod_confirmed": False,
        "d1_open_status": "NOT_TRIGGERED",
        "pivot_distance_pct": pivot_dist_pct,
        "vol_ratio": vol_ratio,
        "reason": reason,
    }
