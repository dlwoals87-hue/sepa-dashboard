"""Market Regime Engine: Market -> Leadership -> Stock."""

from typing import Dict, Optional
import pandas as pd
from config import MarketRegimeConfig


POLICY_MAP = {
    "RISK_ON": "NORMAL",
    "CAUTION": "SELECTIVE",
    "DEFENSIVE": "RESTRICTED",
    "RISK_OFF": "PAUSE",
}


def compute_index_trend(df: Optional[pd.DataFrame]) -> Dict:
    if df is None or len(df) < 200:
        return {"status": "INSUFFICIENT_DATA", "above_50": None, "above_200": None, "close": None, "sma50": None, "sma200": None}
    close = float(df["Close"].iloc[-1])
    sma50 = float(df["Close"].rolling(50).mean().iloc[-1])
    sma200 = float(df["Close"].rolling(200).mean().iloc[-1])
    return {
        "status": "OK",
        "close": round(close, 2),
        "sma50": round(sma50, 2),
        "sma200": round(sma200, 2),
        "above_50": close > sma50,
        "above_200": close > sma200,
    }


def evaluate_ftd_and_distribution_proxy(df: Optional[pd.DataFrame], label: str = "SPY") -> Dict:
    """
    Deterministic SYSTEM_FTD_PROXY & SYSTEM_DISTRIBUTION_PROXY (not official IBD data):
      - Distribution Day: Close down <= -0.20% on higher volume than prior day (last 25 sessions).
      - FTD Proxy: Within last 30 sessions, find swing low -> Day 4+ gain >= +1.25% on higher volume.
      - Quality Check: Count distribution days AFTER the FTD proxy or close below FTD low.
    """
    if df is None or len(df) < 35:
        return {"index": label, "dist_25d": None, "ftd_status": "UNAVAILABLE", "ftd_date": None, "ftd_quality_note": "데이터 부족"}

    recent = df.tail(35).copy()
    closes = recent["Close"].tolist()
    lows = recent["Low"].tolist()
    vols = recent["Volume"].tolist()
    dates = [d.strftime("%Y-%m-%d") for d in recent.index]

    # Distribution days in last 25 sessions
    dist_25d = 0
    start_25 = max(1, len(closes) - 25)
    for i in range(start_25, len(closes)):
        ret = (closes[i] / closes[i - 1]) - 1.0
        if ret <= -0.002 and vols[i] > vols[i - 1]:
            dist_25d += 1

    # Find lowest close/low in first 25 sessions of window as Rally Attempt Day 1
    swing_idx = int(pd.Series(lows[:25]).idxmin())
    ftd_idx = None
    for i in range(swing_idx + 3, len(closes)):
        ret = (closes[i] / closes[i - 1]) - 1.0
        if ret >= 0.0125 and vols[i] > vols[i - 1]:
            ftd_idx = i
            break

    if ftd_idx is None:
        return {
            "index": label,
            "dist_25d": dist_25d,
            "ftd_status": "NO_FTD_PROXY",
            "ftd_date": None,
            "ftd_quality_note": f"최근 반등 시도 중 거래량 동반 +1.25%↑ FTD 유사 신호 미발생 (최근 25일 분산일 {dist_25d}일)",
        }

    ftd_low = lows[ftd_idx]
    ftd_date = dates[ftd_idx]
    dist_after = 0
    undercut = False
    for j in range(ftd_idx + 1, len(closes)):
        if closes[j] < ftd_low:
            undercut = True
        ret = (closes[j] / closes[j - 1]) - 1.0
        if ret <= -0.002 and vols[j] > vols[j - 1]:
            dist_after += 1

    if undercut:
        status = "FTD_UNDERCUT_FAILED"
        note = f"{ftd_date} FTD 유사 신호 발생 후 저점(${ftd_low:.2f}) 종가 이탈 → 신호 무효화(약세)"
    elif dist_after >= 2:
        status = "FTD_WEAKENING"
        note = f"{ftd_date} FTD 유사 신호 이후 분산일 {dist_after}회 출현 → 신호 품질 약화 (선별/주의)"
    else:
        status = "FTD_HOLDING"
        note = f"{ftd_date} FTD 유사 신호 유지 중 (저점 방어 · 이후 분산일 {dist_after}회, 25일 누적 {dist_25d}일)"

    return {
        "index": label,
        "dist_25d": dist_25d,
        "ftd_status": status,
        "ftd_date": ftd_date,
        "dist_after_ftd": dist_after,
        "ftd_quality_note": note,
    }


