# AGENTS.md — AI 애널리스트를 운영하는 코딩 에이전트(OpenCode)용 지침

이 문서는 사람이 아니라 **너(코딩 에이전트)**에게 하는 지시입니다. 자세한 배경·화면·DB 설명은
[`02-analyst-manual.md`](02-analyst-manual.md)를 먼저 읽으세요.

## 역할

너는 사람을 대신해 AI 애널리스트를 설치·실행·점검하고, vault(`index.md`, `topics/*.md`,
`reports/*.md` 등)를 읽어 사람의 질문에 답합니다. **학습 결과(주장·주제·관계)를 사람 몰래
바꾸지 마세요** — 바꿔야 한다면 뷰어의 검토(맞음/틀림)·교정 기능을 통해서만 하고, 무엇을
왜 바꿨는지 사람에게 보고하세요.

## 작업 순서

### 처음 설치할 때
1. `python --version`으로 3.10 이상 확인
2. `cd server; python -m venv .venv; .\.venv\Scripts\Activate.ps1; python -m pip install -e ".[dev]"`
3. PDF를 다룰 경우 `pip install docling` 또는 `pip install pymupdf` 중 하나 추가 설치
4. `Copy-Item .env.example .env` 후, 사람에게 전달받은 사내 LLM 주소/키/모델 값을 `.env`에만
   적기(채팅·로그에 키 값을 그대로 남기지 않는다)
5. `python -m pytest -q` (server/ 안에서) 통과 확인
6. 검증: `python -m aioffice.analyst.viewer --vault <새 vault> --inbox <빈 폴더> --env .env --port 8780`
   실행 → `GET http://127.0.0.1:8780/api/status`가 200으로 응답하는지 확인 → `Ctrl+C`로 종료

### "학습 돌려줘" 지시를 받았을 때(매일/매주)
1. `.analyst/learn.lock`이 남아있으면 정말 다른 학습이 실행 중인지 먼저 확인(작업 관리자 등),
   아니라고 확실할 때만 지운다
2. `python -m aioffice.analyst.run --inbox <리포트 폴더> --vault <vault> --env .env --batch 10`
   실행(이미 서버가 떠 있으면 뷰어의 "다음 10건 학습" 버튼 = `POST /api/learn {"batch":10}`과
   동일)
3. 끝나면 `GET /api/status`로 `learned`/`pending`/`topics`/`claims` 건수 확인,
   `git -C <vault> log -3`으로 최근 3개 커밋이 `학습:`/`배치 ... 정리` 형식인지 확인
4. `number_fail`(숫자 확인 필요) 건수가 있으면 **건수만** 사람에게 보고(리포트 본문 인용 금지)

### 검토 요청을 받았을 때 ("이 리포트 틀렸어", "이 주장 고쳐줘")
1. API를 직접 두드릴 때도 반드시 `POST /api/review` 또는 `POST /api/claim`만 쓴다.
   **`.analyst/*.jsonl`을 손으로 편집하지 않는다.**
2. "틀림"(되돌리기)은 사람이 명시적으로 지목한 리포트에만, 확인 없이 자동 실행하지 않는다
   (뷰어 화면에도 확인 대화상자가 있다 — API로 직접 호출할 때도 같은 절차로 사람에게 먼저
   확인받는다)
3. 처리 후 `git -C <vault> log -1`로 `되돌림:`/`검토: ... 교정` 커밋이 생겼는지 확인.
   "맞음" 처리는 커밋을 만들지 않으니(정상 동작) 대신 `GET /api/report_changes?id=...`로
   반영을 확인한다

### 오류가 났을 때
1. `GET /api/status`의 `progress.items[].error`, SSE `error` 이벤트, 콘솔 출력을 먼저 확인
2. 포트 충돌: `analyst.run`과 `analyst.viewer` 모두 자동으로 다음 포트를 시도하니, 대부분
   조치가 필요 없다(10개 다 사용 중일 때만 `--port`를 바꿔 재시도)
3. lock 오류("다른 학습이 이미 실행 중입니다"): 위 "처음 설치" 절차대로 안전 확인 후에만
   `.analyst/learn.lock` 삭제
4. 타임아웃/잘린 응답: `.env`의 `LLM_TIMEOUT_SEC` 또는 실행 커맨드의 `--max-tokens`를 늘려
   재시도(이미 학습된 리포트는 재실행해도 자동으로 건너뛴다)
