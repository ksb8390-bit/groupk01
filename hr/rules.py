# -*- coding: utf-8 -*-
"""근태·연차 계산 규칙 (DB 비의존 순수 함수).

근거(기억 기반 인용 — 최신 개정 여부는 국가법령정보센터에서 확인 필요):
- 근로기준법 제50조: 1주 40시간, 1일 8시간
- 근로기준법 제53조: 연장근로 1주 12시간 한도 → 주 52시간
- 근로기준법 제54조: 4시간 → 30분 이상, 8시간 → 1시간 이상 휴게
- 근로기준법 제60조: 연차유급휴가
    ① 1년간 80% 이상 출근 시 15일
    ② 1년 미만(또는 80% 미만) 1개월 개근 시 1일 (최대 11일)
    ④ 3년 이상 계속근로 시 최초 1년 초과 매 2년마다 1일 가산, 총 25일 한도
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

WEEKLY_STANDARD_MIN = 40 * 60
WEEKLY_LIMIT_MIN = 52 * 60


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 2/29 입사
        return d.replace(year=d.year + years, day=28)


def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def full_years(hire: date, on: date) -> int:
    n = on.year - hire.year
    if (on.month, on.day) < (hire.month, hire.day):
        n -= 1
    return max(n, 0)


def annual_leave_for_year(service_years: int) -> int:
    """계속근로 service_years년(≥1)을 채운 시점에 발생하는 연차 일수."""
    if service_years < 1:
        return 0
    return min(15 + (service_years - 1) // 2, 25)


def leave_entitlement(hire: date, on: date) -> dict:
    """입사일 기준 연차 산정.

    반환: {"period_start", "period_end", "granted", "kind"}
    - 1년 미만: 입사일~1주년 전일 기간, 경과한 개월 수만큼(최대 11) 발생 (개근 가정)
    - 1년 이상: 직전 입사기념일~다음 입사기념일 전일, 제60조 ①④ 일수
    출근율(80%)·개근 여부는 자동 판정하지 않음 → 관리자가 '조정' 항목으로 반영.
    """
    yrs = full_years(hire, on)
    if yrs == 0:
        months = 0
        while months < 11 and add_months(hire, months + 1) <= on:
            months += 1
        return {
            "period_start": hire,
            "period_end": add_years(hire, 1) - timedelta(days=1),
            "granted": months,
            "kind": "1년 미만(월 단위 발생)",
        }
    start = add_years(hire, yrs)
    return {
        "period_start": start,
        "period_end": add_years(hire, yrs + 1) - timedelta(days=1),
        "granted": annual_leave_for_year(yrs),
        "kind": f"근속 {yrs}년차",
    }


def required_break_minutes(span_min: int) -> int:
    """제54조 최소 휴게시간 (체류시간 기준 단순 적용)."""
    if span_min >= 8 * 60 + 60:  # 휴게 1시간 포함 체류 9시간 이상
        return 60
    if span_min >= 4 * 60 + 30:
        return 30
    return 0


def worked_minutes(check_in: datetime | None, check_out: datetime | None,
                   break_min: int | None = None) -> int:
    if not check_in or not check_out or check_out <= check_in:
        return 0
    span = int((check_out - check_in).total_seconds() // 60)
    brk = required_break_minutes(span) if break_min is None else break_min
    return max(span - brk, 0)


def late_minutes(check_in: datetime, scheduled_start: str, grace_min: int = 0) -> int:
    h, m = map(int, scheduled_start.split(":"))
    sched = check_in.replace(hour=h, minute=m, second=0, microsecond=0)
    diff = math.floor((check_in - sched).total_seconds() / 60)
    return diff if diff > grace_min else 0


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def leave_days(start: date, end: date, unit: str, holidays: set[date] | None = None) -> float:
    """휴가 사용일수. unit: full / am / pm / quarter."""
    if unit in ("am", "pm"):
        return 0.5
    if unit == "quarter":
        return 0.25
    holidays = holidays or set()
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            n += 1
        d += timedelta(days=1)
    return float(n)


def fmt_hm(minutes: int) -> str:
    sign = "-" if minutes < 0 else ""
    minutes = abs(int(minutes))
    return f"{sign}{minutes // 60}:{minutes % 60:02d}"
