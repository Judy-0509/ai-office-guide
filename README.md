# AI Office 데이터 플랫폼

사외 개발 환경에 접근할 수 없는 사내 PC에서 참고하기 위한 문서와 코드 모음입니다. 리포트
원문·파일명·팀 업무 세부 내용은 이 저장소에 올리지 않습니다.

## 이 저장소는 무엇인가

**대시보드 데이터 연결 도구**(`server/aioffice/dataplat`) — 팀이 이미 엑셀을 읽어 SQLite로
집계해 둔 파이프라인을, 다시 만들지 않고 버전 관리(주차별 비교) + HTTP API + 대시보드 + 한국어
챗봇에 연결합니다. **엑셀을 직접 읽지 않습니다** — 팀의 기존 SQLite를 읽기 전용으로만 열고,
표준 컬럼(`dataset, metric, entity, region, period, source, value, unit`)을 내는 뷰/쿼리
하나만 있으면 나머지(버전 관리·API·대시보드·챗봇)는 자동입니다.

## 빠른 시작

1. **받기**: 이 저장소를 사내 PC로 내려받습니다.
2. **설치** (PowerShell):
   ```
   cd server
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -e ".[dev]"
   Copy-Item .env.example .env
   ```
   추가 설치(extras)가 필요 없습니다 — 표준 의존성만으로 동작합니다.
3. **`.env` 작성**: 사내 LLM 주소·키·모델을 채웁니다(챗봇의 spec/설명 두 호출에 씁니다). 키
   하나하나의 뜻은 [`server/README.md`](server/README.md)를 보세요.
4. **팀 SQLite에 연결**: 표준 컬럼을 내는 뷰를 하나 쓰고 `source.yaml`로 가리킵니다 — 예시와
   세 가지 매핑 패턴(컬럼명만 맞추기 / 여러 테이블 UNION / wide→long)은
   [`03-dataplat-manual.md`](03-dataplat-manual.md) 2절을 보세요.
5. **점검 후 실제 적재**:
   ```
   python -m aioffice.dataplat.snapshot --source source.yaml --db dataplat.sqlite --dry-run
   python -m aioffice.dataplat.snapshot --source source.yaml --db dataplat.sqlite
   ```
6. **서버 실행** (API + 대시보드 + 챗봇, 같은 origin):
   ```
   python -m aioffice.dataplat.server --db dataplat.sqlite --source source.yaml --site <대시보드 site/ 빌드 폴더> --env .env
   ```

설치부터 예약 실행·대시보드 연동·검증까지 전체 흐름은
**[`03-dataplat-manual.md`](03-dataplat-manual.md)**에 정리되어 있습니다.

## 문서

- [`03-dataplat-manual.md`](03-dataplat-manual.md) — 데이터 플랫폼 연결 설명서(뷰 작성법,
  `source.yaml`, 스냅샷/예약/서버 명령, API 엔드포인트, 대시보드 연동(`dataplat-client.ts`),
  챗봇 동작·한계, 검증 체크리스트)
- [`server/README.md`](server/README.md) — 설치·`.env` 키·명령어 요약(짧은 안내)
- [`AGENTS.md`](AGENTS.md) — 사내 코딩 에이전트(OpenCode)용 운영 지침
- [`01-topic-importance-approaches.md`](01-topic-importance-approaches.md) — 참고 문서: 처음
  보는 리포트 100~200건에서 이번 주 핵심 주제와 주제 간 연관을 찾는 3가지 방법 비교

## 사내 LLM 성능 측정 스크립트 — `llm_bench.py`

사내 LLM을 동시에 여러 건 호출했을 때의 속도와, 리포트에서 주장을 뽑는 호출의 품질을 잽니다.
Python 3.10 이상, 표준 라이브러리만 사용해서 설치할 것이 없습니다.

```
python llm_bench.py --selftest
python llm_bench.py --base-url http://<사내 주소>/v1 --model <모델 ID> --api-key <키> ^
    --levels 1,5,10,20 --reports <리포트 폴더 또는 파일> --out bench_result.json
```

- `--reports`: 길이가 서로 다른 리포트 10~20개가 든 폴더를 권장합니다. 파일 1개만 넣으면 같은 리포트를 반복해 실제보다 빠르게 나올 수 있습니다. 생략하면 내장 가상 리포트 4개로 돌아갑니다.
- 호출 수: 레벨 합계만큼(위 예시는 36회) + 스트리밍 확인 1회. 레벨 사이에 `--pause`초(기본 20초) 쉽니다.
- "잘린 호출이 있습니다" 안내가 나오면 `--max-tokens`를 늘려 다시 돌립니다(기본 16000).
- 결과: 콘솔 요약과 `bench_result.json`에는 **숫자와 라벨만** 들어갑니다. 리포트 본문, 모델 답변, 파일명, 사내 주소, API 키는 저장되지 않으므로 이 두 가지를 그대로 가져오면 됩니다.
