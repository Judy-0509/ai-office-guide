# AI 애널리스트 사용 설명서

증권사 리포트를 날짜순으로 10건씩 학습해, 주제(topic)·주장(claim)·기업/제품(entity)을
Markdown 지식 폴더("vault", git 저장소)와 SQLite에 쌓아가는 도구입니다. Obsidian은 사내에서
쓸 수 없으므로, vault는 우리가 직접 만든 오프라인 뷰어(브라우저 창 하나)로 봅니다.

## 1. 설치

PowerShell에서:

```powershell
cd server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

PDF 리포트가 있다면 다음 중 하나를 추가로 설치하세요(둘 다 없어도 `.md`/`.txt`는 정상 동작).

- `pip install docling` — 표 구조 인식·OCR을 모두 끄고(`do_ocr=False`,
  `do_table_structure=False`) 변환합니다. 느리지만 문서 구조를 더 잘 살립니다.
- `pip install pymupdf` — docling이 없을 때만 대신 씁니다. 가볍고 빠르지만 표는 그냥 글자로
  풀립니다.
- 둘 다 없으면 `.pdf` 파일은 "PDF 변환 라이브러리(docling/pymupdf)가 없어 건너뜁니다"라는
  경고만 남기고 학습에서 제외됩니다(학습 자체는 계속 진행됩니다).

`.md`/`.txt`는 BOM(맨 앞 U+FEFF)이 있어도 문제없이 읽습니다.

## 2. `.env` 작성

`.env.example`을 복사한 뒤 아래 키를 채웁니다. **분석가가 직접 쓰는 키**만 표시했고, 나머지
(`LLM_CONTEXT_TOKENS`, `KO_TOKENS_PER_CHAR`, `LLM_MAX_CONCURRENCY`)는 다른 도구(리포트
라이브러리 스태킹, LLM 점검 스크립트)가 쓰는 값이라 분석가 동작에는 영향이 없습니다.

| 키 | 기본값 | 설명 |
|---|---:|---|
| `DATA_DIR` | (필수) | 값 자체는 실행 시 자동으로 `<vault>/.analyst`로 덮어써지므로, 분석가만 쓸 거라면 아무 값이나 넣어도 됩니다. 같은 `.env`로 다른 도구(라이브러리 스태킹 등)도 쓴다면 그 도구를 위해 실제 경로를 채워두세요. |
| `LLM_BASE_URL` | (필수) | 사내 LLM의 OpenAI 호환 주소, 예: `http://<사내 LLM 주소>/v1` |
| `LLM_API_KEY` | (필수) | 사내 LLM 키. 로그·화면·git 커밋 어디에도 남지 않습니다. |
| `LLM_MODEL_DEFAULT` | (필수) | 호출할 모델 ID |
| `LLM_TIMEOUT_SEC` | 300 | 호출 1건의 타임아웃(초). 사내 LLM은 thinking이 항상 켜져 있어 호출당 수 초~수십 초가 걸리므로, 자주 타임아웃 나면 늘리세요. |
| `LLM_TEMPERATURE` | 0.2 | 생성 온도 |
| `LLM_DISABLE_THINKING` | false | `true`면 요청에 `chat_template_kwargs.enable_thinking=false`를 실어 보냅니다. 사내 모델이 이 옵션을 실제로 지키는지는 모델마다 다릅니다. |

실행 시 `--max-tokens`(기본 20000, `python -m aioffice.analyst.run`/`learn`/`viewer` 공통
플래그)가 매 호출에 항상 명시적으로 실려가므로, **`.env`의 `LLM_MAX_TOKENS`는 분석가 호출에는
쓰이지 않습니다.** max_tokens를 바꾸려면 `.env`가 아니라 실행 커맨드의 `--max-tokens`를
바꾸세요.

