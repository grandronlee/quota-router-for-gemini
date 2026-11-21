import datetime as dt

from quota_router.clock import next_pt_midnight, pt_date

from helpers import pt

UTC = dt.timezone.utc


def utc(*a: int) -> float:
    return dt.datetime(*a, tzinfo=UTC).timestamp()


def test_spring_forward_day_is_23_hours():
    # 2026-03-08: clocks go from 02:00 PST to 03:00 PDT.
    assert next_pt_midnight(pt(2026, 3, 7, 12)) == utc(2026, 3, 8, 8)    # 00:00 PST = 08:00 UTC
    assert next_pt_midnight(pt(2026, 3, 8, 12)) == utc(2026, 3, 9, 7)    # 00:00 PDT = 07:00 UTC
    assert utc(2026, 3, 9, 7) - utc(2026, 3, 8, 8) == 23 * 3600


def test_fall_back_day_is_25_hours():
    # 2026-11-01: clocks go from 02:00 PDT back to 01:00 PST.
    assert next_pt_midnight(pt(2026, 10, 31, 12)) == utc(2026, 11, 1, 7)
    assert next_pt_midnight(pt(2026, 11, 1, 12)) == utc(2026, 11, 2, 8)
    # Inside the repeated hour (01:30 PST, the second 01:30 of the day).
    assert next_pt_midnight(utc(2026, 11, 1, 9, 30)) == utc(2026, 11, 2, 8)


def test_one_second_before_midnight():
    t = pt(2026, 11, 20, 23, 59, 59)
    assert next_pt_midnight(t) == t + 1
    assert pt_date(t) == "2026-11-20"


def test_exactly_midnight_starts_a_new_day():
    t = pt(2026, 11, 21, 0, 0, 0)
    assert pt_date(t) == "2026-11-21"
    assert next_pt_midnight(t) == pt(2026, 11, 22)
    assert next_pt_midnight(t) - t == 24 * 3600


def test_pt_date_uses_pacific_not_utc():
    # 2026-11-20 20:00 PST is already 11-21 in UTC.
    assert pt_date(pt(2026, 11, 20, 20)) == "2026-11-20"
