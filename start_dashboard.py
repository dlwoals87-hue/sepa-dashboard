"""One-Click Local Server, Auto-Updater & On-Demand OHLCV + Financials API for SEPA V2.0."""

import functools
import http.server
import json
import os
import socket
import socketserver
import threading
import time
import pickle
import urllib.parse
import webbrowser

import yfinance as yf

from run_pipeline import run_sepa_pipeline

PORT = 8765
ALLOWED_PERIODS = {"1y", "2y", "5y", "10y", "max"}


def _extract_fin_table(stmt) -> dict:
    if stmt is None or stmt.empty:
        return {"labels": [], "revenueB": [], "netIncomeB": [], "eps": [], "epsYoY": []}
    # Columns are dates descending; reverse to chronological (oldest -> newest)
    cols = list(reversed(stmt.columns))
    labels, rev_b, ni_b, eps_list = [], [], [], []

    def _find_row(names):
        for n in names:
            if n in stmt.index:
                return stmt.loc[n]
        return None

    r_rev = _find_row(["Total Revenue", "Operating Revenue"])
    r_ni = _find_row(["Net Income", "Net Income Common Stockholders"])
    r_eps = _find_row(["Diluted EPS", "Basic EPS"])

    for col in cols:
        if hasattr(col, "month"):
            q = (col.month - 1) // 3 + 1
            lbl = f"Q{q} '{str(col.year)[2:]}"
        else:
            lbl = str(col)[:4]
        labels.append(lbl)
        rv = float(r_rev[col]) / 1e9 if (r_rev is not None and col in r_rev and r_rev[col] == r_rev[col]) else None
        nv = float(r_ni[col]) / 1e9 if (r_ni is not None and col in r_ni and r_ni[col] == r_ni[col]) else None
        ev = float(r_eps[col]) if (r_eps is not None and col in r_eps and r_eps[col] == r_eps[col]) else None
        rev_b.append(round(rv, 2) if rv is not None else None)
        ni_b.append(round(nv, 2) if nv is not None else None)
        eps_list.append(round(ev, 2) if ev is not None else None)

    return {"labels": labels, "revenueB": rev_b, "netIncomeB": ni_b, "eps": eps_list}


def _compute_yoy(vals, lag):
    out = []
    for i in range(len(vals)):
        if i < lag or vals[i] is None or vals[i - lag] is None or vals[i - lag] == 0:
            out.append(None)
        else:
            out.append(round(((vals[i] - vals[i - lag]) / abs(vals[i - lag])) * 100.0, 1))
    return out


class SepaHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/api/history":
            ticker = (qs.get("ticker", [""])[0] or "").strip().upper()
            period = (qs.get("period", ["5y"])[0] or "5y").lower()
            if not ticker or period not in ALLOWED_PERIODS:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "invalid parameters"}')
                return
            try:
                hist, spy = None, None
                if period in ("1y", "2y", "5y") and os.path.exists("ohlcv_cache.pkl"):
                    try:
                        with open("ohlcv_cache.pkl", "rb") as cf:
                            cmap = pickle.load(cf).get("ohlcv_map", {})
                        n_days = 252 if period == "1y" else (504 if period == "2y" else 1260)
                        if ticker in cmap and not cmap[ticker].empty:
                            hist = cmap[ticker].tail(n_days)
                        if "SPY" in cmap and not cmap["SPY"].empty:
                            spy = cmap["SPY"].tail(n_days)
                    except Exception:
                        hist, spy = None, None
                if hist is None or hist.empty:
                    hist = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
                if spy is None or spy.empty:
                    spy = yf.Ticker("SPY").history(period=period, interval="1d", auto_adjust=True)
                if hist is None or hist.empty:
                    raise ValueError("no data")
                candles = []
                dates, prices = [], []
                for idx, row in hist.iterrows():
                    dt = idx.strftime("%Y-%m-%d")
                    dates.append(dt)
                    c_val = round(float(row["Close"]), 2)
                    prices.append(c_val)
                    candles.append({
                        "time": dt,
                        "open": round(float(row["Open"]), 2),
                        "high": round(float(row["High"]), 2),
                        "low": round(float(row["Low"]), 2),
                        "close": c_val,
                        "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
                    })
                spy_map = {d.strftime("%Y-%m-%d"): round(float(x), 2) for d, x in zip(spy.index, spy["Close"])} if (spy is not None and not spy.empty) else {}
                spy_prices = [spy_map.get(dt) for dt in dates]
                payload = json.dumps({"ticker": ticker, "period": period, "dates": dates, "prices": prices, "candles": candles, "spy_prices": spy_prices}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(payload)
            except Exception as exc:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(exc)}).encode("utf-8"))
            return

        if parsed.path == "/api/financials":
            ticker = (qs.get("ticker", [""])[0] or "").strip().upper()
            if not ticker:
                self.send_response(400)
                self.end_headers()
                return
            try:
                tk = yf.Ticker(ticker)
                ann = _extract_fin_table(tk.income_stmt)
                ann["labels"] = [l.split(" ")[1].replace("'", "20") if "'" in l else l[:4] for l in ann["labels"]]
                qtr = _extract_fin_table(tk.quarterly_income_stmt)
                # Append TTM (sum of last 4 quarters) to Annual just like Finviz
                if len(qtr["labels"]) >= 4:
                    ttm_rev = sum(x for x in qtr["revenueB"][-4:] if x is not None)
                    ttm_ni = sum(x for x in qtr["netIncomeB"][-4:] if x is not None)
                    ttm_eps = sum(x for x in qtr["eps"][-4:] if x is not None)
                    ann["labels"].append("TTM")
                    ann["revenueB"].append(round(ttm_rev, 2))
                    ann["netIncomeB"].append(round(ttm_ni, 2))
                    ann["eps"].append(round(ttm_eps, 2))
                ann["epsYoY"] = _compute_yoy(ann["eps"], 1)
                qtr["epsYoY"] = _compute_yoy(qtr["eps"], 4)
                info_sec = (tk.info or {}).get("sector")
                payload = json.dumps({"ticker": ticker, "sector": info_sec, "annual": ann, "quarterly": qtr}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(payload)
            except Exception as exc:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(exc)}).encode("utf-8"))
            return

        super().do_GET()


def main():
    print("=== SEPA V2.0 원클릭 대시보드 런처 ===")
    json_path = "sepa_dashboard_data.json"
    needs_sector_upgrade = False
    if os.path.exists(json_path):
        try:
            with open(json_path, encoding="utf-8") as jf:
                jdata = json.load(jf)
            stks = jdata.get("stocks", [])
            tt_pass_stks = [x for x in stks if x.get("tt_status") == "PASS"]
            top10_rs = sorted(tt_pass_stks, key=lambda z: z.get("system_rs_rank", 0), reverse=True)[:20]
            no_fund = top10_rs and all((x.get("fundamentals") or {}).get("sepa_fund_badge") in (None, "미수집") for x in top10_rs)
            if stks and (sum(1 for x in stks if x.get("sector") in (None, "", "Unknown")) > len(stks) * 0.5 or "universe_stats" not in jdata or "in_ndx100" not in stks[0] or no_fund):
                needs_sector_upgrade = True
        except Exception:
            pass
    if os.path.exists(json_path) and (time.time() - os.path.getmtime(json_path)) < 43200 and not needs_sector_upgrade:
        print("오늘자 최신 데이터가 이미 준비되어 있어 0.5초 만에 대시보드를 엽니다.")
    else:
        run_sepa_pipeline()

    serve_dir = os.path.abspath("..") if os.path.exists(os.path.join("..", "investment_dashboard_v2.html")) else os.path.abspath(".")
    handler = functools.partial(SepaHandler, directory=serve_dir)
    url = f"http://127.0.0.1:{PORT}/investment_dashboard_v2.html"

    local_ip = "127.0.0.1"
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
        sock.close()
    except Exception:
        pass
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), handler) as httpd:
        print(f"PC 접속 주소: {url}")
        print(f"핸드폰 접속 주소 (같은 와이파이): http://{local_ip}:{PORT}/investment_dashboard_v2.html")
        print("종료하려면 이 창을 닫거나 Ctrl+C를 누르세요.")
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n서버를 종료합니다.")


if __name__ == "__main__":
    main()
