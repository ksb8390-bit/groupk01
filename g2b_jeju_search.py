# -*- coding: utf-8 -*-
"""
나라장터(조달청) 입찰공고정보서비스 - 제주 지역 + 건축설계/CM/안전진단 용역 자동 수집 스크립트

사용 전 준비:
1. pip install requests openpyxl --break-system-packages   (또는 그냥 pip install requests openpyxl)
2. data.go.kr에서 발급받은 "Decoding" 인증키를 환경변수 G2B_SERVICE_KEY 로 설정하세요.
   (마이페이지 > 개발계정 상세보기 > 일반 인증키(Decoding) 값)
   - Linux/macOS : export G2B_SERVICE_KEY="발급키"
   - Windows     : set G2B_SERVICE_KEY=발급키
   ※ 키를 스크립트에 직접 적어 커밋하지 마세요.
3. 조회 기간은 기본값으로 "오늘 기준 최근 7일"이 자동 적용됩니다.

실행:
    python g2b_jeju_search.py                      # 최근 7일
    python g2b_jeju_search.py --days 30            # 최근 30일
    python g2b_jeju_search.py --start 20261101 --end 20261110   # 기간 직접 지정 (YYYYMMDD 또는 YYYYMMDDHHMM)

참고: API 1회 조회 기간은 최대 1개월 이내로 제한될 수 있습니다(확인 필요). 긴 기간은 나누어 조회하세요.

결과:
    - jeju_filtered.xlsx  : 제주 + 건축설계/CM/안전진단 키워드가 걸린 공고만
    - all_raw.csv         : 조회 기간 전체 원본 데이터 (백업/재검토용)
"""

import argparse
import csv
import os
import time
from datetime import datetime, timedelta

import requests

try:
    import openpyxl
    from openpyxl import Workbook
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

# =========================
# 1. 설정값 (여기를 수정하세요)
# =========================
SERVICE_KEY = os.environ.get("G2B_SERVICE_KEY", "").strip()

BASE_URL = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServcPPSSrch"

DEFAULT_DAYS = 7              # 기간 미지정 시 오늘 기준 최근 N일 조회
START_DATE = None             # main()에서 명령행 인자 또는 기본값으로 설정 (YYYYMMDDHHMM)
END_DATE = None

NUM_OF_ROWS = 100             # 페이지당 건수 (최대 100 권장)

# 공고명(bidNtceNm)에 이 키워드 중 하나라도 포함되면 후보로 채택
SUBJECT_KEYWORDS = ["건축설계", "설계용역", "건설사업관리", "CM용역", "안전진단", "정밀안전진단", "정밀점검"]

# 수요기관명(dminsttNm) 또는 공고기관명(ntceInsttNm)에 이 키워드가 포함되면 제주 관련으로 채택
REGION_KEYWORDS = ["제주"]

REQUEST_DELAY_SEC = 0.3       # 호출 간 대기시간 (초당 호출 제한 방지)

OUTPUT_FILTERED = "jeju_filtered.xlsx" if HAS_OPENPYXL else "jeju_filtered.csv"
OUTPUT_RAW_CSV = "all_raw.csv"

# =========================
# 2. 유틸 함수
# =========================

def get_first(d, keys, default=""):
    """딕셔너리에서 여러 후보 키 중 처음 존재하는 값을 반환 (API 응답이 영문/국문 키를 섞어 쓸 수 있어 대응)"""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def extract_items(payload):
    """
    나라장터 응답 JSON에서 항목 리스트를 추출.
    영문 표준 스키마(response/body/items)와, 관측된 국문 스키마(응답/몸/항목) 둘 다 대응.
    """
    if not isinstance(payload, dict):
        return [], 0

    # 영문 표준 스키마
    if "response" in payload:
        body = payload.get("response", {}).get("body", {})
        items = body.get("items", [])
        if isinstance(items, dict):
            items = items.get("item", [])
        total = body.get("totalCount", 0)
        if isinstance(items, dict):
            items = [items]
        return items or [], int(total) if str(total).isdigit() else 0

    # 국문 스키마 (응답/몸/항목)
    if "응답" in payload:
        body = payload.get("응답", {}).get("몸", {})
        items = body.get("항목", [])
        total = body.get("총 개수", 0) or body.get("총개수", 0)
        if isinstance(items, dict):
            items = [items]
        return items or [], int(total) if str(total).isdigit() else 0

    return [], 0


def is_match(item):
    subject = str(get_first(item, ["bidNtceNm", "공고명", "입찰공고명"]))
    inst1 = str(get_first(item, ["dminsttNm", "수요기관명", "수요기관"]))
    inst2 = str(get_first(item, ["ntceInsttNm", "공고기관명", "공고기관"]))

    subject_hit = any(kw in subject for kw in SUBJECT_KEYWORDS)
    region_hit = any(kw in inst1 or kw in inst2 for kw in REGION_KEYWORDS)
    return subject_hit and region_hit


def normalize_row(item):
    return {
        "공고명": get_first(item, ["bidNtceNm", "공고명"]),
        "공고기관": get_first(item, ["ntceInsttNm", "공고기관명"]),
        "수요기관": get_first(item, ["dminsttNm", "수요기관명"]),
        "배정예산액": get_first(item, ["asignBdgtAmt", "배정예산액"]),
        "추정가격": get_first(item, ["presmptPrce", "추정가격"]),
        "입찰마감일시": get_first(item, ["bidClseDt", "입찰마감일시"]),
        "개찰일시": get_first(item, ["opengDt", "개찰일시"]),
        "게시일시": get_first(item, ["bidNtceDt", "공고게시일시"]),
        "공고분류": get_first(item, ["pubPrcrmntClsfcNm", "공공조달분류명"]),
        "상세URL": get_first(item, ["bidNtceDtlUrl", "입찰공고상세URL"]),
    }


