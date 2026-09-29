#!/usr/bin/env python3
"""사내 LLM 병렬 처리 성능 벤치마크.

Python 표준 라이브러리만 사용하는 단일 파일 스크립트입니다. 스트리밍 프로브 1회 + 동시성
레벨별 비스트리밍 "리포트 추출" 호출을 실행해 처리량과 추출 품질을 측정합니다.

개인정보/보안: `server/.env`의 API 키는 절대 출력하거나 파일에 저장하지 않습니다. 콘솔 출력과
`--out` JSON에는 숫자와 라벨만 들어가며, 리포트 본문·모델 출력 텍스트·프롬프트·파일명·API
호스트(스킴 제외)는 절대 포함하지 않습니다.

실행 예:
    python llm_bench.py --env server/.env --levels 1,5,10 --pause 20
    python llm_bench.py --selftest   (네트워크 없이 내부 로직만 점검)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

CLAIM_TYPES = {"fact", "forecast", "opinion"}
CLAIM_DIRECTIONS = {"up", "down", "flat", "none"}

SYSTEM_PROMPT = """You are a market-intelligence analyst's assistant.
Read the numbered sentences of ONE broker report below and extract 3 to 7 key claims.
Rules:
- Each claim cites only sentence ids that appear in the input, in field "sids" (e.g. ["S3"]).
- Do not invent numbers or facts that are not present in the cited sentences.
- Write claim text and topic labels in Korean.
- Answer with ONE JSON object and nothing else -- no explanation, no markdown fence.

JSON schema:
{"report_date": "YYYY-MM-DD|null", "institution": "string|null",
 "claims": [{"sids": ["S3"], "text": "한국어 한 문장 요약 (<=60자)", "type": "fact|forecast|opinion",
             "direction": "up|down|flat|none", "entities": ["Apple", "iPhone 18"],
             "metric": "string|null", "value": "string|null", "unit": "string|null", "period": "string|null"}],
 "topics": ["짧은 주제 라벨", "..."]}
"""

# 아래 4개는 전량 가상의 테스트용 리포트입니다 (실제 기업 실적/가이던스 아님).
BUILTIN_REPORTS = [
    """A증권 리서치센터 | 스마트폰·부품 공급망 위클리 (가상 자료) | 2026-03-14

투자의견: 중립. 본 자료는 전량 가상의 채널 점검과 추정치로 구성된 테스트용 리포트이며 실제
기업의 확정 실적이나 가이던스를 포함하지 않는다. 2026년 1분기 글로벌 스마트폰 출하량은 2억
8,700만 대로 전년 동기 대비 3.4% 감소했다. 중국 내수 판매는 5,200만 대로 전분기 대비 1.8%
늘었으나 전년 대비로는 여전히 6.1% 낮다. 북미 시장은 4,100만 대로 보합권에 머물렀다. 프리미엄
모델 비중은 38%에서 42%로 상승했다. 평균판매가격(ASP)은 412달러로 전년 대비 5.3% 올랐다.
메모리 반도체 가격은 1분기에 12% 상승했고 2분기에도 추가로 6~8% 오를 것으로 전망한다. 낸드
플래시 계약가격은 기가바이트당 0.041달러로 전분기의 0.037달러에서 상승했다. 카메라 모듈
공급사의 수주 잔고는 3,600억원으로 전분기 대비 14% 늘었다. 디스플레이 패널 가동률은 82%로
3개월 연속 상승했다. 배터리셀 공급사 네 곳의 합산 출하량은 1억 9,000만 셀로 추정된다. 폴더블
모델 비중은 전체 출하량의 4.2%까지 확대됐다. 조립업체의 4월 생산계획은 기존 대비 3% 하향
조정됐다. 부품 재고일수는 6.4주로 전분기의 5.8주보다 늘었다. 이는 수요 둔화 신호로 해석될 수
있다. 채널 재고 증가와 함께 프로모션 빈도도 늘었다. 공급망 서베이는 협력사 18곳, 유통사 9곳을
대상으로 3월 1일부터 10일까지 진행했다. 응답 업체의 61%는 2분기 주문량을 유지하거나 소폭 늘릴
계획이라고 답했다. 22%는 주문을 줄일 계획이라고 답했다. 나머지 17%는 미정이라고 응답했다.
반도체 위탁생산 라인 가동률은 첨단 공정 기준 94%로 매우 높은 수준을 유지했다. 레거시 공정
가동률은 71%로 상대적으로 낮았다. 이 격차는 최근 3개 분기 동안 계속 벌어지고 있다. 환율은
달러당 1,320원을 가정했다. 해상 운임은 상하이-LA 노선 기준 40피트 컨테이너당 2,150달러로
전분기 대비 9% 하락했다. 이는 완제품 물류비 부담을 다소 덜어주는 요인이다. 부품 협력사 3곳은
5월 초 가격 재협상을 요청했다고 알려졌다. 반도체 패키징 외주업체의 3월 가동률은 88%로
전월보다 2%포인트 낮아졌다. 스마트워치용 소형 배터리 수요는 전분기 대비 11% 늘었다. 태블릿
출하량은 2,400만 대로 보합세를 보였다. 결론적으로 단기 수요는 둔화됐지만 프리미엄 믹스와 부품
가격 상승이 매출 방어 요인으로 작용할 전망이다. 2분기 실적은 출하량보다 믹스와 ASP 흐름을
중심으로 봐야 한다는 것이 본 자료의 핵심 논지다. 부가 조사로 국내 조립라인 3곳의 4월 근무일수는
평균 22일로 집계됐다. 수출용 완제품 검사 합격률은 97.8%로 전월과 유사한 수준이다. 원자재
구매팀은 알루미늄 프레임 단가가 톤당 2,480달러로 3% 올랐다고 밝혔다. 유리 커버 공급사 두
곳의 3월 합산 생산량은 8,600만 장으로 전월 대비 4% 늘었다. 방수 접착 테이프 공급사는 4월
리드타임이 5일에서 7일로 늘었다고 전했다. 무선충전 코일 부품 수요는 전분기 대비 9% 증가했다.
패키징 박스 공급사의 3월 단가는 개당 0.62달러로 보합세를 유지했다. 완제품 재고는 유통 채널
기준 7.1주로 전분기의 6.3주보다 늘었다. 프로모션 지원 예산은 매출 대비 4.8%로 전분기의
4.1%보다 확대됐다. 조립사 두 곳은 5월 라인 증설 계획을 검토 중이라고 밝혔다. 반도체 테스트
장비 가동률은 90%로 2월의 86%보다 높아졌다. 스피커 모듈 공급사의 수주 잔고는 1,200억원으로
전분기 대비 7% 늘었다. 인쇄회로기판 공급사 세 곳의 합산 가동률은 85%로 집계됐다. 커넥터
부품 단가는 개당 0.28달러로 보합세다. 협력사 설문에서 응답자의 44%는 원자재 가격이 2분기에도
계속 오를 것으로 예상했다. 29%는 보합, 27%는 하락을 예상했다. 물류 창고 임대료는 전년 대비
6% 올랐다고 응답한 업체가 다수였다. 금형 부품 공급사의 3월 신규 수주는 90억원으로 전월 대비
15% 늘었다. 방열 시트 공급사 두 곳의 합산 출하량은 6,200만 장으로 집계됐다. 초음파 지문인식
모듈 채택 모델 수는 3개에서 5개로 늘었다. 진동모터 단가는 개당 0.35달러로 보합세를 유지했다.
안테나 모듈 공급사의 3월 가동률은 87%로 전월의 83%보다 높아졌다. 협력사 인터뷰에서는 5월
성수기 대비 인력 충원 계획이 있다는 응답이 다수 나왔다. 조립라인 신규 채용 규모는 평균 120명
수준으로 파악됐다. 부품 검사 자동화 도입률은 68%로 전년의 58%에서 확대됐다. 전체 공급망
지표를 종합하면 수요 측면의 불확실성은 남아 있으나 공급 측면의 병목은 점차 완화되는 모습이다.
방수 케이스 부자재 공급사 두 곳의 3월 합산 출하량은 4,100만 개로 전월 대비 6% 늘었다. 스피커
그릴 부품 단가는 개당 0.09달러로 보합세를 유지했다. 최종 조립 라인의 3월 평균 가동시간은
20.5시간으로 전월의 19.2시간보다 늘었다.
다음 점검은 4월 초 신제품 사전예약 데이터가
나온 뒤 진행할 예정이다.""",
    """B Securities Equity Research | Foundry Capacity & Smartphone SoC Tracker (fictional test note) | 2026-03-20

