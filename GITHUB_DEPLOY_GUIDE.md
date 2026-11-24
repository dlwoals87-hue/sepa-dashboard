# GitHub + GitHub Pages 자동화 배포 가이드

1. 이 폴더의 모든 파일(`index.html`, `investment_dashboard_v2.html`, `run_pipeline.py`, `.github/workflows/update_sepa.yml` 등)을 새 GitHub 저장소에 올립니다(`git push`).
2. GitHub 저장소 상단 **Settings → Pages**에서 Branch를 `main` (또는 `dev`) `/ (root)`로 설정하고 **Save**를 누릅니다.
3. GitHub 저장소 상단 **Settings → Actions → General → Workflow permissions**에서 **Read and write permissions**를 선택하고 저장합니다.
4. **Actions** 탭에서 `Update SEPA V2.0 Market Data` → **Run workflow**를 한 번 눌러 테스트하면, 이후부터는 매일 한국시간 오전 7:30에 자동 갱신되며 아이폰·PC 어디서나 GitHub Pages 주소로 바로 접속할 수 있습니다.
