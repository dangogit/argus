from types import SimpleNamespace

from argus.v2.connectors.fly import FlyConnector


RAW = [
    {"id": "m1", "state": "started"},
    {"id": "m2", "state": "stopped"},
]


def test_fly_emits_unhealthy_machine_once():
    signals, cursor = FlyConnector.parse(RAW, {}, app="argus")

    assert [signal.fingerprint for signal in signals] == ["fly-argus-m2-stopped"]
    assert cursor == {"active": ["fly-argus-m2-stopped"]}

    again, cursor2 = FlyConnector.parse(RAW, cursor, app="argus")

    assert again == []
    assert cursor2 == cursor


def test_fly_recovers_by_clearing_active_cursor():
    _, cursor = FlyConnector.parse(RAW, {}, app="argus")

    signals, cursor2 = FlyConnector.parse([{"id": "m2", "state": "started"}], cursor, app="argus")

    assert signals == []
    assert cursor2 == {"active": []}


def test_fly_poll_uses_configured_app():
    class FakeFly(FlyConnector):
        def fetch(self, source, state):
            return [{"id": "m1", "status": "failed"}]

    source = SimpleNamespace(name="fly-prod", config={"app": "prod"}, secret="token")

    signals, _ = FakeFly().poll(source, {})

    assert signals[0].fingerprint == "fly-prod-m1-failed"
