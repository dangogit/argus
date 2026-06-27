from argus.v2.connectors import base
import argus.v2.connectors  # trigger registration
from argus.v2.config.schema import SourceRef


def test_registry_has_fake():
    assert "fake" in base.REGISTRY


def test_fake_connector_emits_new_signals_and_advances_cursor():
    src = SourceRef(name="f", type="fake", scope="company", team="dev",
                    config={"signals": [{"fingerprint": "a", "payload": {"x": 1}},
                                        {"fingerprint": "b"}]})
    conn = base.REGISTRY["fake"]()
    signals, state = conn.poll(src, {})
    assert [s.fingerprint for s in signals] == ["a", "b"]
    assert signals[0].payload == {"x": 1}
    # Re-poll with the returned cursor: nothing new.
    signals2, _ = conn.poll(src, state)
    assert signals2 == []


def test_sourceref_config_defaults_empty():
    s = SourceRef(name="s", type="sentry", scope="company")
    assert s.config == {} and s.team is None
