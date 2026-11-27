"""Main SEPA V2.0 Pipeline Runner with Dynamic Delta-Sync Cache (ohlcv_cache.pkl)."""

import json
import os
import pickle
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pandas as pd
import yfinance as yf

from config import DEFAULT_CONFIG
from data_provider import DATA_POLICY, fetch_finviz_candidates, fetch_yfinance_ohlcv_batch, init_db, normalize_ohlcv_df, save_snapshot
from export_service import export_csv, export_excel, export_json
from fundamentals import analyze_fundamentals
from market_regime import evaluate_market_regime
from pattern_engine import detect_vcp_pattern
from pivot_entry import evaluate_entry_status
from relative_strength import compute_universe_rs
from trend_template import evaluate_trend_template
from universe import filter_universe

SEC_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
ALLOWED_SEC_EXCHANGES = {"NASDAQ": "NASDAQ", "NYSE": "NYSE", "NYSE AMERICAN": "AMEX", "CBOE BZX": "AMEX"}
CACHE_FILE = "ohlcv_cache.pkl"


NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25&offset=0&download=true"


NDX100_SET = {
    "ADBE","AMD","ABNB","ALNY","GOOGL","GOOG","AMZN","AEP","AMGN","ADI","AAPL","AMAT","APP","ARM",
    "ASML","ALAB","ADSK","ADP","AXON","BKR","BKNG","AVGO","CDNS","CTAS","CSCO","CCEP","CMCSA","CEG",
    "CPRT","CRWV","COST","CRWD","CSX","DDOG","DXCM","FANG","DASH","EXC","FAST","FER","FTNT","GEHC",
    "GILD","HONA","HON","IDXX","INTC","INTU","ISRG","KDP","KLAC","LRCX","LIN","LITE","MAR","MRVL",
    "MELI","META","MCHP","MU","MSFT","MSTR","MRNA","MDLZ","MPWR","MNST","NBIS","NFLX","NVDA","NXPI",
    "ORLY","ODFL","PCAR","PLTR","PANW","PAYX","PYPL","PDD","PEP","QCOM","REGN","RKLB","ROP","ROST",
    "SNDK","STX","SHOP","SPCX","SBUX","SNPS","TMUS","TTWO","TER","TSLA","TXN","TRI","VRTX","WMT",
    "WDC","WDAY","XEL"
}


