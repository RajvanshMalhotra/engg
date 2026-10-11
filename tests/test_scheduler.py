"""Standing instructions fire on schedule or on events, never overlap, and resync after a reset. No LLM needed."""
from datetime import datetime, timedelta

import pytest

from opsagent import config, scheduler


@pytest.fixture
def sched(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(scheduler, "STATE", tmp_path / "state" / "standing.json")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    launched = []

    def fake_launch(ins, reason):
        run_id = f"run{len(launched)}"
        (config.RUNS_DIR / run_id).mkdir(parents=True)
        (config.RUNS_DIR / run_id / "events.jsonl").write_text('{"kind": "start"}\n')
        launched.append((ins.id, reason))
        return run_id

    return launched, fake_launch


def finish(run_id):
    with open(config.RUNS_DIR / run_id / "events.jsonl", "a") as f:
        f.write('{"kind": "complete"}\n')


def test_event_trigger_fires_only_on_new_events_and_never_overlaps(sched):
    launched, launch = sched
    events = {"mail.new_message": 8}
    cursor = lambda name: events[name]
    ins = scheduler.add("Process any new vendor invoices", on="mail.new_message", cursor_fn=cursor)
    now = datetime(2026, 10, 11, 15, 0)
    assert scheduler.tick(now, launch, cursor) == []                 # existing mail doesn't count
    events["mail.new_message"] = 9                                     # a vendor emails an invoice
    fired = scheduler.tick(now, launch, cursor)
    assert [f[0] for f in fired] == [ins.id] and "(id 9)" in fired[0][2]
    events["mail.new_message"] = 10                                    # another arrives while the run works
    assert scheduler.tick(now, launch, cursor) == []                   # no overlap
    finish("run0")
    assert len(scheduler.tick(now, launch, cursor)) == 1               # picked up once the first run finished


def test_reset_resyncs_without_firing(sched):
    launched, launch = sched
    events = {"mail.new_message": 9}
    cursor = lambda name: events[name]
    scheduler.add("Process any new vendor invoices", on="mail.new_message", cursor_fn=cursor)
    events["mail.new_message"] = 8                                     # company reset: ids start over
    assert scheduler.tick(datetime.now(), launch, cursor) == []
    events["mail.new_message"] = 9
    assert len(scheduler.tick(datetime.now(), launch, cursor)) == 1


def test_interval_and_daily_schedules(sched):
    launched, launch = sched
    t0 = datetime(2026, 10, 11, 10, 0)
    hourly = scheduler.add("Check for unpaid bills due this week", every_minutes=60, now=t0)
    daily = scheduler.add("Email the AP lead the open bills", daily_at="09:00", now=t0)   # 09:00 already passed today
    assert scheduler.tick(t0 + timedelta(minutes=30), launch) == []
    assert [f[0] for f in scheduler.tick(t0 + timedelta(minutes=61), launch)] == [hourly.id]
    finish("run0")
    next_morning = datetime(2026, 10, 12, 9, 1)
    assert {f[0] for f in scheduler.tick(next_morning, launch)} == {hourly.id, daily.id}


def test_paused_instruction_does_not_fire(sched):
    launched, launch = sched
    t0 = datetime(2026, 10, 11, 10, 0)
    ins = scheduler.add("Check for unpaid bills", every_minutes=5, now=t0)
    scheduler.update(ins.id, enabled=False)
    assert scheduler.tick(t0 + timedelta(hours=1), launch) == []


def test_trigger_request_carries_no_environment_content(sched):
    launched, launch = sched
    events = {"mail.new_message": 1}
    scheduler.add("Process any new vendor invoices", on="mail.new_message", cursor_fn=lambda n: events[n])
    events["mail.new_message"] = 2
    _, reason = (scheduler.tick(datetime.now(), launch, lambda n: events[n]) and launched[-1])
    assert "(id 2)" in reason and "@" not in reason                  # counts and times only, never subjects/senders
