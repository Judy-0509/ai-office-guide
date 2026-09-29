# 데이터 플랫폼 연결 설명서

팀이 엑셀을 읽어 이미 SQLite로 집계하고 있는 파이프라인을, **다시 만들지 않고** 버전 관리(주차별
비교) + HTTP API + 대시보드 + 한국어 챗봇에 연결하는 도구입니다. `dataplat`은 팀의 기존 DB를
읽기 전용으로만 열고, 표준 컬럼(`dataset, metric, entity, region, period, source, value, unit`)
을 내는 뷰/쿼리 하나만 있으면 나머지는 자동입니다. 코드/설치는 `server/README.md`의 "데이터
플랫폼" 절을 먼저 보세요 — 이 문서는 **연결 작업 자체**(뷰 쓰기, 점검, 서버·예약 등록, 검증)에
집중합니다.

## 1. 표준 컬럼과 그 의미

| 컬럼 | 필수 | 의미 |
|---|---|---|
| `dataset` | O | 데이터셋 id(영문 권장, 예: `shipments`). 대시보드/챗봇에서 이 값으로 묶입니다 |
| `metric` | O | 지표명(예: "출하량") |
| `entity` | O | 대상(모델/제품/기업 등) |
| `region` | - | 지역. 없으면 빈 문자열로 취급 |
| `period` | O | 기간. `YYYY`/`YYYYQn`/`YYYY-MM`/`YYYYWnn` 표준형은 물론, 실제 사내 표에서 자주
  보이는 표기도 자동 인식합니다 — 두 자리 연도 분기(`25Q3`, `'25Q3`, `3Q25`, `3Q2025`), 띄어쓰기/
  구분자 변형(`2025 Q3`, `2025-Q3`, `Q3 2025`, `Q3'25`), 회계연도(`FY25`, `FY2025`), 점/슬래시
  월(`2025.09`, `2025/09`). 두 자리 연도는 항상 `2000+yy`로 해석합니다. 그 외 텍스트는 그대로
  두되 스냅샷 리포트의 `unparsed_periods`에 카운트됩니다 |
| `source` | - | 기관/출처. 없으면 빈 문자열 |
| `value` | O | 숫자. `"1,234"`/`"12.5%"`/`"(3.2)"` 같은 텍스트도 자동으로 숫자로 바뀌고(`%`는
  단위로 남음), `""`/`"-"`/`"n.a."`류는 빈값으로, 그 외 숫자로 못 바꾸는 값은 "숫자 아님"으로
  세어 버립니다(둘 다 관측치로 저장되지 않음 — 스냅샷 리포트에서 몇 건 빠졌는지 확인하세요) |
| `unit` | - | 단위 |

**`period_from`/`period_to`로 범위를 물을 때(API·챗봇 공통)**: 거친 단위로 준 경계는 그 안의
모든 세부 기간을 포함합니다 — `period_to="2025"`는 `2025Q4`/`2025-12`/`2025W53`까지, `period_to
="2025Q2"`는 `2025-04`~`2025-06`까지(다음 분기는 제외) 포함합니다. 예: 분기 단위로 쌓인
데이터에 `period_from=period_to="2025"`로 물어도 2025년 전체가 잡힙니다(예전에는 빈 표가
나왔던 케이스).

**연/분기 합계 — `rollup`**: `GET /api/query`와 챗봇 spec 모두 `rollup: "year"|"quarter"`,
`agg: "sum"|"avg"`(기본 `sum`)를 받습니다. 지정하면 그 단위로 기간을 묶어 합산/평균한 뒤
표를 만듭니다 — "2025년 합계" 같은 질문은 챗봇이 표에 없는 숫자를 지어내지 않고 이 코드
계산값을 그대로 인용합니다. `version_column`을 쓰는 데이터셋에서는 `history`/`diff` 조회에는
적용되지 않습니다(버전 비교는 그 자체로 이미 집계 단위가 다름).

## 2. 뷰 작성 예시

전체 예시는 `server/aioffice/dataplat/samples/example_view.sql`(같은 내용을
`samples/fake_source.py`가 테스트용 SQLite로도 만듭니다). 세 가지 패턴을 보여줍니다.

**(1) 이미 long 형태인 테이블 — 컬럼명만 맞추기.** 팀 테이블이 이미 "한 행 = 관측치 하나"라면
`SELECT`에서 이름만 바꿉니다:

```sql
CREATE VIEW v_dataplat_observations AS
SELECT 'shipments' AS dataset, metric, entity, dept AS region, period, inst AS broker,
       qty AS value, '' AS unit
FROM tbl_shipments_long;
```

`inst`처럼 뷰에서 안 바꾸고 남겨둔 컬럼은 `source.yaml`의 `rename:`에서 매핑해도 됩니다(뷰
SQL을 아예 안 건드리고 싶을 때 — 아래 4절).

**(2) 여러 테이블을 하나로 — `UNION ALL`.** 데이터셋마다 다른 테이블에 있어도 `dataset` 컬럼
값만 다르게 주고 이어붙이면 됩니다:

```sql
CREATE VIEW v_dataplat_observations AS
SELECT 'shipments' AS dataset, metric, entity, region, period, source, value, unit FROM v_shipments
UNION ALL
SELECT 'price_index' AS dataset, metric, entity, region, period, source, value, unit FROM v_price_index;
```

**(3) wide(기간이 컬럼) → long — 기간 컬럼마다 `UNION ALL` 한 번.** 분기가 `q1`..`q4` 컬럼으로
퍼져 있는 테이블은, 컬럼마다 SELECT를 하나씩 만들어 기간을 리터럴로, 그 컬럼값을 `value`로 넣고
전부 이어붙입니다:

