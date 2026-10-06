"""Cost estimates checked against hand calculations.

Stopped-disk rate = volume_gb × $0.20 / 730 h.
  20 GB → 0.0054794520547945 $/h,  50 GB → 0.0136986301369863 $/h,  10 GB → 0.0027397260273973 $/h
"""

import pytest

from moni_pod import cost
from moni_pod.ledger import EXITED, RUNNING, TERMINATED, parse_iso
from tests.conftest import make_record


def test_stopped_disk_rate_hand_value():
    assert cost.stopped_disk_rate(20) == pytest.approx(0.0054794520547945)
    assert cost.stopped_disk_rate(0) == 0


def test_run_then_stopped():
    # 4090 @ $0.44/h, ran 10:00-12:30 (2.5 h) = $1.10; stopped 12:30-14:30 (2 h) × 20 GB = $0.0109589
    rec = make_record(status=EXITED, runs=[{"start": "2026-10-06T10:00:00Z", "stop": "2026-10-06T12:30:00Z"}])
    e = cost.estimate(rec, parse_iso("2026-10-06T14:30:00Z"))
    assert e.running_hours == pytest.approx(2.5)
    assert e.running_usd == pytest.approx(1.10)
    assert e.stopped_hours == pytest.approx(2.0)
    assert e.stopped_disk_usd == pytest.approx(0.0109589041)
    assert e.total_usd == pytest.approx(1.1109589041)


def test_two_runs_second_open():
    # $0.30/h: run1 1 h, gap 3 h (50 GB stopped), run2 open 0.5 h → 1.5 h × 0.30 = 0.45; 3 × 0.0136986 = 0.0410959
    rec = make_record(cost_per_hr=0.30, volume_gb=50, status=RUNNING, runs=[
        {"start": "2026-10-06T08:00:00Z", "stop": "2026-10-06T09:00:00Z"},
        {"start": "2026-10-06T12:00:00Z", "stop": None},
    ])
    e = cost.estimate(rec, parse_iso("2026-10-06T12:30:00Z"))
    assert e.running_usd == pytest.approx(0.45)
    assert e.stopped_disk_usd == pytest.approx(0.0410958904)
    assert e.total_usd == pytest.approx(0.4910958904)


def test_terminated_stops_accruing():
    # $1.00/h for 1 h, stopped 1 h (10 GB), terminated; asked a day later → 1.00 + 0.00273973
    rec = make_record(cost_per_hr=1.0, volume_gb=10, status=TERMINATED,
                      terminated_at="2026-10-06T12:00:00Z",
                      runs=[{"start": "2026-10-06T10:00:00Z", "stop": "2026-10-06T11:00:00Z"}])
    e = cost.estimate(rec, parse_iso("2026-10-07T12:00:00Z"))
    assert e.total_usd == pytest.approx(1.0027397260)


def test_cpu_pod_no_volume():
    # CPU $0.06/h, open run 15 min → $0.015
    rec = make_record(compute="CPU", hw_id="cpu3c", cost_per_hr=0.06, volume_gb=0,
                      runs=[{"start": "2026-10-06T10:00:00Z"}])
    e = cost.estimate(rec, parse_iso("2026-10-06T10:15:00Z"))
    assert e.total_usd == pytest.approx(0.015)
    assert e.stopped_disk_usd == 0


def test_no_runs_is_zero():
    assert cost.estimate(make_record(runs=[]), parse_iso("2026-10-06T10:00:00Z")).total_usd == 0


def test_max_cost_is_rate_times_ttl():
    assert cost.max_cost(0.44, 7200) == pytest.approx(0.88)
    assert cost.max_cost(0.06, 600) == pytest.approx(0.01)


def test_billed_total_and_check():
    resp = {"records": [
        {"podId": "a", "totalAmount": 0.5}, {"podId": "b", "totalAmount": 0.25},
        {"podId": "a", "totalAmount": 0.1}, {"podId": "a", "totalAmount": None},
    ]}
    assert cost.billed_total(resp) == pytest.approx(0.85)
    assert cost.billed_total(resp, "a") == pytest.approx(0.6)
    chk = cost.BillingCheck(estimate_usd=0.5, billed_usd=0.6)
    assert chk.diff_usd == pytest.approx(0.1)
    assert chk.diff_ratio == pytest.approx(0.2)
    assert cost.BillingCheck(0, 0.1).diff_ratio is None