| 키 | 기본값 | 없으면 어떻게 되나 |
|---|---:|---|
| `EMBED_BASE_URL` | (선택) | 비워두면 후보 주제/과거 교정 예시 검색이 문자 3-gram 코사인 유사도로 대체됩니다(네트워크 호출 없음, 정확도는 낮음). 채우면 `bge-m3` 등으로 임베딩해 코사인 유사도를 씁니다. |
| `EMBED_API_KEY` | (선택) | 비워두면 `LLM_API_KEY`를 그대로 씁니다. |
| `EMBED_MODEL` | `BAAI-bge-m3` | |
| `EMBED_BATCH_SIZE` | 32 | |
| `RERANK_BASE_URL` | (선택) | **`EMBED_BASE_URL`도 함께 채워야 실제로 쓰입니다.** `EMBED_BASE_URL`이 비어 있으면 이 값을 채워도 재랭커는 호출되지 않고 3-gram 유사도로 대체됩니다(코드 우선순위: 임베딩이 켜져 있어야 재랭커 분기를 봄). |
| `RERANK_API_KEY` | (선택) | 비워두면 `LLM_API_KEY` |
| `RERANK_MODEL` | `BAAI-bge-reranker-v2-m3` | |
| `RERANK_API_STYLE` | `cohere` | `cohere`/`tei`/`score` 중 하나만 허용 |
| `RERANK_BATCH_SIZE` | 32 | |

## 3. 실행

한 번에 실행(가장 흔한 경우) — vault 준비 + 뷰어 서버 + 앱 창 + 다음 배치 자동 학습을 한
프로세스에서 처리합니다.

```powershell
python -m aioffice.analyst.run --inbox <리포트 폴더> --vault <vault 폴더> --env .env --batch 10
```

플래그: `--inbox`(필수), `--vault`(필수), `--env`(`.env` 경로), `--batch`(배치당 리포트 수,
기본 10), `--port`(기본 8780, 사용 중이면 다음 10개 포트를 자동으로 시도), `--host`(기본
`127.0.0.1`), `--no-browser`(창을 열지 않음), `--no-learn`(시작할 때 자동 학습을 건너뜀),
`--max-tokens`(기본 20000).

시작 순서: vault 뼈대 생성(없으면 git init까지) → 학습 잠금 파일 확보 → 뷰어 서버 기동 → Edge를
표준 설치 경로에서 찾으면 앱 창(`msedge --app=...`)으로, 못 찾으면 기본 브라우저로 열기 →
`--no-learn`이 아니면 대기 중인 리포트가 있을 때 배경 스레드로 다음 배치 학습 시작. `Ctrl+C`로
종료하면 진행 중이던 리포트까지만 끝내고 서버를 닫고 잠금을 풉니다.

학습과 뷰어를 따로 띄우려면:

```powershell
python -m aioffice.analyst.learn --vault <vault> --inbox <폴더> --env .env --batch 10 --batches 1
python -m aioffice.analyst.viewer --vault <vault> --inbox <폴더> --env .env --port 8780
```

`learn`은 화면 없이 터미널에서 배치를 실행하고 한글 로그를 그대로 출력합니다(`--batches`로 여러
배치 연속 실행, 더 학습할 리포트가 없으면 "학습할 리포트가 더 없습니다"를 출력하고 멈춥니다).
`viewer`는 서버만 띄우며 자동 학습은 하지 않습니다 — 화면의 "다음 10건 학습" 버튼을 눌러야
시작됩니다. `analyst.viewer`와 `analyst.run` 둘 다 포트가 사용 중이면 다음 10개 포트를
차례로 자동 시도합니다(`--port`를 그대로 둬도 됩니다). 두 커맨드 모두 시작할 때
`analyst.sqlite` 미러를 한 번 다시 만듭니다 — 기존 vault를 열자마자 SQL로 최신 상태를 조회할
수 있습니다(7절).

## 4. 화면 설명

### 상단바
왼쪽부터 칩(chip) 4개(학습 N건 — `status=learned`인 리포트 수, 대기 N건 — inbox에서 아직
학습 안 한 파일 수, 주제 N개, 주장 N개)와 모델명, 오른쪽에 **다음 10건 학습** 버튼(학습 중이면
비활성화)과 학습 중에만 나타나는 **중지** 버튼(누르면 지금 처리 중인 리포트까지만 끝내고
멈춤).

