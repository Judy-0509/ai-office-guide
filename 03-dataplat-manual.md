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
| `period` | O | 기간. `YYYY`/`YYYYQn`/`YYYY-MM`/`YYYYWnn` 형식을 자동 인식하고, 그 외 텍스트는
  그대로 두되 스냅샷 리포트의 `unparsed_periods`에 카운트됩니다 |
| `source` | - | 기관/출처. 없으면 빈 문자열 |
| `value` | O | 숫자. `"1,234"`/`"12.5%"`/`"(3.2)"` 같은 텍스트도 자동으로 숫자로 바뀌고(`%`는
  단위로 남음), `""`/`"-"`/`"n.a."`류는 빈값으로, 그 외 숫자로 못 바꾸는 값은 "숫자 아님"으로
  세어 버립니다(둘 다 관측치로 저장되지 않음 — 스냅샷 리포트에서 몇 건 빠졌는지 확인하세요) |
| `unit` | - | 단위 |

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

## 3. `source.yaml` 작성

```yaml
db: C:/data/team.sqlite            # 이 파일(source.yaml) 기준 상대경로도 허용
view: v_dataplat_observations      # 뷰 이름 (또는 query: "SELECT ..." 로 직접 쿼리)
rename:                            # 뷰가 표준 이름과 다른 컬럼명을 낼 때만 (선택)
  broker: source
```

`db`/`view`(또는 `query`) 중 하나라도 빠지면, 또는 `query`와 `view`를 동시에 쓰면 바로
`ValueError`로 실패합니다(잘못된 설정으로 조용히 넘어가지 않음).

## 4. 점검: `snapshot --dry-run`

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

## 5. 서버 시작 (`--site`로 대시보드까지 같이)

```powershell
python -m aioffice.dataplat.server --db dataplat.sqlite --source source.yaml ^
  --site <대시보드 site/ 빌드 폴더> --pre "<기존 집계 명령>" --env .env --port 8000 --admin-token <토큰>
```

브라우저로 `http://127.0.0.1:8000/`을 열면 `--site`로 지정한 대시보드가, `/api/*`로 API가,
`/chat`으로 참고용 챗 페이지가 같은 origin에서 뜹니다(운영 환경에서 CORS 설정 불필요).
`127.0.0.1`이 아닌 주소로 열려면 `--admin-token`이 필수입니다(없으면 시작 자체를 거부합니다).

## 6. 대시보드의 데이터 로딩을 API로 바꾸기

`server/aioffice/dataplat/clients/dataplat-client.ts`를 대시보드 프로젝트에 복사합니다(차트
라이브러리를 지정하지 않은, `fetch`만 쓰는 타입 클라이언트라 팀이 쓰는 어떤 라이브러리와도
같이 쓸 수 있습니다). 기존에 엑셀/CSV를 직접 읽던 곳을 아래처럼 바꿉니다:

```tsx
import { DataplatClient } from "./dataplat-client";
const client = new DataplatClient(""); // 서버가 site를 같이 서빙하므로 같은 origin("")

// 필터 드롭다운 채우기
const { datasets } = await client.catalog();

// 표 하나 (엔티티×기간 wide)
const table = await client.queryWide({
  dataset: "shipments", format: "wide", rows: ["entity"], cols: ["period"],
});

// 특정 셀의 개정 이력(리비전 차트)
const { history } = await client.history({
  dataset: "shipments", metric: "출하량", entity: "모델A", period: "2024Q1",
});

// "갱신" 버튼
await client.refresh();               // POST /api/refresh, admin 토큰 필요
const status = await client.refreshStatus();
```

React라면 `useEffect`로 마운트 시 `catalog()`/`queryWide()`를 호출하고 `useState`로 담으면
됩니다(`server/README.md`의 예시 참고). 갱신 버튼은 `refresh()` 호출 후 `refreshStatus()`를
짧은 간격으로 폴링해 `running`이 꺼지면 데이터를 다시 불러오도록 구현하세요.

## 7. 예약 등록 (팀 기존 집계 명령과 함께)

