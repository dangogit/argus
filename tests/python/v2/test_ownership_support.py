from __future__ import annotations

import hashlib

import pytest

from argus.v2.config import loader
from argus.v2.ownership import store
from argus.v2.ownership import support
from argus.v2.support.cycle import DraftDecision


@pytest.fixture()
def support_team(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPPORT_KEY", "test-key")
    path = tmp_path / "support-ownership.yaml"
    path.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        "    - { type: support_apps_script, name: luma-mail, team: luma, "
        "secret_ref: '${env:SUPPORT_KEY}', config: { url: 'https://support.test' } }\n"
        "teams:\n"
        "  - name: luma\n"
        "    autonomy:\n"
        "      actions: { support_reply: auto }\n"
        "    ownership:\n"
        "      enabled: true\n"
        "      support: { auto_send_low_risk: true, min_confidence: 0.92 }\n"
        "    roles: [ { name: support, kind: worker, prompt: p } ]\n"
        "    pipeline: { stages: [support] }\n",
        encoding="utf-8",
    )
    cfg = loader.load(path)
    return cfg, cfg.team("luma"), cfg.company.sources[0]


def _decision(**changes):
    values = {
        "reply": "Open Settings and choose Export.",
        "category": "how_to",
        "risk": "low",
        "confidence": 0.96,
    }
    values.update(changes)
    return DraftDecision(**values)


@pytest.mark.parametrize("category", [
    "billing", "refund", "account_access", "security", "privacy",
    "legal", "deletion", "charge_dispute", "password", "login",
    "payment", "charge", "account_ownership",
])
def test_high_risk_support_never_auto_sends(support_team, category):
    _cfg, team, _source = support_team

    decision = support.classify_for_auto_send(
        team, _decision(category=category, confidence=0.99), "How do I export?"
    )

    assert decision.allowed is False


def test_low_risk_known_answer_can_auto_send(support_team):
    _cfg, team, _source = support_team

    decision = support.classify_for_auto_send(
        team, _decision(), "How do I export my project?"
    )

    assert decision.allowed is True


@pytest.mark.parametrize("field,value", [
    ("raw_thread", "Please r%65fund this"),
    ("raw_thread", "I need a pass-word reset"),
    ("sender", "payment-team@example.com"),
    ("subject", "Account\u200b ownership"),
    ("reply", "Open the ｌｏｇｉｎ page"),
])
def test_sensitive_scan_blocks_encoded_unicode_and_punctuation(
        support_team, field, value):
    _cfg, team, _source = support_team
    decision = _decision(**({"reply": value} if field == "reply" else {}))
    kwargs = {"sender": "user@example.com", "subject": "Export"}
    raw_thread = "How do I export?"
    if field == "raw_thread":
        raw_thread = value
    elif field in kwargs:
        kwargs[field] = value

    result = support.classify_for_auto_send(
        team, decision, raw_thread, **kwargs
    )

    assert result.allowed is False


def test_safe_support_creates_one_obligation_and_one_canonical_action(
        conn, support_team):
    _cfg, team, source = support_team
    decision = _decision()
    kwargs = dict(
        team=team,
        source=source,
        thread_id="T-safe",
        sender="user@example.com",
        subject="Export",
        raw_thread="How do I export?",
        decision=decision,
    )

    first = support.open_or_update_obligation(conn, **kwargs)
    first_action, first_inserted = support.queue_reply_action(
        conn, team=team, source=source, obligation=first, decision=decision
    )
    second = support.open_or_update_obligation(conn, **kwargs)
    second_action, second_inserted = support.queue_reply_action(
        conn, team=team, source=source, obligation=second, decision=decision
    )

    reply_hash = hashlib.sha256(decision.reply.encode()).hexdigest()[:16]
    assert first.status == "working"
    assert second.id == first.id
    assert first_action == second_action
    assert first_inserted is True
    assert second_inserted is False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT type, risk, destination_ref, idempotency_key, payload "
            "FROM actions WHERE id=%s", (first_action,)
        )
        action = cur.fetchone()
    assert action[0:4] == (
        "support_reply",
        "personal_outward",
        "support:luma-mail",
        f"support_reply:luma:T-safe:{reply_hash}",
    )
    assert action[4]["obligation_id"] == str(first.id)
    assert "reply" not in action[4]


def test_reply_action_collision_blocks_obligation(conn, support_team):
    _cfg, team, source = support_team
    decision = _decision()
    obligation = support.open_or_update_obligation(
        conn, team=team, source=source, thread_id="T-collision",
        sender="u@example.com", subject="Export", raw_thread="Export?",
        decision=decision,
    )
    reply_hash = hashlib.sha256(decision.reply.encode()).hexdigest()[:16]
    key = f"support_reply:luma:T-collision:{reply_hash}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions "
            "(team_id,type,risk,destination_ref,idempotency_key,payload) "
            "VALUES ('other','support_reply','personal_outward',"
            "'support:other',%s,'{}'::jsonb)",
            (key,),
        )

    with pytest.raises(RuntimeError, match="idempotency collision"):
        support.queue_reply_action(
            conn, team=team, source=source, obligation=obligation,
            decision=decision,
        )

    assert store.get(conn, obligation.id).status == "blocked"
