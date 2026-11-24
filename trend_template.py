"""Stage 2 Trend Template (TT01~TT10) Atomic Hard-Rule Evaluator."""

from typing import Dict, Optional
import pandas as pd
from config import TrendTemplateConfig


def evaluate_trend_template(
    df: Optional[pd.DataFrame],
    system_rs_rank: Optional[int],
    cfg: TrendTemplateConfig,
) -> Dict:
    """
    Evaluates TT01~TT10 using Simple Moving Averages (SMA).
    All active hard rules must pass for TT_STATUS == 'PASS'.
    """
    if df is None or df.empty:
        return {"tt_status": "DATA_ERROR", "fail_reasons": ["NO_PRICE_DATA"], "rules": {}}
    if len(df) < 220 or system_rs_rank is None:
        return {"tt_status": "INSUFFICIENT_DATA", "fail_reasons": ["NEED_220D_AND_RS"], "rules": {}}

    c = df["Close"]
    sma50 = c.rolling(50).mean()
    sma150 = c.rolling(150).mean()
    sma200 = c.rolling(200).mean()

    close_now = float(c.iloc[-1])
    sma50_now = float(sma50.iloc[-1])
    sma150_now = float(sma150.iloc[-1])
    sma200_now = float(sma200.iloc[-1])
    sma200_20d_ago = float(sma200.iloc[-1 - cfg.sma200_slope_lookback])

    win_52w = df.tail(252)
    low_52w = float(win_52w["Low"].min())
    high_52w = float(win_52w["High"].max())

    if any(pd.isna(v) for v in (close_now, sma50_now, sma150_now, sma200_now, sma200_20d_ago, low_52w, high_52w)):
        return {"tt_status": "DATA_ERROR", "fail_reasons": ["NAN_IN_INDICATORS"], "rules": {}}

    checks = {
        "TT01": {"desc": "Close > SMA150", "passed": close_now > sma150_now, "actual": f"{close_now:.2f} vs {sma150_now:.2f}"},
        "TT02": {"desc": "Close > SMA200", "passed": close_now > sma200_now, "actual": f"{close_now:.2f} vs {sma200_now:.2f}"},
        "TT03": {"desc": "SMA150 > SMA200", "passed": sma150_now > sma200_now, "actual": f"{sma150_now:.2f} vs {sma200_now:.2f}"},
        "TT04": {"desc": "SMA200 > SMA200(20D ago)", "passed": sma200_now > sma200_20d_ago, "actual": f"{sma200_now:.2f} vs {sma200_20d_ago:.2f}"},
        "TT05": {"desc": "SMA50 > SMA150", "passed": sma50_now > sma150_now, "actual": f"{sma50_now:.2f} vs {sma150_now:.2f}"},
        "TT06": {"desc": "SMA50 > SMA200", "passed": sma50_now > sma200_now, "actual": f"{sma50_now:.2f} vs {sma200_now:.2f}"},
        "TT07": {"desc": "Close > SMA50", "passed": close_now > sma50_now, "actual": f"{close_now:.2f} vs {sma50_now:.2f}"},
        "TT08": {"desc": "Close >= 52W Low * 1.30", "passed": close_now >= low_52w * cfg.min_above_52w_low_mult, "actual": f"{close_now:.2f} vs {(low_52w * cfg.min_above_52w_low_mult):.2f}"},
        "TT09": {"desc": "Close >= 52W High * 0.75", "passed": close_now >= high_52w * cfg.min_of_52w_high_mult, "actual": f"{close_now:.2f} vs {(high_52w * cfg.min_of_52w_high_mult):.2f}"},
        "TT10": {"desc": f"System RS Rank >= {cfg.min_rs_rank}", "passed": system_rs_rank >= cfg.min_rs_rank, "actual": f"{system_rs_rank}"},
    }

    fail_reasons = [f"{k}({v['desc']})" for k, v in checks.items() if not v["passed"]]
    status = "PASS" if not fail_reasons else "FAIL"

    return {
        "tt_status": status,
        "fail_reasons": fail_reasons,
        "rules": checks,
        "metrics": {
            "close": round(close_now, 2),
            "sma50": round(sma50_now, 2),
            "sma150": round(sma150_now, 2),
            "sma200": round(sma200_now, 2),
            "low_52w": round(low_52w, 2),
            "high_52w": round(high_52w, 2),
        },
    }