def fetch_sp500_set() -> set:
    try:
        req = urllib.request.Request(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html_txt = resp.read().decode("utf-8", errors="ignore")
        import io
        tables = pd.read_html(io.StringIO(html_txt))
        if tables and "Symbol" in tables[0].columns:
            return {str(x).strip().upper().replace(".", "-") for x in tables[0]["Symbol"].tolist()}
    except Exception:
        pass
    return set()


def fetch_nasdaq_sector_map() -> Dict[str, Dict]:
    """Fetches real Sector & Industry for ALL US stocks in 1 fast request from Nasdaq official API."""
    try:
        req = urllib.request.Request(
            NASDAQ_SCREENER_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        rows = payload.get("data", {}).get("rows", [])
        out = {}
        for r in rows:
            sym = str(r.get("symbol", "")).strip().upper()
            if sym:
                mc_raw = str(r.get("marketCap") or "").replace(",", "").replace("$", "").strip()
                try:
                    mc_b = round(float(mc_raw) / 1e9, 2) if mc_raw else None
                except Exception:
                    mc_b = None
                out[sym] = {
                    "sector": str(r.get("sector") or "Unknown").strip() or "Unknown",
                    "industry": str(r.get("industry") or "Unknown").strip() or "Unknown",
                    "market_cap_b": mc_b,
                }
        return out
    except Exception:
        return {}


def fetch_sec_all_us_universe() -> Dict[str, Dict]:
    req = urllib.request.Request(
        SEC_EXCHANGE_URL,
        headers={"User-Agent": "SEPA-Dashboard-V2/2.0 (research@example.com)"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    fields = data.get("fields", [])
    rows = data.get("data", [])
    idx_cik, idx_name, idx_ticker, idx_exch = fields.index("cik"), fields.index("name"), fields.index("ticker"), fields.index("exchange")

    meta: Dict[str, Dict] = {}
    for row in rows:
        exch_raw = str(row[idx_exch] or "").strip().upper()
        if exch_raw not in ALLOWED_SEC_EXCHANGES:
            continue
        ticker = str(row[idx_ticker] or "").strip().upper()
        name = str(row[idx_name] or ticker).strip()
        if not ticker or "-" in ticker or "." in ticker or len(ticker) > 5:
            continue
        if len(ticker) == 5 and ticker[-1] in ("W", "R", "U", "P"):
            continue
        meta[ticker] = {"cik": str(row[idx_cik]).zfill(10), "ticker": ticker, "name": name, "sector": "Unknown", "industry": "Unknown", "exchange": ALLOWED_SEC_EXCHANGES[exch_raw]}
    return meta


def fetch_yfinance_delta_batch(tickers: List[str], start_date: str) -> Dict[str, pd.DataFrame]:
    all_symbols = list(dict.fromkeys(tickers + ["SPY", "QQQ", "IWM"]))
    raw = yf.download(all_symbols, start=start_date, interval="1d", auto_adjust=True, group_by="ticker", threads=True, progress=False)
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


def load_or_update_ohlcv(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    ohlcv_map: Dict[str, pd.DataFrame] = {}
    last_date_str = None

    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "rb") as f:
                cached = pickle.load(f)
            ohlcv_map = cached.get("ohlcv_map", {})
            last_date_str = cached.get("last_date")
        except Exception:
            ohlcv_map = {}

    today_utc = datetime.now(timezone.utc).date()
    if ohlcv_map and last_date_str:
        last_dt = datetime.strptime(last_date_str, "%Y-%m-%d").date()
        delta_days = (today_utc - last_dt).days
        if delta_days <= 0:
            print(f"      로컬 캐시({last_date_str})가 이미 최신입니다. 네트워크 다운로드를 건너뜁니다.")
            return ohlcv_map
        start_str = (last_dt - timedelta(days=2)).strftime("%Y-%m-%d")
        print(f"      증분 업데이트 모드: {last_date_str} 이후 {delta_days}일치만 유동적으로 수집합니다 (start={start_str})...")
        chunk_size = 300
        total_chunks = (len(tickers) + chunk_size - 1) // chunk_size
        for i in range(0, len(tickers), chunk_size):
            chunk = tickers[i : i + chunk_size]
            print(f"      증분 배치 {(i // chunk_size) + 1}/{total_chunks} 수집 중...")
            try:
                delta_map = fetch_yfinance_delta_batch(chunk, start_str)
                for sym, new_df in delta_map.items():
                    if sym in ohlcv_map and not ohlcv_map[sym].empty:
                        merged = pd.concat([ohlcv_map[sym], new_df])
                        merged = merged.loc[~merged.index.duplicated(keep="last")].sort_index().tail(1300)
                        ohlcv_map[sym] = merged
                    else:
                        ohlcv_map[sym] = new_df
            except Exception as exc:
                print(f"      증분 배치 일부 오류(계속 진행): {exc}")
            time.sleep(0.2)
    else:
        print(f"      최초 실행: 총 {len(tickers)}개 종목 5년치 일봉 분할 다운로드 중...")
        chunk_size = 200
        total_chunks = (len(tickers) + chunk_size - 1) // chunk_size
        for i in range(0, len(tickers), chunk_size):
            chunk = tickers[i : i + chunk_size]
            print(f"      배치 {(i // chunk_size) + 1}/{total_chunks} ({len(chunk)}개 종목) 수집 중...")
            try:
                ohlcv_map.update(fetch_yfinance_ohlcv_batch(chunk, period="5y"))
            except Exception as exc:
                print(f"      배치 {(i // chunk_size) + 1} 일부 오류(계속 진행): {exc}")
            time.sleep(0.3)

    spy_df = ohlcv_map.get("SPY")
    latest_dt = spy_df.index[-1].strftime("%Y-%m-%d") if (spy_df is not None and not spy_df.empty) else today_utc.strftime("%Y-%m-%d")
    with open(CACHE_FILE, "wb") as f:
        pickle.dump({"last_date": latest_dt, "ohlcv_map": ohlcv_map}, f)
    return ohlcv_map



def build_breadth_history(history, spy_df, eligible_set, sp500_set, ndx100_set, analyzed_stocks, regime, trade_date):
    hist_path = os.path.join("data", "market_history.json")
    os.makedirs("data", exist_ok=True)
    records = {}
    if os.path.exists(hist_path):
        try:
            with open(hist_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                for row in loaded:
                    if isinstance(row, dict) and row.get("date"):
                        records[row["date"]] = row
        except Exception:
            records = {}

    if len(records) < 60 and spy_df is not None and not spy_df.empty:
        dates_idx = spy_df.tail(252).index
        date_strs = [d.strftime("%Y-%m-%d") for d in dates_idx]
        elig_b50_cnt = [0] * len(date_strs)
        elig_b50_tot = [0] * len(date_strs)
        elig_b200_cnt = [0] * len(date_strs)
        elig_b200_tot = [0] * len(date_strs)
        sp_risk_cnt = [0] * len(date_strs)
        sp_tot_cnt = [0] * len(date_strs)
        ndx_risk_cnt = [0] * len(date_strs)
        ndx_tot_cnt = [0] * len(date_strs)

        date_to_pos = {ds: idx for idx, ds in enumerate(date_strs)}
        target_tickers = set(eligible_set) | set(sp500_set) | set(ndx100_set)

        for t in target_tickers:
            df = history.get(t)
            if df is None or df.empty or "Close" not in df.columns:
                continue
            closes = df["Close"].astype(float)
            sma50 = closes.rolling(50).mean()
            sma200 = closes.rolling(200).mean()
            roll_peak = closes.rolling(504, min_periods=20).max()
            roll_low = closes.rolling(504, min_periods=20).min()

            is_elig = t in eligible_set
            is_sp = t in sp500_set
            is_ndx = t in ndx100_set

            for dt, c_val, s50, s200, pk, lw in zip(df.index, closes, sma50, sma200, roll_peak, roll_low):
                ds = dt.strftime("%Y-%m-%d")
                pos = date_to_pos.get(ds)
                if pos is None:
                    continue
                if is_elig:
                    if pd.notna(s50):
                        elig_b50_tot[pos] += 1
                        if c_val > s50:
                            elig_b50_cnt[pos] += 1
                    if pd.notna(s200):
                        elig_b200_tot[pos] += 1
                        if c_val > s200:
                            elig_b200_cnt[pos] += 1
                if is_sp or is_ndx:
                    in_5080 = False
                    if pd.notna(pk) and pd.notna(lw) and lw > 0 and pk > 0:
                        runup = (pk / lw - 1.0) * 100.0
                        dd = (c_val / pk - 1.0) * 100.0
                        if runup >= 100.0 and dd <= -10.0:
                            in_5080 = True
                    if is_sp:
                        sp_tot_cnt[pos] += 1
                        if in_5080:
                            sp_risk_cnt[pos] += 1
                    if is_ndx:
                        ndx_tot_cnt[pos] += 1
                        if in_5080:
                            ndx_risk_cnt[pos] += 1

        for pos, ds in enumerate(date_strs):
            if ds in records:
                continue
            row = {"date": ds}
            if elig_b50_tot[pos] > 0:
                row["b50"] = round((elig_b50_cnt[pos] / elig_b50_tot[pos]) * 100.0, 1)
            if elig_b200_tot[pos] > 0:
                row["b200"] = round((elig_b200_cnt[pos] / elig_b200_tot[pos]) * 100.0, 1)
            if sp_tot_cnt[pos] > 0:
                row["sp500_5080_pct"] = round((sp_risk_cnt[pos] / sp_tot_cnt[pos]) * 100.0, 1)
            if ndx_tot_cnt[pos] > 0:
                row["ndx100_5080_pct"] = round((ndx_risk_cnt[pos] / ndx_tot_cnt[pos]) * 100.0, 1)
            records[ds] = row

    sp_risk_now = sum(1 for x in analyzed_stocks if x.get("in_sp500") and x.get("rule_50_80_status") in ("고위험 (50/80 경보)", "진행중 (-50% 도달)", "초기 이탈 관찰"))
    ndx_risk_now = sum(1 for x in analyzed_stocks if x.get("in_ndx100") and x.get("rule_50_80_status") in ("고위험 (50/80 경보)", "진행중 (-50% 도달)", "초기 이탈 관찰"))
    sp_tot_now = len(sp500_set) if sp500_set else 500
    ndx_tot_now = len(ndx100_set) if ndx100_set else 101

    records[trade_date] = {
        "date": trade_date,
        "b50": regime.get("pct_above_50_sma"),
        "b200": regime.get("pct_above_200_sma"),
        "leader_pct": regime.get("leader_tt_pass_pct"),
        "sp500_5080_pct": round((sp_risk_now / sp_tot_now) * 100.0, 1) if sp_tot_now else None,
        "ndx100_5080_pct": round((ndx_risk_now / ndx_tot_now) * 100.0, 1) if ndx_tot_now else None,
    }

    ordered = [records[k] for k in sorted(records.keys())][-260:]
    try:
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(ordered, f, ensure_ascii=False)
    except Exception:
        pass
    return ordered

def run_sepa_pipeline(finviz_filters: Dict[str, str] = None, max_universe: int = 0, out_json: str = "sepa_dashboard_data.json") -> Dict:
    cfg_u = DEFAULT_CONFIG["universe"]
    cfg_r = DEFAULT_CONFIG["regime"]
    cfg_rs = DEFAULT_CONFIG["rs"]
    cfg_tt = DEFAULT_CONFIG["tt"]
    cfg_vcp = DEFAULT_CONFIG["vcp"]
    cfg_entry = DEFAULT_CONFIG["entry"]

    print("[1/4] Stage 1: SEC EDGAR 미국 전체 상장사(NYSE/NASDAQ/AMEX) 목록 확보 중...")
    try:
        meta_map = fetch_sec_all_us_universe()
        tickers = list(meta_map.keys())
        source_label = "sec_edgar_all_us+yfinance"
        sec_sectors = fetch_nasdaq_sector_map()
        if sec_sectors:
            print(f"      Nasdaq 공식 API에서 {len(sec_sectors)}개 종목 섹터/산업 매핑 완료!")
            for t, info in meta_map.items():
                if t in sec_sectors:
                    info["sector"] = sec_sectors[t]["sector"]
                    info["industry"] = sec_sectors[t]["industry"]
                    info["market_cap_b"] = sec_sectors[t].get("market_cap_b")
    except Exception:
        fv = fetch_finviz_candidates(finviz_filters or {"Index": "NASDAQ 100"}, max_retries=1)
        meta_map = fv.get("meta", {})
        tickers = fv.get("tickers", [])
        source_label = "finviz+yfinance"

    if max_universe > 0:
        tickers = tickers[:max_universe]

    print("[2/4] Stage 2: yfinance 주가 데이터 확보 중...")
    ohlcv_map = load_or_update_ohlcv(tickers)
    spy_df, qqq_df, iwm_df = ohlcv_map.get("SPY"), ohlcv_map.get("QQQ"), ohlcv_map.get("IWM")

    print("[3/4] Stage 0 유니버스 필터링 및 SEPA 엔진(RS, TT01~TT10, VCP, Regime) 계산 중...")
    raw_records: List[Dict] = []
    for t in tickers:
        df = ohlcv_map.get(t)
        meta = meta_map.get(t, {})
        if df is None or df.empty:
            raw_records.append({"ticker": t, **meta, "history_days": 0})
            continue
        close_now = float(df["Close"].iloc[-1])
        avg_dv50 = float((df["Close"] * df["Volume"]).tail(50).mean())
        raw_records.append({
            "cik": meta.get("cik"),
            "ticker": t,
            "name": meta.get("name", t),
            "sector": meta.get("sector", "Unknown"),
            "industry": meta.get("industry", "Unknown"),
            "market_cap_b": meta.get("market_cap_b"),
            "in_ndx100": t in NDX100_SET,
            "exchange": meta.get("exchange", "NASDAQ"),
            "security_type": "COMMON",
            "history_days": len(df),
            "price": round(close_now, 2),
            "avg_dollar_vol_50d": round(avg_dv50, 0),
            "eps_ttm": 1.0,
        })

    sp500_set = fetch_sp500_set()
    for r in raw_records:
        r["in_sp500"] = r["ticker"] in sp500_set
    uni = filter_universe(raw_records, cfg_u)
    rs_map = compute_universe_rs(ohlcv_map, spy_df, cfg_rs)

    above_50_cnt, above_200_cnt, total_valid = 0, 0, 0
    leader_total, leader_tt_pass = 0, 0
    analyzed_stocks = []

    for rec in uni["eligible"]:
        t = rec["ticker"]
        df = ohlcv_map.get(t)
        rs_info = rs_map.get(t, {})
        rs_rank = rs_info.get("system_rs_rank")
        tt = evaluate_trend_template(df, rs_rank, cfg_tt)
        pat = detect_vcp_pattern(df, cfg_vcp)
        if df is not None and len(df) >= 200:
            total_valid += 1
            c = df["Close"]
            if float(c.iloc[-1]) > float(c.rolling(50).mean().iloc[-1]):
                above_50_cnt += 1
            if float(c.iloc[-1]) > float(c.rolling(200).mean().iloc[-1]):
                above_200_cnt += 1
        if rs_rank is not None and rs_rank >= cfg_r.leader_rs_threshold:
            leader_total += 1
            if tt["tt_status"] == "PASS":
                leader_tt_pass += 1
        analyzed_stocks.append({**rec, **rs_info, **tt, **pat})

    pct_50 = (above_50_cnt / total_valid * 100.0) if total_valid > 0 else None
    pct_200 = (above_200_cnt / total_valid * 100.0) if total_valid > 0 else None
    leader_pct = (leader_tt_pass / leader_total * 100.0) if leader_total > 0 else None
    regime = evaluate_market_regime(spy_df, qqq_df, iwm_df, pct_50, pct_200, leader_pct, cfg_r)

    # Enrich ALL TT PASS candidates concurrently (8 threads -> ~10-15s for ~200 stocks)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    tt_pass_items = sorted(
        [x for x in analyzed_stocks if x.get("tt_status") == "PASS" or x.get("in_ndx100")],
        key=lambda x: (x.get("tt_status") == "PASS", x.get("system_rs_rank", 0)),
        reverse=True,
    )
    tt_pass_map = {x["ticker"]: x for x in tt_pass_items}

    def _fetch_sec_financials(cik_str):
        if not cik_str:
            return [], [], None
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_str}.json"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "QuantResearchApp/2.0 (contact@quant-local.org)", "Accept-Encoding": "gzip, deflate"},
        )
        import gzip
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            facts = json.loads(raw.decode("utf-8")).get("facts", {}).get("us-gaap", {})

        def _extract_q_map(tag_names):
            for tag in tag_names:
                units = facts.get(tag, {}).get("units", {})
                arr = units.get("USD/shares") or units.get("USD") or []
                q_map = {}
                for row in arr:
                    if row.get("form") not in ("10-Q", "10-K"):
                        continue
                    frame = row.get("frame", "")
                    if frame.startswith("CY") and "Q" in frame and len(frame) <= 9:
                        # CY2025Q1 -> sort key & pretty label "Q1 '25"
                        yr = frame[2:6]
                        qt = frame[6:]
                        q_map[f"{yr}-{qt}"] = (f"{qt} '{yr[2:]}", float(row["val"]))
                    elif row.get("fp") in ("Q1", "Q2", "Q3", "Q4") and row.get("start") and row.get("end"):
                        try:
                            d0 = datetime.strptime(row["start"], "%Y-%m-%d")
                            d1 = datetime.strptime(row["end"], "%Y-%m-%d")
                            if 70 <= (d1 - d0).days <= 110:
                                yr = row["end"][:4]
                                m = int(row["end"][5:7])
                                q_num = (m - 1) // 3 + 1
                                q_map[f"{yr}-Q{q_num}"] = (f"Q{q_num} '{yr[2:]}", float(row["val"]))
                        except Exception:
                            pass
                if len(q_map) >= 4:
                    return q_map
            return {}

        def _extract_a_map(tag_names):
            for tag in tag_names:
                units = facts.get(tag, {}).get("units", {})
                arr = units.get("USD/shares") or units.get("USD") or []
                a_map = {}
                for row in arr:
                    if row.get("form") != "10-K":
                        continue
                    frame = row.get("frame", "")
                    if frame.startswith("CY") and len(frame) == 6 and frame[2:].isdigit():
                        a_map[frame[2:]] = float(row["val"])
                    elif row.get("start") and row.get("end"):
                        try:
                            d0 = datetime.strptime(row["start"], "%Y-%m-%d")
                            d1 = datetime.strptime(row["end"], "%Y-%m-%d")
                            if 330 <= (d1 - d0).days <= 390:
                                a_map[row["end"][:4]] = float(row["val"])
                        except Exception:
                            pass
                if len(a_map) >= 2:
                    return a_map
            return {}

        q_eps_map = _extract_q_map(["EarningsPerShareDiluted", "EarningsPerShareBasic"])
        q_rev_map = _extract_q_map([
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ])
        q_net_map = _extract_q_map(["NetIncomeLoss", "ProfitLoss"])

        q_keys = sorted(set(q_eps_map.keys()) | set(q_rev_map.keys()))[-8:]
        q_eps = [q_eps_map[k][1] for k in q_keys if k in q_eps_map]
        q_rev = [q_rev_map[k][1] for k in q_keys if k in q_rev_map]

        q_labels = [
            (q_eps_map.get(k) or q_rev_map.get(k) or (k, 0))[0]
            for k in q_keys
        ]
        q_rev_b = [round(q_rev_map[k][1] / 1e9, 2) if k in q_rev_map else None for k in q_keys]
        q_net_b = [round(q_net_map[k][1] / 1e9, 2) if k in q_net_map else None for k in q_keys]
        q_eps_vals = [round(q_eps_map[k][1], 2) if k in q_eps_map else None for k in q_keys]
        q_eps_yoy = []
        for idx_q, k in enumerate(q_keys):
            yr_i, qt_i = k.split("-")
            prev_k = f"{int(yr_i) - 1}-{qt_i}"
            cur_v = q_eps_vals[idx_q]
            prev_v = round(q_eps_map[prev_k][1], 2) if prev_k in q_eps_map else None
            if cur_v is not None and prev_v is not None and abs(prev_v) > 1e-6:
                q_eps_yoy.append(round(((cur_v - prev_v) / abs(prev_v)) * 100.0, 1))
            else:
                q_eps_yoy.append(None)

        a_eps_map = _extract_a_map(["EarningsPerShareDiluted", "EarningsPerShareBasic"])
        a_rev_map = _extract_a_map([
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ])
        a_net_map = _extract_a_map(["NetIncomeLoss", "ProfitLoss"])
        a_keys = sorted(set(a_eps_map.keys()) | set(a_rev_map.keys()))[-4:]
        a_labels = list(a_keys)
        a_rev_b = [round(a_rev_map[k] / 1e9, 2) if k in a_rev_map else None for k in a_keys]
        a_net_b = [round(a_net_map[k] / 1e9, 2) if k in a_net_map else None for k in a_keys]
        a_eps_vals = [round(a_eps_map[k], 2) if k in a_eps_map else None for k in a_keys]
        # Append TTM if we have at least 4 quarters
        if len(q_keys) >= 4:
            last4 = q_keys[-4:]
            ttm_rev = sum(q_rev_map[k][1] for k in last4 if k in q_rev_map) if all(k in q_rev_map for k in last4) else None
            ttm_net = sum(q_net_map[k][1] for k in last4 if k in q_net_map) if all(k in q_net_map for k in last4) else None
            ttm_eps = sum(q_eps_map[k][1] for k in last4 if k in q_eps_map) if all(k in q_eps_map for k in last4) else None
            if ttm_rev is not None or ttm_eps is not None:
                a_labels.append("TTM")
                a_rev_b.append(round(ttm_rev / 1e9, 2) if ttm_rev is not None else None)
                a_net_b.append(round(ttm_net / 1e9, 2) if ttm_net is not None else None)
                a_eps_vals.append(round(ttm_eps, 2) if ttm_eps is not None else None)

        a_eps_yoy = []
        for idx_a in range(len(a_labels)):
            if idx_a > 0 and a_eps_vals[idx_a] is not None and a_eps_vals[idx_a - 1] is not None and abs(a_eps_vals[idx_a - 1]) > 1e-6:
                a_eps_yoy.append(round(((a_eps_vals[idx_a] - a_eps_vals[idx_a - 1]) / abs(a_eps_vals[idx_a - 1])) * 100.0, 1))
            else:
                a_eps_yoy.append(None)

        sec_prof = {}
        try:
            sub_url = f"https://data.sec.gov/submissions/CIK{cik_str}.json"
            sub_req = urllib.request.Request(
                sub_url,
                headers={"User-Agent": "QuantResearchApp/2.0 (contact@quant-local.org)", "Accept-Encoding": "gzip, deflate"},
            )
            with urllib.request.urlopen(sub_req, timeout=8) as sresp:
                sraw = sresp.read()
                if sresp.headers.get("Content-Encoding") == "gzip":
                    sraw = gzip.decompress(sraw)
                sdata = json.loads(sraw.decode("utf-8"))
                sec_prof = {
                    "sic_desc": sdata.get("sicDescription", ""),
                    "website": sdata.get("website", ""),
                    "state": sdata.get("stateOfIncorporation", ""),
                    "category": sdata.get("category", ""),
                }
        except Exception:
            pass
        fin_payload = {
            "ticker": "",
            "source": "SEC EDGAR XBRL",
            "profile": sec_prof,
            "annual": {"labels": a_labels, "revenueB": a_rev_b, "netIncomeB": a_net_b, "eps": a_eps_vals, "epsYoY": a_eps_yoy},
            "quarterly": {"labels": q_labels, "revenueB": q_rev_b, "netIncomeB": q_net_b, "eps": q_eps_vals, "epsYoY": q_eps_yoy},
        }
        return q_eps, q_rev, fin_payload

    def _fetch_one_fund(ticker_sym):
        time.sleep(0.15)
        cik_str = tt_pass_map[ticker_sym].get("cik")
        try:
            q_eps, q_rev, fin_payload = _fetch_sec_financials(cik_str)
            if len(q_eps) >= 4 or len(q_rev) >= 4:
                if fin_payload:
                    fin_payload["ticker"] = ticker_sym
                return ticker_sym, {}, q_eps, q_rev, None, fin_payload
        except Exception:
            pass
        # Fallback to yfinance only if SEC XBRL lacked tags (with gentle handling)
        try:
            tk = yf.Ticker(ticker_sym)
            q_stmt = tk.quarterly_income_stmt
            q_eps, q_sales = [], []
            if q_stmt is not None and not q_stmt.empty:
                cols = list(reversed(q_stmt.columns))
                eps_row = q_stmt.loc["Diluted EPS"] if "Diluted EPS" in q_stmt.index else (q_stmt.loc["Basic EPS"] if "Basic EPS" in q_stmt.index else None)
                rev_row = q_stmt.loc["Total Revenue"] if "Total Revenue" in q_stmt.index else None
                for c in cols:
                    q_eps.append(float(eps_row[c]) if (eps_row is not None and c in eps_row and eps_row[c] == eps_row[c]) else None)
                    q_sales.append(float(rev_row[c]) if (rev_row is not None and c in rev_row and rev_row[c] == rev_row[c]) else None)
            return ticker_sym, {}, q_eps, q_sales, None, None
        except Exception:
            return ticker_sym, {}, [], [], None, None

    fund_cache = {}
    if tt_pass_items:
        print(f"      Stage 3: 상승추세 합격 {len(tt_pass_items)}개 종목 분기 실적(EPS·매출) 병렬 조회 중...")
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(_fetch_one_fund, t) for t in tt_pass_map]
            for fut in as_completed(futs):
                t_sym, info, qe, qs, ae, ar = fut.result()
                fund_cache[t_sym] = (info, qe, qs, ae, ar)

    ok_fund_cnt = sum(1 for v in fund_cache.values() if v[0] or len(v[1]) >= 5 or len(v[2]) >= 5)
    if tt_pass_items:
        print(f"      Stage 3 완료: 상위 {len(tt_pass_items)}개 중 {ok_fund_cnt}개 종목 실적 확보")

    for item in analyzed_stocks:
        t = item["ticker"]
        df = ohlcv_map.get(t)
        entry = evaluate_entry_status(df, item["tt_status"], item["pattern_status"], item.get("pivot"), regime["regime"], cfg_entry)
        item.update(entry)
        # 50/80 Rule Risk Metric
        if df is not None and len(df) >= 60:
            peak_idx = df["High"].idxmax()
            peak_2y = round(float(df["High"].max()), 2)
            prior_slice = df.loc[:peak_idx]
            low_prior = round(float(prior_slice["Low"].min()), 2) if not prior_slice.empty else round(float(df["Low"].min()), 2)
            close_now = float(df["Close"].iloc[-1])
            runup_pct = round(((peak_2y - low_prior) / low_prior) * 100.0, 1) if low_prior > 0 else 0.0
            dd_pct = round(((close_now - peak_2y) / peak_2y) * 100.0, 1) if peak_2y > 0 else 0.0
            if runup_pct >= 100.0 and item.get("tt_status") == "FAIL":
                if dd_pct <= -50.0:
                    r5080 = "진행중 (-50% 도달)"
                elif dd_pct <= -15.0:
                    r5080 = "고위험 (50/80 경보)"
                elif dd_pct <= -10.0:
                    r5080 = "초기 이탈 관찰"
                else:
                    r5080 = "정상"
            else:
                r5080 = "정상"
            item["peak_2y"] = peak_2y
            item["low_prior_2y"] = low_prior
            item["prior_runup_pct"] = runup_pct
            item["drawdown_from_peak_pct"] = dd_pct
            item["rule_50_80_status"] = r5080

        fund_input = {}
        if t in fund_cache:
            info, q_eps, q_sales, ann_e, ann_r = fund_cache[t]
            if info.get("sector") and item.get("sector") in (None, "", "Unknown"):
                item["sector"] = info["sector"]
            if info.get("trailingPE"):
                item["pe"] = round(float(info["trailingPE"]), 1)
            if info.get("returnOnEquity") is not None:
                item["roe"] = round(float(info["returnOnEquity"]) * 100.0, 1)
            fund_input = {
                "quarterly_eps": q_eps if len(q_eps) >= 5 else None,
                "quarterly_sales": q_sales if len(q_sales) >= 5 else None,
                "eps_growth_yoy": round(float(info["earningsGrowth"]) * 100.0, 1) if info.get("earningsGrowth") is not None else ann_e,
                "sales_growth_yoy": round(float(info["revenueGrowth"]) * 100.0, 1) if info.get("revenueGrowth") is not None else ann_r,
            }

        fund_res = analyze_fundamentals(fund_input)
        eg = fund_res.get("eps_growth_yoy")
        sg = fund_res.get("sales_growth_yoy")
        eyoy = fund_res.get("eps_yoy_history") or []
        consec_3q = len([v for v in eyoy[-3:] if isinstance(v, (int, float)) and v >= 15.0]) >= 3
        if consec_3q and isinstance(sg, (int, float)) and sg >= 15.0:
            fund_res["sepa_fund_badge"] = "합격 (3분기↑ 15%+)"
        elif isinstance(eg, (int, float)) and isinstance(sg, (int, float)) and eg >= 15.0 and sg >= 15.0:
            fund_res["sepa_fund_badge"] = "합격 (15%+)"
        elif (isinstance(eg, (int, float)) and eg >= 15.0) or (isinstance(sg, (int, float)) and sg >= 15.0):
            fund_res["sepa_fund_badge"] = "부분충족"
        elif isinstance(eg, (int, float)) or isinstance(sg, (int, float)):
            fund_res["sepa_fund_badge"] = "미달"
        else:
            fund_res["sepa_fund_badge"] = "미수집"
        item["fundamentals"] = fund_res

        if df is not None and not df.empty:
            tail_df = df.tail(252)
            item["dates"] = [d.strftime("%Y-%m-%d") for d in tail_df.index]
            item["prices"] = [round(float(x), 2) for x in tail_df["Close"].tolist()]

    trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    breadth_history = build_breadth_history(history, spy_df, set(uni["eligible"]), sp500_set, NDX100_SET, analyzed_stocks, regime, trade_date)
    payload = {
        "breadth_history": breadth_history,
        "schema_version": "2.0.0",
        "trade_date": trade_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_policy": DATA_POLICY,
        "market_regime": regime,
        "universe_stats": {
            "total_scanned": len(tickers),
            "eligible_count": len(uni["eligible"]),
            "excluded_count": len(uni["excluded"]) + len(uni["errors"]),
            "sp500_total": len(sp500_set) if sp500_set else 500,
            "ndx100_total": len(NDX100_SET),
        },
        "spy_prices": [round(float(x), 2) for x in spy_df.tail(252)["Close"].tolist()] if (spy_df is not None and not spy_df.empty) else [],
        "stocks": analyzed_stocks,
        "excluded": uni["excluded"] + uni["errors"],
    }

    print("[4/4] JSON / CSV / Excel 저장 중...")
    conn = init_db()
    save_snapshot(conn, trade_date, source_label, "2.0.0", {"regime": regime["regime"], "count": len(analyzed_stocks)})
    export_json(payload, out_json)
    # Also copy JSON to parent folder if investment_dashboard_v2.html lives there
    parent_json = os.path.join("..", out_json)
    if os.path.exists(os.path.join("..", "investment_dashboard_v2.html")):
        export_json(payload, parent_json)
    # Export static 10Y history & financials JSONs for GitHub Pages (works without .bat!)
    os.makedirs("data/history", exist_ok=True)
    os.makedirs("data/financials", exist_ok=True)
    # Remove any legacy non-prefixed files (e.g., Windows-reserved CON.json)
    for _d in ("data/history", "data/financials"):
        for _fn in os.listdir(_d):
            if _fn.endswith(".json") and not _fn.startswith("t_"):
                try:
                    os.remove(os.path.join(_d, _fn))
                except Exception:
                    pass
    gh_targets = list(dict.fromkeys([x["ticker"] for x in analyzed_stocks if x.get("tt_status") == "PASS" or x.get("in_ndx100")]))
    if gh_targets:
        print(f"      GitHub Pages용 정적 10년 차트/실적 파일 생성 중 ({len(gh_targets)}개 주요 종목)...")
        spy_10y = yf.Ticker("SPY").history(period="10y", interval="1d", auto_adjust=True)
        spy_map_10y = {d.strftime("%Y-%m-%d"): round(float(v), 2) for d, v in zip(spy_10y.index, spy_10y["Close"])} if (spy_10y is not None and not spy_10y.empty) else {}
        for i in range(0, len(gh_targets), 50):
            chunk = gh_targets[i : i + 50]
            try:
                h10_map = fetch_yfinance_ohlcv_batch(chunk, period="10y")
                for sym in chunk:
                    hdf = h10_map.get(sym)
                    if hdf is None or hdf.empty:
                        continue
                    dts = [d.strftime("%Y-%m-%d") for d in hdf.index]
                    candles = [
                        {
                            "time": dt,
                            "open": round(float(r["Open"]), 2),
                            "high": round(float(r["High"]), 2),
                            "low": round(float(r["Low"]), 2),
                            "close": round(float(r["Close"]), 2),
                            "volume": int(r["Volume"]) if r["Volume"] == r["Volume"] else 0,
                        }
                        for dt, (_, r) in zip(dts, hdf.iterrows())
                    ]
                    sp_list = [spy_map_10y.get(dt) for dt in dts]
                    with open(f"data/history/t_{sym}.json", "w", encoding="utf-8") as hf:
                        json.dump({"ticker": sym, "dates": dts, "prices": [c["close"] for c in candles], "candles": candles, "spy_prices": sp_list}, hf, separators=(",", ":"))
            except Exception:
                pass
        # Also save financials JSON + company profile from fund_cache where available
        analyzed_by_sym = {x["ticker"]: x for x in analyzed_stocks}
        for sym in gh_targets:
            if sym in fund_cache:
                _, qe, qs, _, fin_payload = fund_cache[sym]
                if fin_payload and (fin_payload.get("quarterly", {}).get("labels") or fin_payload.get("annual", {}).get("labels")):
                    st_info = analyzed_by_sym.get(sym, {})
                    prof = fin_payload.get("profile") or {}
                    prof.setdefault("name", st_info.get("name", sym))
                    prof.setdefault("sector", st_info.get("sector", ""))
                    prof.setdefault("industry", st_info.get("industry", ""))
                    prof.setdefault("market_cap_b", st_info.get("market_cap_b"))
                    prof.setdefault("cik", st_info.get("cik", ""))
                    fin_payload["profile"] = prof
                    with open(f"data/financials/t_{sym}.json", "w", encoding="utf-8") as ff:
                        json.dump(fin_payload, ff, separators=(",", ":"))

    export_csv(analyzed_stocks, "sepa_candidates.csv")
    export_excel(payload, "sepa_report_v2.xlsx")
    return payload


if __name__ == "__main__":
    res = run_sepa_pipeline()
    print(f"SEPA V2.0 Pipeline Complete: {len(res['stocks'])} eligible stocks, Regime={res['market_regime']['regime']}")
