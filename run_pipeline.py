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
    tt_pass_items = sorted([x for x in analyzed_stocks if x.get("tt_status") == "PASS"], key=lambda x: x.get("system_rs_rank", 0), reverse=True)
    tt_pass_map = {x["ticker"]: x for x in tt_pass_items}

    def _fetch_sec_quarterly_eps_rev(cik_str):
        if not cik_str:
            return [], []
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

        def _extract_q_series(tag_names):
            for tag in tag_names:
                units = facts.get(tag, {}).get("units", {})
                arr = units.get("USD/shares") or units.get("USD") or []
                q_map = {}
                for row in arr:
                    if row.get("form") not in ("10-Q", "10-K"):
                        continue
                    frame = row.get("frame", "")
                    if frame.startswith("CY") and "Q" in frame and len(frame) <= 9:
                        q_map[frame] = float(row["val"])
                    elif row.get("fp") in ("Q1", "Q2", "Q3", "Q4") and row.get("start") and row.get("end"):
                        try:
                            d0 = datetime.strptime(row["start"], "%Y-%m-%d")
                            d1 = datetime.strptime(row["end"], "%Y-%m-%d")
                            if 70 <= (d1 - d0).days <= 110:
                                key = row["end"][:7]
                                q_map[key] = float(row["val"])
                        except Exception:
                            pass
                if len(q_map) >= 5:
                    keys = sorted(q_map.keys())[-8:]
                    return [q_map[k] for k in keys]
            return []

        q_eps = _extract_q_series(["EarningsPerShareDiluted", "EarningsPerShareBasic"])
        q_rev = _extract_q_series([
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ])
        return q_eps, q_rev

    def _fetch_one_fund(ticker_sym):
        time.sleep(0.15)
        cik_str = tt_pass_map[ticker_sym].get("cik")
        try:
            q_eps, q_rev = _fetch_sec_quarterly_eps_rev(cik_str)
            if len(q_eps) >= 5 or len(q_rev) >= 5:
                return ticker_sym, {}, q_eps, q_rev, None, None
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
    payload = {
        "schema_version": "2.0.0",
        "trade_date": trade_date,
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
    export_csv(analyzed_stocks, "sepa_candidates.csv")
    export_excel(payload, "sepa_report_v2.xlsx")
    return payload


if __name__ == "__main__":
    res = run_sepa_pipeline()
    print(f"SEPA V2.0 Pipeline Complete: {len(res['stocks'])} eligible stocks, Regime={res['market_regime']['regime']}")
