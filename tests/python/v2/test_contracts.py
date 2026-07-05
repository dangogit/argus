from argus.v2.roles import contracts


def test_parse_result_extracts_json_line():
    out = "did the thing\nARGUS_RESULT: {\"ready\": true, \"summary\": \"fixed\"}"
    assert contracts.parse_result(out) == {"ready": True, "summary": "fixed"}


def test_parse_result_absent_is_empty():
    assert contracts.parse_result("no marker here") == {}


def test_parse_result_bad_json_is_empty():
    assert contracts.parse_result("ARGUS_RESULT: {not json}") == {}


def test_qa_verdict_uses_test_exit_without_structured_verdict():
    assert contracts.qa_verdict({}, test_exit=0) == "pass"
    assert contracts.qa_verdict({}, test_exit=1) == "fail"
    assert contracts.qa_verdict({"verdict": "fail"}, test_exit=None) == "fail"


def test_qa_verdict_prefers_structured_verdict():
    assert contracts.qa_verdict({"verdict": "pass"}, test_exit=1) == "pass"
    assert contracts.qa_verdict({"verdict": "fail"}, test_exit=0) == "fail"


def test_qa_verdict_fails_postgres_environment_blocker():
    assert contracts.qa_verdict({
        "verdict": "pass",
        "qa_environment_blocker": "postgres-unavailable",
    }, test_exit=1) == "fail"


def test_senior_decision_defaults_changes_when_unclear():
    assert contracts.senior_decision({"decision": "approve"}) == "approve"
    assert contracts.senior_decision({}) == "changes"  # fail-safe: don't ship unreviewed


# C1: converse_decision unit tests
def test_converse_decision_answer():
    action, reply, task = contracts.converse_decision(
        {"action": "answer", "reply": "Here are the PRs.", "task": ""})
    assert action == "answer"
    assert reply == "Here are the PRs."
    assert task == ""


def test_converse_decision_dispatch():
    action, reply, task = contracts.converse_decision(
        {"action": "dispatch", "reply": "On it.", "task": "Fix the login bug."})
    assert action == "dispatch"
    assert reply == "On it."
    assert task == "Fix the login bug."


def test_converse_decision_ignore():
    action, reply, task = contracts.converse_decision(
        {"action": "ignore", "reply": "", "task": ""})
    assert action == "ignore"
    assert reply == ""
    assert task == ""


def test_converse_decision_missing_action_defaults_ignore():
    action, reply, task = contracts.converse_decision({})
    assert action == "ignore"
    assert reply == ""
    assert task == ""


def test_converse_decision_unknown_action_defaults_ignore():
    action, reply, task = contracts.converse_decision({"action": "blorp", "reply": "hi"})
    assert action == "ignore"
    assert reply == "hi"
    assert task == ""


def test_converse_decision_missing_reply_and_task_default_empty():
    action, reply, task = contracts.converse_decision({"action": "answer"})
    assert action == "answer"
    assert reply == ""
    assert task == ""
