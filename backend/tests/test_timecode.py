import pytest

from clipper.timecode import to_seconds, to_timecode


def test_round_trip():
    assert to_seconds("01:02:03") == 3723
    assert to_timecode(3723.9) == "01:02:03"


def test_accepts_minutes_seconds_and_fractions():
    assert to_seconds("2:05") == 125
    assert to_seconds("00:00:01.5") == 1.5
    assert to_seconds("9:59:59") == 35999


def test_rejects_non_timecodes():
    with pytest.raises(ValueError):
        to_seconds("soon")
