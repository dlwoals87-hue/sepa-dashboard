"""Portfolio & Pyramiding Manager: Blocks Averaging Down on Losers & Enforces Risk Caps."""

from typing import Dict


def evaluate_pyramid_addon(
    position: Dict,
    current_price: float,
    addon_shares: int,
    new_stop_price: float,
    account_equity_krw: float,
    fx_rate: float,
    hard_risk_cap_pct: float = 1.0,
    addon_trigger_pct: float = 8.0,
) -> Dict:
    """
    Core Rules:
      1. Never average down on a losing position (current_price <= avg_entry).
      2. Never widen existing stop (new_stop_price < current_stop).
      3. Recalculate total open risk after add-on; block if exceeding hard cap.
    """
    avg_entry = float(position["avg_entry"])
    cur_shares = int(position["shares"])
    cur_stop = float(position["stop_price"])

    if current_price <= avg_entry:
        return {"allowed": False, "reason": "NO_AVERAGING_DOWN_ON_LOSERS (손실 중 물타기 금지)"}

    gain_pct = ((current_price - avg_entry) / avg_entry) * 100.0
    if new_stop_price < cur_stop:
        return {"allowed": False, "reason": "STOP_WIDENING_PROHIBITED (기존 손절선 하향 금지)"}

    total_shares = cur_shares + addon_shares
    new_avg = ((avg_entry * cur_shares) + (current_price * addon_shares)) / total_shares
    total_risk_krw = max(0.0, (new_avg - new_stop_price) * total_shares * fx_rate)
    total_risk_pct = (total_risk_krw / account_equity_krw) * 100.0 if account_equity_krw > 0 else 999.0

    if total_risk_pct > hard_risk_cap_pct:
        return {
            "allowed": False,
            "reason": f"PORTFOLIO_RISK_EXCEEDED ({total_risk_pct:.2f}% > {hard_risk_cap_pct}%)",
        }

    return {
        "allowed": True,
        "gain_pct": round(gain_pct, 2),
        "meets_8pct_observation": gain_pct >= addon_trigger_pct,
        "new_total_shares": total_shares,
        "new_avg_entry": round(new_avg, 2),
        "new_stop_price": round(new_stop_price, 2),
        "total_risk_pct": round(total_risk_pct, 2),
    }


def compute_open_position_metrics(position: Dict, current_price: float, peak_price: float) -> Dict:
    avg_entry = float(position["avg_entry"])
    shares = int(position["shares"])
    initial_stop = float(position["initial_stop"])
    cur_stop = float(position["stop_price"])

    r_unit = avg_entry - initial_stop
    unrealized_pnl_usd = (current_price - avg_entry) * shares
    current_r = ((current_price - avg_entry) / r_unit) if r_unit > 0 else 0.0
    drawdown_from_peak_pct = ((current_price - peak_price) / peak_price) * 100.0 if peak_price > 0 else 0.0

    return {
        "unrealized_pnl_usd": round(unrealized_pnl_usd, 2),
        "current_r": round(current_r, 2),
        "drawdown_from_peak_pct": round(drawdown_from_peak_pct, 2),
        "stop_breached": current_price <= cur_stop,
    }
