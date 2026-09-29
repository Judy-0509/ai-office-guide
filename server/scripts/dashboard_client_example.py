#!/usr/bin/env python3
"""대시보드 연동 예시 (표준 라이브러리만 사용, urllib). 사내 대시보드가 분석가 지식 API를 어떻게
호출하면 되는지 보여주는 참고용 스크립트입니다. 실제 대시보드 코드에 맞게 조정해서 쓰세요.

실행:
    python server/scripts/dashboard_client_example.py --base-url http://127.0.0.1:8790 --metric 가격
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request


def _get(base_url: str, path: str, token: str | None = None) -> dict:
    req = urllib.request.Request(f"{base_url}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="분석가 지식 API 대시보드 연동 예시")
    parser.add_argument("--base-url", default="http://127.0.0.1:8790")
    parser.add_argument("--metric", default="가격", help="조회할 지표명 (부분 일치)")
    parser.add_argument("--token", default=None, help="--token으로 띄운 서버라면 지정")
    args = parser.parse_args()

    overview = _get(args.base_url, "/api/v1/overview", args.token)
    print(f"학습 리포트 {overview['reports']}건 / 주제 {overview['topics']}개 "
          f"/ 유효 주장 {overview['claims_valid']}건 (기간 {overview['date_range']})")

    q = urllib.parse.urlencode({"metric": args.metric, "valid": "valid", "limit": 10})
    rows = _get(args.base_url, f"/api/v1/claims?{q}", args.token)["rows"]
    print(f"\n'{args.metric}' 관련 최근 주장 {len(rows)}건:")
    print(f"{'날짜':<12}{'증권사':<10}{'값':>8} {'단위':<6}{'주제'}")
    for r in rows:
        print(f"{r['date']:<12}{r['broker']:<10}{str(r['value'] or ''):>8} "
              f"{(r['unit'] or ''):<6}{r['topic']['name'] or ''}")

    if rows:
        entity = (rows[0]["entities"] or [""])[0]
        q = urllib.parse.urlencode({"entity": entity, "metric": args.metric})
        groups = _get(args.base_url, f"/api/v1/metric_history?{q}", args.token)["groups"]
        for group in groups[:1]:
            chain = " -> ".join(str(c["value"] or "?") for c in group["chain"])
            print(f"\n{group['broker']} {args.metric} 변경 이력: {chain}")


if __name__ == "__main__":
    main()