Rating: Neutral. This note is a fully hypothetical channel-check exercise built for internal
testing and does not reflect any company's actual results or guidance. Leading-edge foundry
utilization reached 96% in February, up from 91% a month earlier. Mature-node utilization stayed
at 68%, roughly flat quarter over quarter. Advanced packaging backlog rose to 14 weeks from 11
weeks in January. Smartphone SoC unit shipments are estimated at 310 million units for the
quarter, down 4.5% year over year. Average selling price for flagship SoCs increased 6.1% to
roughly 58 dollars per unit. Wafer starts for the newest process node grew 9% sequentially. Test
and assembly subcontractors reported backlog growth of 18%, the fastest pace in five quarters.
Capital expenditure guidance across three tracked foundries totals an estimated 42 billion
dollars for the year, up 7% from the prior estimate. Yield rates on the newest node are estimated
at 74%, still below the mature 92% yield on the prior node. Design-in activity for next-generation
chipsets rose across 6 of 9 tracked customers. Memory attach ratios per smartphone increased from
8.2 GB average to 8.9 GB average. Component lead times lengthened to 11 weeks from 9 weeks in the
prior survey. Distributor channel inventory is estimated at 5.6 weeks of sales, up from 4.9 weeks.
Three of twelve surveyed suppliers flagged tighter allocation for premium-tier chipsets. Currency
assumption used is 1 dollar to 7.1 local units for translation purposes. Ocean freight rates on
the trans-Pacific lane fell 6% quarter over quarter to 2,300 dollars per forty-foot container.
Two contract manufacturers indicated flat headcount plans through the second quarter. Test yield
for the newest packaging technology improved to 81% from 77%. Net-net, capacity additions are
outrunning near-term demand, which should keep pricing power with device makers rather than
component suppliers through mid-year, in our fictional model. We plan to revisit these assumptions
once April shipment data is available from tracked channel partners. Overall sentiment among
surveyed supply-chain contacts was cautiously constructive, with 7 of 12 citing improving order
visibility into the third quarter. A supplemental survey of 15 equipment vendors found average
tool utilization at 79%, up from 73% two months earlier. Spare parts lead times for lithography
tools extended to 16 weeks from 13 weeks. Substrate suppliers reported backlog growth of 10%
quarter over quarter. Two of six surveyed substrate vendors flagged capacity additions for the
second half. Power management chip average selling price rose 3.4% sequentially. RF front-end
module demand grew 5% sequentially on higher content per device. Testing house utilization for
mixed-signal chips reached 83%, the highest level in four quarters. Roughly half of surveyed
customers indicated order visibility now extends 10 weeks or more, versus 7 weeks in the prior
survey. Inventory at three tracked distributors averaged 6.2 weeks of sales, little changed from
6.0 weeks previously. We view the combination of rising utilization and lengthening lead times as
an early sign that the current capacity cycle may be turning, though this remains a hypothesis to
be confirmed with the next data set.""",
    """C증권 산업분석팀 | 카메라 모듈·광학부품 공급망 점검 (가상 자료) | 2026-04-02