### 사이드바
검색창(전체 항목 실시간 필터) 아래 5개 접이식 섹션: 개요(개요/학습 기록 고정 2개), 주제(주장
수 기준 내림차순), 리포트(날짜 내림차순, 왼쪽에 검토 상태 점 — 회색 없음/초록 맞음/amber
수정됨/빨강 틀림, 되돌려진 리포트는 제목에 취소선), 기업·제품(언급 횟수순), 배치 기록. 클릭하면
문서 탭에서 그 페이지가 열립니다.

### 문서 탭
왼쪽에 vault의 마크다운을 렌더링합니다(위키링크 `[[경로|라벨]]` 클릭 시 이동, `{새로움}` 
`{강화}` `{수정}` `{반대}` `{무효}` 뱃지는 색깔 알약으로 표시). 리포트 페이지를 열면 오른쪽에
"이 리포트가 바꾼 것" 패널이 자동으로 뜹니다:

- 주장 목록(관계 뱃지 + 문장 + 숫자 확인 필요 경고 + **수정** 버튼)
- 무효화된 주장(취소선 + 원래 주제명)
- 주제 변화(새로 만든 주제 / 갱신된 주제 링크)
- 검토 상태와 버튼 **맞음** / **틀림(되돌리기)**

버튼 효과(코드 기준, 정확히):

- **맞음**: `report.review`를 `"ok"`로만 표시하고 SQLite 미러를 다시 만듭니다. **git 커밋은
  하지 않습니다**(파일 내용이 바뀌지 않으므로).
- **틀림(되돌리기)**: 클릭하면 "정말 되돌리시겠습니까?" 확인이 뜨고, 예를 눌러야 실행됩니다.
  실행 내용: 이 리포트가 무효화했던 기존 주장을 모두 유효로 복원 → 이 리포트가 만든 관계의
  증거를 제거(증거가 하나도 안 남는 관계는 삭제) → 이 리포트의 주장을 전부 삭제 → 이 리포트가
  새로 만든 주제 중 다른 리포트의 주장이 하나도 안 남은 주제는 삭제 → 영향받은 나머지 주제는
  즉시 재정리(consolidate) 호출로 요약 다시 생성 → `report.status="rolled_back"`,
  `review="wrong"` → 이 리포트의 주장이 언급했던 기업·제품 페이지(`entities/*.md`)도 함께
  다시 씀(그 기업 언급이 없어졌으니 갱신) → git 커밋 `되돌림: <날짜> <증권사> <제목>`.
- **수정**(주장 하나씩): 해당 주장 카드가 텍스트/주제 선택 입력으로 바뀌고, 저장하면 그
  주장의 텍스트·주제를 바꾸고 `feedback.jsonl`에 원본/교정본을 남기며(다음 통합 호출의 참고
  예시로 재사용), `report.review="fixed"`로 표시, 관련 리포트/주제 페이지와 그 주장이 언급한
  기업·제품 페이지(`entities/*.md`)를 다시 쓰고 git 커밋 `검토: <리포트 id 앞 8자> 교정`.

### 그래프 탭
캔버스에 힘-방향(force-directed) 그래프를 그립니다(반발력 + 스프링, 노드 약 1500개까지는
매끄럽게 동작). 노드 색: 주제=보라, 기업·제품=파랑, 리포트=회색. 노드 크기는 유효 주장
수/언급 횟수. 무효화된 주제나 되돌려진 리포트는 반투명으로 흐리게 표시됩니다. 위쪽 체크박스로
주제/기업·제품/리포트를 켜고 끕니다. 엣지 범례: about·mentions(연회색), related(보라),
causes(검정, 화살표), affects(진회색, 화살표), contradicts(빨강 점선). 오른쪽 **기준일**
슬라이더 + 재생 버튼(▶/❚❚)으로 하루씩 재생하며 그 날짜까지 있던 노드/엣지만 보여줍니다. 휠로
확대·축소, 드래그로 화면 이동 또는 노드 위치 옮기기, 클릭하면 그 문서로 이동합니다.

