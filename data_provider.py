"""Stage 1 & 2 Data Provider: finvizfinance + yfinance + SQLite Cache & Explicit Data Policy."""

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd


DATA_POLICY = {
    "price_adjustment": "auto_adjust=True (splits and dividends adjusted consistently)",
    "timezone": "America/New_York (US/Eastern trading calendar)",
    "missing_value_policy": "Never coerce missing fundamentals/prices to 0; mark N/A or DATA_ERROR",
}


def init_db(db_path: str = "sepa_cache.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            source TEXT NOT NULL,
            rule_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS regime_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            prev_regime TEXT,
            new_regime TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS fetch_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage TEXT NOT NULL,
            ticker TEXT,
            error_msg TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def fetch_finviz_candidates(
    filters_dict: Optional[Dict[str, str]] = None,
    max_retries: int = 2,
    sleep_sec: float = 1.0,
) -> Dict:
    """
    Stage 1: finvizfinance Screener wrapper with retry & partial result preservation.
    """
    try:
        from finvizfinance.screener.overview import Overview
    except ImportError:
        return {"ok": False, "tickers": [], "meta": {}, "error": "finvizfinance not installed"}

    filters_dict = filters_dict or {}
    last_err = None
    partial_rows = []
    for attempt in range(max_retries + 1):
        try:
            foverview = Overview()
            if filters_dict:
                foverview.set_filter(filters_dict=filters_dict)
            df = foverview.screener_view(limit=-1, verbose=0)
            if df is None or df.empty:
                return {"ok": True, "tickers": [], "meta": {}, "error": None}
            meta = {}
            for _, row in df.iterrows():
                t = str(row.get("Ticker", "")).strip()
                if t:
                    meta[t] = {
                        "ticker": t,
                        "name": row.get("Company", t),
                        "sector": row.get("Sector", "Unknown"),
                        "industry": row.get("Industry", "Unknown"),
                        "country": row.get("Country", "USA"),
                    }
            return {"ok": True, "tickers": list(meta.keys()), "meta": meta, "error": None}
        except Exception as exc:
            last_err = str(exc)
            if hasattr(exc, "partial") and getattr(exc, "partial") is not None:
                partial_rows = getattr(exc, "partial")
            time.sleep(sleep_sec * (attempt + 1))

    return {
        "ok": False,
        "tickers": [r.get("Ticker") for r in partial_rows if isinstance(r, dict) and r.get("Ticker")],
        "meta": {},
        "error": last_err,
    }


def normalize_ohlcv_df(df: pd.DataFrame) -> pd.DataFrame:
    """Handles yfinance MultiIndex columns, duplicate dates, and sorting."""
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [c[0] for c in df.columns]
    df = df.loc[~df.index.duplicated(keep="last")].sort_index()
    required = ["Open", "High", "Low", "Close", "Volume"]
    for col in required:
        if col not in df.columns:
            return pd.DataFrame()
    return df[required].dropna(subset=["Close"])


def fetch_yfinance_ohlcv_batch(tickers: List[str], period: str = "2y") -> Dict[str, pd.DataFrame]:
    """Stage 2: Batch OHLCV download using consistent auto_adjust=True."""
    import yfinance as yf

    all_symbols = list(dict.fromkeys(tickers + ["SPY", "QQQ", "IWM"]))
    raw = yf.download(
        all_symbols,
        period=period,
        interval="1d",
        auto_adjust=True,
        group_by="ticker",
        threads=True,
        progress=False,
    )
    out: Dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out

    if not isinstance(raw.columns, pd.MultiIndex):
        if len(all_symbols) == 1:
            out[all_symbols[0]] = normalize_ohlcv_df(raw)
        return out

    top_level = set(raw.columns.get_level_values(0))
    for sym in all_symbols:
        if sym in top_level:
            norm = normalize_ohlcv_df(raw[sym])
            if not norm.empty:
                out[sym] = norm
    return out


def save_snapshot(conn: sqlite3.Connection, trade_date: str, source: str, rule_version: str, payload: Dict) -> None:
    conn.execute(
        "INSERT INTO snapshots (trade_date, source, rule_version, created_at, payload_json) VALUES (?, ?, ?, ?, ?)",
        (trade_date, source, rule_version, datetime.now(timezone.utc).isoformat(), json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
