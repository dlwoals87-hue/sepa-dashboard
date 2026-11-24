"""Trade Journal & Point-in-Time Strategy Validation Engine."""

from typing import Dict, List


def compute_trade_r(entry_price: float, exit_price: float, initial_stop: float) -> float:
    r_unit = entry_price - initial_stop
    if r_unit <= 0:
        return 0.0
    return round((exit_price - entry_price) / r_unit, 2)


def summarize_journal(trades: List[Dict], data_mode: str = "ALL") -> Dict:
    """
    Summarizes Win Rate, Avg Win R, Avg Loss R, Expectancy(R), Profit Factor, and Rule Compliance.
    Separates LIVE vs SIMULATION trades when requested.
    """
    filtered = [
        t for t in trades
        if data_mode == "ALL" or str(t.get("record_type", "LIVE")).upper() == data_mode.upper()
    ]
    if not filtered:
        return {
            "count": 0,
            "win_rate_pct": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
            "expectancy_r": 0.0,
            "profit_factor": 0.0,
            "rule_compliance_pct": 0.0,
        }

    r_vals = [compute_trade_r(t["entry_price"], t["exit_price"], t["initial_stop"]) for t in filtered]
    wins = [r for r in r_vals if r > 0]
    losses = [r for r in r_vals if r <= 0]

    win_rate = len(wins) / len(r_vals)
    loss_rate = 1.0 - win_rate
    avg_win_r = sum(wins) / len(wins) if wins else 0.0
    avg_loss_r = sum(losses) / len(losses) if losses else 0.0
    expectancy_r = (win_rate * avg_win_r) + (loss_rate * avg_loss_r)

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)

    compliant = sum(1 for t in filtered if t.get("rule_compliant", True))
    return {
        "count": len(filtered),
        "win_rate_pct": round(win_rate * 100.0, 1),
        "avg_win_r": round(avg_win_r, 2),
        "avg_loss_r": round(avg_loss_r, 2),
        "expectancy_r": round(expectancy_r, 2),
        "profit_factor": profit_factor,
        "rule_compliance_pct": round((compliant / len(filtered)) * 100.0, 1),
    }