### 실시간 학습 패널(하단)
헤더를 클릭하면 접고 펼칩니다. 표 컬럼: 순서·날짜·증권사·제목·상태(대기/추출 중…/통합
중…/완료 ✓/오류 ✕)·추출 초·통합 초·주장(건수)·숫자 확인(대조 실패 건수)·판정(새로움/강화/
수정/반대 뱃지, 0건인 종류는 숨김). 완료된 행을 클릭하면 그 리포트 문서로 이동합니다. 아래
"이벤트 로그" 토글을 열면 `log.md`와 같은 원문 로그 줄을 볼 수 있습니다.

**강제 종료된 배치를 다시 열었을 때**: 학습 도중 프로세스가 (정상적인 `Ctrl+C`가 아니라)
강제로 죽으면, 그 배치에서 "추출 중…"/"통합 중…"에 멈춰있던 행은 다음에 뷰어를 열었을 때
자동으로 상태 **오류(✕)**로 표시되고 오류 사유는 `중단됨`으로 기록됩니다(현재 아무 학습도
돌고 있지 않을 때만 이렇게 재계산됩니다 — `GET /api/status`가 매번 이벤트 로그를 다시 읽어
판단하며, 표에 사유 문구 자체가 별도 칸으로 보이지는 않습니다). 그 리포트는 vault에
저장되기 전에 멈춘 것이라 다음 배치를 실행하면 처음부터 다시 학습됩니다.

## 5. 학습 과정

리포트 1건:

1. **문장 번호 매기기**: 문단 단위로 나눈 뒤 문장 종결부호(`. ! ? 。`) 기준으로 쪼개고, 15자
   미만 조각은 버리고, 최대 12,000자까지만 남겨 `[S1] ... [S2] ...` 형태로 번호를 붙입니다.
2. **추출 호출**(LLM 1회): 번호 매긴 문장 전체를 주고 핵심 주장 3~7개를 JSON으로 요청합니다.
   호출 자체는 실패 시 자동으로 1번만 재시도합니다(최대 2회 시도) — **그래도 실패하면 이
   리포트 하나만 `status="error"`로 표시되고, 오류 이벤트(`error {report_id, text}`)가
   그 행에 남을 뿐 배치는 계속 진행됩니다.** 이 리포트는 아무것도 커밋되지 않고, 다음
   배치를 실행할 때 대기 목록에 다시 나타나 처음부터 재시도됩니다(같은 리포트가 다시 실패해도
   이전 오류 기록을 덮어쓸 뿐 중복으로 쌓이지 않습니다).
