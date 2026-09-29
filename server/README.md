# AI Office 데이터 플랫폼

팀이 엑셀을 읽어 이미 SQLite로 집계하고 있는 파이프라인을, 다시 만들지 않고 버전 관리(주차별
비교) + HTTP API + 대시보드 + 한국어 챗봇에 연결하는 도구입니다. **엑셀을 직접 읽지 않습니다**
— 팀의 기존 SQLite를 읽기 전용으로만 열고, 표준 컬럼(`dataset, metric, entity, region, period,
source, value, unit`)을 내는 뷰/쿼리 하나만 있으면 나머지는 자동입니다.

## 설치

```powershell
cd server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

추가 설치(extras)가 필요 없습니다 — `httpx`/`pyyaml`/`numpy`(표준 의존성)만으로 동작합니다.

## `.env` 키 (챗봇이 쓰는 것만)

| 키 | 필수 | 설명 |
|---|---|---|
| `DATA_DIR` | 필수 | 설정 로더(`Settings.load`)의 공통 요구사항이라 값은 있어야 하지만, dataplat 자체는 이 경로를 쓰지 않습니다(LLM 호출 로그는 `--db`로 지정한 `dataplat.sqlite`에 함께 쌓입니다) — 쓰기 가능한 아무 경로나 넣으세요 |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL_DEFAULT` | 필수 | 사내 LLM 연결(챗봇의 spec/설명 두 호출에 사용) |
| `LLM_TIMEOUT_SEC`, `LLM_MAX_TOKENS`, `LLM_TEMPERATURE`, `LLM_DISABLE_THINKING` | 선택(기본값 있음) | 호출 시간·토큰·온도·thinking 여부 |
| `EMBED_*`, `RERANK_*` | 사용 안 함 | AI 애널리스트 전용 — dataplat 챗봇은 트라이그램 유사도만 씁니다(임베딩 호출 없음) |

## 명령 3개

```powershell
# 1) 팀 SQLite -> dataplat.sqlite 스냅샷(버전 관리)
python -m aioffice.dataplat.snapshot --source source.yaml --db dataplat.sqlite --dry-run

# 2) 매주 예약 실행 등록 (팀 기존 집계 명령과 함께)
python -m aioffice.dataplat.schedule install --day MON --time 08:00 --pre "<기존 집계 명령>" --source source.yaml --db dataplat.sqlite

# 3) API + 대시보드 + 챗봇 서버
python -m aioffice.dataplat.server --db dataplat.sqlite --source source.yaml --site <대시보드 site/ 빌드 폴더> --env .env
```

`source.yaml` 작성법(뷰 예시 포함), API 엔드포인트 표, 대시보드 연동(`dataplat-client.ts`),
검증 체크리스트는 **[`../03-dataplat-manual.md`](../03-dataplat-manual.md)**를 보세요 — 이
문서는 설치·설정만 다루는 짧은 안내입니다.

## 테스트

```powershell
cd server
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```
