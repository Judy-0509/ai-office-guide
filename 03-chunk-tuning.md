이 문서는 사내 PC의 코딩 에이전트(OpenCode)에게 그대로 전달하는 작업 지시서입니다.
아래 내용은 프로젝트 저장소의 원본 지시서를 그대로 옮긴 것이며, 사람이 이 문서를 사내
에이전트에게 넘겨 청크 규칙(`chunking.yaml`) 튜닝을 위임할 때 씁니다.

---

# 청크 규칙 튜닝 작업 지시서 (사내 에이전트용)

이 문서는 **사내 PC의 OpenCode 에이전트**가 실제 증권사 리포트로 청크 규칙
(`server/chunking.yaml`)을 맞추기 위한 절차서입니다. 사람(MI 팀원)이 이 작업을 위임했고,
결과 보고는 사람이 사외로 가져가 개발 쪽에 전달합니다.

---

## 0. 목적과 행동 규칙

- **목적**: 리포트를 AI 생성 없이 청크로 나눌 때, 끝부분 disclaimer·머리말/꼬리말·상투 문구가
  **하나도 남지 않고**, 본문은 **잘못 잘리지 않으며**, 1페이지 요약이 제대로 잡히도록 규칙을 맞춥니다.
- **보안 (가장 중요)**:
  - 리포트 원문, 청크 텍스트, 파일명, 종목·수치가 담긴 문장은 **보고서에 절대 넣지 않습니다.**
    파일명도 종목이 드러날 수 있으므로 `문서 1`, `문서 2`처럼 번호로만 부릅니다.
  - 보고서에 넣어도 되는 것: 집계 수치, `chunking.yaml`에 추가·수정한 **일반 제목 문구**
    (예: `Compliance Notice`, `투자의견 비율`), 문제 패턴에 대한 **일반화된 설명**
    (예: "disclaimer가 제목 없이 표로 시작함").
  - `chunk_preview.html`, `chunks.json`은 사내 PC 밖으로 옮기지 않습니다. 작업이 끝나면 지웁니다.
- **수정 범위**: `server/chunking.yaml`만 수정합니다. **Python 코드는 수정하지 않습니다.**
  규칙 파일로 해결이 안 되는 패턴은 6장 보고 형식의 "코드 반영 필요"에 적습니다.
- 이 도구는 **AI 모델을 호출하지 않습니다.** 네트워크 없이 PC 안에서만 돕니다.
- 각 단계의 "확인" 항목을 통과해야 다음 단계로 넘어갑니다. 판단이 필요한 상황이면 멈추고 사람에게 묻습니다.

---

## 1. 준비

저장소 루트에서:

```powershell
cd server
.\.venv\Scripts\python.exe -m aioffice.tools.chunk_preview --help
```

- 확인: 도움말이 출력됩니다.
- 사람에게 받을 것: 이번 주(또는 최근 1~2주) 리포트 zip 경로 1~3개. 증권사가 다양할수록 좋습니다.
- 사람에게 물을 것: 사내에서 이미 정리해 둔 disclaimer 시작 문구·키워드 목록이 있는지.
  있으면 3장 첫 단계에서 합칩니다.

---

## 2. 기준선 측정

```powershell
.\.venv\Scripts\python.exe -m aioffice.tools.chunk_preview "<zip 경로>" --out "$env:TEMP\chunk_preview.html" --json "$env:TEMP\chunks.json"
```

docling이 설치되어 있으면 비교용으로 한 번 더 돌립니다 (없으면 건너뜀):

```powershell
.\.venv\Scripts\python.exe -m aioffice.tools.chunk_preview "<zip 경로>" --parser docling --out "$env:TEMP\chunk_preview_docling.html" --json "$env:TEMP\chunks_docling.json"
```

아래 점검 스크립트를 `server\check_chunks.py`로 저장해 실행합니다 (작업 후 삭제).

```python
import json, sys, yaml
from collections import defaultdict

data = json.load(open(sys.argv[1], encoding="utf-8"))
rules = yaml.safe_load(open("chunking.yaml", encoding="utf-8"))
leak_terms = [t.casefold() for t in rules.get("disclaimer_keywords", []) + rules.get("inline_notice_patterns", [])]

rows = defaultdict(lambda: defaultdict(int))
for i, doc in enumerate(data["documents"], 1):
    b = doc.get("broker_guess") or "(미상)"
    kinds = doc["stats"].get("chunks_by_kind", {})
    reasons = doc["stats"].get("removed_by_reason", {})
    r = rows[b]
    r["문서"] += 1
    r["disclaimer 검출"] += int(any(k.startswith("disclaimer") for k in reasons))
    r["요약 0개"] += int(kinds.get("summary", 0) == 0)
    r["요약 대체"] += int(any(c.get("origin") == "fallback" for c in doc["chunks"]))
    r["본문 0개"] += int(kinds.get("body", 0) == 0)
    leaks = sum(1 for c in doc["chunks"] if any(t in c["text"].casefold() for t in leak_terms))
    r["누출 청크"] += leaks
    if leaks:
        print(f"누출: 문서 {i} ({b}) {leaks}건")
    r["남은 토큰"] += doc["stats"].get("tokens_kept", 0)
    r["제거 토큰"] += doc["stats"].get("tokens_removed", 0)
    r["초"] += doc["stats"].get("seconds", 0)

cols = ["문서", "disclaimer 검출", "요약 0개", "요약 대체", "본문 0개", "누출 청크", "남은 토큰", "제거 토큰", "초"]
print("증권사 | " + " | ".join(cols))
for b, r in sorted(rows.items()):
    print(b + " | " + " | ".join(str(round(r[c], 1)) for c in cols))
print("오류 문서:", len(data.get("errors", [])))
```