```sql
CREATE VIEW v_dataplat_observations AS
-- (앞의 long 테이블 SELECT들...)
UNION ALL
SELECT 'price_index' AS dataset, '가격지수' AS metric, entity, region, '2024Q1' AS period,
       '' AS broker, q1 AS value, 'pt' AS unit FROM tbl_price_index_wide
UNION ALL
SELECT 'price_index' AS dataset, '가격지수' AS metric, entity, region, '2024Q2' AS period,
       '' AS broker, q2 AS value, 'pt' AS unit FROM tbl_price_index_wide
UNION ALL
SELECT 'price_index' AS dataset, '가격지수' AS metric, entity, region, '2024Q3' AS period,
       '' AS broker, q3 AS value, 'pt' AS unit FROM tbl_price_index_wide
UNION ALL
SELECT 'price_index' AS dataset, '가격지수' AS metric, entity, region, '2024Q4' AS period,
       '' AS broker, q4 AS value, 'pt' AS unit FROM tbl_price_index_wide;
```

병합/다중행 헤더처럼 이미 팀 파이프라인이 걷어낸 엑셀 특유의 문제(병합 셀, 여러 표가 한 시트에
있는 경우 등)는 신경 쓸 필요 없습니다 — 그 파이프라인의 최종 산출 테이블만 보고 뷰를 짜면
됩니다.

## 3. 한 표에 버전(vintage)이 여러 개 있을 때 — `version_column`

가끔 팀 원본 테이블 하나에 **예측 시점이 다른 값 여러 개**가 함께 들어 있습니다 — 예를 들어
"2026-07에 만든 예측"과 "2026-08에 만든 예측"이 표준 8개 컬럼 말고 별도 컬럼(예: `vintage`)에
표시되어 같은 표 안에 섞여 있는 경우입니다.

**이런 테이블은 `version_column` 없이 절대 그대로 쓰면 안 됩니다.** `version_column`을 지정하지
않고 이 표를 읽으면 서로 다른 시점의 예측이 `entity`/`period`가 같다는 이유만으로 같은 관측치로
뒤섞입니다 — 합계를 내거나 최신값을 뽑으면 7월 예측과 8월 예측이 무작위로 섞인, 어느 시점의
것도 아닌 의미 없는 숫자가 나옵니다(어느 쪽 행이 "이겼는지"도 알 수 없습니다).

뷰가 그 컬럼을 표준 8개 컬럼과 함께 내보내게 하고, `source.yaml`에 `version_column`만
추가하면 됩니다:

```sql
CREATE VIEW v_forecast_versions AS
SELECT 'forecast' AS dataset, metric, entity, region, period, source, value, unit,
       vintage                      -- 표준 8개 컬럼 + 버전 컬럼
FROM tbl_forecast;
```

```yaml
db: C:/data/team.sqlite
view: v_forecast_versions
version_column: vintage            # 뷰가 낸 버전 컬럼 이름 (rename 적용 후 이름 기준)
```

동작 방식(전체 예시는 `samples/fake_source.py`의 `build_versioned()`, `samples/source_versioned.yaml`
참고):

- 스냅샷은 `(dataset, 버전값)`별로 따로 버전 관리됩니다. 같은 버전 값이 내용까지 같으면
  건너뛰고(`skipped_duplicate`), 내용이 바뀌었으면(수정/재발행) 그 버전의 새 적재를 씁니다.
  다른 버전끼리는 중복 판정에 영향을 주지 않습니다.
- 버전은 시간순이 아니라 **버전 값 자체**로 정렬됩니다 — `YYYY-MM`/`YYYY`/`YYYYQn` 형식이면
  자연스럽게 시간순, 그 외 형식이면 처음 등장한 순서를 씁니다.
- `latest` = 가장 높은 버전의 최신 적재. `GET /api/catalog`가 데이터셋별 버전 목록을 보여주고,
  `GET /api/query?...&version=2026-08`처럼 특정 버전을 직접 조회할 수 있습니다.
- 리포트의 diff는 "바로 이전 버전"과 비교합니다(예: 2026-08 적재 시 2026-07과 비교) — 같은
  버전을 재발행한 경우엔 그 버전의 이전 내용과 비교합니다.
- `version_column`을 쓰지 않는 데이터셋은 이 절의 내용과 전혀 무관합니다 — 지금까지와 완전히
  똑같이 동작합니다.
- 챗봇도 버전을 알아듣습니다: "7월 버전"처럼 특정 버전을 콕 집으면 그 버전으로 조회하고,
  "지난 버전 대비 가장 많이 바뀐/증가한/감소한 모델은?"처럼 **여러 대상에 걸쳐 두 버전을
  비교**하는 질문은 old/new/diff/pct를 코드가 직접 계산하는 버전 비교(아래 참고)로 자동
  라우팅됩니다 — 표에 없는 숫자를 지어내지 않으며, 설명 문장은 그 계산된 숫자만 인용할 수
  있습니다. 비교할 버전 이름을 특정할 수 없을 때만(카탈로그에 없는 이름 등) 되물어봅니다 —
  9절 검증 체크리스트에서 함께 확인하세요.

## 4. `source.yaml` 작성

```yaml
db: C:/data/team.sqlite            # 이 파일(source.yaml) 기준 상대경로도 허용
view: v_dataplat_observations      # 뷰 이름 (또는 query: "SELECT ..." 로 직접 쿼리)
rename:                            # 뷰가 표준 이름과 다른 컬럼명을 낼 때만 (선택)
  broker: source
version_column: vintage            # 한 표에 여러 버전(vintage)이 섞여 있을 때만 (선택, 3절)
```

