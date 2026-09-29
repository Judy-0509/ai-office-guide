# AI Office 서버

사내에서 만든 리포트 markdown 파일을 주차별 라이브러리로 쌓아 에이전트가 `INDEX.md`와 검색
MCP 도구로 바로 읽게 하는 도구, 사내 LLM 호출 모듈(`aioffice.llm`), 그리고 LLM/임베딩/리랭커
연결 점검 스크립트로 구성됩니다. 리포트의 PDF 파싱·docling 변환·면책조항 제거는 이 저장소가
아니라 사내 파이프라인이 처리하고, 그 결과물(clean markdown)을 입력으로 받습니다.

## 설치

Windows PowerShell에서:

```powershell
cd server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

`.env`에서 사내 LLM/임베딩/리랭커 주소와 모델을 설정합니다.

## .env 키

| 설정 | 기본값 | 용도 |
|---|---:|---|
| `DATA_DIR` | 필수 | `llm_calls`/`embeddings` SQLite DB 위치 |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL_DEFAULT` | 필수 | 사내 LLM 연결 |
| `LLM_TIMEOUT_SEC`, `LLM_MAX_TOKENS`, `LLM_TEMPERATURE` | 300, 8192, 0.2 | 생성 요청 시간·토큰 상한·온도 |
| `LLM_DISABLE_THINKING` | false | 모델의 thinking 비활성화 여부 |
| `LLM_CONTEXT_TOKENS`, `KO_TOKENS_PER_CHAR` | 32768, 1.0 | 입력 문맥 계산, 한글 토큰 추정 비율 |
| `LLM_MAX_CONCURRENCY` | 4 | 동시 요청 수 제한 |
| `EMBED_BASE_URL`, `EMBED_API_KEY`, `EMBED_MODEL`, `EMBED_BATCH_SIZE` | 모델 `BAAI-bge-m3`, 묶음 32 | 라이브러리 벡터 인덱스, `LLMClient.embed()` |
| `RERANK_BASE_URL`, `RERANK_API_KEY`, `RERANK_MODEL`, `RERANK_API_STYLE`, `RERANK_BATCH_SIZE` | 비활성, `LLM_API_KEY`, `BAAI-bge-reranker-v2-m3`, `cohere`, 32 | `search_reports` 재랭킹, `LLMClient.rerank()` |

## 주간 리포트 라이브러리 (스태킹)

입력은 사내 파이프라인이 만든 clean 리포트 markdown 한 개, 폴더, 또는 zip입니다. YAML front
matter가 있으면 그 필드(`broker`, `title`, `pub_date`/`date`, `companies`, `analyst`, `source`
등)를 쓰고, 없으면 제목은 첫 `#` 헤딩(또는 첫 줄), 증권사는 `brokers.yaml`의 별칭 중 본문
앞부분에서 찾은 가장 긴 것, 날짜는 본문 앞부분의 정규식 매칭, 기업은 `companies.yaml` 매칭으로
추정합니다. 자세한 규칙은 `aioffice/library/mdlibrary.py`를 참고하세요.

```powershell
python -m aioffice.tools.stack_reports <md 파일|zip|폴더> --library <라이브러리 경로> --env .env
```

라이브러리 폴더에는 `catalog.jsonl`(기계용 색인), `originals/`(원본 md), 주차별 `INDEX.md`와
`reports/*.md`(우리 front matter + 원본 본문 그대로), 검색용 `search.sqlite`가 생깁니다.
`--no-embed`로 벡터 인덱스를 건너뛸 수 있고, `--force`로 이미 쌓은 리포트도 다시 처리합니다.

검색 MCP 서버는 다음으로 실행합니다:

```powershell
python -m aioffice.tools.library_mcp --library <라이브러리 경로> --env .env
```

`search_reports(query, week=None, broker=None, k=8)`는 FTS5(trigram)와 벡터 검색을 RRF로
합치고(`RERANK_BASE_URL` 설정 시 재랭킹), 리포트 한 개당 최대 2개까지만 반환합니다.
`list_reports(week=None, broker=None, company=None)`은 카탈로그에서 목록을 반환합니다.

