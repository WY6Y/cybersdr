import os

from decoder.wspr import SmartScheduler


def test_capture_failure_does_not_park_band(monkeypatch):
    monkeypatch.setenv("SMART_ROTATION", "true")
    monkeypatch.setenv("BAND_SKIP_EMPTY", "1")
    scheduler = SmartScheduler("EM15")

    scheduler.record_failure("30m")

    assert scheduler.status_snapshot()["empty_streak"]["30m"] == 0
    assert "30m" not in scheduler.status_snapshot()["parked_until"]


def test_empty_successful_capture_still_parks_band(monkeypatch):
    monkeypatch.setenv("SMART_ROTATION", "true")
    monkeypatch.setenv("BAND_SKIP_EMPTY", "1")
    scheduler = SmartScheduler("EM15")

    scheduler.record_result("30m", 0)

    assert scheduler.status_snapshot()["empty_streak"]["30m"] == 1
    assert "30m" in scheduler.status_snapshot()["parked_until"]