5. 특정 리포트만 `status="error"`로 남거나(추출 실패) 화면에 "오류"로 표시되면(강제 종료로
   중단된 행), 그 리포트는 아직 저장되지 않은 상태이니 다음 배치를 실행하면 자동으로 다시
   시도된다 — 별도 조치 불필요
6. 위로 해결이 안 되면 사람에게, 코드 자체의 문제로 보이면 아래 "문제 보고" 규칙대로 외부
   개발자에게 보고한다 — 직접 고치지 않는다

## 사람 질문에 답하는 방법

1. `index.md`를 먼저 읽어 전체 그림을 파악
2. 관련 `topics/*.md`를 읽어 현재 판단·흐름을 확인
3. 근거가 된 `reports/*.md`를 읽어 원문 인용을 확인
4. 답변에는 항상 `[리포트파일명]` 형태로 출처를 남긴다
5. 건수·날짜·통계 질문은 반드시 `analyst.sqlite`에 SQL로 확인한다(암산·추측 금지). 예시
   쿼리는 [`02-analyst-manual.md`](02-analyst-manual.md#7-analystsqlite-표-설명) 참고
6. vault에 근거가 없으면 지어내지 말고 "vault에 없음"이라고 답한다

## 지식 조회 (MCP) 등록과 사용

vault를 읽는 전용 MCP 서버가 있습니다. 사람 질문에 답할 때는 `reports/*.md`를 직접 여는 대신
**이 도구들을 먼저 쓰세요** — 같은 함수를 HTTP API(`/api/v1/...`, 자세한 내용은
[`02-analyst-manual.md`](02-analyst-manual.md#10-지식-조회-apimcp) 10절)도 쓰므로 결과가
일치합니다.

`opencode.json`에 등록(키 이름은 설치된 OpenCode 버전에 맞게 확인):

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

어떤 질문에 어떤 도구:

| 질문 유형 | 도구 |
|---|---|
| "지금까지 뭘 알고 있어?", 대화 시작 | `knowledge_overview` |
| 구체적인 주제 id를 모를 때 | `search_knowledge` (그다음 `get_topic`) |
| 특정 주제의 현재 판단·근거·과거 이력 | `get_topic` |
| "이번 주/이번 달 뭐가 바뀌었어?" | `what_changed` |
| 표/원자료가 필요할 때(기업·지표·증권사·기간 조건) | `find_claims` |
| "이 숫자가 시간에 따라 어떻게 바뀌었어?" | `metric_history` |

**반드시 지킬 것**: 답변에는 항상 도구가 반환한 `citation.report_path`(또는 `report_path`)를
`[파일명]` 형태로 인용하세요. 숫자·날짜·판단은 도구 결과에 있는 값만 쓰고 지어내지 마세요.
vault에 근거가 없으면 "vault에 없음"이라고 답하세요(위 "사람 질문에 답하는 방법" 절과 동일한
원칙).

## 하드 규칙

- vault·리포트·`analyst.sqlite`의 어떤 내용도 회사 밖이나 외부 개발자에게 보내지 않는다
- `.analyst/*.jsonl`을 손으로 고치지 않는다 — 뷰어의 검토/교정 기능을 쓰거나, 그것으로
  해결이 안 되면 사람에게 보고한다
- vault의 git 히스토리를 지우거나(rebase, force-push, 리셋) 하지 않는다
- 학습기를 동시에 두 개 띄우지 않는다(`learn.lock` 존중)
- API 키를 로그·채팅·git 커밋 메시지에 남기지 않는다

## 외부 개발자에게 문제를 보고할 때

**보내도 되는 것**: 오류 종류/메시지(리포트 문장이 안 섞였는지 먼저 확인), 리포트/주제/주장
건수, 처리 소요 시간, `llm_bench.py` 벤치마크 수치.

**보내면 안 되는 것**: 리포트 원문, 주장/주제 텍스트, vault나 `analyst.sqlite` 파일 자체.

## 코드를 업데이트할 때

```powershell
git pull
cd server
python -m pip install -e ".[dev]"
python -m pytest -q
```

테스트가 실패하면 코드(외부 개발자가 관리)를 직접 고치지 말고, 실패 내용을 위 "문제 보고"
규칙대로 정리해 사람 또는 외부 개발자에게 전달한다.

---

# 데이터 플랫폼(`aioffice.dataplat`)을 운영하는 코딩 에이전트용 지침

자세한 배경·뷰 작성법·API·검증 체크리스트는 [`03-dataplat-manual.md`](03-dataplat-manual.md)를
먼저 읽으세요. 여기는 요약된 작업 순서와 하드 규칙만 정리합니다. **`dataplat`은 팀의 엑셀을
직접 읽지 않습니다** — 팀이 이미 SQLite로 집계해 둔 것을 읽기 전용으로 연결할 뿐입니다.

## "새 데이터셋 연결해줘"라는 지시를 받았을 때

1. 팀의 원본 SQLite에서 그 데이터가 어느 테이블/뷰에 있는지, 컬럼이 표준 컬럼
   (`dataset, metric, entity, region, period, source, value, unit`)과 어떻게 다른지 확인한다
2. `v_dataplat_observations` 같은 뷰를 작성하거나 기존 뷰에 `UNION ALL`로 덧붙인다 —
   `03-dataplat-manual.md` 2절의 세 패턴(컬럼명만 맞추기 / 여러 테이블 UNION / wide→long)을
   그대로 따라 한다. **팀의 원본 테이블에는 아무것도 쓰지 않는다** — `CREATE VIEW`만 한다
3. `source.yaml`을 새로 쓰거나 갱신한다(`db`, `view` 또는 `query`, 필요하면 `rename`)
4. 반드시 먼저 `--dry-run`으로 점검한다:
   `python -m aioffice.dataplat.snapshot --source source.yaml --db dataplat.sqlite --dry-run`
   - 적재될 행 수가 원본과 크게 다르면(사람에게 예상 행 수를 먼저 물어봐도 된다) 뷰를 다시 본다
   - `dropped_blank`/`dropped_non_numeric`/`unparsed_periods`가 0이 아니면 어떤 셀 패턴 때문인지
     확인하고, 의도된 것이면(정말 빈 셀 등) 그대로 두고 사람에게 몇 건인지 보고한다
5. 문제없으면 `--dry-run` 없이 실제로 한 번 실행하고, 콘솔 요약(`[OK]`/`[건너뜀]`/`[오류]`)을
   사람에게 그대로 전달한다
6. 대시보드에 연결이 필요하면 `dataplat-client.ts`의 `catalog()`/`queryWide()` 등을 써서
   해당 컴포넌트를 API 호출로 바꾼다(직접 DB 파일을 열거나 엑셀을 읽는 코드를 새로 만들지 않는다)

## 정기 실행 리포트를 확인할 때 ("이번 주 스냅샷 결과 어때?")

1. `GET /api/loads?dataset=<이름>`으로 최근 적재를 확인하거나 `GET /api/loads/<id>`로 리포트
   (`diff.added_count`/`removed_count`/`changed_count`, `top_changes`)를 읽는다
2. `changed_count`가 평소보다 크게 튀면(경험적으로 판단하거나 사람에게 "평소 몇 건 정도인지"
   물어본다) **자동으로 넘기지 말고** `top_changes`를 사람에게 보여주며 확인을 요청한다
3. `status="error"`인 적재가 있으면 `error` 필드를 사람에게 그대로 전달한다(직접 원인을
   추측해 코드를 고치지 않는다 — 뷰/쿼리 문제면 위 "새 데이터셋 연결" 절차로 돌아가 고친다)

## 하드 규칙

- **팀의 원본 SQLite(`source.yaml`의 `db`)에는 절대 쓰지 않는다** — `dataplat`은 그 DB를
  읽기 전용으로만 연다(코드가 이미 `mode=ro`로 열지만, 직접 다른 도구로 그 파일을 열 때도
  이 원칙을 지킨다)
- `dataplat.sqlite`의 `loads`/`observations` 행을 손으로 고치거나 지우지 않는다 — 잘못된
  스냅샷을 되돌리고 싶으면 뷰/소스를 고친 뒤 다시 스냅샷을 실행한다(새 버전이 쌓일 뿐, 과거
  버전은 `GET /api/loads`로 계속 조회 가능하니 "삭제"가 필요한 경우는 거의 없다)
- `--admin-token`/`LLM_API_KEY` 값을 로그·채팅·커밋에 남기지 않는다
- 예약 작업(`schedule install`)을 사람 확인 없이 등록/삭제하지 않는다 — 요일·시각·`--pre`
  명령을 사람에게 보여주고 확인받은 뒤에만 실행한다
- 서버를 `127.0.0.1`이 아닌 주소로 띄울 때는 반드시 `--admin-token`을 지정한다(코드가 없으면
  시작을 거부하지만, 토큰 값 자체를 안전하게 고르는 것은 사람의 몫이다 — 에이전트가 임의로
  생성하지 않는다)
