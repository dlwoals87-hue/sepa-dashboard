# Mark Minervini SEPA Investment Dashboard V2.0

## 1. 아키텍처 개요
`Market -> Leadership -> Stock -> Setup -> Entry -> Risk -> Position Management -> Review` 원칙에 따라 설계된 2단계 데이터 파이프라인 및 의사결정 지원 시스템입니다.

- **Stage 0 (`universe.py`)**: 비보통주(ETF/ETN/우선주), OTC, 음수 EPS, 유동성($10M/일 미만), $10 미만 종목 필터링 및 제외 사유 보존
- **Stage 1 (`data_provider.py`)**: `finvizfinance` 기반 후보 탐색(재시도·부분결과 보존)
- **Stage 2 (`data_provider.py`)**: `yfinance` 기반 일봉 OHLCV(`auto_adjust=True`, US/Eastern 거래일 기준, 252+ 거래일) 및 SQLite 스냅샷 저장
- **분석 엔진**:
  - `market_regime.py`: SPY/QQQ/IWM 및 시장 폭 기반 4단계 시장 상태(`RISK_ON`, `CAUTION`, `DEFENSIVE`, `RISK_OFF`)
  - `relative_strength.py`: 전체 유니버스 기반 `System RS Rank (1~99)` 및 `Emerging Leader` 참고 플래그
  - `trend_template.py`: TT01~TT10 Stage 2 원자 조건(`PASS`/`FAIL`/`DATA_ERROR`/`INSUFFICIENT_DATA`)
  - `fundamentals.py`: 분기 EPS·매출 YoY, 가속/둔화 판별, 실적 갭 리스크 플래그
  - `pattern_engine.py`: VCP 수축 깊이·거래량 건조·피벗 탐지(`VCP_VALIDATED`, `VCP_REVIEW_REQUIRED`, `VCP_CANDIDATE`)
  - `pivot_entry.py`: EOD 돌파 확인(거래량 1.40x) 및 D+1 시가 진입 가능성(+5% 이내) 구분
  - `risk_engine.py`: 0.5% 허용위험(1.0% 하드 상한), 7% 최대 손절폭, 환율 반영 수량 산출
  - `portfolio.py`: 물타기 금지, 손절폭 확대 금지, 총위험 재계산 피라미딩 검증
  - `journal.py`: R-multiple, 승률, 기대값(Expectancy), `LIVE` vs `SIMULATION` 분리 복기
  - `export_service.py`: `sepa_dashboard_data.json`, CSV, 다중 시트 Excel(`sepa_report_v2.xlsx`) 내보내기

## 2. 로컬 실행 방법
```bash
pip install finvizfinance yfinance pandas numpy openpyxl
python run_pipeline.py
```
실행 후 생성된 `sepa_dashboard_data.json`을 `investment_dashboard_v2.html` 우측 상단 **JSON 가져오기**로 업로드하면 실제 최신 시장 데이터로 전환됩니다.

## 3. 자동화 테스트 실행
```bash
python -m unittest discover -s tests -v
```
