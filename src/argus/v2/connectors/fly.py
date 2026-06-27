"""Fly.io Machines connector."""
from __future__ import annotations

from argus.v2.connectors.base import Signal, register


HEALTHY_STATES = {"started", "starting"}


@register
class FlyConnector:
    type = "fly"

    @staticmethod
    def parse(raw, state: dict, *, app: str):
        machines = raw.get("machines", raw) if isinstance(raw, dict) else raw
        if not isinstance(machines, list):
            machines = []
        previous = set(state.get("active") or [])
        active: set[str] = set()
        signals: list[Signal] = []
        for machine in machines:
            if not isinstance(machine, dict):
                continue
            machine_id = str(machine.get("id") or machine.get("name") or "")
            if not machine_id:
                continue
            status = str(machine.get("state") or machine.get("status") or "unknown")
            if status in HEALTHY_STATES:
                continue
            fingerprint = f"fly-{app}-{machine_id}-{status}"
            active.add(fingerprint)
            if fingerprint in previous:
                continue
            signals.append(Signal(
                fingerprint=fingerprint,
                payload={
                    "source": "fly",
                    "app": app,
                    "machine_id": machine_id,
                    "status": status,
                    "message": f"Fly app {app} machine {machine_id} is {status}",
                    "kind": "incident",
                    "severity": "error",
                },
            ))
        return signals, {"active": sorted(active)}

    def fetch(self, source, state: dict):  # pragma: no cover
        import httpx

        cfg = source.config or {}
        app = cfg["app"]
        base = str(cfg.get("base_url") or "https://api.machines.dev").rstrip("/")
        response = httpx.get(
            f"{base}/v1/apps/{app}/machines",
            headers={"Authorization": f"Bearer {source.secret}"},
            timeout=float(cfg.get("timeout", 20)),
        )
        response.raise_for_status()
        return response.json()

    def poll(self, source, state: dict):
        cfg = source.config or {}
        app = str(cfg.get("app") or source.name)
        return self.parse(self.fetch(source, state), state, app=app)
