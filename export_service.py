"""Export Service: JSON, CSV, and Multi-Sheet Excel Workbook (openpyxl)."""

import json
from typing import Dict, List
import pandas as pd


def export_json(payload: Dict, out_path: str = "sepa_dashboard_data.json") -> str:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out_path


def export_csv(stocks: List[Dict], out_path: str = "sepa_candidates.csv") -> str:
    pd.DataFrame(stocks).to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path


def export_excel(payload: Dict, out_path: str = "sepa_report_v2.xlsx") -> str:
    """Creates multi-sheet Excel workbook: Dashboard, Candidates, All Stocks, Market Regime, Excluded, Config."""
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        summary_rows = [
            {"Metric": "Market Regime", "Value": payload.get("market_regime", {}).get("regime", "N/A")},
            {"Metric": "New Buy Policy", "Value": payload.get("market_regime", {}).get("policy", "N/A")},
            {"Metric": "Trade Date", "Value": payload.get("trade_date", "N/A")},
            {"Metric": "Total Analyzed", "Value": len(payload.get("stocks", []))},
            {"Metric": "Excluded Count", "Value": len(payload.get("excluded", []))},
        ]
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Dashboard", index=False)

        stocks = payload.get("stocks", [])
        candidates = [s for s in stocks if s.get("tt_status") == "PASS"]
        pd.DataFrame(candidates).to_excel(writer, sheet_name="Candidates", index=False)
        pd.DataFrame(stocks).to_excel(writer, sheet_name="All Stocks", index=False)
        pd.DataFrame([payload.get("market_regime", {})]).to_excel(writer, sheet_name="Market Regime", index=False)
        pd.DataFrame(payload.get("excluded", [])).to_excel(writer, sheet_name="Excluded", index=False)
    return out_path
