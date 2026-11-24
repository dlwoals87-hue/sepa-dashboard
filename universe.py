"""Stage 0: Investment Universe Filtering with Explicit Exclusion Reasons."""

from typing import Dict, List, Tuple
from config import UniverseConfig


NON_COMMON_KEYWORDS = ("ETF", "ETN", "FUND", "TRUST", "PREFERRED", "WARRANT", "RIGHT", "UNIT")


def classify_universe_candidate(record: Dict, cfg: UniverseConfig) -> Tuple[str, str]:
    """
    Returns (status, reason):
      status in {'ACTIONABLE', 'WATCH_ONLY', 'EXCLUDED', 'DATA_ERROR', 'INSUFFICIENT_DATA'}
    """
    ticker = str(record.get("ticker", "")).strip()
    if not ticker:
        return "DATA_ERROR", "MISSING_TICKER"

    exchange = str(record.get("exchange", "")).upper()
    if exchange == "OTC" or (exchange and exchange not in cfg.allowed_exchanges):
        return "EXCLUDED", f"UNSUPPORTED_EXCHANGE:{exchange}"

    sec_type = str(record.get("security_type", "COMMON")).upper()
    name_upper = str(record.get("name", "")).upper()
    if sec_type != "COMMON" or any(kw in name_upper for kw in NON_COMMON_KEYWORDS):
        return "EXCLUDED", "NON_COMMON_STOCK"

    if cfg.exclude_spac and record.get("is_spac", False):
        return "EXCLUDED", "SPAC_EXCLUDED"

    sector = str(record.get("sector", ""))
    if cfg.exclude_healthcare and sector.lower() in ("healthcare", "health care"):
        return "EXCLUDED", "HEALTHCARE_EXCLUDED_BY_SETTING"

    history_days = record.get("history_days")
    if history_days is None:
        return "DATA_ERROR", "MISSING_HISTORY_DAYS"
    if history_days < cfg.min_history_days:
        return "INSUFFICIENT_DATA", f"HISTORY_LT_{cfg.min_history_days}D"

    eps_ttm = record.get("eps_ttm")
    if cfg.exclude_negative_eps:
        if eps_ttm is None:
            return "WATCH_ONLY", "EPS_MISSING_PROVISIONAL"
        if eps_ttm <= 0:
            return "EXCLUDED", "NEGATIVE_OR_ZERO_EPS"

    avg_dollar_vol = record.get("avg_dollar_vol_50d")
    if avg_dollar_vol is None:
        return "DATA_ERROR", "MISSING_DOLLAR_VOLUME"
    if avg_dollar_vol < cfg.min_avg_dollar_vol_50d:
        return "EXCLUDED", "LOW_LIQUIDITY_LT_10M"

    price = record.get("price")
    if price is None or price <= 0:
        return "DATA_ERROR", "INVALID_PRICE"
    if price < cfg.min_price_watch:
        return "EXCLUDED", "PRICE_LT_10"
    if price < cfg.min_price_actionable:
        return "WATCH_ONLY", "PRICE_10_TO_15_WATCH_ONLY"

    return "ACTIONABLE", "ELIGIBLE"


def filter_universe(records: List[Dict], cfg: UniverseConfig) -> Dict[str, List[Dict]]:
    actionable, watch_only, excluded, errors = [], [], [], []
    for rec in records:
        status, reason = classify_universe_candidate(rec, cfg)
        enriched = {**rec, "universe_status": status, "universe_reason": reason}
        if status == "ACTIONABLE":
            actionable.append(enriched)
        elif status == "WATCH_ONLY":
            watch_only.append(enriched)
        elif status in ("DATA_ERROR", "INSUFFICIENT_DATA"):
            errors.append(enriched)
        else:
            excluded.append(enriched)
    return {
        "actionable": actionable,
        "watch_only": watch_only,
        "excluded": excluded,
        "errors": errors,
        "eligible": actionable + watch_only,
    }