`db`/`view`(또는 `query`) 중 하나라도 빠지면, 또는 `query`와 `view`를 동시에 쓰면 바로
`ValueError`로 실패합니다(잘못된 설정으로 조용히 넘어가지 않음).

## 5. 점검: `snapshot --dry-run`

DB에 쓰지 않고 리포트만 봅니다. 뷰를 새로 짜거나 고칠 때마다 이걸로 먼저 확인하세요:

```powershell
python -m aioffice.dataplat.snapshot --source source.yaml --db dataplat.sqlite --dry-run
```

콘솔 출력 읽는 법:

```
[OK] shipments: 5행 적재 (추가 0, 삭제 1, 변경 1)
[건너뜀] price_index: 이전 스냅샷과 동일
(dry-run: DB에 쓰지 않았습니다)
```

- `[OK] ... N행 적재`: N이 기대한 행 수와 크게 다르면(엑셀/원본에서 센 행 수와 비교) 뷰의
  `WHERE`/`JOIN`을 다시 확인하세요.
- 괄호 안 `추가/삭제/변경`은 **이전 스냅샷과 비교한** 값입니다. 처음 실행이면(비교 대상이 없음)
  나타나지 않습니다.
- `[건너뜀] ... 이전 스냅샷과 동일`: 뷰가 반환한 내용이 지난 실행과 완전히 같다는 뜻(정상 —
  같은 주에 다시 돌렸거나, 이번 주 원본이 아직 안 바뀐 경우).
- 실제로 DB에 쓰려면(스냅샷을 남기려면) `--dry-run`을 빼고 다시 실행하세요.

값이 몇 건 비었는지(빈값/숫자 아님/기간 파싱 실패)도 `--dry-run` 리포트에 나오니, 뷰의
`value`/`period` 표현식이 기대와 다르게 나오는 셀이 있는지 여기서 걸러내세요.

## 6. 서버 시작 (`--site`로 대시보드까지 같이)

```powershell
python -m aioffice.dataplat.server --db dataplat.sqlite --source source.yaml ^
  --site <대시보드 site/ 빌드 폴더> --pre "<기존 집계 명령>" --env .env --port 8000 --admin-token <토큰>
```

브라우저로 `http://127.0.0.1:8000/`을 열면 `--site`로 지정한 대시보드가, `/api/*`로 API가,
`/chat`으로 참고용 챗 페이지가 같은 origin에서 뜹니다(운영 환경에서 CORS 설정 불필요).
`127.0.0.1`이 아닌 주소로 열려면 `--admin-token`이 필수입니다(없으면 시작 자체를 거부합니다).

## 7. 대시보드의 데이터 로딩을 API로 바꾸기

### API 엔드포인트

| 엔드포인트 | 설명 |
|---|---|
| `GET /api/catalog` | 데이터셋별 title·metrics·entities·regions·sources·units·기간·`version_labels`·마지막 적재 |
| `GET /api/query?dataset=&metric=&entity=&region=&source=&period_from=&period_to=&version=latest\|all\|<id 또는 버전 라벨>&format=long\|wide&rows=&cols=&rollup=year\|quarter&agg=sum\|avg` | 조회(다중값은 콤마 구분). `version_column` 데이터셋은 `version=2026-08`처럼 라벨로도 조회 가능. `rollup`/`agg`는 위 1절 참고 |
| `GET /api/history?dataset=&metric=&entity=&period=&source=` | 특정 값의 버전(스냅샷)별 변화 — `version_column` 데이터셋은 라벨 하나당 한 점 |
| `GET /api/version_diff?dataset=&from=&to=&metric=&entity=&region=&source=&period_from=&period_to=&group_by=&top=&sort=abs\|rel` | 두 버전 비교(3절). 기본: 최신 라벨 vs 바로 이전 라벨. `group_by`(기본 `entity`) 기준 합산 후 old/new/diff/pct 계산, 상위 `top`(기본 20)건 |
| `GET /api/loads[?dataset=]`, `GET /api/loads/<id>` | 적재 이력 목록/상세(diff 포함 리포트) |
| `POST /api/refresh {dataset?}`, `GET /api/refresh/status` | 스냅샷 실행(admin 토큰 필요) |
| `POST /api/chat {message, history?, explain?}` 또는 `{spec_patch, message?, explain?}` | 챗봇. **LLM을 자동 호출하지 않음** — 응답의 `path`(`"rule"\|"cache"\|"button"\|"none"`)와 `timings`(10절), `path:"none"`이면 `options`(12·16절) |
| `GET /api/suggestions?dataset=` | 데이터셋별 추천 질문(`spec_patch` 포함, 클릭 시 즉시 실행) — 10·16절 |
| `GET /api/digest?dataset=` | 최신 배치 요약(다이제스트) — 13절 |
| `GET /api/aliases`, `DELETE /api/aliases/learned/<id>` (admin 토큰) | 별칭 설정 + 학습된 별칭 조회/삭제 — 12·15절 |
| `POST /api/ask {message, history?}` (admin 토큰) → `202 {job_id}`, `GET /api/ask/<id>`, `DELETE /api/ask/<id>` (admin 토큰), `GET /api/ask?recent=` | 비동기 "AI에게 물어보기" — LLM이 실제로 호출되는 유일한 경로 — 14절 |

읽기 엔드포인트는 인증이 필요 없습니다. `server/README.md`에 같은 표가 조금 더 자세히 있습니다.

### TS 클라이언트

