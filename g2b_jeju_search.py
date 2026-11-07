# -*- coding: utf-8 -*-
"""
나라장터(조달청) 입찰공고정보서비스 - 제주 지역 + 건축설계/CM/안전진단 용역 자동 수집 스크립트 (v2.0.0)

사용 전 준비:
1. pip install -r requirements.txt
2. 인증키 설정 (코드에 직접 쓰지 않음) — 둘 중 하나:
   - 환경변수:  export G2B_SERVICE_KEY="발급받은_Decoding_인증키"
   - .env 파일: .env.example 을 .env 로 복사한 뒤 키 입력 (.env 는 git 에 올라가지 않음)
   ※ data.go.kr 마이페이지 > 개발계정 상세보기 > 일반 인증키(Decoding)
     Encoding 키를 넣어도 자동으로 디코딩해서 사용합니다.
3. 키워드·지역은 config.json 에서 수정 (없으면 스크립트 기본값 사용)

실행 예:
    python g2b_jeju_search.py                         # 최근 7일
    python g2b_jeju_search.py --days 14               # 최근 14일
    python g2b_jeju_search.py --from 20261101 --to 20261107
    python g2b_jeju_search.py --from 202611010900 --to 202611071800 --out-dir 결과

결과 (--out-dir, 기본: 현재 폴더):
    - jeju_filtered.xlsx  : 제주 + 대상 키워드 공고 (마감일순, 신규·마감임박 표시, 링크 클릭 가능)
    - all_raw.csv         : 조회 기간 전체 원본 (API 응답 필드 전체)
    - seen_notices.json   : 이전 실행에서 본 공고 목록 ("신규" 판정용)
"""

__version__ = "2.0.0"

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import unquote

import requests

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

BASE_URL = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServcPPSSrch"
ENV_KEY_NAME = "G2B_SERVICE_KEY"

# config.json 이 없거나 일부 항목이 빠졌을 때 쓰는 기본값
DEFAULT_CONFIG = {
    # 공고명에 하나라도 포함되면 대상 업무로 판단
    "subject_keywords": [
        "건축설계", "설계용역", "기본설계", "실시설계", "리모델링",
        "건설사업관리", "CM용역", "감리",
        "안전진단", "정밀안전진단", "정밀점검", "정밀안전점검", "정기안전점검", "안전점검",
    ],
    # 공고명에 하나라도 포함되면 제외 (예: "전기", "통신")
    "exclude_keywords": [],
    # 아래 region_fields 중 어느 하나에라도 포함되면 제주 관련으로 판단
    "region_keywords": ["제주"],
    # 지역 판정에 쓸 항목: demand_inst(수요기관), notice_inst(공고기관), subject(공고명)
    "region_fields": ["demand_inst", "notice_inst", "subject"],
    "num_of_rows": 100,          # 페이지당 건수
    "chunk_days": 7,             # 조회 기간을 이 일수 단위로 나눠 호출 (API 기간 제한 대비)
    "request_delay_sec": 0.3,    # 호출 간 대기 (초)
    "max_retries": 3,            # 네트워크 오류·5xx 재시도 횟수
    "deadline_warn_days": 3,     # 마감 D-n 이내 강조
}

# API 응답 키 후보 (영문 표준 / 관측된 국문 키)
FIELD_KEYS = {
    "no": ["bidNtceNo", "입찰공고번호"],
    "ord": ["bidNtceOrd", "입찰공고차수"],
    "kind": ["ntceKindNm", "공고종류명"],
    "subject": ["bidNtceNm", "공고명", "입찰공고명"],
    "notice_inst": ["ntceInsttNm", "공고기관명", "공고기관"],
    "demand_inst": ["dminsttNm", "수요기관명", "수요기관"],
    "budget": ["asignBdgtAmt", "배정예산액"],
    "est_price": ["presmptPrce", "추정가격"],
    "posted": ["bidNtceDt", "공고게시일시", "입찰공고일시"],
    "close": ["bidClseDt", "입찰마감일시"],
    "open": ["opengDt", "개찰일시"],
    "category": ["pubPrcrmntClsfcNm", "공공조달분류명"],
    "url": ["bidNtceDtlUrl", "입찰공고상세URL"],
}