```powershell
.\.venv\Scripts\python.exe check_chunks.py "$env:TEMP\chunks.json"
```

- 확인: 증권사별 표가 출력됩니다. 이 표를 **기준선**으로 저장해 둡니다 (보고서에 들어감).

---

## 3. 규칙 수정 (문제 유형별)

HTML을 브라우저로 열고, 경고가 붙은 문서부터 봅니다. 문서마다 "남은 청크"와 "제거된 부분(이유)"이 나옵니다.

**0) 사내 기존 키워드 합치기** — 1장에서 받은 목록 중 `chunking.yaml`에 없는 것을
해당 키(`disclaimer_keywords` 또는 `inline_notice_patterns`)에 추가합니다.

| 증상 | 확인할 곳 | 수정 |
|---|---|---|
| `disclaimer 미검출` 또는 누출 청크 | 문서 끝부분의 남은 청크에서 고지·면책·투자등급 설명이 **시작되는 제목 문구** | 그 제목 문구를 `disclaimer_keywords`에 추가 (문장 전체가 아니라 제목만) |
| 페이지마다 붙는 한 줄 고지가 남음 | 남은 청크 중 "Please see…", "본 자료는…" 같은 한 줄 | 특징적인 앞부분을 `inline_notice_patterns`에 추가 |
| 본문이 disclaimer로 잘림 (과잉 삭제) | 제거된 부분 중 이유가 `disclaimer`인데 분석 내용인 것 | 원인 키워드를 더 구체적인 문구로 바꾸거나 삭제. 여전히 앞쪽에서 잘리면 `disclaimer_min_position`을 0.4~0.5로 올림 |
| `요약 0개` / `요약 대체` | 1페이지 구조: 요약 제목이 있는지, 불릿 기호가 무엇인지 | 요약 제목 문구를 `summary_headings`에, 처음 보는 불릿 기호를 `bullet_markers`에 추가 |
| 머리말/꼬리말이 남음 | 페이지마다 반복되는 증권사명·리포트명이 청크에 섞임 | 위치가 조금 안쪽이면 `header_footer_zone`을 0.07→0.09로. 반복 비율이 낮으면 `header_footer_min_ratio`를 0.5→0.4로 |
| 주가·목표주가 박스가 본문에 섞임 | 1페이지 옆 박스 | 그 박스에만 있는 단어를 `sidebar_keywords`에 추가 |
| 같은 증권사 상투 문단이 남음 | 여러 문서에 똑같은 문단 | 해당 증권사 문서가 3건 미만이면 자동 제거가 안 됨 → 반복 문단의 앞부분을 `inline_notice_patterns`에 추가 |
| 표 캡션이 엉뚱하게 붙음 | 표 청크의 캡션 | 규칙으로 못 고침 → 6장 "코드 반영 필요"에 기록 |

규칙:
- 한 번에 한 유형씩 고치고 2장을 다시 돌려 **수치가 나빠지지 않았는지** 확인합니다.
- 너무 일반적인 단어(예: `Risk`, `위험`, `Note`)는 키워드로 넣지 않습니다. 본문을 잘라 먹습니다.

---

## 4. 통과 기준

모두 만족하면 끝입니다.

- 증권사 리포트의 **95% 이상**에서 disclaimer 검출 (조사기관 리포트는 disclaimer가 짧거나 없을 수 있어 제외 가능)
- **누출 청크 0건**
- 과잉 삭제 0건 (본문이 disclaimer로 잘린 문서 없음 — HTML로 표본 5건 이상 확인)
- **90% 이상**의 문서에서 요약 청크 1개 이상 (대체 포함). 대체 비율이 30%를 넘으면 증권사별로 원인 기록
- 오류 문서 0건 (있으면 원인을 일반화해 기록)

---

## 5. docling 비교 (설치된 경우만)

같은 zip에 대해 두 결과를 비교해 표로 남깁니다: 문서당 평균 초, 요약 0개 문서 수,
표 청크 수, 누출 청크 수, 눈으로 본 품질 차이(일반화된 설명). 원문 인용 금지.

---

## 6. 보고 형식 (사외 반출용 — 원문·파일명 금지)

```
# 청크 규칙 튜닝 결과 (YYYY-MM-DD)
- 대상: zip N개, 문서 N건, 증권사 N곳
## 1. 수치 (기준선 → 최종)
(2장 표를 증권사별로, 기준선과 최종 두 벌)
## 2. chunking.yaml 변경 내역
(git diff server/chunking.yaml 결과 그대로. 추가한 것은 모두 일반 제목 문구여야 함)
## 3. 코드 반영 필요 (규칙으로 해결 불가)
- 패턴: (일반화된 설명, 예: "○○ 유형 리포트는 disclaimer가 제목 없이 투자의견 변동 표로 시작함")
- 영향 문서 수 / 증권사 수
## 4. docling 비교 (선택)
## 5. 기타 관찰
```

작업이 끝나면 `check_chunks.py`, `$env:TEMP\chunk_preview*.html`, `$env:TEMP\chunks*.json`을 지웁니다.