`server/aioffice/dataplat/clients/dataplat-client.ts`를 대시보드 프로젝트에 복사합니다(차트
라이브러리를 지정하지 않은, `fetch`만 쓰는 타입 클라이언트라 팀이 쓰는 어떤 라이브러리와도
같이 쓸 수 있습니다). 기존에 엑셀/CSV를 직접 읽던 곳을 아래처럼 바꿉니다:

```tsx
import { DataplatClient } from "./dataplat-client";
const client = new DataplatClient(""); // 서버가 site를 같이 서빙하므로 같은 origin("")

// 필터 드롭다운 채우기 (version_labels도 여기 들어있음)
const { datasets } = await client.catalog();

// 표 하나 (엔티티×기간 wide)
const table = await client.queryWide({
  dataset: "shipments", format: "wide", rows: ["entity"], cols: ["period"],
});

// 특정 셀의 개정 이력(리비전 차트)
const { history } = await client.history({
  dataset: "shipments", metric: "출하량", entity: "모델A", period: "2024Q1",
});

// 두 버전 비교 (기본: 최신 vs 바로 이전 라벨) — 모델별 가장 많이 바뀐 순
const diff = await client.versionDiff({ dataset: "forecast", group_by: ["entity"] });

// "갱신" 버튼
await client.refresh();               // POST /api/refresh, admin 토큰 필요
const status = await client.refreshStatus();
```

React라면 `useEffect`로 마운트 시 `catalog()`/`queryWide()`를 호출하고 `useState`로 담으면
됩니다(`server/README.md`의 예시 참고). 갱신 버튼은 `refresh()` 호출 후 `refreshStatus()`를
짧은 간격으로 폴링해 `running`이 꺼지면 데이터를 다시 불러오도록 구현하세요.

챗봇 3단계 경로용 메서드도 같은 클라이언트에 있습니다: `chat()`(자동으로 LLM을 부르지 않음),
`chatWithSpec()`(버튼/추천 질문의 `spec_patch` 실행), `suggestions()`/`digest()`(대시보드 첫
화면 카드), `ask()`/`askStatus()`/`askRecent()`/`cancelAsk()`(비동기 "AI에게 물어보기"),
`aliases()`/`deleteLearnedAlias()`. 타입과 사용 순서는 10·12~16절과 `.ts` 파일의 주석을
참고하세요 — `server/aioffice/dataplat/static/chat.html`이 이 모든 메서드를 실제로 쓰는
참고 구현입니다.

## 8. 예약 등록 (팀 기존 집계 명령과 함께)

```powershell
python -m aioffice.dataplat.schedule install --day MON --time 08:00 ^
  --pre "C:\team\run_aggregate.exe" --source source.yaml --db dataplat.sqlite
```

매주 월요일 08:00에 `--pre` 명령(팀이 이미 쓰던 집계 실행 파일/스크립트)을 먼저 실행하고,
**성공했을 때만** 스냅샷을 실행하는 `.cmd`가 `dataplat.sqlite` 옆에 만들어지고 그걸 Windows
작업 스케줄러에 등록합니다. `--pre`를 생략하면 스냅샷만 등록됩니다. **스냅샷이 성공하면
이어서 다이제스트(13절)도 자동 실행**됩니다(다이제스트가 실패해도 스냅샷의 성공/실패 판정에는
영향을 주지 않습니다). 다이제스트는 LLM을 쓰므로 `.env`가 필요하면 `--env`를 추가하세요. 로그는
`dataplat_run.log`(같은 폴더)에 남습니다. `schedule show`로 등록 상태, `schedule remove`로
삭제합니다.

## 9. 검증 체크리스트

새 데이터셋을 연결했거나 뷰를 크게 고쳤을 때마다:

1. **총계 2~3개 대조**: 엑셀(또는 팀 원본 집계 화면)에서 임의로 2~3개 숫자(예: 특정 모델의
   특정 분기 출하량, 전체 합계)를 골라 `GET /api/query?dataset=...&metric=...&entity=...`
   결과와 손으로 비교합니다. 다르면 뷰의 집계/필터 로직부터 의심하세요.