투자의견: 비중확대. 아래 수치는 모두 내부 테스트를 위해 만든 가상의 채널 점검 결과이며 실제
기업 실적을 의미하지 않는다. 3월 카메라 모듈 출하량은 1억 4,200만 개로 전월 대비 5.7% 늘었다.
폴디드 줌 모듈 비중은 전체의 9.3%로 전분기의 6.8%에서 확대됐다. 광학식 손떨림 보정 부품
공급사 두 곳의 합산 매출은 2,100억원으로 추정된다. 이미지센서 평균단가는 개당 3.2달러로
전년 대비 4% 올랐다. 렌즈 배럴 공급사의 3월 가동률은 89%로 2월의 84%보다 높아졌다. 신제품용
1억 화소 센서 채택 모델 수는 7개에서 11개로 늘었다. 광학부품 재고일수는 4.9주로 전분기의
5.6주보다 짧아졌다. 공급망 서베이는 모듈 조립사 14곳, 센서 공급사 6곳을 대상으로 3월 15일부터
25일까지 진행했다. 응답사의 57%는 2분기 주문량을 확대할 계획이라고 답했다. 19%는 유지, 24%는
축소를 계획하고 있다고 답했다. 자동초점 액추에이터 단가는 개당 1.15달러로 전분기 대비 2.6%
하락했다. 카메라 모듈 조립 인건비 비중은 원가의 18%로 추정된다. 상위 3개 모듈 공급사의 합산
수주잔고는 5,800억원으로 전분기 대비 9% 늘었다. 검사장비 공급사의 3월 신규 수주는 320억원으로
전월 대비 22% 증가했다. 반사방지 코팅 필름 공급사 한 곳은 4월 가격 인상을 통보했다고 알려졌다.
프리미엄 모델의 후면 카메라 개수는 평균 3.4개로 전년의 3.1개보다 늘었다. 중저가 모델의 카메라
사양 상향도 관찰됐다. 환율은 달러당 1,335원을 가정했다. 해상 운임은 부산-롱비치 노선 기준
20피트 컨테이너당 1,780달러로 전분기 대비 4% 하락했다. 광학 부품 수출 물량은 전년 동기 대비
7.2% 늘었다. 결론적으로 폴디드 줌과 고화소 센서 채택 확대가 모듈 공급사의 믹스 개선을 이끌고
있으며, 2분기에도 이 흐름은 이어질 전망이다. 부가 조사로 렌즈 코팅 소재 공급사의 3월 매출은
480억원으로 전월 대비 6% 늘었다. 초소형 짐벌 모듈 채택 모델 수는 4개에서 6개로 늘었다. 카메라
모듈 검사 불량률은 1.8%로 전분기의 2.3%보다 낮아졌다. 광학필터 공급사의 3월 수주는 210억원으로
전월 대비 11% 증가했다. 센서 패키징 외주업체 두 곳의 합산 가동률은 91%로 집계됐다. 협력사
설문에서 응답사의 48%는 2분기 설비투자를 확대할 계획이라고 답했다. 31%는 유지, 21%는 축소를
계획한다고 답했다. 렌즈 연마 공정 수율은 93%로 전분기의 90%보다 높아졌다. 카메라 모듈 조립
자동화 비율은 62%로 전년의 54%에서 확대됐다. 부품 운송용 특수 포장재 단가는 개당 1.4달러로
보합세를 유지했다. 광학 접착제 공급사 한 곳은 4월 리드타임이 3일에서 5일로 늘었다고 밝혔다.
카메라 모듈 최종 검수 인력은 전분기 대비 8% 늘었다. 이는 물량 증가에 대응한 조치로 해석된다. 적외선 필터 공급사의 3월 매출은 150억원으로 전월
대비 8% 늘었다. ToF 센서 채택 모델 수는 2개에서 4개로 늘었다. 렌즈 하우징 사출 부품 단가는
개당 0.22달러로 보합세다. 광학 검사장비 공급사의 신규 수주는 95억원으로 전월 대비 18%
늘었다. 협력사 설문에서 응답사의 39%는 원자재 수급이 2분기에 개선될 것으로 예상했다. 33%는
현재와 유사할 것으로, 28%는 악화될 것으로 예상했다. 카메라 모듈 포장 공정 자동화율은 55%로
전년의 47%에서 확대됐다. 반사방지 코팅 불량률은 1.2%로 전분기의 1.6%보다 낮아졌다. 광학
부품 창고 재고일수는 5.3주로 전분기의 5.9주보다 짧아졌다. 이는 수요 대응 속도가 개선됐음을
시사한다. 렌즈 모듈 접합 공정의 3월 불량률은 0.9%로 전분기의 1.3%보다 낮아졌다. 카메라 모듈
전용 클린룸 가동률은 96%로 전월과 비슷한 수준을 유지했다. 광학 부품 수입 관세율 변경으로
일부 공급사의 원가 부담이 소폭 늘었다는 응답이 있었다. 협력사 두 곳은 4월 중 신규 라인
가동을 시작할 예정이라고 밝혔다. 카메라 모듈 최종 조립 수율은 94%로 전분기의 91%보다
개선됐다. 광학 부품 협력사 설문 응답률은 87%로 지난 조사의 79%보다 높아졌다. 렌즈 표면 처리
소재 공급사 한 곳은 4월 단가를 3% 인상하겠다고 통보했다. 카메라 모듈 방진 테스트 통과율은
98.4%로 전분기의 97.9%와 비슷한 수준이다. 이미지 신호처리 칩 공급사의 3월 출하량은 1억
1,000만 개로 전월 대비 5% 늘었다. 협력사 다수는 5월 중 부품 단가 재협상 테이블이 열릴
것으로 예상한다고 응답했다. 카메라 모듈 완제품 출하 검수 인원은 전분기 대비 6% 늘었다. 광학
부품 물류 창고 두 곳의 3월 입고량은 5,400톤으로 전월 대비 3% 늘었다. 렌즈 모듈 최종 포장
공정의 자동화율은 71%로 전년의 63%에서 확대됐다.
다음 점검은 5월 초 신모델 초기 양산 데이터 확인
이후 진행할 예정이다. 본 자료의 모든 수치는 테스트 목적의 가상 추정치임을 다시 한번 밝힌다.""",
    """D Capital Research | Memory Pricing & Smartphone BOM Impact (fictional test note) | 2026-04-10

