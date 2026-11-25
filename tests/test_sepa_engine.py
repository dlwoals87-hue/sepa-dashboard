"""Automated Boundary, Data Reliability & Regression Tests for SEPA V2.0."""

import os
import sys
import tempfile
import unittest
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import EntryConfig, MarketRegimeConfig, RSConfig, RiskConfig, TrendTemplateConfig, UniverseConfig, VCPConfig
from export_service import export_csv, export_excel, export_json
from fundamentals import analyze_fundamentals
from journal import compute_trade_r, summarize_journal
from market_regime import evaluate_market_regime
from pattern_engine import detect_vcp_pattern
from pivot_entry import evaluate_entry_status
from portfolio import evaluate_pyramid_addon
from relative_strength import compute_universe_rs
from risk_engine import calculate_position_plan
from trend_template import evaluate_trend_template
from universe import classify_universe_candidate


def make_uptrend_df(days: int = 260, start: float = 50.0, end: float = 120.0, vol: float = 1_000_000.0) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-11-20", periods=days)
    close = np.linspace(start, end, days)
    return pd.DataFrame(
        {
            "Open": close * 0.995,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": np.full(days, vol),
        },
        index=dates,
    )


class TestSepaEngine(unittest.TestCase):
    def test_universe_filtering_boundaries(self):
        cfg = UniverseConfig()
        self.assertEqual(classify_universe_candidate({"ticker": "SPY", "name": "SPDR S&P 500 ETF", "exchange": "NYSE"}, cfg)[0], "EXCLUDED")
        self.assertEqual(classify_universe_candidate({"ticker": "OTCM", "name": "Test Corp", "exchange": "OTC"}, cfg)[0], "EXCLUDED")
        self.assertEqual(classify_universe_candidate({"ticker": "UNH", "name": "UnitedHealth", "exchange": "NYSE", "sector": "Health Care", "history_days": 260}, UniverseConfig(exclude_healthcare=True))[0], "EXCLUDED")
        self.assertEqual(classify_universe_candidate({"ticker": "UNH", "name": "UnitedHealth", "exchange": "NYSE", "sector": "Health Care", "history_days": 260, "eps_ttm": 2.0, "avg_dollar_vol_50d": 2e7, "price": 50.0}, cfg)[0], "ACTIONABLE")
        self.assertEqual(classify_universe_candidate({"ticker": "LOSS", "name": "Loss Co", "exchange": "NASDAQ", "sector": "Technology", "history_days": 260, "eps_ttm": -1.2, "avg_dollar_vol_50d": 2e7, "price": 25.0}, cfg)[0], "EXCLUDED")
        self.assertEqual(classify_universe_candidate({"ticker": "WTCH", "name": "Watch Co", "exchange": "NASDAQ", "sector": "Technology", "history_days": 260, "eps_ttm": 1.0, "avg_dollar_vol_50d": 2e7, "price": 12.5}, cfg)[0], "WATCH_ONLY")
        self.assertEqual(classify_universe_candidate({"ticker": "ACTV", "name": "Active Co", "exchange": "NASDAQ", "sector": "Technology", "history_days": 260, "eps_ttm": 2.0, "avg_dollar_vol_50d": 2e7, "price": 50.0}, cfg)[0], "ACTIONABLE")
        self.assertEqual(classify_universe_candidate({"ticker": "SHRT", "name": "Short Co", "exchange": "NASDAQ", "history_days": 100}, cfg)[0], "INSUFFICIENT_DATA")

    def test_trend_template_pass_and_insufficient(self):
        cfg = TrendTemplateConfig()
        df = make_uptrend_df(260, 50.0, 120.0)
        res_pass = evaluate_trend_template(df, system_rs_rank=85, cfg=cfg)
        self.assertEqual(res_pass["tt_status"], "PASS")
        res_fail_rs = evaluate_trend_template(df, system_rs_rank=69, cfg=cfg)
        self.assertEqual(res_fail_rs["tt_status"], "FAIL")
        res_short = evaluate_trend_template(df.tail(150), system_rs_rank=85, cfg=cfg)
        self.assertEqual(res_short["tt_status"], "INSUFFICIENT_DATA")

    def test_relative_strength_full_universe(self):
        cfg = RSConfig()
        spy = make_uptrend_df(260, 400, 450)
        ohlcv = {
            "SPY": spy,
            "STRONG": make_uptrend_df(260, 50, 150),
            "MID": make_uptrend_df(260, 50, 80),
            "WEAK": make_uptrend_df(260, 100, 70),
        }
        rs = compute_universe_rs(ohlcv, spy, cfg)
        self.assertGreater(rs["STRONG"]["system_rs_rank"], rs["MID"]["system_rs_rank"])
        self.assertGreater(rs["MID"]["system_rs_rank"], rs["WEAK"]["system_rs_rank"])

    def test_market_regime_states(self):
        cfg = MarketRegimeConfig()
        bull = make_uptrend_df(260, 400, 500)
        bear = make_uptrend_df(260, 500, 350)
        self.assertEqual(evaluate_market_regime(bull, bull, bull, 65.0, 70.0, 75.0, cfg)["regime"], "RISK_ON")
        self.assertEqual(evaluate_market_regime(bear, bear, bear, 20.0, 20.0, 20.0, cfg)["regime"], "RISK_OFF")

    def test_fundamentals_acceleration_and_missing(self):
        acc = analyze_fundamentals({"quarterly_eps": [1.0, 1.0, 1.0, 1.0, 1.15, 1.25, 1.50]})
        self.assertIn("가속", acc["eps_trend"])
        missing = analyze_fundamentals({})
        self.assertEqual(missing["eps_growth_yoy"], "N/A")
        self.assertIn("EPS_DATA_UNAVAILABLE", missing["quality_flags"])

    def test_pivot_entry_volume_and_chase_boundaries(self):
        cfg = EntryConfig()
        df = make_uptrend_df(60, 90.0, 99.0, vol=1_000_000.0)
        # Breakout day with exact 1.40x volume
        df.iloc[-1, df.columns.get_loc("High")] = 102.0
        df.iloc[-1, df.columns.get_loc("Close")] = 101.0
        df.iloc[-1, df.columns.get_loc("Volume")] = 1_400_000.0

        res_ok = evaluate_entry_status(df, "PASS", "VCP_VALIDATED", 100.0, "RISK_ON", cfg, next_open=105.0)
        self.assertTrue(res_ok["eod_confirmed"])
        self.assertEqual(res_ok["d1_open_status"], "ACTIONABLE_AT_OPEN")

        res_chase = evaluate_entry_status(df, "PASS", "VCP_VALIDATED", 100.0, "RISK_ON", cfg, next_open=105.1)
        self.assertEqual(res_chase["d1_open_status"], "GAP_EXTENDED_NO_ENTRY")

        # Volume 1.39x should fail EOD confirmation
        df.iloc[-1, df.columns.get_loc("Volume")] = 1_390_000.0
        res_low_vol = evaluate_entry_status(df, "PASS", "VCP_VALIDATED", 100.0, "RISK_ON", cfg)
        self.assertFalse(res_low_vol["eod_confirmed"])

        # RISK_OFF blocks entry
        res_blocked = evaluate_entry_status(df, "PASS", "VCP_VALIDATED", 100.0, "RISK_OFF", cfg)
        self.assertEqual(res_blocked["entry_status"], "BLOCKED BY MARKET")

    def test_risk_engine_sizing_and_caps(self):
        cfg = RiskConfig(account_equity_krw=10_000_000, fx_rate_krw_per_usd=1_400, risk_per_trade_pct=0.5, slippage_bps=0.0)
        plan = calculate_position_plan(entry_price=100.0, cfg=cfg, technical_stop=93.0)
        self.assertEqual(plan["planned_shares"], 5)
        self.assertEqual(plan["risk_status"], "OK")

        # Technical stop > 7% must be rejected
        too_wide = calculate_position_plan(entry_price=100.0, cfg=cfg, technical_stop=91.0)
        self.assertEqual(too_wide["risk_status"], "TRADE_RISK_TOO_WIDE")
        self.assertEqual(too_wide["shares"], 0)

        # Hard risk cap > 1.0% clamped
        cfg_high = RiskConfig(risk_per_trade_pct=2.5, hard_risk_cap_pct=1.0)
        clamped = calculate_position_plan(entry_price=100.0, cfg=cfg_high)
        self.assertEqual(clamped["risk_status"], "CAP_CLAMPED")

    def test_portfolio_no_averaging_down(self):
        pos = {"avg_entry": 100.0, "shares": 10, "stop_price": 93.0}
        res_loser = evaluate_pyramid_addon(pos, current_price=98.0, addon_shares=5, new_stop_price=93.0, account_equity_krw=10_000_000, fx_rate=1_400)
        self.assertFalse(res_loser["allowed"])
        res_widen = evaluate_pyramid_addon(pos, current_price=110.0, addon_shares=5, new_stop_price=90.0, account_equity_krw=10_000_000, fx_rate=1_400)
        self.assertFalse(res_widen["allowed"])

    def test_journal_r_and_expectancy(self):
        self.assertEqual(compute_trade_r(100.0, 114.0, 93.0), 2.0)
        trades = [
            {"entry_price": 100.0, "exit_price": 114.0, "initial_stop": 93.0, "record_type": "LIVE", "rule_compliant": True},
            {"entry_price": 100.0, "exit_price": 93.0, "initial_stop": 93.0, "record_type": "LIVE", "rule_compliant": True},
            {"entry_price": 100.0, "exit_price": 121.0, "initial_stop": 93.0, "record_type": "SIMULATION", "rule_compliant": False},
        ]
        live_summary = summarize_journal(trades, data_mode="LIVE")
        self.assertEqual(live_summary["count"], 2)
        self.assertEqual(live_summary["win_rate_pct"], 50.0)
        self.assertEqual(live_summary["expectancy_r"], 0.5)

    def test_export_service_outputs(self):
        payload = {"trade_date": "2026-11-21", "market_regime": {"regime": "CAUTION", "policy": "SELECTIVE"}, "stocks": [{"ticker": "NVDA", "tt_status": "PASS"}], "excluded": []}
        with tempfile.TemporaryDirectory() as tmp:
            j = export_json(payload, os.path.join(tmp, "out.json"))
            c = export_csv(payload["stocks"], os.path.join(tmp, "out.csv"))
            x = export_excel(payload, os.path.join(tmp, "out.xlsx"))
            self.assertTrue(os.path.exists(j) and os.path.exists(c) and os.path.exists(x))



    def test_breakout_bar_does_not_distort_contraction(self):
        dates = pd.date_range("2026-01-01", periods=220, freq="B")
        # Prior uptrend +100% then 60D contracting base around 50 -> 58, then big breakout day to 70 on 2x volume
        closes = [25.0] * 100 + [25.0 + i * 0.5 for i in range(60)]
        # T1: 55 -> 47 (-14.5%), T2: 56 -> 51 (-8.9%), T3: 58 -> 55 (-5.2%)
        base_c = [55,54,53,51,49,47,48,50,52,53,54,55,54,53,52,53,54,55,56,55] +                  [56,55,54,53,52,51,52,53,54,55,56,55,54,53,54,55,56,56,57,56] +                  [57,56,56,55,55,56,57,58,57,56,56,57,57,58,57,57,58,58,58]
        closes = closes + base_c + [70.0]
        vols = [1000000] * 219 + [2500000]
        df = pd.DataFrame({
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": vols,
        }, index=dates[:len(closes)])
        res = detect_vcp_pattern(df, VCPConfig())
        self.assertIn(res["pattern_status"], ("VCP_VALIDATED", "VCP_REVIEW_REQUIRED"))
        self.assertLess(res["pivot"], 65.0)

if __name__ == "__main__":
    unittest.main()
