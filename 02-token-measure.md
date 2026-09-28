# 토큰·시간 측정

이 문서는 AI Office가 쓰는 SQLite DB에서 LLM 호출 기록을 집계해, 생성 시간이
어느 단계·어느 백엔드에서 가장 많이 쓰이는지 확인하는 방법을 설명합니다.

## 실행

프로젝트의 `server` 폴더에서 아래 한 줄을 실행합니다. `<DATA_DIR>`은 DB가 있는
실제 폴더 경로로 바꿉니다.

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); [print(r) for r in c.execute('SELECT step,backend,status,COUNT(*),ROUND(AVG(latency_ms)/1000,1),ROUND(SUM(latency_ms)/3600000.0,2),ROUND(AVG(prompt_tokens)),ROUND(AVG(completion_tokens)) FROM llm_calls GROUP BY 1,2,3 ORDER BY 6 DESC')]" <DATA_DIR>\aioffice.db
```

## 출력 컬럼 읽는 법

| 컬럼 | 의미 |
|---|---|
| 단계 | 파이프라인 내부 단계 이름 |
| 백엔드 | 호출한 백엔드 종류 |
| 성공/실패 | 호출 상태 |
| 호출 수 | 해당 조합의 총 호출 횟수 |
| 평균 초 | 호출 1건의 평균 소요 시간(초) |
| 총 시간(시간) | 해당 조합이 차지한 총 시간(시간 단위, 정렬 기준) |
| 평균 입력 토큰 | 호출당 평균 프롬프트 토큰 수 |
| 평균 출력 토큰 | 호출당 평균 완료(생성) 토큰 수 |

## 해석 방법

- 출력 토큰 수가 소요 시간을 좌우합니다(측정치 기준 초당 약 35~43토큰).
- thinking(추론 과정) 토큰도 출력 토큰 수에 포함됩니다.
- 호출을 병렬로 보내도 개별 생성 속도 자체는 빨라지지 않습니다.

## 가져올 것

출력된 표의 행(row)만 그대로 가져갑니다. 문서 이름은 포함되지 않으니 별도로 가리지
않아도 됩니다.