```powershell
python -m aioffice.dataplat.schedule install --day MON --time 08:00 ^
  --pre "C:\team\run_aggregate.exe" --source source.yaml --db dataplat.sqlite
```

매주 월요일 08:00에 `--pre` 명령(팀이 이미 쓰던 집계 실행 파일/스크립트)을 먼저 실행하고,
**성공했을 때만** 스냅샷을 실행하는 `.cmd`가 `dataplat.sqlite` 옆에 만들어지고 그걸 Windows
작업 스케줄러에 등록합니다. `--pre`를 생략하면 스냅샷만 등록됩니다. 로그는
`dataplat_run.log`(같은 폴더)에 남습니다. `schedule show`로 등록 상태, `schedule remove`로
삭제합니다.

## 8. 검증 체크리스트

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
   | 6~10 | (팀 데이터셋에 맞게 반복) | | | | |

   "정답" 판정은 `table`에 나온 숫자를 1번 항목처럼 원본과 대조해서 내립니다. 모호한 질문에서
   되물음 없이 틀린 값을 확신에 차서 답하면 실패로 기록하세요(챗봇은 표에 없는 숫자를 답하지
   않도록 설계되어 있지만, spec 단계에서 잘못된 데이터셋/지표를 고르는 것까지는 막지 못합니다).

   응답의 `warnings` 배열도 함께 보세요. `"존재하지 않는 조건 '...'는 제외했습니다"`는
   **되물음이 아닙니다** — 모델이 질문에 없던 조건(기관/지역, 또는 사용자가 직접 지목하지
   않은 대상)을 스스로 덧붙였는데 카탈로그에 없어서 그 조건만 빼고 나머지는 정상 조회했다는
   뜻이니, `table`이 채워져 있으면 정답 판정을 그대로 진행하세요. 표가 비고 `clarify`
   워닝만 있을 때만 "되물음"으로 집계합니다 — 데이터셋/지표를 못 찾았거나, 사용자가 메시지에
   직접 적은 이름(예: "모델A랑 없는모델 비교해줘"의 "없는모델")이 카탈로그에 없을 때만
   되물음이 발생합니다.
3. **스케줄 확인**: `schedule show`로 다음 실행 시각이 기대한 요일/시각인지 확인합니다.
4. **`--admin-token` 없이 갱신이 막히는지**: `POST /api/refresh`를 토큰 없이 호출해 401이
   나오는지 한 번은 확인하세요(운영 배포 전 1회면 충분).

## 9. 문제 해결

| 증상 | 확인할 것 |
|---|---|
| 스냅샷이 매번 `skipped_duplicate` | 팀 원본이 실제로 안 바뀌었는지 먼저 확인(정상 동작일 수 있음). `--pre` 집계가 조용히 실패해 원본이 그대로인 경우도 있으니 `dataplat_run.log`를 보세요 |
| 특정 값이 관측치에서 빠짐 | `--dry-run` 리포트의 `dropped_blank`/`dropped_non_numeric`/`unparsed_periods`를 확인 — 뷰의 `value`/`period` 표현식이 그 셀에서 기대와 다른 텍스트를 내고 있을 가능성 |
| API가 401 | `--admin-token` 설정 여부와 `Authorization: Bearer <token>` 헤더 확인(읽기 엔드포인트는 토큰이 필요 없음 — `/api/refresh`만) |
| 서버가 비loopback 주소로 시작을 거부 | `--host`가 `127.0.0.1`/`localhost`가 아니면 `--admin-token`이 필수입니다 |
| 챗봇이 계속 되물음만 함 | 질문에 **직접 쓴** 이름(데이터셋/지표, 또는 메시지에 등장한 대상명)이 카탈로그(`GET /api/catalog`)에 있는 이름과 너무 다른지 확인 — 유사도 0.85 미만이면 자동 교정 대신 후보를 보여줍니다. 기관/지역, 또는 모델이 스스로 추가한 대상(사용자가 언급하지 않은)이 카탈로그에 없을 때는 되물음 대신 `warnings`에 "존재하지 않는 조건 '...'는 제외했습니다"만 남기고 조회는 계속됩니다 — 이건 정상 동작입니다 |