OpenCode `opencode.json` 등록 예시:

```json
{
  "mcp": {
    "ai-office-library": {
      "type": "local",
      "command": ["python", "-m", "aioffice.tools.library_mcp", "--library", "<library 경로>"],
      "enabled": true
    }
  }
}
```

설치된 OpenCode 버전에 따라 키 이름(`mcp`/`type`/`command`)이 다를 수 있으니 실제 등록 전에
확인하세요.

## LLM 연결 점검

```powershell
python server/scripts/llm_smoke.py --env .env
```

모델 목록, 일반 채팅, thinking 비활성화, tool_choice 구조화 출력, 컨텍스트 길이, 동시성,
임베딩, 리랭커를 순서대로 점검하고 권장 `.env` 값을 출력합니다. `--skip-context`/
`--skip-concurrency`로 오래 걸리는 점검을 건너뛸 수 있습니다.

## 사내 LLM 병렬 성능 측정

```powershell
python server/scripts/llm_bench.py --env server/.env --levels 1,5,10,20 --pause 20
```

표준 라이브러리만 사용하는 단일 파일 스크립트로, 스트리밍 응답 특성(첫 토큰까지 시간, thinking
채널 여부)과 동시 요청 수별 처리량·오류율·추출 품질(JSON 정합성 등)을 측정해 콘솔과 `--out`
JSON(기본 `bench_result.json`)에 출력합니다. 실제 리포트로 측정하려면 `--reports <폴더 또는
파일>`을 지정하고, 없으면 내장된 가상 리포트 4개로 측정합니다. 결과 파일에는 숫자와 라벨만
기록되며 리포트 본문, 모델 출력 텍스트, 파일명, API 호스트는 절대 포함되지 않습니다. 네트워크
없이 내부 로직만 점검하려면 `--selftest`를 사용하세요.

## 슬라이드 스튜디오

MI팀 주간 "이번 주 어떤 리포트를 다뤘는지" 덱을 사내 LLM(Qwen)이 Claude 슬라이드처럼 라이브로
그려주는 도구. 채팅 옆 패널에 슬라이드가 스트리밍으로 나타나고, 텍스트/표 셀/차트 값을 클릭해
바로 고칠 수 있으며, 최종 결과는 네이티브 편집 가능한 표·차트가 들어간 PPTX로 내보낸다. 서버는
Python 표준 라이브러리 `http.server`(SSE 스트리밍) 하나, 프런트엔드는 CDN·빌드 스텝 없는 단일
`index.html`이라 사내 오프라인 인트라넷 PC에서 그대로 동작한다.

```powershell
python -m aioffice.slides.server --env .env --data <저장 폴더> --port 8765
```

브라우저로 `http://127.0.0.1:8765/` 접속. `--topic`으로 다른 입력 자료 JSON을(기본은
`aioffice/slides/samples/topic_memory.json`의 가상 예시), `--deck`으로 시작 덱을 지정할 수
있다(생략하면 `<저장 폴더>/deck.json`, 그것도 없으면 견본 `deck_memory.json`). `--host`로 바인드
주소를 바꿀 수 있다(기본 `127.0.0.1`).

