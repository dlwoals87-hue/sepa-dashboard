"""SEPA Investment Dashboard V2.0 Configuration & Rule Constants."""

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class UniverseConfig:
    min_price_actionable: float = 15.0
    min_price_watch: float = 10.0
    min_avg_dollar_vol_50d: float = 10_000_000.0  # $10M/day
    exclude_healthcare: bool = False
    exclude_spac: bool = True
    exclude_negative_eps: bool = True
    allowed_exchanges: List[str] = field(default_factory=lambda: ["NYSE", "NASDAQ", "AMEX"])
    min_history_days: int = 252


@dataclass
class MarketRegimeConfig:
    breadth_healthy_pct: float = 50.0
    leader_health_healthy_pct: float = 60.0
    leader_rs_threshold: int = 80
    use_leader_health_hard_rule: bool = False  # Keep provisional until validated


@dataclass
class RSConfig:
    weight_3m: float = 0.40
    weight_6m: float = 0.30
    weight_12m: float = 0.30
    days_3m: int = 63
    days_6m: int = 126
    days_12m: int = 252
    emerging_3m_pctl: float = 85.0
    emerging_accel_min: float = 15.0


@dataclass
class TrendTemplateConfig:
    min_above_52w_low_mult: float = 1.30
    min_of_52w_high_mult: float = 0.75
    min_rs_rank: int = 70
    sma200_slope_lookback: int = 20


@dataclass
class VCPConfig:
    min_prior_uptrend_pct: float = 25.0
    max_final_contraction_pct: float = 12.0
    volume_dryup_ratio: float = 0.75
    pivot_near_pct: float = 3.0


@dataclass
class EntryConfig:
    breakout_volume_mult: float = 1.40
    max_chase_pct: float = 5.0


@dataclass
class RiskConfig:
    account_equity_krw: float = 10_000_000.0
    fx_rate_krw_per_usd: float = 1_400.0
    risk_per_trade_pct: float = 0.5
    hard_risk_cap_pct: float = 1.0
    max_initial_stop_pct: float = 7.0
    initial_tranche_pct: float = 50.0
    addon_trigger_pct: float = 8.0
    max_positions: int = 4
    max_single_position_pct: float = 25.0
    slippage_bps: float = 10.0


DEFAULT_CONFIG: Dict[str, object] = {
    "version": "2.0.0",
    "universe": UniverseConfig(),
    "regime": MarketRegimeConfig(),
    "rs": RSConfig(),
    "tt": TrendTemplateConfig(),
    "vcp": VCPConfig(),
    "entry": EntryConfig(),
    "risk": RiskConfig(),
}