3. **숫자 대조**: 인용 문장 번호가 하나도 안 남은 주장은 버리고, 값(숫자)이 있는 주장은 인용된
   문장에 그 숫자가 그대로 들어있는지 대조합니다(불일치해도 주장은 지우지 않고 "숫자 확인
   필요"로만 표시).
4. **관련 주제/과거 교정 찾기**: 기존 주제(이름+요약+최근 주장 5개)와 이번 리포트 주장 전체를
   비교해 후보 주제 상위 8개를 고릅니다. `EMBED_BASE_URL`이 있으면 임베딩 코사인(재랭커까지
   있으면 재랭커 점수), 둘 다 없으면 문자 3-gram 코사인. `feedback.jsonl`에서도 같은 방식으로
   비슷한 과거 교정 최대 3개를 고릅니다.
5. **통합 호출**(LLM 1~2회): 주장마다 기존 주제에 붙일지 새 주제를 만들지, 관계(새로움/강화/
   수정/반대), 무효화할 기존 주장(target), 주제 간 관계까지 한 번에 JSON으로 요청합니다.
   결과 형태가 이상하면(주장 하나라도 결정이 없거나 topic 형식이 틀리면) 최대 2번 다시
   시도하고(각 시도 안에서 추가로 최대 1회 자동 재시도가 있어 최악의 경우 호출 4회), 그래도
   안 되면 **모든 주장을 "미분류" 주제의 새 주장으로 넣는 폴백**을 쓰고 오류를 남깁니다.
6. **무효화**: 관계가 "수정"/"반대"이고 대상 주장이 있으면 그 기존 주장을 `valid=false`,
   `invalid_at=이 리포트 날짜`로 표시합니다. **삭제하지 않습니다** — 무효 표시만 합니다.
7. **페이지·git 기록**: 리포트 페이지(`reports/*.md`)와 이번 리포트가 언급한 기업/제품
   페이지(`entities/*.md`, 전체를 다시 계산해 덮어씀)를 쓰고, `index.md`도 이 리포트 1건
   기준으로 즉시 다시 씀(배치가 끝날 때까지 기다리지 않고 리포트마다 갱신 — 진행 중에도
   개요 숫자가 최신으로 보입니다), git 커밋 `학습: <날짜> <증권사> <제목>`(추출 실패로
   `status="error"`가 된 리포트는 이 커밋을 건너뜁니다).

배치 전체(리포트 여러 건이 끝난 뒤 1회): 이번 배치에서 새로 만들었거나 갱신된 주제만 모아
**정리(consolidate) 호출**을 보냅니다. 한 번에 최대 12개 주제씩 묶어 요약/추세를 다시
씁니다 — 한 묶음이 실패해도 나머지는 계속 진행되고, 실패한 묶음의 주제는 이전 요약을 그대로
유지합니다. 주제 페이지들과 `index.md`, `batches/NNN.md`를 쓰고 git 커밋
`배치 NNN 정리`.

**소요 시간**: 사내 LLM은 한 번에 한 요청만 처리하고 호출당 대략 12초 안팎이 걸린다고 가정하면,
리포트 1건은 최소 호출 2회(추출 1 + 통합 1) ≈ 25~40초, 10건 배치는 여기에 배치 정리 호출
몇 초를 더해 대략 4~7분 정도로 잡으면 됩니다. 실측치는 화면의 "예상 남은 시간"을 보세요.

## 6. 지식 폴더 구조

```
<vault>/
  index.md                        개요: 학습한 리포트 수, 주제 수, 최근 바뀐 판단 top 10, 최근 배치 5개
  log.md                          학습 기록(append-only, 새 줄이 맨 아래에 추가됨)
  reports/<날짜>_<증권사slug>_<id8자>.md   리포트별 학습 노트
  topics/<topic_id>.md            주제 페이지(t001, t002, ...)
  entities/<안전한 이름>.md        회사·제품·부품 페이지
  batches/<NNN>.md                배치 기록(새 주제/판단 갱신/무효화/새 관계/리포트별 요약)
  .analyst/                       기계 상태(JSON/JSONL, git으로 diff 가능)
    reports.jsonl claims.jsonl topics.json relations.jsonl feedback.jsonl events.jsonl
    learn.lock                    학습 중에만 존재하는 잠금 파일(git에는 안 올라감)
  analyst.sqlite                  아래 6절의 조회 전용 SQLite 미러 + LLM 호출 로그(git에는 안 올라감)
  .gitignore                      analyst.sqlite* 와 learn.lock 을 커밋에서 제외
```

vault는 그 자체로 하나의 git 저장소입니다(처음 만들 때 자동으로 `git init`, 커밋 계정은
`AI Office Analyst <analyst@local>`로 고정). git이 없는 PC라면 경고만 남기고 커밋 없이
동작합니다.

## 7. `analyst.sqlite` 표 설명

`.analyst/*.jsonl`이 항상 최종 진실이고, 이 DB는 리포트/배치/검토/교정마다 통째로 지우고
다시 채우는(drop + recreate, 트랜잭션 1개) **조회 전용** 미러입니다. `analyst.viewer`나
`analyst.run`을 시작할 때도 한 번 다시 만들어지므로, 기존 vault를 열자마자(아직 아무것도
학습하기 전이라도) SQL로 최신 상태를 조회할 수 있습니다. `LLMClient`의
`llm_calls`/`embeddings` 테이블과 같은 파일에 들어있고, 이 미러는 그 두 테이블을 절대 건드리지
않습니다.

| 테이블 | 주요 컬럼 |
|---|---|
| `reports` | id, path, broker, title, date, date_source, status(`learned`/`rolled_back`), review, fictional |
| `claims` | id, report_id/report_date/broker, text, type, direction, metric/value/unit/period, quote, number_ok, topic_id, relation, target_claim_id, valid, invalid_at, invalidated_by |
| `topics` | id, name, summary, trend, created_report_id, created_date |
| `topic_summaries` | topic_id, date, summary, trend — consolidate가 쌓는 판단 변화 이력 |
| `relations` | src, dst, kind(`causes`/`affects`/`contradicts`/`related`), why, first_seen, last_seen, evidence(JSON 배열) |
| `feedback` | report_id, claim_id, text(원본), corrected_text, corrected_topic_id |
| `llm_calls` | (공통 스키마) task_id, agent, step, backend, model, prompt_tokens, completion_tokens, latency_ms, status, error, created_at — 이 미러는 건드리지 않음 |

예시 쿼리(`sqlite3 <vault>/analyst.sqlite`로 접속):

```sql
-- 이번 주 무효화된 주장
SELECT text, broker, invalid_at FROM claims
WHERE valid = 0 AND invalid_at >= date('now', '-7 day');

-- 주제별 주장 수
SELECT t.name, COUNT(*) AS n FROM claims c
JOIN topics t ON t.id = c.topic_id
GROUP BY t.name ORDER BY n DESC;

-- 특정 증권사가 이번 달 낸 리포트
SELECT date, title FROM reports
WHERE broker = 'A증권' AND date >= '2026-09-01' ORDER BY date;
```

## 8. git 기록 활용

vault 폴더 자체가 git 저장소이므로 그 안에서 바로 씁니다.

```powershell
cd <vault>
git log --oneline
git log -3
git show <커밋>:reports/<파일명>.md
```

커밋 메시지 규칙: 리포트 1건마다 `학습: <날짜> <증권사> <제목>`, 배치 종료마다
`배치 NNN 정리`, 주장 교정마다 `검토: <리포트 id 앞 8자> 교정`, 되돌리기마다
`되돌림: <날짜> <증권사> <제목>`. **"맞음" 검토는 커밋을 만들지 않습니다**(4절 참고).

## 9. 문제 해결

- **포트가 사용 중**: `analyst.run`과 `analyst.viewer` 모두 자동으로 다음 10개 포트를 차례로
  시도하고 실제 URL을 출력합니다. 10개 다 사용 중이면 그때만 `--port`를 바꿔 다시 실행하세요.
- **"다른 학습이 이미 실행 중입니다" (lock 오류)**: `.analyst/learn.lock`이 남아있다는
  뜻입니다. 정말 다른 프로세스가 없는지 먼저 확인한 뒤에만(작업 관리자 등) 그 파일을
  지우고 다시 실행하세요. 정상 종료(`Ctrl+C`)하면 자동으로 지워집니다.
- **LLM 타임아웃**: `.env`의 `LLM_TIMEOUT_SEC`(기본 300초)을 늘리세요.
- **응답이 잘림(truncated)**: 실행 커맨드의 `--max-tokens`(기본 20000)를 늘려 다시
  실행하세요. `.env`의 `LLM_MAX_TOKENS`는 분석가에 쓰이지 않습니다(2절 참고). 이미 학습된
  리포트는 다시 실행해도 건너뛰므로 안심하고 재실행할 수 있습니다.
- **JSON 파싱/형식 실패**: 통합 단계는 재시도 후에도 실패하면 "미분류" 주제로 자동
  폴백됩니다. 추출 단계는 재시도 후에도 실패하면 그 리포트만 `status="error"`로 남고 배치는
  계속되며, 다음 배치 때 자동으로 재시도됩니다(5절) — 둘 다 배치 전체를 멈추지 않습니다.
- **강제 종료 후 화면에 "오류"로 남은 행**: 4절 "실시간 학습 패널" 참고 — 그 리포트는 아직
  저장되지 않았으므로 다음 배치를 실행하면 자동으로 다시 학습됩니다. 별도 조치가 필요 없습니다.
- **느릴 때**: 사내 LLM은 한 번에 한 요청만 처리하므로(`LLM_MAX_CONCURRENCY`는 분석가에 영향
  없음), 리포트당 호출 2회 이상이 순차로 걸리는 것이 정상입니다. 임베딩/재랭커 없이 돌리면
  후보 주제 검색 정확도는 낮아지지만 네트워크 호출이 없어 오히려 더 빠릅니다.

## 10. 지식 조회 (API·MCP)

학습 내용을 프로그램(사내 대시보드 등)과 AI 코딩 에이전트(OpenCode) 양쪽에서 쓸 수 있도록,
같은 읽기 전용 함수 6개를 HTTP API와 MCP 두 door로 노출합니다. 둘 다 `<vault>/analyst.sqlite`
(7절)만 읽고 아무것도 바꾸지 않으며, LLM은 검색 순위를 매길 때만(설정된 경우) 쓰입니다 — 답을
새로 생성하지 않습니다.

### 실행 방법

1. **뷰어에 이미 마운트되어 있음**: `analyst.run`/`analyst.viewer`를 띄우면 같은 포트에서
   `/api/v1/...`도 바로 응답합니다(추가 인증 없음 — 뷰어 자체가 127.0.0.1 기본 바인딩).
2. **독립 실행(읽기 전용)**:
   ```powershell
   python -m aioffice.analyst.api --vault <vault> --env .env --port 8790
   ```
3. **MCP(OpenCode 등 에이전트용, stdio)**:
   ```powershell
   python -m aioffice.analyst.mcp --vault <vault> --env .env
   ```

### 엔드포인트

| 엔드포인트 | 파라미터 | 설명 |
|---|---|---|
| `GET /api/v1` | (없음) | 엔드포인트 목록(파라미터 포함, 자기서술형) |
| `GET /api/v1/overview` | `as_of` | 리포트/유효·무효 주장/주제/관계 수, 학습 기간, 상위 주제, 최근 배치 |
| `GET /api/v1/search` | `query`(필수), `k`, `date_from`, `date_to` | 주제·주장 검색(임베딩+재랭커 설정 시 사용, 아니면 3-gram) — LLM 생성 없음 |
| `GET /api/v1/topics` | (없음) | 주제 목록: id, 이름, 유효 주장 수, 추세, 최근 갱신일 |
| `GET /api/v1/topics/<id 또는 이름>` | `as_of` | 주제 상세: 요약·추세, 유효/무효 주장(출처 포함), 관련 주제 |
| `GET /api/v1/changes` | `date_from`, `date_to`(둘 다 필수) | 기간 내 변화: 학습된 리포트, 새 주제, 판단 변화(이전→이후), 무효화, 신규/갱신 관계 |
| `GET /api/v1/claims` | `entity`, `metric`, `broker`, `type`, `topic`, `date_from`, `date_to`, `valid`(`valid`/`invalid`/`all`, 기본 `valid`), `numeric_only`, `limit`(기본 200), `offset` | 조건별 주장 목록(대시보드용 평탄화된 행, 출처 포함). `entity`/`metric`은 대소문자 구분 없는 부분일치 |
| `GET /api/v1/metric_history` | `entity`(필수), `metric`(필수), `period` | 증권사별 수치 변경 이력(수정 체인) — 예: "A증권 힌지 수율 72%→65%→70%" |

날짜는 모두 `YYYY-MM-DD`. 에러는 `{"error": "..."}` + 400(파라미터 누락)/404(vault·주제 없음)로
응답합니다. 리포트에서 나온 항목에는 항상 출처(citation)가 붙습니다:
`{"report_id", "report_path", "date", "broker", "title", "quote"}` (인용문은 300자로 자름).

### 예시 응답 (가상 데모 vault 기준, 일부 생략)

```
GET /api/v1/overview
{"reports": 14, "claims_valid": 67, "claims_invalid": 25, "topics": 9, "relations": 10,
 "date_range": ["2026-07-01", "2026-08-14"],
 "top_topics": [{"id": "t009", "name": "온디바이스 AI 스마트폰", "valid_claims": 16}, …],
 "last_batch": {"n": 1, "path": "batches/001.md"}}
```

```
GET /api/v1/claims?metric=수율&limit=2
{"rows": [{"id": "70fbe550-c3", "date": "2026-08-05", "broker": "A증권",
           "text": "힌지 협력사 한 곳은 자체 수율이 70% 수준이라고 밝혔다",
           "metric": "자체 수율", "value": "70", "unit": "%",
           "topic": {"id": "t004", "name": "폴더블 공급 개선"},
           "citation": {"report_path": "reports/2026-08-05_a_70fbe550.md", …}}, …],
 "total": 5}
```

### 대시보드 연동

사내 대시보드가 지금은 vault의 markdown/숫자를 손으로 파싱한다면, 대신 이 API를 호출하도록
바꾸세요. `server/scripts/dashboard_client_example.py`(표준 라이브러리 `urllib`만 사용, 약
40줄)가 `overview` → `claims(metric=…)` → `metric_history`를 차례로 불러 작은 표를 찍는
전체 흐름을 보여줍니다. 실행:

```powershell
python server/scripts/dashboard_client_example.py --base-url http://127.0.0.1:8790 --metric 가격
```

핵심은 세 줄뿐입니다(표준 라이브러리 `urllib.request`):

```python
import json, urllib.request
def get(base_url, path):
    with urllib.request.urlopen(f"{base_url}{path}") as r:
        return json.loads(r.read().decode("utf-8"))
rows = get("http://127.0.0.1:8790", "/api/v1/claims?metric=가격&limit=10")["rows"]
```

### 보안 (다른 PC에 열 때)

기본은 `127.0.0.1`(같은 PC에서만 접속 가능, 인증 없음). `--host`를 loopback이 아닌 값(사내
다른 PC에서도 접속하게 열 때)으로 주면 **`--token`이 없으면 서버가 아예 시작되지 않습니다**.
토큰을 주면 모든 요청에 `Authorization: Bearer <token>` 또는 `X-API-Key: <token>` 헤더가
필요하고, 없거나 틀리면 401입니다. 브라우저 대시보드에서 직접 호출한다면 `--cors <origin>`으로
그 origin 하나만 CORS를 허용하세요(기본은 CORS 헤더 없음). GET만 지원하며, 이 API는 `.env`나
vault의 다른 어떤 파일도 서빙하지 않습니다.

```powershell
python -m aioffice.analyst.api --vault <vault> --host 0.0.0.0 --port 8790 --token <임의의-긴-문자열> --cors http://dashboard.internal
```

### MCP 등록 (OpenCode)

`opencode.json`에 추가(키 이름은 설치된 OpenCode 버전에 맞게 확인하세요):

```json
{
  "mcp": {
    "ai-analyst": {
      "type": "local",
      "command": ["python", "-m", "aioffice.analyst.mcp", "--vault", "<vault 경로>", "--env", "<.env 경로>"],
      "enabled": true
    }
  }
}
```

도구 6개: `knowledge_overview`(전체 현황, 대화 시작 시), `search_knowledge`(자연어 검색),
`get_topic`(주제 상세), `what_changed`(기간 내 변화), `find_claims`(조건별 주장 목록),
`metric_history`(지표 변경 이력). 각 도구 설명에 언제 쓸지, 출처(`report_path`) 인용 필수,
숫자는 도구 결과만 사용(추측 금지)이 한글+영어로 적혀 있습니다. 목록/체인 길이는 모델 컨텍스트를
위해 자동으로 짧게 잘립니다.

지식 API/MCP로 조회한 내용도 아래 11절 "외부 반출 규칙"과 동일하게 취급하세요 —
`citation`/`quote`에 실제 리포트 문장이 들어있으므로, API 응답을 그대로 회사 밖에 보내지
마세요.

## 11. 외부 반출 규칙

**나가도 되는 것**: `llm_bench.py` 결과(숫자·라벨만 담긴 `bench_result.json`), 오류의 종류와
메시지(리포트 문장이 섞여 있지 않은지 먼저 확인), 리포트/주제/주장 **건수**와 처리 소요 시간.

**나가면 안 되는 것**: vault 전체 또는 일부(리포트 원문, 주장 텍스트, 주제 요약 — 전부 실제
리포트 내용을 담고 있음), `reports/`·`topics/`·`entities/`·`batches/` 아래 파일, `analyst.sqlite`
(claims/reports 텍스트 포함), `.analyst/*.jsonl`, 제목·증권사명이 들어간 git 커밋 메시지.
