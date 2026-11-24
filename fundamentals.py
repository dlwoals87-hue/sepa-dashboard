"""SEPA Fundamentals Analyzer: EPS/Sales Acceleration, Margins, Quality & Qualitative Risk Flags."""

from typing import Dict, List, Optional


def _calc_yoy_series(quarterly_vals: Optional[List[Optional[float]]]) -> List[Optional[float]]:
    """Expects chronological quarterly values (oldest -> newest, >=5 quarters for YoY)."""
    if not quarterly_vals or len(quarterly_vals) < 5:
        return []
    yoy = []
    for i in range(4, len(quarterly_vals)):
        cur, prev = quarterly_vals[i], quarterly_vals[i - 4]
        if cur is None or prev is None or prev == 0:
            yoy.append(None)
        elif prev < 0 and cur > 0:
            yoy.append(999.0)  # Turnaround from loss
        elif prev < 0 and cur <= 0:
            yoy.append(None)
        else:
            yoy.append(round(((cur - prev) / abs(prev)) * 100.0, 1))
    return yoy


def analyze_fundamentals(data: Dict) -> Dict:
    """
    Analyzes EPS & Sales YoY, acceleration/deceleration, margins, debt, and earnings risk.
    Never replaces missing values with 0; returns 'N/A' or 'UNAVAILABLE'.
    """
    q_eps = data.get("quarterly_eps")
    q_sales = data.get("quarterly_sales")
    eps_yoy = _calc_yoy_series(q_eps)
    sales_yoy = _calc_yoy_series(q_sales)

    eps_latest = eps_yoy[-1] if eps_yoy else data.get("eps_growth_yoy")
    sales_latest = sales_yoy[-1] if sales_yoy else data.get("sales_growth_yoy")

    # Acceleration vs Deceleration check over last 3 YoY quarters
    eps_trend = "확인 불가"
    valid_eps_yoy = [x for x in eps_yoy[-3:] if x is not None]
    if len(valid_eps_yoy) >= 3:
        if valid_eps_yoy[0] < valid_eps_yoy[1] < valid_eps_yoy[2]:
            eps_trend = f"가속 ({valid_eps_yoy[0]}% → {valid_eps_yoy[1]}% → {valid_eps_yoy[2]}%)"
        elif valid_eps_yoy[0] > valid_eps_yoy[1] > valid_eps_yoy[2]:
            eps_trend = f"둔화 주의 ({valid_eps_yoy[0]}% → {valid_eps_yoy[1]}% → {valid_eps_yoy[2]}%)"
        else:
            eps_trend = f"혼조 ({' → '.join(str(v) + '%' for v in valid_eps_yoy)})"

    flags = []
    if eps_latest is None:
        flags.append("EPS_DATA_UNAVAILABLE")
    elif eps_latest < 0:
        flags.append("NEGATIVE_EPS_GROWTH")
    if data.get("one_off_tax_or_non_recurring"):
        flags.append("NON_RECURRING_ITEM_CAUTION")
    if data.get("days_to_earnings") is not None and 0 <= data["days_to_earnings"] <= 14:
        flags.append(f"EARNINGS_GAP_RISK_{data['days_to_earnings']}D")

    return {
        "eps_growth_yoy": eps_latest if eps_latest is not None else "N/A",
        "sales_growth_yoy": sales_latest if sales_latest is not None else "N/A",
        "eps_yoy_history": eps_yoy,
        "sales_yoy_history": sales_yoy,
        "eps_trend": eps_trend,
        "gross_margin_pct": data.get("gross_margin_pct", "N/A"),
        "operating_margin_pct": data.get("operating_margin_pct", "N/A"),
        "roe_pct": data.get("roe_pct", "N/A"),
        "debt_to_equity": data.get("debt_to_equity", "N/A"),
        "next_earnings_date": data.get("next_earnings_date", "확인 불가"),
        "quality_flags": flags,
    }