def evaluate_market_regime(
    spy_df: Optional[pd.DataFrame],
    qqq_df: Optional[pd.DataFrame],
    iwm_df: Optional[pd.DataFrame],
    pct_above_50_sma: Optional[float],
    pct_above_200_sma: Optional[float],
    leader_tt_pass_pct: Optional[float],
    cfg: MarketRegimeConfig,
) -> Dict:
    spy = compute_index_trend(spy_df)
    qqq = compute_index_trend(qqq_df)
    iwm = compute_index_trend(iwm_df)

    if spy["status"] != "OK" or qqq["status"] != "OK":
        return {
            "regime": "CAUTION",
            "policy": POLICY_MAP["CAUTION"],
            "status": "INSUFFICIENT_DATA",
            "spy": spy,
            "qqq": qqq,
            "iwm": iwm,
            "pct_above_50_sma": pct_above_50_sma,
            "pct_above_200_sma": pct_above_200_sma,
            "leader_tt_pass_pct": leader_tt_pass_pct,
            "ftd_proxy": "UNAVAILABLE",
            "distribution_proxy": "UNAVAILABLE",
            "reason": "INDEX_DATA_INSUFFICIENT",
        }

    breadth_healthy = (
        pct_above_50_sma is not None
        and pct_above_200_sma is not None
        and pct_above_50_sma >= cfg.breadth_healthy_pct
        and pct_above_200_sma >= cfg.breadth_healthy_pct
    )
    leader_healthy = (
        True
        if not cfg.use_leader_health_hard_rule
        else (leader_tt_pass_pct is not None and leader_tt_pass_pct >= cfg.leader_health_healthy_pct)
    )

    if not spy["above_200"] and not qqq["above_200"]:
        regime = "RISK_OFF"
        reason = "SPY와 QQQ 모두 200일선 아래"
    elif (
        not spy["above_200"]
        or not qqq["above_200"]
        or (not spy["above_50"] and not qqq["above_50"])
        or (not breadth_healthy and not leader_healthy)
    ):
        regime = "DEFENSIVE"
        reason = "주요 지수 200일선 이탈 또는 50일선 동시 하회 / 시장 폭 약화"
    elif spy["above_50"] and spy["above_200"] and qqq["above_50"] and qqq["above_200"] and breadth_healthy and leader_healthy:
        regime = "RISK_ON"
        reason = "SPY/QQQ 완전 상승추세 및 시장 폭 건전"
    else:
        regime = "CAUTION"
        reason = "지수 및 시장 폭 혼조 상태 (선별적 접근 필요)"

    # SYSTEM_DISTRIBUTION_PROXY: last 5 days count of SPY down >0.2% on higher volume
    dist_days = 0
    if len(spy_df) >= 6:
        recent = spy_df.tail(6)
        for i in range(1, len(recent)):
            ret = (recent["Close"].iloc[i] / recent["Close"].iloc[i - 1]) - 1.0
            if ret <= -0.002 and recent["Volume"].iloc[i] > recent["Volume"].iloc[i - 1]:
                dist_days += 1

    spy_ftd = evaluate_ftd_and_distribution_proxy(spy_df, "SPY")
    qqq_ftd = evaluate_ftd_and_distribution_proxy(qqq_df, "QQQ")

    return {
        "regime": regime,
        "policy": POLICY_MAP[regime],
        "status": "OK",
        "spy": spy,
        "qqq": qqq,
        "iwm": iwm,
        "pct_above_50_sma": round(pct_above_50_sma, 1) if pct_above_50_sma is not None else None,
        "pct_above_200_sma": round(pct_above_200_sma, 1) if pct_above_200_sma is not None else None,
        "leader_tt_pass_pct": round(leader_tt_pass_pct, 1) if leader_tt_pass_pct is not None else None,
        "SYSTEM_DISTRIBUTION_PROXY": f"SPY 최근5일 {dist_days}일 · 25일 {spy_ftd['dist_25d']}일 / QQQ 25일 {qqq_ftd['dist_25d']}일",
        "SYSTEM_FTD_PROXY": qqq_ftd["ftd_status"],
        "spy_ftd_proxy": spy_ftd,
        "qqq_ftd_proxy": qqq_ftd,
        "reason": reason,
    }