SUCCESS_CODES = {"00", "0", "000", "INFO-000"}


class ApiError(Exception):
    """API가 오류를 돌려준 경우 (재시도해도 해결되지 않는 오류)"""


class RetryableError(Exception):
    """일시적 오류 (5xx 등) — 재시도 대상"""


# =========================
# 1. 설정 / 인증키
# =========================

def load_config(path):
    config = dict(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        unknown = sorted(set(user) - set(DEFAULT_CONFIG) - {"_설명"})
        if unknown:
            print(f"경고: config 에 알 수 없는 항목이 있어 무시합니다: {unknown}")
        config.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
    return config


def read_env_file(path):
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def load_service_key():
    key = os.environ.get(ENV_KEY_NAME, "").strip()
    if not key:
        for env_path in (os.path.join(os.getcwd(), ".env"), os.path.join(SCRIPT_DIR, ".env")):
            key = read_env_file(env_path).get(ENV_KEY_NAME, "").strip()
            if key:
                break
    if key and "%" in key:
        # Encoding 키를 넣은 경우 — requests 가 다시 인코딩하므로 디코딩해서 사용
        key = unquote(key)
    return key


# =========================
# 2. 날짜 처리
# =========================

def parse_arg_datetime(value, is_end=False):
    """YYYYMMDD 또는 YYYYMMDDHHMM → datetime"""
    value = value.strip().replace("-", "")
    if re.fullmatch(r"\d{8}", value):
        value += "2359" if is_end else "0000"
    if not re.fullmatch(r"\d{12}", value):
        raise argparse.ArgumentTypeError(f"날짜 형식 오류: {value} (YYYYMMDD 또는 YYYYMMDDHHMM)")
    return datetime.strptime(value, "%Y%m%d%H%M")


def parse_api_datetime(value):
    s = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M%S", "%Y%m%d%H%M",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def split_period(start, end, chunk_days):
    """[start, end] 를 chunk_days 단위 구간으로 분할"""
    chunks = []
    cur = start
    step = timedelta(days=max(1, int(chunk_days)))
    while cur <= end:
        chunk_end = min(cur + step - timedelta(minutes=1), end)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(minutes=1)
    return chunks


# =========================
# 3. 응답 파싱
# =========================

def get_first(d, keys, default=""):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def field(item, name):
    return get_first(item, FIELD_KEYS[name])


def find_header(obj):
    """응답 JSON 어디에 있든 resultCode 를 가진 header 를 찾음 (정상/오류 응답 모두)"""
    if isinstance(obj, dict):
        if "resultCode" in obj:
            return obj
        for v in obj.values():
            found = find_header(v)
            if found is not None:
                return found
    return None


def extract_items(payload):
    """응답 JSON 에서 (항목 리스트, 전체건수) 추출. 영문 표준/국문 스키마 대응."""
    if not isinstance(payload, dict):
        return [], 0

    if "response" in payload:
        body = payload.get("response", {}).get("body", {}) or {}
        items = body.get("items", [])
        total = body.get("totalCount", 0)
    elif "응답" in payload:
        body = payload.get("응답", {}).get("몸", {}) or {}
        items = body.get("항목", [])
        total = body.get("총 개수", 0) or body.get("총개수", 0)
    else:
        return [], 0

    if isinstance(items, dict):
        items = items.get("item", items)
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []
    total = int(total) if str(total).isdigit() else 0
    return items, total


def parse_response(resp):
    """HTTP 응답 → JSON payload. 오류 응답이면 ApiError/RetryableError 발생."""
    if resp.status_code >= 500 or resp.status_code == 429:
        raise RetryableError(f"HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except ValueError:
        # data.go.kr 게이트웨이 오류는 type=json 이어도 XML 로 올 수 있음
        text = resp.text or ""
        msgs = []
        for tag in ("returnAuthMsg", "returnReasonCode", "errMsg", "resultCode", "resultMsg"):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
            if m:
                msgs.append(f"{tag}={m.group(1).strip()}")
        detail = ", ".join(msgs) if msgs else text[:300]
        raise ApiError(f"HTTP {resp.status_code} / JSON 아님: {detail}")

    if resp.status_code >= 400:
        raise ApiError(f"HTTP {resp.status_code}: {str(payload)[:300]}")

    header = find_header(payload)
    if header is not None:
        code = str(header.get("resultCode", "")).strip()
        if code and code not in SUCCESS_CODES:
            raise ApiError(f"API 오류 resultCode={code}, resultMsg={header.get('resultMsg', '')}")
    return payload


# =========================
# 4. 수집
# =========================

def request_page(session, params, max_retries, delay):
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(BASE_URL, params=params, timeout=30)
            return parse_response(resp)
        except (requests.ConnectionError, requests.Timeout, RetryableError) as e:
            last_err = e
            if attempt < max_retries:
                wait = delay * (2 ** attempt)
                print(f"  일시 오류({e.__class__.__name__}: {e}) → {wait:.1f}초 후 재시도 ({attempt}/{max_retries - 1})")
                time.sleep(wait)
    raise ApiError(f"{max_retries}회 시도 후 실패: {last_err}")


def fetch_period(session, service_key, start, end, config):
    rows = config["num_of_rows"]
    items_all = []
    page = 1
    total_count = None
    while True:
        params = {
            "serviceKey": service_key,
            "pageNo": page,
            "numOfRows": rows,
            "inqryDiv": 1,   # 1: 공고게시일시 기준
            "inqryBgnDt": start.strftime("%Y%m%d%H%M"),
            "inqryEndDt": end.strftime("%Y%m%d%H%M"),
            "type": "json",
        }
        payload = request_page(session, params, config["max_retries"], max(config["request_delay_sec"], 0.5))
        items, total = extract_items(payload)
        if total_count is None:
            total_count = total
        if not items:
            break
        items_all.extend(items)
        if len(items) < rows or (total_count and len(items_all) >= total_count):
            break
        page += 1
        time.sleep(config["request_delay_sec"])
    print(f"  {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}: {len(items_all)}건 (API 전체 {total_count or 0}건)")
    return items_all


def fetch_all(service_key, start, end, config, session=None):
    session = session or requests.Session()
    collected = []
    for i, (cs, ce) in enumerate(split_period(start, end, config["chunk_days"])):
        if i:
            time.sleep(config["request_delay_sec"])
        collected.extend(fetch_period(session, service_key, cs, ce, config))
    return collected


# =========================
# 5. 정리 (중복 제거 / 필터 / 정규화)
# =========================

def notice_key(item):
    no = str(field(item, "no")).strip()
    ord_ = str(field(item, "ord")).strip()
    return f"{no}-{ord_}" if no else ""


def _ord_int(item):
    try:
        return int(str(field(item, "ord")).strip() or 0)
    except ValueError:
        return 0


def dedupe_exact(items):
    """같은 공고번호-차수 중복 제거 (원본 저장용)"""
    seen, out = set(), []
    for it in items:
        k = notice_key(it) or json.dumps(it, ensure_ascii=False, sort_keys=True)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def keep_latest_order(items):
    """같은 공고번호는 최신 차수(정정·재공고 반영)만 남김"""
    latest = {}
    order = []
    for it in items:
        no = str(field(it, "no")).strip()
        if not no:
            order.append(it)
            continue
        if no not in latest:
            order.append(no)
            latest[no] = it
        elif _ord_int(it) >= _ord_int(latest[no]):
            latest[no] = it
    return [latest[x] if isinstance(x, str) else x for x in order]


def match_info(item, config):
    """(대상 여부, 걸린 키워드 목록)"""
    subject = str(field(item, "subject"))
    if any(kw and kw in subject for kw in config["exclude_keywords"]):
        return False, []
    subject_hits = [kw for kw in config["subject_keywords"] if kw and kw in subject]
    if not subject_hits:
        return False, []
    region_text = " ".join(str(field(item, f)) for f in config["region_fields"] if f in FIELD_KEYS)
    if not any(kw and kw in region_text for kw in config["region_keywords"]):
        return False, []
    return True, subject_hits


def to_number(value):
    s = str(value or "").replace(",", "").strip()
    if not s:
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return int(n) if n.is_integer() else n


def deadline_status(close_dt, now, warn_days):
    """(상태 문자열, 마감임박 여부)"""
    if close_dt is None:
        return "", False
    if close_dt < now:
        return "마감", False
    days = (close_dt.date() - now.date()).days
    return f"D-{days}", days <= warn_days


def build_rows(items, config, seen_keys, now):
    rows = []
    for it in items:
        ok, hits = match_info(it, config)
        if not ok:
            continue
        key = notice_key(it)
        close_dt = parse_api_datetime(field(it, "close"))
        status, urgent = deadline_status(close_dt, now, config["deadline_warn_days"])
        rows.append({
            "_key": key,
            "_close": close_dt,
            "_urgent": urgent,
            "신규": "신규" if key and key not in seen_keys else "",
            "상태": status,
            "공고번호": key,
            "공고종류": field(it, "kind"),
            "공고명": field(it, "subject"),
            "공고기관": field(it, "notice_inst"),
            "수요기관": field(it, "demand_inst"),
            "배정예산액": to_number(field(it, "budget")),
            "추정가격": to_number(field(it, "est_price")),
            "게시일시": field(it, "posted"),
            "입찰마감일시": field(it, "close"),
            "개찰일시": field(it, "open"),
            "공고분류": field(it, "category"),
            "매칭키워드": ", ".join(hits),
            "상세URL": field(it, "url"),
        })
    # 진행 중 공고: 마감 빠른 순 → 마감일 미상 → 마감된 공고
    def sort_key(r):
        c = r["_close"]
        if c is None:
            return (1, datetime.max)
        if c < now:
            return (2, -c.timestamp())
        return (0, c)
    rows.sort(key=sort_key)
    return rows


# =========================
# 6. 신규 판정 상태 파일
# =========================

def load_seen(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("seen", {})
    except (ValueError, OSError):
        print(f"경고: {path} 를 읽지 못해 모든 공고를 신규로 처리합니다.")
        return {}


def save_seen(path, seen, rows, now):
    for r in rows:
        if r["_key"] and r["_key"] not in seen:
            seen[r["_key"]] = now.strftime("%Y-%m-%d %H:%M")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": __version__, "seen": seen}, f, ensure_ascii=False, indent=1)


# =========================
# 7. 저장
# =========================

def save_raw_csv(items, path):
    if not items:
        print("원본 저장 생략: 수집된 공고가 없습니다.")
        return
    fieldnames = []
    for it in items:
        for k in it:
            if k not in fieldnames:
                fieldnames.append(k)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(items)
    print(f"전체 원본 저장: {path} ({len(items)}건, {len(fieldnames)}개 필드)")


def public_columns(rows):
    return [k for k in rows[0] if not k.startswith("_")]


def save_filtered(rows, path):
    if not rows:
        print("조건에 맞는 공고가 없습니다.")
        return None

    headers = public_columns(rows)

    if not (HAS_OPENPYXL and path.endswith(".xlsx")):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"필터링 결과 저장: {path} ({len(rows)}건)")
        return path

    wb = Workbook()
    ws = wb.active
    ws.title = "제주_필터결과"
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="D9E1F2")

    urgent_fill = PatternFill("solid", fgColor="F8CBAD")
    new_fill = PatternFill("solid", fgColor="FFF2CC")
    closed_font = Font(color="808080")
    col = {h: i + 1 for i, h in enumerate(headers)}

    for r_idx, row in enumerate(rows, start=2):
        for h in headers:
            ws.cell(row=r_idx, column=col[h], value=row[h])
        for h in ("배정예산액", "추정가격"):
            ws.cell(row=r_idx, column=col[h]).number_format = "#,##0"
        url = row["상세URL"]
        if url:
            c = ws.cell(row=r_idx, column=col["상세URL"])
            c.value = "공고 보기"
            c.hyperlink = url
            c.style = "Hyperlink"
        if row["신규"]:
            ws.cell(row=r_idx, column=col["신규"]).fill = new_fill
        if row["_urgent"]:
            for h in ("상태", "공고명", "입찰마감일시"):
                ws.cell(row=r_idx, column=col[h]).fill = urgent_fill
        elif row["상태"] == "마감":
            for h in headers:
                if h != "상세URL":
                    ws.cell(row=r_idx, column=col[h]).font = closed_font

    widths = {"신규": 6, "상태": 7, "공고번호": 18, "공고종류": 9, "공고명": 50, "공고기관": 22,
              "수요기관": 22, "배정예산액": 15, "추정가격": 15, "게시일시": 18, "입찰마감일시": 18,
              "개찰일시": 18, "공고분류": 16, "매칭키워드": 18, "상세URL": 11}
    for h, i in col.items():
        ws.column_dimensions[get_column_letter(i)].width = widths.get(h, 15)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)
    print(f"필터링 결과 저장: {path} ({len(rows)}건)")
    return path


