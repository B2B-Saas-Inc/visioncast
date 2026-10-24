from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import pytest
from django.core.exceptions import ValidationError

from webinars.timeutils import local_to_utc, validate_timezone

UTC = dt_timezone.utc


def test_london_spring_forward_gap_is_nonexistent():
    with pytest.raises(ValidationError) as exc:
        local_to_utc(datetime(2026, 3, 29, 1, 30), "Europe/London", None)
    assert exc.value.code == "nonexistent"


def test_london_gap_rejected_even_with_fold():
    for fold in (0, 1):
        with pytest.raises(ValidationError) as exc:
            local_to_utc(datetime(2026, 3, 29, 1, 30), "Europe/London", fold)
        assert exc.value.code == "nonexistent"


def test_london_fall_back_ambiguous_without_fold():
    with pytest.raises(ValidationError) as exc:
        local_to_utc(datetime(2026, 10, 25, 1, 30), "Europe/London", None)
    assert exc.value.code == "ambiguous"


def test_london_fall_back_fold_choices_one_hour_apart():
    first = local_to_utc(datetime(2026, 10, 25, 1, 30), "Europe/London", 0)
    second = local_to_utc(datetime(2026, 10, 25, 1, 30), "Europe/London", 1)
    assert first == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # BST (UTC+1)
    assert second == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # GMT
    assert second - first == timedelta(hours=1)


def test_unambiguous_london_times():
    assert local_to_utc(datetime(2026, 7, 1, 12, 0), "Europe/London", None) == datetime(2026, 7, 1, 11, 0, tzinfo=UTC)
    assert local_to_utc(datetime(2026, 1, 15, 12, 0), "Europe/London", None) == datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def test_new_york_conversions():
    assert local_to_utc(datetime(2026, 7, 1, 12, 0), "America/New_York", None) == datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    assert local_to_utc(datetime(2026, 1, 15, 12, 0), "America/New_York", None) == datetime(2026, 1, 15, 17, 0, tzinfo=UTC)


def test_new_york_dst_edges():
    with pytest.raises(ValidationError) as exc:
        local_to_utc(datetime(2026, 3, 8, 2, 30), "America/New_York", None)
    assert exc.value.code == "nonexistent"
    with pytest.raises(ValidationError) as exc:
        local_to_utc(datetime(2026, 11, 1, 1, 30), "America/New_York", None)
    assert exc.value.code == "ambiguous"
    a = local_to_utc(datetime(2026, 11, 1, 1, 30), "America/New_York", 0)
    b = local_to_utc(datetime(2026, 11, 1, 1, 30), "America/New_York", 1)
    assert a == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert b == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


def test_result_is_aware_utc():
    result = local_to_utc(datetime(2026, 7, 1, 12, 0), "Asia/Tokyo", None)
    assert result.utcoffset() == timedelta(0)
    assert result == datetime(2026, 7, 1, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize("name", ["Mars/Olympus_Mons", "", "../../etc/passwd", "Europe/London ", "GMT+25"])
def test_invalid_timezone_rejected(name):
    with pytest.raises(ValidationError):
        validate_timezone(name)
    with pytest.raises(ValidationError):
        local_to_utc(datetime(2026, 7, 1, 12, 0), name, None)


def test_valid_timezone_returns_zoneinfo():
    tz = validate_timezone("America/New_York")
    assert str(tz) == "America/New_York"