2. **챗봇 질문 10개**: `/chat` 페이지(또는 `POST /api/chat`)에 아래 유형을 섞어 10개 질문하고,
   각각 정답 여부·소요 시간(초)·되물음(clarify) 발생 여부만 표로 기록합니다(리포트 원문·숫자
   맥락 같은 세부 내용은 기록하지 않습니다 — 숫자만):

   | # | 유형 | 질문 예시 | 정답 | 초 | 되물음 |
   |---|---|---|---|---|---|
   | 1 | 단일 값 | "모델A 2024Q1 출하량 얼마야?" | | | |
   | 2 | 추세 | "모델A 출하량 분기별로 보여줘" | | | |
   | 3 | 기관 비교 | "기관A랑 기관B 출하량 비교해줘" | | | |
   | 4 | 버전 변화 | "지난 스냅샷 대비 뭐가 바뀌었어?" | | | |
   | 5 | 모호한 질문 | "그거 얼마였지?" (되물음 기대) | | | |
   | 6 | 특정 버전 (version_column 쓰는 데이터셋만) | "모델A 7월 버전 얼마야?" | | | |
   | 7 | 버전 비교 (version_column 쓰는 데이터셋만) | "지난 버전 대비 가장 많이 바뀐 모델은?" | | | |
   | 8~10 | (팀 데이터셋에 맞게 반복) | | | | |

   7번(버전 비교) 질문은 `table`에 나온 `old`/`new`/`diff`/`pct` 값을 `GET /api/version_diff`
   결과와 직접 대조해서 정답 여부를 판정하세요 — 이 값들은 챗봇이 지어낸 게 아니라 코드가
   계산한 값이므로, 표만 맞으면 설명 문장의 모델명·수치도 함께 맞습니다.

   "정답" 판정은 `table`에 나온 숫자를 1번 항목처럼 원본과 대조해서 내립니다. 모호한 질문에서
   되물음 없이 틀린 값을 확신에 차서 답하면 실패로 기록하세요(챗봇은 표에 없는 숫자를 답하지
   않도록 설계되어 있지만, spec 단계에서 잘못된 데이터셋/지표를 고르는 것까지는 막지 못합니다).

   응답의 `warnings` 배열도 함께 보세요. `"존재하지 않는 조건 '...'는 제외했습니다"`는
   **되물음이 아닙니다** — 모델이 질문에 없던 조건(기관/지역, 또는 사용자가 직접 지목하지
   않은 대상)을 스스로 덧붙였는데 카탈로그에 없어서 그 조건만 빼고 나머지는 정상 조회했다는
   뜻이니, `table`이 채워져 있으면 정답 판정을 그대로 진행하세요. 표가 비고 `clarify`
   워닝만 있을 때만 "되물음"으로 집계합니다 — 데이터셋/지표를 못 찾았거나, 사용자가 메시지에
   직접 적은 이름(예: "모델A랑 없는모델 비교해줘"의 "없는모델")이 카탈로그에 없을 때만
   되물음이 발생합니다. `"'...'은(는) 해당 기간 데이터가 없습니다"`는 이름은 카탈로그에
   있지만 그 기간에는 값이 아예 없다는 뜻 — 표에 그 대상 행이 통째로 빠져 있어도 정상입니다.

   10절의 "`rule` 경로로 바로 답하는 질문 유형" 표에 있는 예시 몇 개를 섞어 물어보면
   `path`가 실제로 `"rule"`/`"cache"`로 오는지, LLM 호출 없이도 정답이 나오는지 함께
   확인할 수 있습니다.
3. **스케줄 확인**: `schedule show`로 다음 실행 시각이 기대한 요일/시각인지 확인합니다.
4. **`--admin-token` 없이 갱신이 막히는지**: `POST /api/refresh`를 토큰 없이 호출해 401이
   나오는지 한 번은 확인하세요(운영 배포 전 1회면 충분).
5. **되물음 버튼 플로우**: 2번 항목의 "모호한 질문"이 `path:"none"` + `options`로 오는지,
   버튼(spec_patch)을 누르면 `path:"button"`으로 즉시(LLM 호출 없이) 표가 나오는지, 마지막
   "AI에게 물어보기" 버튼이 항상 있는지 확인합니다(10·12·16절).
6. **`/api/ask` 왕복**: "AI에게 물어보기"를 한 번 눌러 `202`가 오는지, `GET /api/ask/<id>`를
   몇 초 간격으로 폴링해 `queued`→`running`→`done`(또는 `error`)으로 넘어가는지, 완료된
   `result`가 `POST /api/chat`과 같은 모양인지 확인합니다(14절). 취소(`DELETE`)도 한 번
   테스트하세요.
7. **다이제스트 확인**: `GET /api/digest?dataset=<이름>`에 최신 요약이 있는지, 문장에 표에
   없는 숫자가 없는지 확인합니다(13절). 처음 연결한 데이터셋이면 비어 있는 게 정상입니다
   (다음 스냅샷 이후 생깁니다).
8. **별칭 점검(쓰는 경우)**: `aliases.yaml`에 적어둔 별칭 몇 개로 질문해 `path:"rule"`로
   바로 풀리는지 확인하고, `GET /api/aliases`의 `learned`에 이상한 학습이 쌓이지 않았는지
   가끔 훑어봅니다(12·15절).

## 10. 챗봇 3단계 응답 경로 (`path`, `explain`) — `POST /api/chat`는 이제 LLM을 자동 호출하지 않습니다

사내 공용 LLM 엔드포인트는 다른 사용자 요청과 큐를 공유해서, 호출 1건이 수십 초~수 분 걸릴 수
있습니다(모델 자체는 느리지 않습니다 — 대기열 문제). 그래서 **사용자는 절대 LLM 대기열에서
기다리게 하지 않는다**는 원칙으로, `POST /api/chat`(일반 채팅 입력)은 LLM을 **자동으로도
호출하지 않습니다**. LLM은 (a) 스냅샷 후 배치로 도는 다이제스트(13절)와 (b) 사용자가 명시적으로
누르는 "AI에게 물어보기" 버튼(14절, 비동기 `/api/ask`) 두 곳에서만 쓰입니다.

`POST /api/chat` 응답의 `path` 필드:

| `path` | 의미 | LLM 호출 |
|---|---|---|
| `"rule"` | 카탈로그 이름/기간/의도 키워드만으로 질의를 바로 만들었음(아래 표 참고) | 0회 |
| `"cache"` | 카탈로그가 안 바뀐 상태에서 같은 질문을 이전에 이미 물어봤음(`/api/ask`가 예전에 풀어낸 spec 재사용 포함) | 0회 |
| `"button"` | 되물음(clarify) 버튼이나 추천 질문(12절)을 눌러 `spec_patch`를 직접 실행함 | 0회 |
| `"none"` | 위 세 경로 모두 못 풀었음 — 응답에 클릭 가능한 `options`(버튼 목록, 마지막은 항상 "AI에게 물어보기")가 옴. **`POST /api/chat` 자체는 여기서 끝** — LLM은 사용자가 "AI에게 물어보기"를 눌러야만(14절, `/api/ask`) 호출됩니다 | 0회 (버튼을 누르기 전까지) |