# =========================
# 8. 실행
# =========================

def build_arg_parser():
    p = argparse.ArgumentParser(
        description="나라장터 제주 건축설계/CM/안전진단 용역 공고 수집",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--from", dest="date_from", help="조회 시작 (YYYYMMDD 또는 YYYYMMDDHHMM)")
    p.add_argument("--to", dest="date_to", help="조회 종료 (YYYYMMDD 또는 YYYYMMDDHHMM, 기본: 현재)")
    p.add_argument("--days", type=int, default=7, help="--from 미지정 시 최근 N일 (기본 7)")
    p.add_argument("--config", default=os.path.join(SCRIPT_DIR, "config.json"), help="설정 파일 경로")
    p.add_argument("--out-dir", default=".", help="결과 저장 폴더 (기본: 현재 폴더)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def resolve_period(args, now):
    end = parse_arg_datetime(args.date_to, is_end=True) if args.date_to else now.replace(second=0, microsecond=0)
    if args.date_from:
        start = parse_arg_datetime(args.date_from)
    else:
        start = (end - timedelta(days=max(1, args.days))).replace(second=0, microsecond=0)
    if start > end:
        raise argparse.ArgumentTypeError(f"시작일({start:%Y-%m-%d %H:%M})이 종료일({end:%Y-%m-%d %H:%M})보다 늦습니다.")
    return start, end


def main(argv=None, session=None, now=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    now = now or datetime.now()

    try:
        start, end = resolve_period(args, now)
    except argparse.ArgumentTypeError as e:
        parser.error(str(e))

    service_key = load_service_key()
    if not service_key:
        print(f"오류: 인증키가 없습니다. 환경변수 {ENV_KEY_NAME} 또는 .env 파일에 설정하세요 (.env.example 참고).")
        return 1

    config = load_config(args.config)
    os.makedirs(args.out_dir, exist_ok=True)
    out_filtered = os.path.join(args.out_dir, "jeju_filtered.xlsx" if HAS_OPENPYXL else "jeju_filtered.csv")
    out_raw = os.path.join(args.out_dir, "all_raw.csv")
    state_path = os.path.join(args.out_dir, "seen_notices.json")

    print(f"g2b_jeju_search v{__version__}")
    print(f"조회 기간: {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M} ({config['chunk_days']}일 단위 분할 조회)")
    print(f"공고명 키워드: {config['subject_keywords']}")
    if config["exclude_keywords"]:
        print(f"제외 키워드: {config['exclude_keywords']}")
    print(f"지역 키워드: {config['region_keywords']} (판정 항목: {config['region_fields']})")
    print("-" * 60)

    try:
        items = fetch_all(service_key, start, end, config, session=session)
    except ApiError as e:
        print(f"오류: {e}")
        print("※ 인증키 등록 직후라면 활성화까지 시간이 걸릴 수 있습니다. 키·조회기간을 확인하세요.")
        return 1

    raw = dedupe_exact(items)
    latest = keep_latest_order(raw)
    print("-" * 60)
    print(f"총 수집 {len(items)}건 → 중복 제거 {len(raw)}건 → 최신 차수 기준 {len(latest)}건")

    seen = load_seen(state_path)
    rows = build_rows(latest, config, set(seen), now)
    new_count = sum(1 for r in rows if r["신규"])
    urgent_count = sum(1 for r in rows if r["_urgent"])

    save_raw_csv(raw, out_raw)
    save_filtered(rows, out_filtered)
    save_seen(state_path, seen, rows, now)
    print(f"대상 공고 {len(rows)}건 (신규 {new_count}건, 마감 D-{config['deadline_warn_days']} 이내 {urgent_count}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
