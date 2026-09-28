# AI Office 사내 작업 안내서

이 저장소는 사외 개발 환경에 접근할 수 없는 사내 PC에서, AI Office 프로젝트와 관련된
점검·구현·측정 작업을 진행하기 위한 안내서 모음입니다. 코드는 들어있지 않으며,
전체 문서는 한국어 마크다운입니다. 사내 PC의 코딩 에이전트(OpenCode)와 사람이
같이 참고합니다.

## 문서 목록

- [`01-llm-check.md`](01-llm-check.md) — 사내 LLM·임베딩·리랭커 연결 상태를 점검하는 스모크 체크 실행 방법
- [`02-token-measure.md`](02-token-measure.md) — 생성 시간이 어디서 쓰이는지 토큰/시간 지표로 측정하는 방법
- [`03-claims-spec.md`](03-claims-spec.md) — 정리된 리포트 md에서 주간 주장 목록("주장 300줄")을 만드는 구현 명세 (사내 에이전트용)
- [`04-export-rules.md`](04-export-rules.md) — 결과를 사외로 가져갈 때 지켜야 하는 반출 규칙
- [`05-docling-speed.md`](05-docling-speed.md) — docling PDF→md 변환 속도 개선 설정과 측정 절차 (사내 에이전트용)

## 권장 순서

① 사내 LLM·임베딩·리랭커 점검 → ② 리포트 md 만들기(사내 파이프라인: 압축 해제 → docling → disclaimer 제거, 사내에서 진행 중) → ③ 주장 추출 구현(03, 사내 에이전트에게 위임) → ④ 소요 시간·토큰 측정 → ⑤ 반출 규칙에 따라 결과 정리
