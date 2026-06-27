from datetime import datetime, timezone

from argus.v2.advisor import digest, state


GROUP = "120363000000000001@g.us"


def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_ADVISOR_DIR", str(tmp_path / "advisor"))
    monkeypatch.setenv("ARGUS_ADVISOR_GROUPS", GROUP)


def seed(day_ts: int, count: int) -> None:
    for idx in range(count):
        state.record_message(GROUP, {
            "id": f"M{day_ts}-{idx}",
            "ts": day_ts + idx,
            "push_name": "Alice",
            "body": f"topic {idx}",
        })


def rows():
    return state.digests(GROUP)


def test_quiet_day_records_without_posting(tmp_path, monkeypatch, conn):
    env(monkeypatch, tmp_path)
    monkeypatch.setenv("ARGUS_ADVISOR_DIGEST_MIN_MESSAGES", "8")
    now = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)

    recorded = digest.run(now=now, engine_runner=lambda *_: "never",
                          sender=lambda *args: (_ for _ in ()).throw(AssertionError()))

    assert recorded == 1
    row = rows()[0]
    assert row["date"] == "2026-06-16"
    assert row["posted"] is False
    assert row["reason"] == "quiet"


def test_digest_posts_recap_and_seed(tmp_path, monkeypatch, conn):
    env(monkeypatch, tmp_path)
    monkeypatch.setenv("ARGUS_ADVISOR_DIGEST_MIN_MESSAGES", "2")
    now = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)
    seed(int(datetime(2026, 6, 16, tzinfo=timezone.utc).timestamp()), 2)
    sends = []

    recorded = digest.run(
        now=now,
        engine_runner=lambda prompt: "Recap\n---SEED---\nQuestion?",
        sender=lambda *args: sends.append(args) or True,
    )

    assert recorded == 1
    assert [send[1] for send in sends] == ["Recap", "Question?"]
    row = rows()[0]
    assert row["posted"] is True
    assert row["seed_topic"] == "Question?"


def test_digest_idempotent_per_day(tmp_path, monkeypatch, conn):
    env(monkeypatch, tmp_path)
    now = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)
    digest.run(now=now, engine_runner=lambda *_: "SKIP", sender=lambda *args: True)

    recorded = digest.run(now=now, engine_runner=lambda *_: "SKIP", sender=lambda *args: True)

    assert recorded == 0
    assert len(rows()) == 1


def test_engine_skip_records_without_posting(tmp_path, monkeypatch, conn):
    env(monkeypatch, tmp_path)
    monkeypatch.setenv("ARGUS_ADVISOR_DIGEST_MIN_MESSAGES", "1")
    now = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)
    seed(int(datetime(2026, 6, 16, tzinfo=timezone.utc).timestamp()), 1)

    digest.run(now=now, engine_runner=lambda *_: "SKIP",
               sender=lambda *args: (_ for _ in ()).throw(AssertionError()))

    row = rows()[0]
    assert row["posted"] is False
    assert row["reason"] == "engine_skip"
