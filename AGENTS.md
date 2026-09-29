# AGENTS.md — 데이터 플랫폼(`aioffice.dataplat`)을 운영하는 코딩 에이전트(OpenCode)용 지침

이 문서는 사람이 아니라 **너(코딩 에이전트)**에게 하는 지시입니다. 자세한 배경·뷰 작성법·API·
검증 체크리스트는 [`03-dataplat-manual.md`](03-dataplat-manual.md)를 먼저 읽으세요. 여기는
요약된 작업 순서와 하드 규칙만 정리합니다. **`dataplat`은 팀의 엑셀을 직접 읽지 않습니다** —
팀이 이미 SQLite로 집계해 둔 것을 읽기 전용으로 연결할 뿐입니다.

## 처음 설치할 때

1. `python --version`으로 3.10 이상 확인
2. `cd server; python -m venv .venv; .\.venv\Scripts\Activate.ps1; python -m pip install -e ".[dev]"`
   (추가 설치(extras) 필요 없음)
3. `Copy-Item .env.example .env` 후, 사람에게 전달받은 사내 LLM 주소/키/모델 값을 `.env`에만
   적기(채팅·로그에 키 값을 그대로 남기지 않는다)
4. `python -m pytest -q` (server/ 안에서) 통과 확인

## "새 데이터셋 연결해줘"라는 지시를 받았을 때

1. 팀의 원본 SQLite에서 그 데이터가 어느 테이블/뷰에 있는지, 컬럼이 표준 컬럼
   (`dataset, metric, entity, region, period, source, value, unit`)과 어떻게 다른지 확인한다
2. `v_dataplat_observations` 같은 뷰를 작성하거나 기존 뷰에 `UNION ALL`로 덧붙인다 —
   `03-dataplat-manual.md` 2절의 세 패턴(컬럼명만 맞추기 / 여러 테이블 UNION / wide→long)을
   그대로 따라 한다. **팀의 원본 테이블에는 아무것도 쓰지 않는다** — `CREATE VIEW`만 한다.
   표 하나에 예측 시점(버전/vintage)이 여러 개 섞여 있으면 **반드시** `version_column`을
   써야 한다(`03-dataplat-manual.md` 3절) — 없이 그대로 쓰면 서로 다른 시점 값이 조용히
   합쳐진다
3. `source.yaml`을 새로 쓰거나 갱신한다(`db`, `view` 또는 `query`, 필요하면 `rename`, 여러
   버전이 섞인 표면 `version_column`)
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

## 챗봇 질문을 검증할 때

`POST /api/chat`(또는 `/chat` 페이지)에 질문하면 응답의 `warnings` 배열을 항상 확인한다.

- `"존재하지 않는 조건 '...'는 제외했습니다"`류 문구는 **정상 동작**이다 — 사용자가 말하지
  않은 대상/기관/지역을 모델이 임의로 넣었다가 카탈로그에 없어서 그 조건만 빼고 나머지는
  정상 조회된 것이다. 표(`table`)가 채워져 있으면 걱정할 필요 없다.
- `"clarify"`가 있으면 표가 없고(`table: null`) 되물음만 온 것 — 사용자가 특정 이름을 직접
  말했는데 그 이름이 카탈로그에 없을 때만 이렇게 막는다(모델이 스스로 추가한 조건은 위처럼
  드롭되지, 되물음으로 막지 않는다)
- `"explain_number_mismatch_used_template"`/`"explain_failed_used_template"`이 있으면 모델의
  설명 문장이 표에 없는 숫자를 썼거나 호출이 실패해서, 코드가 만든 템플릿 문장(첫→마지막 값,
  최댓값/최솟값)으로 바뀐 것이다 — 표 자체는 여전히 정확하니 표를 기준으로 답한다
- `"'...'은(는) 해당 기간 데이터가 없습니다"`는 요청한 대상이 카탈로그에는 있지만 그 기간에는
  값이 없다는 뜻 — 표에서 그 대상 행이 빠져 있는 게 정상이다
- 응답의 `path`(`"rule"`/`"cache"`/`"llm"`)를 보면 LLM을 실제로 호출했는지 알 수 있다.
  `explain`은 기본이 `"template"`(즉시 응답, LLM 호출 없음) — `"llm"`으로 요청하면 설명
  문장에 LLM 호출이 하나 더 붙어 느려질 수 있다(`03-dataplat-manual.md` 10절)

## 하드 규칙

- **팀의 원본 SQLite(`source.yaml`의 `db`)에는 절대 쓰지 않는다** — `dataplat`은 그 DB를
  읽기 전용으로만 연다(코드가 이미 `mode=ro`로 열지만, 직접 다른 도구로 그 파일을 열 때도
  이 원칙을 지킨다)
- `dataplat.sqlite`의 `loads`/`observations` 행을 손으로 고치거나 지우지 않는다 — 잘못된
  스냅샷을 되돌리고 싶으면 뷰/소스를 고친 뒤 다시 스냅샷을 실행한다(새 버전이 쌓일 뿐, 과거
  버전은 `GET /api/loads`로 계속 조회 가능하니 "삭제"가 필요한 경우는 거의 없다)
- `--admin-token`/`LLM_API_KEY` 값을 로그·채팅·git 커밋 메시지에 남기지 않는다
- 예약 작업(`schedule install`)을 사람 확인 없이 등록/삭제하지 않는다 — 요일·시각·`--pre`
  명령을 사람에게 보여주고 확인받은 뒤에만 실행한다
- 서버를 `127.0.0.1`이 아닌 주소로 띄울 때는 반드시 `--admin-token`을 지정한다(코드가 없으면
  시작을 거부하지만, 토큰 값 자체를 안전하게 고르는 것은 사람의 몫이다 — 에이전트가 임의로
  생성하지 않는다)

## 오류가 났을 때

1. 스냅샷/서버 콘솔 출력과, 서버라면 `dataplat_run.log`(예약 실행 로그)를 먼저 확인
2. 위로 해결이 안 되면 사람에게, 코드 자체의 문제로 보이면 아래 "문제 보고" 규칙대로 외부
   개발자에게 보고한다 — 직접 고치지 않는다

## 외부 개발자에게 문제를 보고할 때

**보내도 되는 것**: 오류 종류/메시지, 적재/쿼리 건수, 처리 소요 시간, `llm_bench.py` 벤치마크
수치.

**보내면 안 되는 것**: 팀 원본 SQLite의 실제 데이터, `dataplat.sqlite`에 쌓인 실제 관측치 값,
챗봇 질문/답변에 등장한 실제 수치나 대상명.

## 코드를 업데이트할 때

```powershell
git pull
cd server
python -m pip install -e ".[dev]"
python -m pytest -q
```

테스트가 실패하면 코드(외부 개발자가 관리)를 직접 고치지 말고, 실패 내용을 위 "문제 보고"
규칙대로 정리해 사람 또는 외부 개발자에게 전달한다.