버튼:
- **보내기**: 덱이 비어 있으면 새로 생성, 있으면 선택된 슬라이드 기준으로 편집 지시(예: "2번
  슬라이드 제목 줄여줘")
- **새로 생성**: 덱을 비우고 처음부터 다시 생성
- **되돌리기**: 마지막 수정 취소(직전 덱 상태로 복원 후 저장)
- **PPTX 내보내기**: 현재 덱을 네이티브 표/차트가 있는 PPTX로 다운로드
- **중단**: 생성/편집 스트리밍 도중 취소

사내 LLM(Qwen3.8-27B) 관련 주의사항:
- thinking을 끌 수 없고 간단한 요청에도 과도하게 오래 생각할 수 있다고 알려져 있어, 생성/편집
  호출은 `max_tokens`를 16000으로 제한한다(`SLIDES_MAX_TOKENS` 환경변수로 조정). 프롬프트에서도
  짧게 계획하고 빨리 op 줄을 쓰라고 지시한다.
- 모델 출력은 한 줄에 op 하나인 JSON Lines 프로토콜만 사용한다 — 한 줄이 깨져도 그 줄만
  경고로 남고 나머지 슬라이드는 그대로 이어진다.
- 슬라이드 아래 "확인 필요 숫자" 같은 경고는 STATS/SOURCES에 없는 숫자를 모델이 지어냈을 가능성을
  표시하는 것으로, 화면 렌더링이나 PPTX 내보내기를 막지 않는다.
- `LLM_API_KEY`는 절대 로그·SSE 이벤트·에러 메시지에 노출되지 않는다.

로그 DB는 `<저장 폴더>/slides.sqlite`(메인 `aioffice.db`와 별도)에 `llm_calls` 테이블로 쌓인다.

## 분석가 (증권사 리포트 학습 + 지식 vault)

증권사 리포트를 10건씩 순서대로 학습해, 그 결과를 Markdown 폴더("vault", git 저장소)로 쌓아가는
AI 애널리스트. 리포트에서 주장(claim)을 뽑아 주제(topic)에 연결하고, 같은 주제에 새로운
수치·판단이 나오면 "강화/수정/반대"로 분류해 이전 주장을 무효 처리(삭제 아님, `invalid_at`
표시)한다. 사내 LLM은 named `tool_choice`가 검증되지 않아 별도의 JSON-in-content 호출
(`LLMClient.complete_json`)만 사용하고, 리포트 1건당 1~2회 LLM 호출을 순차적으로만 보낸다
(동시 요청은 사내 서버에서 줄만 서고 대기하기 때문).

가장 간단한 실행 방법(한 번에 실행): vault 초기화 + 뷰어 서버 시작 + 브라우저를 앱 창으로 열기
+ 다음 배치 자동 학습을 한 프로세스에서 처리한다.

```powershell
python -m aioffice.analyst.run --inbox <리포트 폴더> --vault <vault 폴더> --env .env --batch 10
```

Edge(`msedge --app=...`)를 표준 설치 경로에서 찾으면 앱 창으로 열고, 없으면 기본 브라우저로
연다. `--no-browser`로 창을 열지 않고, `--no-learn`으로 시작 시 자동 학습을 건너뛴다. 포트가
사용 중이면 다음 10개 포트를 차례로 시도하고 실제 URL을 출력한다. `Ctrl+C`로 종료하면 서버를
닫고 vault 잠금을 해제한다.

학습과 뷰어를 따로 실행하려면:

```powershell
python -m aioffice.analyst.learn --vault <vault 폴더> --inbox <리포트 폴더> --env .env --batch 10
python -m aioffice.analyst.viewer --vault <vault 폴더> --inbox <리포트 폴더> --env .env --port 8780
```

`--inbox` 폴더의 `.md`/`.txt`(우선) 또는 `.pdf`(docling 있으면 OCR 없이 변환, 없으면 pymupdf,
둘 다 없으면 건너뛰고 경고) 파일을 날짜순으로 학습한다. vault에는 `index.md`, `log.md`,
`reports/`, `topics/`, `entities/`, `batches/`, 기계 상태를 담은 `.analyst/*.jsonl`(git으로 diff
가능한 텍스트)가 생기고, 리포트/배치/검토마다 자동으로 git 커밋된다(git이 없으면 경고 후 커밋
없이 진행). 뷰어는 `analyst/static/viewer.html`(별도 작업)을 서빙하며 `GET/POST /api/*`로 트리·
페이지·그래프 조회, 학습 실행(SSE 진행 상황), 검토(`ok`/`wrong`→되돌림), 주장 교정을 제공한다.
`aioffice/analyst/samples/reports/`에 가상 증권사 7곳의 30건짜리 견본 리포트가 들어 있다(실제
채널 점검이 아니며 `fictional: true`로 표시됨).

### DB 표 설명 (`<vault>/analyst.sqlite`)

`.analyst/*.jsonl`이 항상 최종 진실이며, 이 DB는 리포트/배치/검토/주장 교정마다 그 상태로부터
전체를 지우고 다시 채우는(drop + recreate, 트랜잭션 1개) 조회 전용 미러다. `LLMClient`의
`llm_calls`/`embeddings` 테이블과 같은 파일에 들어 있다("DB 하나에 다 있음"). vault
`.gitignore`가 `analyst.sqlite*`(WAL 부가 파일 포함)와 `learn.lock`을 커밋에서 제외한다.

| 테이블 | 설명 |
|---|---|
| `reports` | 리포트 1건당 1행: id, path, broker, title, date, date_source, status(`learned`/`rolled_back`/`error`), review, fictional |
| `claims` | 주장 1건당 1행: report_id/report_date/broker, text, type, direction, metric/value/unit/period, quote, number_ok, topic_id, relation, target_claim_id, valid, invalid_at, invalidated_by |
| `topics` | 주제 1건당 1행: name, 현재 summary/trend, created_report_id, created_date |
| `topic_summaries` | 주제별 판단 변화 이력(consolidate가 쌓음): topic_id, date, summary, trend |
| `relations` | 주제 간 관계: src, dst, kind(`causes`/`affects`/`contradicts`/`related`), why, first_seen, last_seen, evidence(JSON 배열) |
| `feedback` | 주장 교정 이력(few-shot 예시로 재사용): report_id, claim_id, text(원본), corrected_text, corrected_topic_id |
| `llm_calls` | (`aioffice.db` 공통 스키마) LLM 호출 로그 — 이 미러가 절대 건드리지 않음 |

### 지식 조회 (HTTP API·MCP)

프로그램(사내 대시보드 등)과 AI 코딩 에이전트(OpenCode)가 학습 내용을 조회하는 두 door. 읽기
전용 함수 6개(`aioffice/analyst/knowledge.py`: `overview`, `search`, `topic`, `changes`,
`claims`, `metric_history`)를 `<vault>/analyst.sqlite`에서 읽어 그대로 공유한다 — LLM은
`search`의 순위 매기기에만(임베딩/재랭커 설정 시) 쓰이고 답을 새로 생성하지 않는다.

```powershell
# 뷰어에 이미 마운트되어 있음 (analyst.run/viewer 실행 시 자동)
# 독립 실행(읽기 전용):
python -m aioffice.analyst.api --vault <vault> --env .env --port 8790
# MCP(stdio):
python -m aioffice.analyst.mcp --vault <vault> --env .env
```

`GET /api/v1` 이 엔드포인트 목록을 자기서술형으로 반환한다(`overview`/`search`/`topics`/
`topics/<id>`/`changes`/`claims`/`metric_history`). 기본은 127.0.0.1이며, loopback이 아닌
`--host`로 열려면 `--token`이 필수(없으면 시작 거부)이고 모든 요청에
`Authorization: Bearer <token>` 또는 `X-API-Key`가 필요하다. `--cors <origin>`으로 브라우저
대시보드 하나만 허용할 수 있다. 대시보드 연동 예시는
`server/scripts/dashboard_client_example.py`(표준 라이브러리만 사용). 자세한 내용(엔드포인트
표, 예시 응답, `opencode.json` 등록)은 공개 가이드 `02-analyst-manual.md` 10절 참고.

## 데이터 플랫폼 (팀 기존 SQLite 연결 → 버전 관리 → API/대시보드/챗봇)

**엑셀을 직접 읽지 않습니다.** 팀의 엑셀 집계는 이미 사내 파이프라인이 SQLite로 만들고 있으므로,
`dataplat`은 그 SQLite를 읽기 전용으로 연결해 **표준 컬럼**(`dataset, metric, entity, region,
period, source, value, unit`)으로 매핑하는 뷰/쿼리 하나만 있으면 됩니다. 스냅샷마다 내용이
바뀌었는지 확인해 바뀐 것만 버전으로 쌓고, HTTP API(대시보드용)와 한국어 챗봇을 제공합니다.
추가 설치가 필요 없습니다(표준 라이브러리 `sqlite3` + 이미 있는 `pyyaml`만 사용).

### 1. 연결 설정 (`source.yaml`)

팀 DB에 표준 컬럼을 내는 뷰(또는 쿼리)를 하나 만들고, `source.yaml`로 가리킵니다:

```yaml
db: C:/data/team.sqlite          # source.yaml 기준 상대경로도 허용
view: v_dataplat_observations    # 또는 query: "SELECT ... FROM ..."
rename:                          # 뷰가 표준 이름과 다른 컬럼명을 쓸 때만
  broker: source
```

기존 테이블을 표준 컬럼으로 매핑/UNION하는 예시(리네임, 여러 테이블 UNION, wide→long 변환
포함)는 `aioffice/dataplat/samples/example_view.sql`과 `aioffice/dataplat/samples/source.yaml`을,
직접 손으로 쓰는 전체 안내는 공개 가이드 `03-dataplat-manual.md`를 보세요.

### 2. 스냅샷 (`snapshot`) — 버전 관리의 핵심

```powershell
python -m aioffice.dataplat.snapshot --source source.yaml --db dataplat.sqlite [--dataset NAME] [--dry-run]
```

소스 쿼리를 읽어(읽기 전용 연결) `dataset` 컬럼으로 묶고, 데이터셋별로 이전 스냅샷과 내용이
같으면(정렬된 행의 해시 비교) `skipped_duplicate`, 다르면 새 버전(load)을 `dataplat.sqlite`에
씁니다. 콘솔에 한국어 요약(적재 행 수, 추가/삭제/변경 건수)을 출력하고, 오류가 있는 데이터셋이
있어도 나머지는 계속 처리하며 종료 코드 1을 반환합니다. `--dry-run`은 DB에 쓰지 않고 리포트만
보여줍니다.

### 3. 예약 실행 (`schedule`)

```powershell
python -m aioffice.dataplat.schedule install --day MON --time 08:00 --pre "<기존 집계 명령>" --source source.yaml --db dataplat.sqlite
python -m aioffice.dataplat.schedule show
python -m aioffice.dataplat.schedule remove
```

`dataplat.sqlite` 옆에 `.cmd` 래퍼를 만들어(로그는 `dataplat_run.log`) `--pre` 명령이 성공했을
때만 스냅샷을 실행하도록 Windows 작업 스케줄러(`schtasks`)에 매주 작업으로 등록합니다. 로그온한
사용자 세션에서만 실행됩니다(`/RU` 없음 — 팀의 집계 단계가 엑셀/매핑 드라이브를 쓸 수 있어
무인 실행으로 등록하지 않습니다).

### 4. 서버 (API + 대시보드 + 챗봇)

```powershell
python -m aioffice.dataplat.server --db dataplat.sqlite --source source.yaml --site <대시보드 site/ 빌드 폴더> --pre "<기존 집계 명령>" --env .env --port 8000 --admin-token <토큰>
```

대시보드 정적 빌드(`--site`, SPA 폴백)와 API를 같은 origin에서 서빙하므로 운영 환경에서는
CORS 설정이 필요 없습니다(`--cors`는 Vite 개발 서버 전용). `127.0.0.1`이 아닌 `--host`로 열려면
`--admin-token`이 필수(없으면 시작 거부)입니다.

| 엔드포인트 | 설명 |
|---|---|
| `GET /api/catalog` | 데이터셋별 title·metrics·entities·regions·sources·units·기간·마지막 적재 |
| `GET /api/query?dataset=&metric=&entity=&region=&source=&period_from=&period_to=&version=latest\|all\|<id>&format=long\|wide&rows=&cols=` | 조회(다중값은 콤마 구분). `format=wide`일 때 `rows`/`cols`로 행·열 축 지정. 5,000행/20,000셀 초과 시 400 |
| `GET /api/history?dataset=&metric=&entity=&period=&source=` | 특정 값의 버전(스냅샷)별 변화 — 수정 이력 차트용 |
| `GET /api/loads[?dataset=]`, `GET /api/loads/<id>` | 적재 이력 목록/상세(diff 포함 리포트) |
| `POST /api/refresh {dataset?}` | `--pre` 명령(설정 시) 성공 후 스냅샷 실행(백그라운드 스레드, 동시 1건, 실행 중이면 409). `GET /api/refresh/status`로 진행 확인. **`--admin-token` 필요** |
| `POST /api/chat {message, history?}` | 챗봇(아래 참고). 동시 1건(처리 중이면 409 + 한국어 메시지) |

읽기 엔드포인트는 인증 없이 열려 있습니다(사내망 대시보드 전제). `POST /api/refresh`만
`Authorization: Bearer <token>` 또는 `X-API-Key` 헤더를 확인합니다.

### 5. 대시보드 연동

`aioffice/dataplat/clients/dataplat-client.ts`(fetch만 쓰는 프레임워크 무관 타입 클라이언트,
차트 라이브러리는 지정하지 않음)를 Vite+React+shadcn 대시보드에 복사해 쓰세요.

```tsx
import { useEffect, useState } from "react";
import { DataplatClient, WideResult } from "./dataplat-client";

const client = new DataplatClient(""); // 같은 origin이면 빈 문자열 (server.py가 site를 같이 서빙)

function ShipmentsTable() {
  const [data, setData] = useState<WideResult | null>(null);
  useEffect(() => {
    client.queryWide({ dataset: "shipments", format: "wide", rows: ["entity"], cols: ["period"] })
      .then(setData)
      .catch(console.error);
  }, []);
  if (!data) return <p>불러오는 중...</p>;
  return (/* data.columns / data.rows로 표 렌더링 */ null);
}
```

기존 정적 서버(`dataplat.server`가 대체)에서 직접 엑셀/파일을 읽던 로딩 로직이 있었다면 위
클라이언트의 `catalog()`/`queryWide()`/`history()` 호출로 바꾸면 됩니다.

### 6. 챗봇 (`dataplat/chat.py`)

LLM은 숫자나 SQL을 직접 쓰지 않고 **조회 명세(JSON)만** 채웁니다: (1) LLM 호출 없이 트라이그램
유사도로 카탈로그를 좁히고(지표·대상 최대 40개), (2) `LLMClient.complete_json`으로 조회 명세를
받고, (3) 이름을 카탈로그와 대조해 유사도 0.85 이상이면 자동 교정, 아니면 후보 5개를 담은
되물음(clarify)을 반환하고, (4) 코드가 직접 쿼리를 실행하고, (5)
`LLMClient.complete_json`으로 설명 문장 하나를 받되 **문장 속 모든 숫자가 결과 표에 있는지**
(`aioffice.analyst.steps.normalize_number` 재사용) 확인해서, 하나라도 없으면 코드가 만든
템플릿 문장(첫 값→마지막 값, 최댓값/최솟값)으로 바꿔치기합니다. 참고용 단일 페이지 챗
UI(오프라인, 외부 라이브러리 없음)가 `/chat`에서 서빙됩니다 — 팀 React 앱에 그대로 임베드하거나
다시 구현하세요.

### 보안

- 소스 SQLite는 항상 읽기 전용으로 엽니다(`sqlite3.connect(..., uri=True)` + `mode=ro`) —
  `dataplat`은 팀의 원본 DB에 쓰지 않습니다.
- `--admin-token`은 로그·콘솔에 출력되지 않습니다. `.env`의 `LLM_API_KEY`와 마찬가지로 값
  자체를 채팅·커밋에 남기지 마세요.
- `dataplat.sqlite`, `source.yaml`(팀 DB 경로가 들어 있음)은 git에 커밋하지 않는 걸 권장합니다.

## 테스트

```powershell
cd server
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

## 이전 버전

주간 이슈 카드·주제 지도·심층 분석·종합 보고서를 만들던 이전 전체 파이프라인(FastAPI 서버,
웹 UI, PDF 파싱, docling, 면책조항 제거 등)은 git 태그 `archive/v2-full-pipeline`에 보존되어
있습니다.