**대시보드 위젯 가이드**: `path === "none"`이면 `options`를 버튼으로 렌더링하세요 —
`spec_patch`가 있는 버튼은 클릭 시 `POST /api/chat {spec_patch, message}`로 즉시 재실행되고
(`path:"button"`), `action:"ask_ai"` 버튼(항상 마지막)은 `POST /api/ask`로 넘어갑니다(14절).
"사내 LLM 응답 대기 중…" 같은 경과 시간 표시는 `/api/ask` 작업의 상태가 `running`일 때만
보여주세요 — `rule`/`cache`/`button`은 항상 즉시 응답하므로 로딩 표시가 오히려 어색합니다.

**설명 문장은 기본이 즉시 응답(`explain`)**: `explain_seconds`가 0이면 코드가 만든 템플릿
문장을 썼다는 뜻(항상 숫자가 맞고 LLM 호출이 없습니다) — 서버 기본값이며 `--explain llm`으로
서버 전체 기본을 바꾸거나, 요청 본문에 `"explain":"llm"`을 넣어 그 질문만 LLM 설명을 받을 수
있습니다(스펙이 이미 rule/cache/button으로 풀린 뒤에 붙는 별도 호출이라, 이것만으로는 자동
LLM 호출 규칙을 어기지 않습니다 — 사용자가 명시적으로 요청한 경우에만 씁니다).
`timings.spec_seconds`/`explain_seconds`/`query_seconds`로 어느 단계가 오래 걸렸는지 구분할 수
있습니다.

**`rule` 경로로 바로 답하는 질문 유형(예시)**:

| 질문 예시 | 인식 근거 |
|---|---|
| "모델A 출하량 추이 보여줘" | 대상 1개 + "추이" → 기간별 추이(꺾은선) — **버전 이력이 아닙니다** |
| "모델A 분기별 추이" | "분기별" → 기간별 추이 + 분기 단위 롤업 |
| "모델A 지난 버전 대비 얼마나 바뀌었어" | "지난 버전 대비"(버전 단어) → 버전 비교 |
| "모델A 모델B 비교해줘" | 대상 2개 이상 + "비교" |
| "2024년 모델A 합계는" | 연도 + "합계" → 연간 롤업 |
| "모델A 2024Q1 출하량 얼마야" | 대상 1개 + 기간 1개 + "얼마" |
| "모델A 25Q3 얼마야" | 두 자리 연도 분기(`25Q3`) 표기도 인식 |

