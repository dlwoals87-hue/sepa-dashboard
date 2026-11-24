"""Deterministic Risk Engine: Stop Loss, Hard Risk Cap, Position Sizing & FX Conversion."""

import math
from typing import Dict, Optional
from config import RiskConfig


def calculate_position_plan(
    entry_price: float,
    cfg: RiskConfig,
    technical_stop: Optional[float] = None,
    available_cash_krw: Optional[float] = None,
    open_positions_count: int = 0,
    fx_timestamp: str = "User Configured",
) -> Dict:
    """
    Calculates max allowed shares respecting:
      - Hard Risk Cap (<= 1.0%)
      - Max initial stop distance (<= 7.0%)
      - Technical stop validation (TRADE_RISK_TOO_WIDE if > 7.0%)
      - Available cash, max single position weight, and max positions (4)
    """
    if entry_price <= 0:
        return {"risk_status": "INVALID_ENTRY_PRICE", "shares": 0}

    if open_positions_count >= cfg.max_positions:
        return {"risk_status": "MAX_POSITIONS_REACHED", "shares": 0, "reason": f"최대 보유 종목({cfg.max_positions}개) 한도 도달"}

    effective_risk_pct = min(cfg.risk_per_trade_pct, cfg.hard_risk_cap_pct)
    cap_clamped = cfg.risk_per_trade_pct > cfg.hard_risk_cap_pct

    max_stop_price = round(entry_price * (1.0 - cfg.max_initial_stop_pct / 100.0), 2)
    stop_source = "MAX_PROTECTIVE_7PCT"

    if technical_stop is not None:
        tech_dist_pct = round(((entry_price - technical_stop) / entry_price) * 100.0, 4)
        if tech_dist_pct <= 0:
            return {"risk_status": "INVALID_TECHNICAL_STOP", "shares": 0, "reason": "손절가가 매수가 이상입니다"}
        if tech_dist_pct > cfg.max_initial_stop_pct:
            return {
                "risk_status": "TRADE_RISK_TOO_WIDE",
                "shares": 0,
                "stop_price": round(technical_stop, 2),
                "stop_distance_pct": round(tech_dist_pct, 2),
                "reason": f"기술적 손절폭({tech_dist_pct:.2f}%)이 최대 허용폭({cfg.max_initial_stop_pct}%) 초과 — 진입 부적격",
            }
        stop_price = round(technical_stop, 2)
        stop_source = "VALIDATED_TECHNICAL_STOP"
    else:
        stop_price = max_stop_price

    risk_per_share_usd = round(entry_price - stop_price, 4)
    if risk_per_share_usd <= 0:
        return {"risk_status": "INVALID_RISK_PER_SHARE", "shares": 0}

    slippage_mult = 1.0 + (cfg.slippage_bps / 10_000.0)
    risk_per_share_krw = risk_per_share_usd * cfg.fx_rate_krw_per_usd * slippage_mult
    allowed_loss_krw = cfg.account_equity_krw * (effective_risk_pct / 100.0)

    shares_by_risk = math.floor(allowed_loss_krw / risk_per_share_krw)
    max_weight_krw = cfg.account_equity_krw * (cfg.max_single_position_pct / 100.0)
    cash_krw = available_cash_krw if available_cash_krw is not None else cfg.account_equity_krw
    budget_krw = min(max_weight_krw, cash_krw)
    shares_by_budget = math.floor(budget_krw / (entry_price * cfg.fx_rate_krw_per_usd * slippage_mult))

    planned_shares = max(0, min(shares_by_risk, shares_by_budget))
    initial_tranche_shares = math.floor(planned_shares * (cfg.initial_tranche_pct / 100.0))
    if planned_shares >= 1 and initial_tranche_shares == 0:
        initial_tranche_shares = 1

    return {
        "risk_status": "CAP_CLAMPED" if cap_clamped else "OK",
        "entry_price": round(entry_price, 2),
        "stop_price": stop_price,
        "stop_source": stop_source,
        "stop_distance_pct": round(((entry_price - stop_price) / entry_price) * 100.0, 2),
        "allowed_loss_krw": round(allowed_loss_krw, 0),
        "risk_per_share_krw": round(risk_per_share_krw, 0),
        "planned_shares": planned_shares,
        "initial_tranche_shares": initial_tranche_shares,
        "position_value_krw": round(planned_shares * entry_price * cfg.fx_rate_krw_per_usd, 0),
        "fx_rate": cfg.fx_rate_krw_per_usd,
        "fx_timestamp": fx_timestamp,
        "gap_disclaimer": "손절가 지정이 시장 갭 하락 시 체결을 보장하지 않으며 실제 손실이 커질 수 있습니다.",
    }