# =========================
# 3. 메인 수집 로직
# =========================

def fetch_all():
    all_items = []
    page = 1
    total_count = None

    while True:
        params = {
            "serviceKey": SERVICE_KEY,
            "pageNo": page,
            "numOfRows": NUM_OF_ROWS,
            "inqryDiv": 1,
            "inqryBgnDt": START_DATE,
            "inqryEndDt": END_DATE,
            "type": "json",
        }

        resp = None
        for attempt in range(1, 5):
            try:
                resp = requests.get(BASE_URL, params=params, timeout=60)
                break
            except requests.exceptions.RequestException as e:
                wait = 2 ** attempt
                print(f"[페이지 {page}] 연결 실패({attempt}/4): {e.__class__.__name__} - {wait}초 후 재시도")
                time.sleep(wait)
        if resp is None:
            print(f"[페이지 {page}] 연결 재시도 모두 실패. 수집 중단.")
            break

        try:
            data = resp.json()
        except ValueError:
            print(f"[페이지 {page}] JSON 파싱 실패. 응답 일부: {resp.text[:300]}")
            break

        err = data.get("OpenAPI_ServiceResponse", {}).get("cmmMsgHeader") if isinstance(data, dict) else None
        if err:
            print(f"[API 오류] {err.get('errMsg')} / {err.get('returnAuthMsg')} (코드 {err.get('returnReasonCode')})")
            break
        header = data.get("response", {}).get("header", {}) if isinstance(data, dict) else {}
        if header and str(header.get("resultCode", "00")) != "00":
            print(f"[API 오류] {header.get('resultCode')} {header.get('resultMsg')}")
            break

        items, total = extract_items(data)

        if total_count is None:
            total_count = total
            print(f"전체 건수: {total_count}건 (페이지당 {NUM_OF_ROWS}건씩 조회)")

        if not items:
            print(f"[페이지 {page}] 결과 없음. 수집 종료.")
            break

        all_items.extend(items)
        print(f"[페이지 {page}] {len(items)}건 수집 (누적 {len(all_items)}건)")

        # 종료 조건: 누적 건수가 전체 건수 이상이거나, 이번 페이지가 numOfRows 미만이면 마지막 페이지
        if len(items) < NUM_OF_ROWS or (total_count and len(all_items) >= total_count):
            break

        page += 1
        time.sleep(REQUEST_DELAY_SEC)

    return all_items


def save_raw_csv(items, path):
    if not items:
        return
    rows = [normalize_row(it) for it in items]
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"전체 원본 저장 완료: {path} ({len(rows)}건)")


def save_filtered(items, path):
    filtered = [normalize_row(it) for it in items if is_match(it)]

    if not filtered:
        print("조건에 맞는 공고가 없습니다 (제주 + 건축설계/CM/안전진단 키워드).")
        return

    if HAS_OPENPYXL and path.endswith(".xlsx"):
        wb = Workbook()
        ws = wb.active
        ws.title = "제주_필터결과"
        headers = list(filtered[0].keys())
        ws.append(headers)
        for row in filtered:
            ws.append([row[h] for h in headers])
        # 컬럼 너비 자동 조정 (간단 버전)
        for i, h in enumerate(headers, start=1):
            ws.column_dimensions[chr(64 + i) if i <= 26 else "A"].width = 22
        wb.save(path)
    else:
        headers = list(filtered[0].keys())
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(filtered)

    print(f"필터링 결과 저장 완료: {path} ({len(filtered)}건)")


def _norm_dt(value, end=False):
    """YYYYMMDD 또는 YYYYMMDDHHMM 을 YYYYMMDDHHMM 으로 정규화"""
    value = value.strip()
    if len(value) == 8 and value.isdigit():
        return value + ("2359" if end else "0000")
    if len(value) == 12 and value.isdigit():
        return value
    raise argparse.ArgumentTypeError(f"날짜 형식 오류: {value} (YYYYMMDD 또는 YYYYMMDDHHMM)")


def resolve_period(args):
    if args.start or args.end:
        if not (args.start and args.end):
            raise SystemExit("오류: --start 와 --end 는 함께 지정하세요.")
        return _norm_dt(args.start), _norm_dt(args.end, end=True)
    now = datetime.now()
    begin = (now - timedelta(days=args.days)).replace(hour=0, minute=0)
    return begin.strftime("%Y%m%d%H%M"), now.strftime("%Y%m%d%H%M")


def main():
    global START_DATE, END_DATE

    parser = argparse.ArgumentParser(description="나라장터 제주 건축설계/CM/안전진단 용역 공고 수집")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="오늘 기준 최근 N일 (기본 7)")
    parser.add_argument("--start", help="조회 시작 (YYYYMMDD 또는 YYYYMMDDHHMM)")
    parser.add_argument("--end", help="조회 종료 (YYYYMMDD 또는 YYYYMMDDHHMM)")
    args = parser.parse_args()

    if not SERVICE_KEY:
        print("오류: 환경변수 G2B_SERVICE_KEY 에 data.go.kr 인증키(Decoding)를 설정하세요.")
        return

    START_DATE, END_DATE = resolve_period(args)

    print(f"조회 기간: {START_DATE} ~ {END_DATE}")
    print(f"공고명 키워드: {SUBJECT_KEYWORDS}")
    print(f"지역 키워드: {REGION_KEYWORDS}")
    print("-" * 50)

    items = fetch_all()
    print("-" * 50)
    print(f"총 수집 건수: {len(items)}건")

    save_raw_csv(items, OUTPUT_RAW_CSV)
    save_filtered(items, OUTPUT_FILTERED)


if __name__ == "__main__":
    main()
