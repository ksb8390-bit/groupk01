# -*- coding: utf-8 -*-
"""
나라장터(조달청) 입찰공고정보서비스 - 제주 지역 + 건축설계/CM/안전진단 용역 자동 수집 스크립트

사용 전 준비:
1. pip install requests openpyxl --break-system-packages   (또는 그냥 pip install requests openpyxl)
2. 인증키 설정 (코드에 직접 넣지 말 것 — 저장소에 커밋되지 않도록)
   - 스크립트와 같은 폴더에 .env 파일을 만들고 한 줄 입력:  G2B_SERVICE_KEY=발급받은키
     (.env 는 .gitignore 에 포함되어 있음)
   - 또는 환경변수 G2B_SERVICE_KEY 로 지정
   - Encoding/Decoding 키 어느 쪽을 넣어도 됨 (%2F 등 인코딩된 키는 자동 디코딩)
3. 조회 기간: 인자로 지정하거나, 생략하면 최근 7일 (형식: YYYYMMDDHHMM)

실행:
    python g2b_jeju_search.py                              # 최근 7일
    python g2b_jeju_search.py 202611010000 202611072359    # 기간 지정

결과:
    - jeju_filtered.xlsx  : 제주 + 건축설계/CM/안전진단 키워드가 걸린 공고만
    - all_raw.csv         : 조회 기간 전체 원본 데이터 (백업/재검토용)
"""

import os
import sys
import time
import csv
import urllib.parse
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
def load_service_key():
    """환경변수 G2B_SERVICE_KEY → 스크립트 폴더의 .env 순으로 인증키를 읽는다."""
    key = os.environ.get("G2B_SERVICE_KEY", "").strip()
    if not key:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("G2B_SERVICE_KEY="):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
    # Encoding 키(%2F, %3D 등 포함)가 들어오면 Decoding 키로 변환 (requests가 다시 인코딩함)
    if "%" in key:
        key = urllib.parse.unquote(key)
    return key


SERVICE_KEY = load_service_key()

BASE_URL = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServcPPSSrch"

# 조회 기간 (YYYYMMDDHHMM): 명령행 인자 > 기본값(최근 7일)
_now = datetime.now()
START_DATE = sys.argv[1] if len(sys.argv) > 1 else (_now - timedelta(days=7)).strftime("%Y%m%d0000")
END_DATE   = sys.argv[2] if len(sys.argv) > 2 else _now.strftime("%Y%m%d2359")

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

MAX_RETRIES = 5


def get_with_retry(params, page):
    """일시적 연결 끊김(Connection reset 등)에 대비해 지수 백오프로 재시도."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return requests.get(BASE_URL, params=params, timeout=30)
        except requests.exceptions.RequestException as e:
            wait = 2 ** attempt
            print(f"[페이지 {page}] 요청 실패({type(e).__name__}) - {wait}초 후 재시도 {attempt}/{MAX_RETRIES}")
            time.sleep(wait)
    return None


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

        resp = get_with_retry(params, page)
        if resp is None:
            print(f"[페이지 {page}] 재시도 초과로 수집 중단 (누적 {len(all_items)}건까지 저장)")
            break

        try:
            data = resp.json()
        except ValueError:
            print(f"[페이지 {page}] JSON 파싱 실패. 응답 일부: {resp.text[:300]}")
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


def main():
    if not SERVICE_KEY:
        print("오류: 인증키가 없습니다. .env 파일에 G2B_SERVICE_KEY=키 를 입력하거나 환경변수로 지정하세요.")
        return

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
