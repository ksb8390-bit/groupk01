from datetime import date, datetime

from hr import rules


def test_annual_leave_by_years():
    assert [rules.annual_leave_for_year(y) for y in (1, 2, 3, 4, 5, 21, 30)] == [15, 15, 16, 16, 17, 25, 25]


def test_first_year_monthly_accrual():
    hire = date(2026, 3, 15)
    assert rules.leave_entitlement(hire, date(2026, 4, 14))["granted"] == 0
    assert rules.leave_entitlement(hire, date(2026, 4, 15))["granted"] == 1
    assert rules.leave_entitlement(hire, date(2027, 3, 14))["granted"] == 11


def test_after_one_year():
    e = rules.leave_entitlement(date(2023, 1, 2), date(2026, 11, 10))
    assert e["granted"] == 16 and e["period_start"] == date(2026, 1, 2)


def test_worked_minutes_and_break():
    ci, co = datetime(2026, 11, 10, 9, 0), datetime(2026, 11, 10, 18, 0)
    assert rules.worked_minutes(ci, co, 60) == 480
    assert rules.worked_minutes(ci, co) == 480
    assert rules.worked_minutes(ci, None) == 0


def test_late():
    assert rules.late_minutes(datetime(2026, 11, 10, 9, 7), "09:00") == 7
    assert rules.late_minutes(datetime(2026, 11, 10, 9, 7), "09:00", grace_min=10) == 0
    assert rules.late_minutes(datetime(2026, 11, 10, 8, 50), "09:00") == 0


def test_leave_days_excludes_weekend_and_holidays():
    # 2026-11-09(월) ~ 11-15(일)
    assert rules.leave_days(date(2026, 11, 9), date(2026, 11, 15), "full") == 5
    assert rules.leave_days(date(2026, 11, 9), date(2026, 11, 15), "full", {date(2026, 11, 11)}) == 4
    assert rules.leave_days(date(2026, 11, 9), date(2026, 11, 9), "am") == 0.5


def test_haversine():
    # 제주시청 ~ 약 1km 북쪽
    assert 1000 < rules.haversine_m(33.4996, 126.5312, 33.5090, 126.5312) < 1100
