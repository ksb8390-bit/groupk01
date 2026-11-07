# -*- coding: utf-8 -*-
"""
아침 브리핑용 나라장터 제주 입찰공고 수집 → Markdown 출력 (표준 라이브러리만 사용)

- 조회: 용역·공사 입찰공고 × 수요기관명/공고기관명 "제주" (서버측 필터), 공고번호+차수 중복 제거
- 기간: 기본 = 전일 00:00 ~ 오늘 23:59 (KST), 월요일은 직전 금요일 00:00부터
        인자로 지정 가능: python g2b_brief_fetch.py 202611010000 202611072359
- 인증키: 환경변수 G2B_SERVICE_KEY (Encoding/Decoding 키 모두 가능)
- 출력: 표준출력으로 Markdown (A. 업종 키워드 해당 / B. 기타 제주 공고)
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
API_ROOT = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/"
OPERATIONS = {"용역": "getBidPblancListInfoServcPPSSrch", "공사": "getBidPblancListInfoCnstwkPPSSrch"}
REGION_FIELDS = ["dminsttNm", "ntceInsttNm"]
KEYWORDS = ["건축", "설계", "감리", "건설사업관리", "CM", "안전진단", "안전점검", "정밀점검",
            "정비사업", "학술", "연구용역", "인테리어", "실내건축", "리모델링"]
ROWS = 100


def period():
    if len(sys.argv) >= 3:
        return sys.argv[1], sys.argv[2]
    now = datetime.now(KST)
    back = 3 if now.weekday() == 0 else 1  # 월요일은 금요일부터
    return (now - timedelta(days=back)).strftime("%Y%m%d0000"), now.strftime("%Y%m%d2359")


def call(key, op, field, start, end, page):
    q = urllib.parse.urlencode({
        "serviceKey": key, "pageNo": page, "numOfRows": ROWS, "inqryDiv": 1,
        "inqryBgnDt": start, "inqryEndDt": end, "type": "json", field: "제주",
    })
    last = None
    for attempt in range(1, 6):
        try:
            with urllib.request.urlopen(API_ROOT + op + "?" + q, timeout=30) as r:
                text = r.read().decode("utf-8")
            data = json.loads(text)
            if "response" not in data:
                raise RuntimeError(text[:300])
            hdr = data["response"].get("header", {})
            if hdr.get("resultCode") not in ("00", None):
                raise RuntimeError(f"{hdr.get('resultCode')} {hdr.get('resultMsg')}")
            body = data["response"].get("body", {})
            items = body.get("items") or []
            if isinstance(items, dict):
                items = items.get("item", [])
            if isinstance(items, dict):
                items = [items]
            return items, int(body.get("totalCount") or 0)
        except Exception as e:  # 일시적 연결 끊김 등 재시도
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"{op}/{field} p{page}: {last}")


def collect(key, start, end):
    merged, errors = {}, []
    for kind, op in OPERATIONS.items():
        for field in REGION_FIELDS:
            page = 1
            try:
                while True:
                    items, total = call(key, op, field, start, end, page)
                    for it in items:
                        k = (it.get("bidNtceNo"), it.get("bidNtceOrd"))
                        if k not in merged:
                            it["_kind"] = kind
                            merged[k] = it
                    if len(items) < ROWS or page * ROWS >= total:
                        break
                    page += 1
            except Exception as e:
                errors.append(str(e)[:300])
    return list(merged.values()), errors


def cell(v):
    return str(v if v not in (None, "") else "-").replace("|", "／").replace("\n", " ")


def won(v):
    try:
        return f"{int(float(v)):,}"
    except (TypeError, ValueError):
        return "-"


def render(items, errors, start, end):
    hit = [i for i in items if any(k in i.get("bidNtceNm", "") for k in KEYWORDS)]
    rest = [i for i in items if i not in hit]
    hit.sort(key=lambda i: i.get("bidClseDt") or "9999")
    rest.sort(key=lambda i: i.get("bidClseDt") or "9999")
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    out = [
        f"# 나라장터 제주 입찰공고 ({start[:8]} ~ {end[:8]})",
        "",
        f"- 생성시각: {now} KST / 출처: 조달청 나라장터 입찰공고정보서비스(Open API, 공공데이터포털)",
        "- 조회조건: 용역·공사 입찰공고, 공고게시일시 기준, 수요기관명 또는 공고기관명에 '제주' 포함",
        f"- 결과: 전체 {len(items)}건 / 업종 키워드 해당 {len(hit)}건",
        f"- 업종 키워드: {', '.join(KEYWORDS)}",
        "- 조회 상태: " + ("정상" if not errors else "일부 오류 — " + " / ".join(errors)),
        "",
        f"## A. 업종 키워드 해당 ({len(hit)}건, 마감 빠른 순)",
        "",
        "| 구분 | 공고번호-차수 | 공고명 | 공고기관 | 수요기관 | 추정가격(원) | 배정예산(원) | 게시일시 | 입찰마감 | 개찰 | 계약방법 | 낙찰방법 | 지역제한 | 상세URL |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i in hit:
        out.append("| " + " | ".join(cell(x) for x in [
            i["_kind"], f"{i.get('bidNtceNo')}-{i.get('bidNtceOrd')}", i.get("bidNtceNm"),
            i.get("ntceInsttNm"), i.get("dminsttNm"), won(i.get("presmptPrce")), won(i.get("asignBdgtAmt")),
            i.get("bidNtceDt"), i.get("bidClseDt"), i.get("opengDt"), i.get("cntrctCnclsMthdNm"),
            i.get("sucsfbidMthdNm"), i.get("rgnLmtBidLocplcJdgmBssNm"), i.get("bidNtceDtlUrl"),
        ]) + " |")
    if not hit:
        out.append("| - | - | 해당 없음 | | | | | | | | | | | |")
    out += ["", f"## B. 기타 제주 공고 ({len(rest)}건, 참고용)", "",
            "| 구분 | 공고명 | 수요기관 | 추정가격(원) | 입찰마감 | 상세URL |", "|---|---|---|---|---|---|"]
    for i in rest:
        out.append("| " + " | ".join(cell(x) for x in [
            i["_kind"], i.get("bidNtceNm"), i.get("dminsttNm"), won(i.get("presmptPrce")),
            i.get("bidClseDt"), i.get("bidNtceDtlUrl"),
        ]) + " |")
    return "\n".join(out) + "\n"


def main():
    key = urllib.parse.unquote(os.environ.get("G2B_SERVICE_KEY", "").strip())
    if not key:
        sys.exit("G2B_SERVICE_KEY 환경변수가 없습니다.")
    start, end = period()
    items, errors = collect(key, start, end)
    sys.stdout.write(render(items, errors, start, end))
    if errors and not items:
        sys.exit(2)


if __name__ == "__main__":
    main()
