"""Branch-drift connector: both-direction thresholds, tiered-fingerprint
escalation/dedup, and registration. Pure parse() + stubbed-fetch poll()."""
from argus.v2.connectors.base import REGISTRY
from argus.v2.connectors.branch_drift import BranchDriftConnector as BD

_KW = dict(project="dev", base="main", head="staging")


def test_registered():
    import argus.v2.connectors  # noqa: F401
    assert REGISTRY.get("branch_drift") is BD


def test_emits_when_behind_over_threshold():
    sigs, _ = BD.parse({"ahead_by": 0, "behind_by": 3}, {},
                       behind_threshold=1, ahead_threshold=0, **_KW)
    assert len(sigs) == 1
    p = sigs[0].payload
    assert p["behind"] == 3 and p["ahead"] == 0 and "3 behind" in p["message"]


def test_no_emit_under_threshold():
    sigs, _ = BD.parse({"ahead_by": 2, "behind_by": 0}, {},
                       behind_threshold=1, ahead_threshold=10, **_KW)
    assert sigs == []                       # 0 behind, 2 ahead < ahead_threshold 10


def test_both_directions():
    sigs, _ = BD.parse({"ahead_by": 12, "behind_by": 0}, {},
                       behind_threshold=1, ahead_threshold=10, **_KW)
    assert len(sigs) == 1 and "12 ahead" in sigs[0].payload["message"]


def test_tiered_fingerprint_dedups_same_tier_escalates_higher():
    a, _ = BD.parse({"ahead_by": 0, "behind_by": 2}, {}, behind_threshold=1, ahead_threshold=0, **_KW)
    b, _ = BD.parse({"ahead_by": 0, "behind_by": 2}, {}, behind_threshold=1, ahead_threshold=0, **_KW)
    assert a[0].fingerprint == b[0].fingerprint          # same tier -> same fp (ingest dedups)
    c, _ = BD.parse({"ahead_by": 0, "behind_by": 15}, {}, behind_threshold=1, ahead_threshold=0, **_KW)
    assert c[0].fingerprint != a[0].fingerprint          # worse tier -> new fp -> re-triage


def test_fetch_fail_is_noop():
    assert BD.parse(None, {"x": 1}, behind_threshold=1, ahead_threshold=0, **_KW) == ([], {"x": 1})


def test_poll_wires_fetch_to_parse():
    source = type("S", (), {"config": {"github_repo": "o/r", "head": "staging",
                                       "behind_threshold": 1}, "team": "dev",
                            "name": "drift", "secret": None})()

    class Canned(BD):
        def fetch(self, source, state):
            return {"ahead_by": 0, "behind_by": 5}

    sigs, _ = Canned().poll(source, {})
    assert len(sigs) == 1 and sigs[0].payload["behind"] == 5
