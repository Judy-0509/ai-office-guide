# AI Office 사내 작업 안내서

사외 개발 환경에 접근할 수 없는 사내 PC에서 참고하기 위한 문서와 코드 모음입니다. 리포트
원문·파일명·팀 업무 세부 내용은 이 저장소에 올리지 않습니다.

## 이 저장소는 무엇인가

- **AI 애널리스트** (`server/aioffice/analyst`) — 증권사 리포트를 날짜순으로 10건씩 학습해
  주제·주장·관계를 Markdown 지식 폴더("vault")와 SQLite에 쌓고, 오프라인 뷰어로 보는 도구입니다.
- **사내 LLM 벤치마크** — 동시 요청 처리량과 리포트 추출 품질을 재는 독립 실행 스크립트
  (`llm_bench.py`, 아래 참고).
- **슬라이드 스튜디오** (`server/aioffice/slides`) — 사내 LLM이 주간 리포트 슬라이드를 라이브로
  그려주는 이전 작업물입니다. 사용법은 `server/README.md`의 "슬라이드 스튜디오" 절을 보세요.

## 빠른 시작 — AI 애널리스트

1. **받기**: 이 저장소를 사내 PC로 내려받습니다.
2. **설치** (PowerShell):
   ```
   cd server
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -e ".[dev]"
   ```
   PDF 리포트를 읽으려면 `pip install docling`(표·OCR 없이 변환) 또는 `pip install pymupdf`
   (가볍고 빠름) 중 하나를 추가로 설치하세요. 둘 다 없으면 `.pdf`는 건너뜁니다.
3. **`.env` 작성**: `Copy-Item .env.example .env` 후 사내 LLM 주소·키·모델을 채웁니다. 키
   하나하나의 뜻은 [`02-analyst-manual.md`](02-analyst-manual.md)를 보세요.
4. **리포트 폴더 준비**: 학습할 `.md`/`.txt`/`.pdf` 리포트를 폴더 하나에 모읍니다. 견본은
   `server/aioffice/analyst/samples/reports/`에 있습니다.
5. **실행**:
   ```
   python -m aioffice.analyst.run --inbox <리포트 폴더> --vault <vault 폴더> --env .env --batch 10
   ```
   앱 창이 뜨고 다음 10건 학습이 자동으로 시작됩니다.

## 문서

- [`01-topic-importance-approaches.md`](01-topic-importance-approaches.md) — 처음 보는 리포트
  100~200건에서 이번 주 핵심 주제와 주제 간 연관을 찾는 3가지 방법 비교
- [`02-analyst-manual.md`](02-analyst-manual.md) — AI 애널리스트 사용 설명서(설치·화면·학습
  과정·DB·git·문제 해결)
- [`03-dataplat-manual.md`](03-dataplat-manual.md) — 데이터 플랫폼 연결 설명서(팀 기존
  SQLite를 뷰 하나로 연결해 버전 관리·API·대시보드·챗봇에 붙이는 법, 검증 체크리스트)
- [`AGENTS.md`](AGENTS.md) — 사내 코딩 에이전트(OpenCode)용 운영 지침(AI 애널리스트 +
  데이터 플랫폼)

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