"추이"/"흐름"/"분기별"/"월별"/"연도별"은 **기간(period) 축을 따라가는 추세**를 뜻합니다(꺾은선
차트, 전체 기간 또는 지정한 기간). "버전"이 직접 언급된 질문(지난/이전 버전, 버전 대비, "7월
버전" 등)만 버전 비교/특정 버전 조회로 갑니다 — 서로 다른 개념이니 섞어 쓰지 마세요.

별칭(12절)도 이 표의 이름 인식에 함께 적용됩니다 — "삼성"처럼 카탈로그에 없는 사내 용어도
aliases.yaml에 등록해 두면 `rule` 경로로 바로 풀립니다.

대상 이름·기간 중 아무것도 못 찾았거나(카탈로그에 없는 이름 포함, 별칭도 없음) 위 유형에 안
맞으면 `path:"none"` + `options`로 응답합니다(버튼: 인식된 대상별 바로가기, 또는 아무것도
못 찾았으면 12절의 추천 질문 + "AI에게 물어보기"). LLM은 사용자가 그 버튼을 직접 눌러야만
(14절) 호출됩니다 — 자동으로 호출되는 경로는 없습니다.

## 11. 문제 해결

| 증상 | 확인할 것 |
|---|---|
| 스냅샷이 매번 `skipped_duplicate` | 팀 원본이 실제로 안 바뀌었는지 먼저 확인(정상 동작일 수 있음). `--pre` 집계가 조용히 실패해 원본이 그대로인 경우도 있으니 `dataplat_run.log`를 보세요 |
| 특정 값이 관측치에서 빠짐 | `--dry-run` 리포트의 `dropped_blank`/`dropped_non_numeric`/`unparsed_periods`를 확인 — 뷰의 `value`/`period` 표현식이 그 셀에서 기대와 다른 텍스트를 내고 있을 가능성 |
| API가 401 | `--admin-token` 설정 여부와 `Authorization: Bearer <token>` 헤더 확인(읽기 엔드포인트는 토큰이 필요 없음 — `/api/refresh`만) |
| 서버가 비loopback 주소로 시작을 거부 | `--host`가 `127.0.0.1`/`localhost`가 아니면 `--admin-token`이 필수입니다 |
| 챗봇이 계속 되물음만 함 | 질문에 **직접 쓴** 이름(데이터셋/지표, 또는 메시지에 등장한 대상명)이 카탈로그(`GET /api/catalog`)에 있는 이름과 너무 다른지 확인 — 유사도 0.85 미만이면 자동 교정 대신 후보를 보여줍니다. 기관/지역, 또는 모델이 스스로 추가한 대상(사용자가 언급하지 않은)이 카탈로그에 없을 때는 되물음 대신 `warnings`에 "존재하지 않는 조건 '...'는 제외했습니다"만 남기고 조회는 계속됩니다 — 이건 정상 동작입니다 |
| `version_column`을 쓰는 데이터셋의 값이 뒤섞여 보임 | 뷰가 그 컬럼을 실제로 내보내는지(3절), `source.yaml`의 `version_column` 이름이 뷰 출력 컬럼명(rename 적용 후)과 정확히 일치하는지 확인 — 다르면 스냅샷이 바로 `ValueError`로 실패합니다(조용히 섞이지 않음) |
| 특정 버전이 안 보임/최신이 예상과 다름 | `GET /api/catalog`의 `version_labels`로 실제 저장된 버전 목록을 확인. `YYYY-MM` 형식이 아닌 버전 이름(예: "draft")은 시간순이 아니라 처음 적재된 순서로 정렬됩니다(3절) |
| 분기 데이터인데 `period_from=period_to="2025"`로 물으면 빈 표 | 예전 버그 — 이제는 연 단위 경계가 그 해의 모든 분기/월/주를 포함합니다(1절 범위 규칙). 여전히 비면 `GET /api/catalog`의 `period_from`/`period_to`로 실제 저장 범위부터 확인 |
| 챗봇 답이 항상 몇 분씩 걸림 | `POST /api/chat`은 이제 LLM을 자동 호출하지 않습니다(10절) — 느리다면 위젯이 `explain:"llm"`을 항상 보내고 있거나, "AI에게 물어보기" 버튼(14절)을 거치는 경로를 매번 타고 있는 건 아닌지 확인하세요. `path`가 계속 `"none"`이면 질문에 대상 이름과 기간을 함께 넣어보세요 — `rule` 경로가 잡히면 즉시 답합니다 |
| `POST /api/chat`에 물었는데 `path:"none"`만 오고 표가 안 나옴 | 정상 동작입니다(10절) — 사용자가 `options` 중 하나를 눌러야 다음 단계로 갑니다. 위젯이 `options`를 렌더링하고 있는지 확인하세요 |
| `/api/ask` 작업이 `error`로 끝남 | `GET /api/ask/<id>`의 `error` 필드를 확인 — 서버 재시작으로 중단된 작업은 "중단됨"으로 표시됩니다(14절). LLM 타임아웃/연결 실패면 재시도하거나 질문을 더 구체적으로 바꿔 `rule` 경로로 유도하세요 |
| 다이제스트(`/api/digest`)가 계속 비어 있음 | `dataplat.digest`가 아직 실행되지 않았을 수 있습니다(13절) — `python -m aioffice.dataplat.digest --source source.yaml --db dataplat.sqlite --env .env`를 한 번 직접 실행해 로그를 확인하세요. 예약 실행 중이라면 스냅샷이 매번 `skipped_duplicate`(변경 없음)라 다이제스트도 안 도는 정상 상황일 수 있습니다 |

## 12. `aliases.yaml` — 별칭과 그룹

카탈로그에 없는 사내 줄임말/코드명("삼성", "물량")을 `rule` 경로가 바로 알아듣게 하려면
`source.yaml` 옆에 `aliases.yaml`을 둡니다(경로는 `--aliases`로 바꿀 수 있고, 없으면
아예 안 씁니다 — 완전히 선택 사항).

```yaml
shipments:                          # source.yaml의 dataset 이름과 동일
  entities:
    삼성: ["모델A", "모델B"]          # 별칭 → 실제 카탈로그 이름 목록
    갤럭시:                          # 또는 접두사로 여러 개를 한 번에
      column: entities
      prefix: Galaxy                # "Galaxy"로 시작하는 모든 entity 값에 매칭
  regions:
    중국: China
  metrics:
    물량: volume_mu
    출하: volume_mu
  groups:                           # entities와 합쳐서 적용됨(읽기 쉬우라고 구분만 함)
    폴더블: ["모델A", "모델C"]
```

- 목록(`["모델A","모델B"]`)/문자열(`China`) 형태는 카탈로그에 실제로 있는 이름만 남기고
  나머지는 조용히 버립니다(오래된 별칭이 있어도 에러가 나지 않습니다).
- `{column, prefix}` 형태는 해당 차원(entities/regions/metrics)의 카탈로그 값 중 그 접두사로
  시작하는 것을 **매번 새로 계산**합니다 — 카탈로그가 바뀌면(신제품 추가 등) 자동으로 따라갑니다.
- 소스 테이블의 별도 속성(예: `company`)으로 그룹을 매기고 싶다면(뷰가 그 값을 extra 컬럼으로
  내보내는 경우) 이 파일 레벨에서는 지원하지 않습니다 — `groups`에 그 시점의 목록을 손으로
  적어두는 것으로 충분히 저렴하게 해결됩니다. 목록이 자주 바뀌는 회사라면 요청하세요(스키마
  변경이 필요한 더 큰 작업입니다).
- `GET /api/aliases`로 현재 설정(`aliases`)과 15절의 학습된 별칭(`learned`)을 함께 조회할 수
  있습니다.

## 13. 다이제스트 — 스냅샷 이후 자동 요약

`python -m aioffice.dataplat.digest --source source.yaml --db dataplat.sqlite --env .env`는
데이터셋마다 최근 적재를 요약합니다: `version_column`을 쓰는 데이터셋은 최신 버전과 바로 이전
버전의 `version_diff` 상위 변경 요약("이번 버전 주요 변화 요약") + 가장 많이 바뀐 대상 최대
5개 각각 한 줄 메모를, 안 쓰는 데이터셋은 이번 적재의 diff 리포트 기준으로 같은 걸 만듭니다.
문장의 모든 숫자는 챗봇과 똑같은 방식으로 표와 대조 검증되고, 틀리면 코드 템플릿으로
대체됩니다. **한 적재(load_id)당 한 번만** 생성됩니다(다시 실행해도 중복으로 쌓이지 않음).

`schedule install`로 예약을 등록하면 **스냅샷이 성공했을 때만** 자동으로 이어서 실행됩니다
(실패해도 스냅샷 자체의 성공/실패 판정에는 영향을 주지 않습니다 — 로그에만 남습니다).
다이제스트가 LLM을 쓰므로 `.env`가 필요하면 `schedule install`에 `--env`를 추가하세요:

```powershell
python -m aioffice.dataplat.schedule install --day MON --time 08:00 ^
  --pre "C:\team\run_aggregate.exe" --source source.yaml --db dataplat.sqlite --env .env
```

`GET /api/digest?dataset=<이름>`으로 최신 다이제스트를 조회합니다(배열 — 요약 하나 +
대상별 메모 여러 개). 대시보드 첫 화면에 카드로 보여주기 좋습니다(위젯 가이드는 16절).

## 14. AI에게 물어보기 — 비동기 `/api/ask`

10절에서 본 것처럼 `POST /api/chat`은 절대 LLM을 자동 호출하지 않습니다. `rule`/`cache`/
`button`으로도 못 푼 질문(`path:"none"`)에서 사용자가 "AI에게 물어보기" 버튼을 직접 누르면
그때만 아래 흐름을 탑니다:

1. `POST /api/ask {message, history?}` → 즉시 `202 {job_id}`. 단일 백그라운드 워커가 접수
   순서대로(FIFO) 처리합니다 — 사내 LLM 엔드포인트 자체가 한 번에 하나씩 처리하므로 워커를
   여러 개 둬도 소용없습니다.
2. `GET /api/ask/<job_id>`로 상태를 폴링합니다: `{status: queued|running|done|error|cancelled,
   position, elapsed_seconds, result?}`. `result`는 `done`일 때만 채워지고, `POST /api/chat`의
   응답과 완전히 같은 모양입니다(`path:"llm"`).
3. `DELETE /api/ask/<job_id>`로 취소합니다 — 대기 중이면 그냥 제거되고, 이미 실행 중이면
   그 결과가 나와도 버려집니다(`status:"cancelled"`로 남습니다).
4. `GET /api/ask?recent=20`으로 최근 작업 목록을 봅니다(완료 알림 배지 등에 사용).

작업은 `dataplat.sqlite`에 저장되므로 서버를 재시작해도 이미 끝난 작업의 결과는 남아
있습니다. 재시작 시점에 `running`이던 작업(서버가 죽으면서 중단된 것)은 자동으로
`error`("중단됨")로 정리됩니다. `POST /api/ask`/`DELETE /api/ask/<id>`는 `POST /api/refresh`와
같은 규칙으로 `--admin-token`이 필요합니다(비loopback 배포 시).

## 15. 학습된 별칭(Learned Aliases) 검토

"AI에게 물어보기"로 LLM이 대상을 풀어낸 질문 중, `rule` 경로/12절의 별칭으로는 찾지 못했던
이름이 있으면 그 질문 문장 전체를 새로운 별칭으로 자동 저장합니다(`learned_aliases`) — 다음에
**똑같은 질문**이 오면 LLM 호출 없이 바로 풀립니다. 결과가 빈 표였던 질문에서는 절대 학습하지
않습니다.

`GET /api/aliases`의 `learned` 배열로 확인하고, 잘못 학습된 게 있으면
`DELETE /api/aliases/learned/<id>`로 지웁니다(`--admin-token` 규칙은 14절과 동일). 가끔 훑어보고
자주 나오는 패턴은 `aliases.yaml`에 손으로 옮겨 적어두면(12절) 더 명확하고 관리하기 쉽습니다 —
학습된 별칭은 "정확히 같은 문장"에만 반응하는 반면, `aliases.yaml`은 그 단어가 포함된 모든
질문에 반응하기 때문입니다.

## 16. 위젯 연동 가이드 — 무엇을, 언제 렌더링할지

| 상황 | 렌더링 |
|---|---|
| `POST /api/chat` 응답 `path` in `rule`/`cache`/`button` | 표/차트/문장을 바로 렌더링. 로딩 표시 불필요(항상 즉시 응답) |
| `path === "none"` | `options`를 버튼 목록으로: `spec_patch` 있는 버튼은 클릭 시 `POST /api/chat {spec_patch, message}` 재호출, 마지막의 `action:"ask_ai"` 버튼은 14절로 |
| "AI에게 물어보기" 버튼 클릭 | `POST /api/ask` 후 "질문이 접수되었습니다" 안내 + 대기 목록에 추가. 5초 간격 정도로 `GET /api/ask/<id>` 폴링(또는 `?recent=`로 한꺼번에) |
| `/api/ask` 상태가 `running` | 이때만 "사내 LLM 응답 대기 중… (다른 사용자 요청과 순서를 기다리는 중)" 경과 시간 표시 |
| `/api/ask` 상태가 `done` | 완료 알림(배지/토스트) + `result`를 `path:"rule"`과 똑같이 렌더링 |
| 대시보드 첫 화면 | `GET /api/suggestions?dataset=`(추천 질문 버튼, `spec_patch` 포함)과 `GET /api/digest?dataset=`(최신 요약 카드)를 먼저 보여주면 사용자가 아무것도 안 물어봐도 바로 유용한 정보를 봅니다 |

참고용 구현이 `server/aioffice/dataplat/static/chat.html`(`/chat`)에 있습니다 — 추천 질문 카드,
다이제스트 카드, 되물음/추천 버튼, "AI에게 물어보기" 대기열과 폴링까지 전부 바닐라 JS로만
구현되어 있으니 팀 React 앱에 옮길 때 참고하세요.
