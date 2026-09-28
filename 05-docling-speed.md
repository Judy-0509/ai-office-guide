# docling 변환 속도 개선 (사내 에이전트용)

이 문서는 사내에서 리포트 PDF를 md로 바꿀 때 docling이 **PDF 1개에 1~2분** 걸리는 문제를 줄이기 위한
설정과 측정 절차입니다. 사내 파이프라인 코드는 사내 에이전트가 이 문서를 보고 고칩니다.
원칙은 **"리포트 md에 필요 없는 일은 끄고, 필요한 일은 한 번만 한다"**입니다.

---

## 0. 먼저 확인할 것

- docling 버전: `python -c "import docling, importlib.metadata as m; print(m.version('docling'))"`
- CPU 코어 수, GPU 유무 (`nvidia-smi`가 되는지)
- 현재 코드가 **파일마다 `DocumentConverter`를 새로 만드는지** — 그렇다면 1번만 고쳐도 크게 줄어듭니다.

> 아래 옵션 이름은 docling 2.x 기준입니다. 버전에 따라 이름·위치가 다를 수 있으니,
> 설치된 버전에서 import가 안 되면 해당 버전 문서를 확인해 맞춥니다.

---

## 1. 권장 설정 (효과 큰 순서)

| # | 할 일 | 이유 |
|---|---|---|
| 1 | 변환기를 **프로세스당 1번만** 만들고 모든 파일에 재사용 | 만들 때마다 레이아웃·표 인식 모델을 다시 불러옵니다 |
| 2 | **OCR 끄기** | 증권사 리포트는 글자가 들어 있는 PDF라 OCR이 필요 없고, CPU에서 가장 느린 단계입니다 |
| 3 | **이미지 생성 끄기** (페이지·그림·표 이미지) | md에는 이미지가 필요 없습니다 |
| 4 | **표 인식 빠른 모드** | 수치 때문에 표 인식은 유지하되, 정밀 모드 대신 빠른 모드 |
| 5 | **CPU 스레드 = 코어 수** (GPU가 있으면 GPU) | 기본값이 코어보다 적을 수 있습니다 |
| 6 | (품질 비교 후) **가벼운 PDF 백엔드** | 텍스트 읽기가 빨라지지만 품질 차이를 꼭 확인합니다 |
| 7 | **본문 페이지만 변환** | 뒤쪽 disclaimer 페이지는 변환할 필요가 없습니다 (2장) |
| 8 | **여러 파일 동시 처리** (프로세스 2~4개) | docling은 PC의 CPU를 쓰므로, 사내 LLM과 달리 병렬 효과가 있습니다 |
| 9 | **파일 해시로 캐시, 매일 미리 변환** | 이미 변환한 파일은 다시 하지 않고, 주말 작업 때는 새 파일만 처리합니다 |

설정 예시 (docling 2.x):

```python
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
from docling.document_converter import DocumentConverter, PdfFormatOption
# 버전에 따라 둘 중 하나:
from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
# from docling.datamodel.pipeline_options import AcceleratorOptions, AcceleratorDevice

opts = PdfPipelineOptions()
opts.do_ocr = False                                  # 2
opts.generate_page_images = False                    # 3
opts.generate_picture_images = False                 # 3
opts.do_table_structure = True
opts.table_structure_options.mode = TableFormerMode.FAST   # 4
opts.accelerator_options = AcceleratorOptions(       # 5
    num_threads=8,                     # CPU 코어 수에 맞춤
    device=AcceleratorDevice.AUTO,     # GPU가 있으면 자동 사용
)

converter = DocumentConverter(                       # 1: 한 번만 생성
    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
)
# 6 (선택): 가벼운 백엔드 — 품질 비교 후 결정
# from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
# PdfFormatOption(pipeline_options=opts, backend=PyPdfiumDocumentBackend)

for pdf in pdf_files:
    result = converter.convert(pdf)                  # 7: 버전이 지원하면 page_range=(1, 본문 마지막 페이지)
    md = result.document.export_to_markdown()
```

---

## 2. 본문 페이지만 넘기기 (7번)

docling 전에 PyMuPDF로 각 페이지 **첫 5줄**만 읽어(파일당 1초 미만) disclaimer 시작 페이지를 찾습니다.

- 키워드: `Compliance Notice`, `컴플라이언스`, `Disclaimer`, `면책`, `고지사항`, `Important Disclosures`,
  `Analyst Certification`, `투자등급`, `투자의견 비율`, `Rating Definitions` (사내 목록이 있으면 합칩니다)
- 문서 **뒤쪽 절반**에서 처음 걸린 페이지부터 끝까지를 제외합니다. 앞쪽 절반에서 걸리면 무시합니다(1페이지 고지 박스 등).
- 확신이 없으면 전체 페이지를 넘깁니다. 이후 사내 LLM의 disclaimer 제거 단계가 남은 부분을 처리합니다.
- 참고: 사내 LLM으로 disclaimer를 지울 때 **본문을 다시 쓰게 하지 말고, 시작 줄 번호만 답하게** 하면 AI 출력이 몇 토큰으로 줄어듭니다.

---

## 3. 측정 절차

**A. 설정별 측정 (PDF 1개, 20페이지 안팎)**

아래를 하나씩 **누적해서** 켜고, 각각 `초/페이지`를 잽니다. 1번 효과를 보려면 같은 변환기로 2번째 파일부터 잽니다.

| 단계 | 설정 | 초/페이지 | 품질 메모 |
|---|---|---|---|
| 0 | 현재 설정 | | 기준 |
| 1 | + 변환기 재사용 | | |
| 2 | + OCR 끄기 | | |
| 3 | + 이미지 끄기 | | |
| 4 | + 표 빠른 모드 | | 기준 대비 표 2~3개 비교 |
| 5 | + 스레드 = 코어 수 | | |
| 6 | + 가벼운 백엔드 | | 본문·표 비교 필수 |
| 7 | + 본문 페이지만 | | |

**B. 동시 처리 측정 (PDF 10개)**

최종 설정으로 프로세스 1개 / 2개 / 4개를 비교합니다. 프로세스마다 변환기를 따로 만들고,
`num_threads`는 `코어 수 ÷ 프로세스 수`로 나눕니다. **10개 전체 걸린 시간**을 잽니다.

**품질 확인:** 단계 0과 최종 설정의 md를 2~3건 나란히 보고 확인합니다.
- 본문 누락
- 표의 행·열 깨짐
- 제목(`#`) 구조 변화

---

## 4. 보고 형식 (사외 반출용 — 원문·파일명 금지)

```
# docling 속도 측정 (YYYY-MM-DD)
- docling 버전 __, CPU 코어 __, GPU 있음/없음
## A. 설정별 (초/페이지)
0 __ / 1 __ / 2 __ / 3 __ / 4 __ / 5 __ / 6 __ / 7 __
## B. 동시 처리 (PDF 10개 총 초)
1개 __ / 2개 __ / 4개 __
## 품질 (일반화된 설명)
- 표 빠른 모드: ____ / 가벼운 백엔드: ____
## 최종 채택 설정과 예상 처리 시간 (100건 기준)
```
