# Architecture

Argus is a Postgres-backed Python runtime.

1. Ingress writes durable `events` from CLI, webhook receivers, channel
   adapters, and polling connectors.
2. The orchestrator opens or advances `requests`, claims jobs, and executes role
   pipelines.
3. The worker runs configured engines and records `runs`, `actions`, approvals,
   artifacts, and request status.
4. Domain modules write their own Postgres state: alerts, PM memory, retro,
   advisor, support, content, context, assistant memory, and connector cursors.
5. The dashboard reads Postgres directly.

Main modules:

| Module | Responsibility |
|---|---|
| `argus.v2.cli` | CLI surface |
| `argus.v2.db` | migrations and connections |
| `argus.v2.ingress` | events and media |
| `argus.v2.connectors` | polling signal sources |
| `argus.v2.channels` | inbound and outbound chat adapters |
| `argus.v2.orchestrator` | request routing and pipeline control |
| `argus.v2.worker` | role execution |
| `argus.v2.actions` | risk classification and action execution |
| `argus.v2.pm` | PM auto-fix, memory, scanning, pending PRs |
| `argus.v2.host` and `argus.v2.launchd` | host units and maintenance jobs |

No shell product path remains. Runtime state is Postgres first; run-root files
are used only for artifacts such as media, backups, and generated content.