Rating: Neutral. Every figure in this note is a hypothetical estimate created for internal
testing and does not represent any company's actual results or guidance. Contract DRAM prices
rose 9.5% quarter over quarter in March, the third consecutive quarterly increase. NAND flash
contract prices rose 6.8% over the same period. Average DRAM content per flagship smartphone is
estimated at 10.2 GB, up from 8.7 GB a year earlier. Average NAND content per flagship device
rose to 312 GB from 268 GB. Memory now accounts for an estimated 21% of smartphone bill of
materials, up from 17% a year ago. Three surveyed device makers indicated plans to pass part of
the cost increase through to retail pricing. Two of five surveyed contract manufacturers reported
lengthening memory lead times, now averaging 9 weeks versus 6 weeks in January. Spot DRAM prices
have outpaced contract prices, rising 14% over the same quarter. Distributor channel inventory of
memory modules fell to 3.8 weeks of sales from 5.1 weeks, consistent with tighter near-term
supply. Capital expenditure among tracked memory suppliers is estimated at 38 billion dollars for
the year, roughly flat versus the prior estimate. Wafer output for the newest DRAM node grew 6%
sequentially, below the 9% growth seen for logic wafers in the same period. Test and packaging
subcontractors serving memory customers reported backlog growth of 12%. Currency assumption used
is 1 dollar to 1,330 local units. Component substitution toward lower-density modules was
reported by 2 of 9 surveyed device makers as a partial mitigation. Average selling price for
mid-tier smartphones is estimated to rise 3.1% in the second quarter on memory cost pass-through.
Premium-tier smartphone pricing is expected to be more insulated given thinner memory cost share
relative to overall bill of materials. Ocean freight for component shipments on the Asia-Europe
lane fell 5% quarter over quarter. Net-net, memory pricing pressure is a modest headwind to
smartphone gross margin in our fictional model, partially offset by premium mix shift documented
elsewhere in our coverage. We plan to update this estimate once April contract pricing settles
across the tracked supplier base. Sentiment among surveyed component distributors remained
cautious given the pace of the recent price increases. A supplemental check of 8 module makers
found average memory module test yield at 95%, roughly flat versus the prior quarter. Three of
eight surveyed module makers reported extending payment terms with upstream suppliers. Packaging
substrate lead times for memory modules lengthened to 8 weeks from 6 weeks. Average module ASP
rose 4.2% quarter over quarter, broadly tracking the underlying chip price increase. Two large
device makers indicated they had pre-purchased additional memory inventory to hedge against
further price increases, according to channel contacts. Utilization at memory module assembly
lines reached 88%, up from 83% in the prior survey. We continue to view memory pricing as the
single largest swing factor for smartphone bill-of-materials cost in the current cycle, in our
fictional scenario analysis.""",
]

def parse_env(path: Path) -> dict:
    values: dict = {}
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    except (OSError, UnicodeError):
        pass
    return values

def split_sentences(text: str) -> list:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    paragraphs = re.split(r"\n\s*\n", text)
    ender = re.compile(r"(?<=[.!?。])\s+")
    out = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        for piece in ender.split(para):
            piece = piece.strip()
            if len(piece) >= 15:
                out.append(piece)
    return out

def prepare_reports(texts: list, max_chars: int) -> list:
    prepared = []
    for text in texts:
        kept = []
        total = 0
        for sentence in split_sentences(text):
            kept.append(sentence)
            total += len(sentence)
            if total >= max_chars:
                break
        sid_map = {f"S{i + 1}": s for i, s in enumerate(kept)}
        numbered = "\n".join(f"[S{i + 1}] {s}" for i, s in enumerate(kept))
        prepared.append((numbered, sid_map))
    return prepared

def load_reports(reports_path):
    if reports_path is None:
        return list(BUILTIN_REPORTS), "builtin"
    path = Path(reports_path)
    if path.is_file():
        try:
            return [path.read_text(encoding="utf-8-sig")], "file"
        except OSError:
            return list(BUILTIN_REPORTS), "builtin(파일 읽기 실패)"
    if path.is_dir():
        texts = []
        for candidate in sorted(path.iterdir()):
            if candidate.suffix.lower() in (".md", ".txt"):
                try:
                    texts.append(candidate.read_text(encoding="utf-8-sig"))
                except OSError:
                    continue
        if texts:
            return texts, "folder"
        return list(BUILTIN_REPORTS), "builtin(폴더에 유효 파일 없음)"
    return list(BUILTIN_REPORTS), "builtin(경로 없음)"

def build_messages(numbered_text: str) -> list:
    user_content = "다음은 번호가 매겨진 리포트 문장입니다. 각 주장에 근거 문장 번호(sids)를 반드시 포함하세요.\n\n" + numbered_text
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_content}]

def normalize_number(text) -> str:
    if text is None:
        return ""
    return re.sub(r"[,%$\s]", "", str(text))

def value_in_sentences(value, sentences: list) -> bool:
    norm_value = normalize_number(value)
    if not norm_value:
        return False
    return any(norm_value in normalize_number(sentence) for sentence in sentences)

def extract_json(text):
    if not isinstance(text, str):
        return None
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.S | re.I)
    text = re.sub(r"```[a-zA-Z]*", "", text).replace("```", "")
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None

def schema_ok(data) -> bool:
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        return False
    for claim in data["claims"]:
        if not isinstance(claim, dict) or not isinstance(claim.get("sids"), list) or "text" not in claim:
            return False
    return True

def analyze_quality(content, sid_map: dict) -> dict:
    data = extract_json(content)
    if data is None:
        return {"json_ok": False}
    claims = data.get("claims") if isinstance(data, dict) else None
    claims = claims if isinstance(claims, list) else []
    sids_total = sids_ok = numeric_total = numeric_ok = enum_total = enum_ok = 0
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        sids = claim.get("sids")
        cited = []
        if isinstance(sids, list) and sids:
            sids_total += 1
            cited = [sid_map[s] for s in sids if s in sid_map]
            if len(cited) == len(sids):
                sids_ok += 1
        value = claim.get("value")
        if isinstance(value, str) and value.strip() and value.strip().lower() != "null":
            numeric_total += 1
            if value_in_sentences(value, cited):
                numeric_ok += 1
        enum_total += 1
        if claim.get("type") in CLAIM_TYPES and claim.get("direction") in CLAIM_DIRECTIONS:
            enum_ok += 1
    return {
        "json_ok": True, "schema_ok": schema_ok(data), "claims_count": len(claims),
        "sids_total": sids_total, "sids_ok": sids_ok,
        "numeric_total": numeric_total, "numeric_ok": numeric_ok,
        "enum_total": enum_total, "enum_ok": enum_ok,
    }

def aggregate_quality(contents: list) -> dict:
    """`contents` items are (content, sid_map, truncated). Truncated (finish_reason=="length")
    responses are excluded from quality percentages -- a cut-off JSON is not a quality signal --
    but counted separately in `truncated_excluded`."""
    truncated_excluded = sum(1 for _, _, truncated in contents if truncated)
    usable = [(content, sid_map) for content, sid_map, truncated in contents if not truncated]
    total = len(usable)
    json_ok = schema_ok_count = 0
    claims_counts, sids_total = [], 0
    sids_ok = numeric_total = numeric_ok = enum_total = enum_ok = 0
    for content, sid_map in usable:
        q = analyze_quality(content, sid_map)
        if not q.get("json_ok"):
            continue
        json_ok += 1
        schema_ok_count += int(bool(q["schema_ok"]))
        claims_counts.append(q["claims_count"])
        sids_total += q["sids_total"]
        sids_ok += q["sids_ok"]
        numeric_total += q["numeric_total"]
        numeric_ok += q["numeric_ok"]
        enum_total += q["enum_total"]
        enum_ok += q["enum_ok"]

    def pct(n, d):
        return round(100 * n / d, 1) if d else None

    return {
        "responses_total": total, "truncated_excluded": truncated_excluded,
        "json_parse_ok_pct": pct(json_ok, total),
        "schema_ok_pct": pct(schema_ok_count, json_ok),
        "avg_claims_count": round(statistics.mean(claims_counts), 2) if claims_counts else None,
        "sids_valid_pct": pct(sids_ok, sids_total),
        "numeric_value_match_pct": pct(numeric_ok, numeric_total),
        "enum_valid_pct": pct(enum_ok, enum_total),
    }

def estimate_tokens_from_chars(char_count: int) -> int:
    return max(1, char_count // 3)

def usage_reasoning_tokens(usage: dict, message: dict):
    details = (usage or {}).get("completion_tokens_details") or {}
    if isinstance(details.get("reasoning_tokens"), int):
        return details["reasoning_tokens"], False
    reasoning_text = message.get("reasoning_content") or message.get("reasoning")
    if isinstance(reasoning_text, str) and reasoning_text:
        return estimate_tokens_from_chars(len(reasoning_text)), True
    match = re.search(r"<think(?:ing)?>(.*?)</think(?:ing)?>", message.get("content") or "", re.S | re.I)
    if match:
        return estimate_tokens_from_chars(len(match.group(1))), True
    return None, False

def classify_exception(exc):
    if isinstance(exc, urllib.error.HTTPError):
        status = exc.code
        if status == 429:
            return status, "429", "HTTPError"
        if 500 <= status < 600:
            return status, "5xx", "HTTPError"
        return status, "other", "HTTPError"
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
        return None, "timeout", type(exc).__name__
    return None, "other", type(exc).__name__

def post_json(url: str, key: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def fire_extraction_call(base_url, key, model, prompt_text, max_tokens, timeout):
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": build_messages(prompt_text), "max_tokens": max_tokens, "temperature": 0.2}
    started = time.perf_counter()
    try:
        body = post_json(url, key, payload, timeout)
    except Exception as exc:
        status, error_class, error_type = classify_exception(exc)
        return {
            "ok": False, "latency": time.perf_counter() - started, "status": status,
            "error_class": error_class, "error_type": error_type, "finish_reason": None,
            "prompt_tokens": None, "completion_tokens": None, "reasoning_tokens": None, "reasoning_estimated": False,
        }, None
    latency = time.perf_counter() - started
    message, finish_reason = {}, None
    try:
        message = body["choices"][0]["message"] or {}
        finish_reason = body["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        pass
    usage = body.get("usage") or {}
    reasoning_tokens, estimated = usage_reasoning_tokens(usage, message)
    record = {
        "ok": True, "latency": latency, "status": 200, "error_class": None, "error_type": None,
        "finish_reason": finish_reason, "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": reasoning_tokens, "reasoning_estimated": estimated,
    }
    return record, message.get("content")

def run_level(level, prepared, base_url, key, model, max_tokens, timeout):
    def task(i):
        prompt_text, sid_map = prepared[i % len(prepared)]
        record, content = fire_extraction_call(base_url, key, model, prompt_text, max_tokens, timeout)
        return record, content, sid_map

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=level) as pool:
        outcomes = list(pool.map(task, range(level)))
    wall = time.perf_counter() - started
    records = [o[0] for o in outcomes]
    contents = [(o[1], o[2], o[0]["finish_reason"] == "length") for o in outcomes if o[0]["ok"]]
    return wall, records, contents

def percentile(values: list, pct: float):
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)

def level_stats(level, wall, records):
    latencies = [r["latency"] for r in records]
    ok_records = [r for r in records if r["ok"]]
    completion_sum = sum(r["completion_tokens"] or 0 for r in ok_records)
    per_call = [(r["completion_tokens"] or 0) / r["latency"] for r in ok_records if r["latency"] > 0]
    truncated_count = sum(1 for r in ok_records if r.get("finish_reason") == "length")
    reasoning_values = [r["reasoning_tokens"] for r in ok_records if r["reasoning_tokens"] is not None]
    reasoning_estimated = any(r["reasoning_estimated"] for r in ok_records if r["reasoning_tokens"] is not None)
    errors_by_class = {}
    for r in records:
        if not r["ok"]:
            errors_by_class[r["error_class"]] = errors_by_class.get(r["error_class"], 0) + 1
    return {
        "level": level, "wall_sec": round(wall, 2),
        "p50_latency_sec": round(statistics.median(latencies), 2) if latencies else None,
        "p90_latency_sec": round(percentile(latencies, 90), 2) if latencies else None,
        "max_latency_sec": round(max(latencies), 2) if latencies else None,
        "success_count": len(ok_records), "total_count": len(records),
        "completion_tokens_sum": completion_sum,
        "aggregate_tokens_per_sec": round(completion_sum / wall, 2) if wall > 0 else None,
        "mean_call_tokens_per_sec": round(statistics.mean(per_call), 2) if per_call else None,
        "truncated": truncated_count,
        "reasoning_tokens_mean": round(statistics.mean(reasoning_values), 1) if reasoning_values else None,
        "reasoning_tokens_max": max(reasoning_values) if reasoning_values else None,
        "reasoning_tokens_estimated": reasoning_estimated,
        "errors_by_class": errors_by_class,
    }

def stream_probe(base_url, key, model, max_tokens, timeout):
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model, "max_tokens": min(max_tokens, 512), "temperature": 0.2, "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "한 문장으로 답하세요. 반도체 공급망 재고 전망은?"}],
    }
    keys_seen, content_had_think = set(), False
    usage_seen = usage_has_reasoning_tokens = stream_options_retried = False
    for _attempt in range(2):
        started = time.perf_counter()
        first_byte = first_reasoning = first_content = None
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for raw_line in resp:
                    if first_byte is None:
                        first_byte = time.perf_counter() - started
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk_text = line[len("data:"):].strip()
                    if chunk_text == "[DONE]":
                        break
                    try:
                        chunk = json.loads(chunk_text)
                    except ValueError:
                        continue
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("reasoning"):
                            keys_seen.add("reasoning")
                            first_reasoning = first_reasoning if first_reasoning is not None else time.perf_counter() - started
                        if delta.get("reasoning_content"):
                            keys_seen.add("reasoning_content")
                            first_reasoning = first_reasoning if first_reasoning is not None else time.perf_counter() - started
                        content = delta.get("content")
                        if content:
                            keys_seen.add("content")
                            first_content = first_content if first_content is not None else time.perf_counter() - started
                            if "<think" in content:
                                content_had_think = True
                    usage = chunk.get("usage")
                    if usage:
                        usage_seen = True
                        details = (usage or {}).get("completion_tokens_details") or {}
                        if isinstance(details.get("reasoning_tokens"), int):
                            usage_has_reasoning_tokens = True
            total = time.perf_counter() - started
            return {
                "ok": True, "delta_keys_seen": sorted(keys_seen), "content_had_think_tag": content_had_think,
                "usage_chunk_seen": usage_seen, "usage_has_reasoning_tokens": usage_has_reasoning_tokens,
                "stream_options_retried": stream_options_retried,
                "time_to_first_byte_sec": round(first_byte, 3) if first_byte is not None else None,
                "time_to_first_reasoning_sec": round(first_reasoning, 3) if first_reasoning is not None else None,
                "time_to_first_content_sec": round(first_content, 3) if first_content is not None else None,
                "total_sec": round(total, 3),
            }
        except urllib.error.HTTPError as exc:
            try:
                body_text = exc.read().decode("utf-8", "replace")
            except Exception:
                body_text = ""
            if exc.code == 400 and "stream_options" in payload and "stream_options" in body_text:
                payload.pop("stream_options", None)
                stream_options_retried = True
                continue
            status, error_class, error_type = classify_exception(exc)
            return {"ok": False, "status": status, "error_class": error_class, "error_type": error_type}
        except Exception as exc:
            status, error_class, error_type = classify_exception(exc)
            return {"ok": False, "status": status, "error_class": error_class, "error_type": error_type}
    return {"ok": False, "status": None, "error_class": "other", "error_type": "retry_exhausted"}

def format_stream_result(r: dict) -> str:
    if not r.get("ok"):
        return f"실패 — 오류종류={r.get('error_class')} ({r.get('error_type')})"
    return (
        f"delta 키={r['delta_keys_seen']} think태그포함={r['content_had_think_tag']} "
        f"usage수신={r['usage_chunk_seen']} reasoning_tokens필드={r['usage_has_reasoning_tokens']} "
        f"stream_options재시도={r['stream_options_retried']} TTFB={r['time_to_first_byte_sec']}s "
        f"첫reasoning={r['time_to_first_reasoning_sec']}s 첫content={r['time_to_first_content_sec']}s "
        f"총={r['total_sec']}s"
    )

def format_level_line(s: dict) -> str:
    est_mark = "(추정)" if s["reasoning_tokens_estimated"] else ""
    return (
        f"레벨={s['level']:>3} 성공/전체={s['success_count']}/{s['total_count']} "
        f"벽시계={s['wall_sec']}s p50={s['p50_latency_sec']}s p90={s['p90_latency_sec']}s "
        f"출력토큰합={s['completion_tokens_sum']} 합산tok/s={s['aggregate_tokens_per_sec']} "
        f"호출당tok/s={s['mean_call_tokens_per_sec']} 잘림={s['truncated']} "
        f"reasoning평균/최대={s['reasoning_tokens_mean']}/{s['reasoning_tokens_max']}{est_mark} "
        f"오류={s['errors_by_class'] or '없음'}"
    )

def format_quality(q: dict) -> str:
    return (
        f"응답수={q['responses_total']} 잘림제외={q['truncated_excluded']} "
        f"JSON파싱성공률={q['json_parse_ok_pct']}% 스키마정합률={q['schema_ok_pct']}% "
        f"평균claim수={q['avg_claims_count']} sids유효율={q['sids_valid_pct']}% "
        f"숫자일치율={q['numeric_value_match_pct']}% enum유효율={q['enum_valid_pct']}%"
    )

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="사내 LLM 병렬 처리 성능 벤치마크 — 표준 라이브러리만 사용. 콘솔/결과 JSON에는 숫자와 "
                     "라벨만 들어가며 리포트 본문·모델 출력 텍스트·API 호스트는 절대 포함하지 않습니다.")
    parser.add_argument("--base-url", default=None, help="LLM API base URL (예: http://.../v1)")
    parser.add_argument("--model", default=None, help="모델 ID")
    parser.add_argument("--api-key", default=None, help="API 키 (출력·저장되지 않음)")
    parser.add_argument("--env", type=Path, default=None, help="KEY=VALUE 형식 .env 파일 경로")
    parser.add_argument("--levels", default="1,5,10,20", help="동시 요청 수 목록 (쉼표 구분)")
    parser.add_argument("--reports", type=Path, default=None,
                         help="실제 리포트 폴더(.md/.txt) 또는 파일 1개. 생략 시 내장 가상 리포트 4개 사용")
    parser.add_argument("--max-tokens", type=int, default=16000, help="추출 호출의 max_tokens (thinking도 이 안에서 소모)")
    parser.add_argument("--max-chars", type=int, default=12000, help="전처리 후 유지할 최대 문자 수")
    parser.add_argument("--timeout", type=float, default=600, help="호출당 타임아웃(초)")
    parser.add_argument("--pause", type=float, default=20, help="레벨 사이 대기 시간(초, 레이트리밋 배려)")
    parser.add_argument("--out", type=Path, default=Path("bench_result.json"), help="결과 JSON 저장 경로")
    parser.add_argument("--skip-stream", action="store_true", help="스트리밍 프로브 생략")
    parser.add_argument("--selftest", action="store_true", help="네트워크 없이 내부 로직만 자체 점검")
    return parser

def selftest() -> int:
    ko_sample = "아이폰 생산량이 큰 폭으로 감소했다. 이는 수요 둔화를 의미한다.\n\n짧다.\nMemory prices rose 8%. This is a positive signal for suppliers."
    sentences = split_sentences(ko_sample)
    assert any("아이폰" in s for s in sentences), sentences
    assert all(len(s) >= 15 for s in sentences), sentences
    assert not any(s.strip() == "짧다." for s in sentences), sentences

    raw = '<think>내부 추론이 길게 이어지는 중이다...</think>```json\n{"a": 1, "b": [1, 2, 3]}\n```'
    assert extract_json(raw) == {"a": 1, "b": [1, 2, 3]}, extract_json(raw)

    assert normalize_number("1,999") == "1999"
    assert normalize_number("12%") == "12"
    assert normalize_number("$3,500 ") == "3500"
    assert value_in_sentences("1,999", ["매출은 1999억원을 기록했다."])
    assert not value_in_sentences("2,000", ["매출은 1999억원을 기록했다."])

    sid_map = {"S1": "아이폰 출하량은 1999만대로 줄었다.", "S2": "메모리 가격은 8% 올랐다."}
    canned = json.dumps({
        "report_date": "2026-03-14", "institution": "A증권",
        "claims": [
            {"sids": ["S1"], "text": "출하량 감소", "type": "fact", "direction": "down",
             "entities": ["Apple"], "metric": "출하량", "value": "1999", "unit": "만대", "period": "2026Q1"},
            {"sids": ["S2"], "text": "가격 상승", "type": "forecast", "direction": "up",
             "entities": ["memory"], "metric": "가격", "value": "8", "unit": "%", "period": "2026Q1"},
            {"sids": ["S9"], "text": "잘못된 인용", "type": "opinion", "direction": "none",
             "entities": [], "metric": None, "value": None, "unit": None, "period": None},
        ],
        "topics": ["스마트폰"],
    }, ensure_ascii=False)
    q = analyze_quality(canned, sid_map)
    assert q["json_ok"] and q["schema_ok"], q
    assert q["claims_count"] == 3, q
    assert q["sids_ok"] == 2 and q["sids_total"] == 3, q
    assert q["numeric_ok"] == 2 and q["numeric_total"] == 2, q
    assert q["enum_ok"] == 3, q

    fake_records = [
        {"ok": True, "latency": 1.0, "finish_reason": "length", "completion_tokens": 100,
         "reasoning_tokens": 80, "reasoning_estimated": False},
        {"ok": True, "latency": 1.0, "finish_reason": "stop", "completion_tokens": 50,
         "reasoning_tokens": 20, "reasoning_estimated": True},
        {"ok": False, "latency": 0.5, "error_class": "timeout", "finish_reason": None,
         "completion_tokens": None, "reasoning_tokens": None, "reasoning_estimated": False},
    ]
    stats = level_stats(2, 2.0, fake_records)
    assert stats["truncated"] == 1, stats
    assert stats["success_count"] == 2 and stats["total_count"] == 3, stats
    assert stats["reasoning_tokens_max"] == 80 and stats["reasoning_tokens_estimated"], stats

    fake_contents = [(canned, sid_map, False), (canned, sid_map, True)]
    fq = aggregate_quality(fake_contents)
    assert fq["truncated_excluded"] == 1, fq
    assert fq["responses_total"] == 1, fq

    print("셀프테스트 통과: 문장분리 / JSON추출 / 숫자정규화 / 품질계산 / 잘림 집계 모두 정상")
    return 0

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = build_arg_parser()
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    env_config = parse_env(args.env) if args.env else {}
    base_url = (args.base_url or os.environ.get("LLM_BASE_URL") or env_config.get("LLM_BASE_URL") or "").strip()
    api_key = (args.api_key or os.environ.get("LLM_API_KEY") or env_config.get("LLM_API_KEY") or "").strip()
    model = (args.model or os.environ.get("LLM_MODEL_DEFAULT") or env_config.get("LLM_MODEL_DEFAULT") or "").strip()
    if not base_url or not model:
        parser.error("--base-url과 --model이 필요합니다 (직접 지정하거나 --env/환경변수로 제공하세요)")

    try:
        levels = [int(x) for x in args.levels.split(",") if x.strip()]
    except ValueError:
        parser.error("--levels는 쉼표로 구분된 정수 목록이어야 합니다 (예: 1,5,10)")
    if not levels:
        parser.error("--levels가 비어 있습니다")

    texts, source_label = load_reports(args.reports)
    prepared = prepare_reports(texts, args.max_chars)
    distinct_reports = len(texts)

    print(f"모델: {model} | 리포트 {distinct_reports}개 ({source_label}) | 동시성 레벨: {levels}")
    if distinct_reports < max(levels):
        print("리포트 " + str(distinct_reports) + "개를 반복 사용합니다 — 서로 다른 리포트 10개 이상을 권장합니다 "
              "(같은 입력은 서버 캐시로 실제보다 빠르게 나올 수 있음)")

    stream_result = None
    if not args.skip_stream:
        print("\n[스트리밍 프로브]")
        stream_result = stream_probe(base_url, api_key, model, args.max_tokens, args.timeout)
        print(format_stream_result(stream_result))

    level_results, all_contents = [], []
    for idx, level in enumerate(levels):
        print(f"\n[동시성 {level} 실행 중...]")
        wall, records, contents = run_level(level, prepared, base_url, api_key, model, args.max_tokens, args.timeout)
        stats = level_stats(level, wall, records)
        level_results.append(stats)
        all_contents.extend(contents)
        print(format_level_line(stats))
        if idx < len(levels) - 1 and args.pause > 0:
            time.sleep(args.pause)

    if any(r["truncated"] > 0 for r in level_results):
        print("생각 과정이 max_tokens를 다 써서 잘린 호출이 있습니다 — --max-tokens를 늘려 다시 측정하세요")

    quality = aggregate_quality(all_contents)
    print("\n[추출 품질]")
    print(format_quality(quality))

    best = max((r for r in level_results if r["success_count"] > 0),
               key=lambda r: r["aggregate_tokens_per_sec"] or 0, default=None)
    est_minutes = None
    if best and best["wall_sec"] > 0:
        est_minutes = math.ceil(100 / best["level"]) * best["wall_sec"] / 60
        print(f"\n리포트 100건 추출 예상: 병렬 {best['level']}에서 약 {est_minutes:.1f}분 (해당 레벨의 실측 처리량 기준)")
    else:
        print("\n리포트 100건 추출 예상: 성공한 호출이 없어 추정 불가")

    scheme = urlsplit(base_url).scheme or "unknown"
    output = {
        "privacy": "이 파일에는 리포트 본문, 모델 출력 텍스트, 프롬프트, 파일명, API 호스트가 포함되어 있지 않습니다. 숫자와 라벨만 기록합니다.",
        "environment": {
            "python_version": sys.version.split()[0], "model": model, "levels": levels,
            "max_tokens": args.max_tokens, "max_chars": args.max_chars,
            "distinct_reports": distinct_reports, "report_char_lengths": [len(t) for t in texts],
            "report_source": source_label, "base_url_scheme": scheme,
            "pause_sec": args.pause, "timeout_sec": args.timeout,
        },
        "stream_probe": stream_result,
        "levels": level_results,
        "quality": quality,
        "extrapolation_100_reports_minutes": round(est_minutes, 2) if est_minutes is not None else None,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n결과 JSON 저장: {args.out}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
