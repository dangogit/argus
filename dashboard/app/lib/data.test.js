import { describe, it, expect } from "vitest";
import {
  readAlerts,
  readAgents,
  readAgentDetails,
  readDashboardState,
  readOpsStats,
  readProposals,
  attentionAlerts,
  filterAgents,
  normalizeRoleFilter,
  normalizeOpsStats,
  pgPoolConfig,
  resolveDbDsn,
} from "./data.js";

describe("resolveDbDsn", () => {
  it("uses ARGUS_DB_DSN before DATABASE_URL", () => {
    const prevArgus = process.env.ARGUS_DB_DSN;
    const prevDatabase = process.env.DATABASE_URL;
    process.env.ARGUS_DB_DSN = "postgres://argus";
    process.env.DATABASE_URL = "postgres://database";

    expect(resolveDbDsn()).toBe("postgres://argus");

    if (prevArgus === undefined) delete process.env.ARGUS_DB_DSN;
    else process.env.ARGUS_DB_DSN = prevArgus;
    if (prevDatabase === undefined) delete process.env.DATABASE_URL;
    else process.env.DATABASE_URL = prevDatabase;
  });
});

describe("pgPoolConfig", () => {
  it("converts libpq keyword DSNs for node-postgres", () => {
    expect(pgPoolConfig("host=127.0.0.1 port=5440 dbname=argus user=argus")).toEqual({
      host: "127.0.0.1",
      port: 5440,
      database: "argus",
      user: "argus",
    });
  });

  it("keeps URL DSNs as connection strings", () => {
    expect(pgPoolConfig("postgres://argus@127.0.0.1:5440/argus")).toEqual({
      connectionString: "postgres://argus@127.0.0.1:5440/argus",
    });
  });
});

describe("readAlerts", () => {
  it("returns an empty array when no DB DSN is configured", async () => {
    const prevArgus = process.env.ARGUS_DB_DSN;
    const prevDatabase = process.env.DATABASE_URL;
    delete process.env.ARGUS_DB_DSN;
    delete process.env.DATABASE_URL;

    expect(await readAlerts()).toEqual([]);

    if (prevArgus !== undefined) process.env.ARGUS_DB_DSN = prevArgus;
    if (prevDatabase !== undefined) process.env.DATABASE_URL = prevDatabase;
  });

  it("normalizes alert rows from Postgres", async () => {
    const alerts = await readAlerts({
      query: async ({ limit }) => [
        {
          id: "a1",
          ts: new Date("2026-06-05T06:00:00Z"),
          severity: "warn",
          project: "p",
          fingerprint: "f1",
          message: "m1",
          channel: "log",
          payload: { limit },
        },
        {
          id: "a2",
          ts: "2026-06-05T07:00:00Z",
          severity: "error",
          project: "p",
          fingerprint: "f2",
          message: "m2",
          channel: "whatsapp",
        },
      ],
    });

    expect(alerts).toHaveLength(2);
    expect(alerts[0].ts).toBe("2026-06-05T06:00:00.000Z");
    expect(alerts[0].fingerprint).toBe("f1");
    expect(alerts[1].severity).toBe("error");
  });
});

describe("attentionAlerts", () => {
  it("keeps only warn/error/critical, sorted by ts descending", () => {
    const alerts = [
      { ts: "2026-06-05T01:00:00Z", severity: "info", message: "a" },
      { ts: "2026-06-05T02:00:00Z", severity: "warn", message: "b" },
      { ts: "2026-06-05T05:00:00Z", severity: "critical", message: "c" },
      { ts: "2026-06-05T03:00:00Z", severity: "error", message: "d" },
    ];
    const out = attentionAlerts(alerts);
    expect(out.map((a) => a.severity)).toEqual(["critical", "error", "warn"]);
    expect(out.map((a) => a.message)).toEqual(["c", "d", "b"]);
  });

  it("returns an empty array when there is nothing to attend to", () => {
    expect(attentionAlerts([{ ts: "x", severity: "info" }])).toEqual([]);
    expect(attentionAlerts([])).toEqual([]);
  });
});

describe("readOpsStats", () => {
  it("normalizes operational counts by section and status", async () => {
    const stats = await readOpsStats({
      dbDsn: "postgres://argus",
      query: async () => [
        { section: "jobs", status: "pending", count: "2" },
        { section: "jobs", status: "failed", count: 1 },
        { section: "requests", status: "open", count: 3 },
        { section: "unknown", status: "x", count: 9 },
      ],
    });

    expect(stats.dbConfigured).toBe(true);
    expect(stats.sections.jobs).toEqual({ pending: 2, failed: 1 });
    expect(stats.sections.requests).toEqual({ open: 3 });
    expect(stats.sections.unknown).toBeUndefined();
  });

  it("returns empty stats when no DB DSN is configured", () => {
    expect(normalizeOpsStats([], "")).toMatchObject({
      dbConfigured: false,
      sections: { jobs: {}, requests: {} },
    });
  });
});

describe("readDashboardState", () => {
  it("captures DB errors instead of throwing", async () => {
    const state = await readDashboardState({
      dbDsn: "postgres://argus",
      alertsQuery: async () => {
        throw Object.assign(new Error("connection refused"), { code: "ECONNREFUSED" });
      },
      proposalsQuery: async () => [],
      opsQuery: async () => [{ section: "jobs", status: "pending", count: 1 }],
      agentsQuery: async () => [{ source: "job", team_id: "argus", role: "manager", status: "pending", count: 1, last_at: "2026-06-05T06:00:00Z" }],
      agentDetailsQuery: async () => [],
    });

    expect(state.alerts).toEqual([]);
    expect(state.proposals).toEqual([]);
    expect(state.ops.sections.jobs.pending).toBe(1);
    expect(state.errors).toEqual([
      { source: "alerts", message: "connection refused", code: "ECONNREFUSED" },
    ]);
    expect(state.agents[0]).toMatchObject({ team: "argus", role: "manager", pendingJobs: 1 });
  });

  it("selects first worker inside active filter", async () => {
    let detailArgs;
    const state = await readDashboardState({
      dbDsn: "postgres://argus",
      agentFilter: { role: "developer", project: "argus" },
      alertsQuery: async () => [],
      proposalsQuery: async () => [],
      opsQuery: async () => [],
      agentsQuery: async () => [
        { source: "job", team_id: "argus", role: "manager", status: "done", count: 1, last_at: "2026-06-05T05:00:00Z" },
        { source: "job", team_id: "argus", role: "developer", status: "pending", count: 1, last_at: "2026-06-05T06:00:00Z" },
      ],
      agentDetailsQuery: async (args) => {
        detailArgs = args;
        return [];
      },
    });

    expect(state.agents).toHaveLength(1);
    expect(state.selectedAgentId).toBe("argus:developer");
    expect(detailArgs).toMatchObject({ team: "argus", role: "developer" });
    expect(state.allAgentCount).toBe(2);
  });
});

describe("readAgents", () => {
  it("merges jobs, runs, and actions into clickable worker summaries", async () => {
    const agents = await readAgents({
      query: async () => [
        { source: "job", team_id: "argus", role: "developer", status: "pending", count: "2", last_at: "2026-06-05T06:00:00Z" },
        { source: "run", team_id: "argus", role: "developer", status: "ok", count: 3, last_at: "2026-06-05T07:00:00Z" },
        { source: "action", team_id: "argus", role: "developer", status: "failed", count: 1, last_at: "2026-06-05T08:00:00Z" },
      ],
    });

    expect(agents).toHaveLength(1);
    expect(agents[0]).toMatchObject({
      id: "argus:developer",
      roleLabel: "Developer",
      pendingJobs: 2,
      failedActions: 1,
      status: "blocked",
      lastAt: "2026-06-05T08:00:00Z",
    });
  });
});

describe("filterAgents", () => {
  it("filters by role alias and project", () => {
    const agents = [
      { team: "argus", role: "manager" },
      { team: "argus", role: "developer" },
      { team: "luma", role: "developer" },
      { team: "argus", role: "senior" },
    ];

    expect(normalizeRoleFilter("pms")).toBe("manager");
    expect(normalizeRoleFilter("seniorer")).toBe("senior");
    expect(filterAgents(agents, { role: "developers", project: "argus" })).toEqual([
      { team: "argus", role: "developer" },
    ]);
  });
});

describe("readAgentDetails", () => {
  it("normalizes selected worker timeline rows", async () => {
    const details = await readAgentDetails({
      agentId: "argus:manager",
      query: async ({ team, role }) => [
        {
          kind: "job",
          status: "done",
          at: new Date("2026-06-05T06:00:00Z"),
          role,
          team_id: team,
          detail: "  answered   status  ",
        },
      ],
    });

    expect(details).toEqual({
      agentId: "argus:manager",
      items: [
        {
          kind: "job",
          status: "done",
          at: "2026-06-05T06:00:00.000Z",
          role: "manager",
          team: "argus",
          detail: "answered status",
        },
      ],
    });
  });
});

describe("readProposals", () => {
  it("returns an empty array when no DB DSN is configured", async () => {
    const prevArgus = process.env.ARGUS_DB_DSN;
    const prevDatabase = process.env.DATABASE_URL;
    delete process.env.ARGUS_DB_DSN;
    delete process.env.DATABASE_URL;

    expect(await readProposals()).toEqual([]);

    if (prevArgus !== undefined) process.env.ARGUS_DB_DSN = prevArgus;
    if (prevDatabase !== undefined) process.env.DATABASE_URL = prevDatabase;
  });

  it("normalizes proposal rows from Postgres", async () => {
    const proposals = await readProposals({
      query: async ({ limit }) => [
        {
          id: "a1",
          team_id: "proj-a",
          status: "done",
          provider_ref: "https://github.com/o/r/pull/7",
          created_at: new Date("2026-06-05T06:00:00Z"),
          fingerprint: "fp-1",
          request_status: "done",
          payload: {
            title: "Fix bug",
            summary_short: "patched login",
            checks: `limit:${limit}`,
            risk_summary: "low",
          },
        },
      ],
    });

    expect(proposals).toHaveLength(1);
    expect(proposals[0]).toMatchObject({
      project: "proj-a",
      fingerprint: "fp-1",
      pr: "https://github.com/o/r/pull/7",
      change: true,
      senior: "low",
      qa: "limit:100",
      summary: "patched login",
      requestStatus: "done",
    });
  });

  it("preserves no-change proposals from payload", async () => {
    const proposals = await readProposals({
      query: async () => [
        {
          team_id: "proj-a",
          status: "done",
          provider_ref: "",
          created_at: "2026-06-05T06:00:00Z",
          fingerprint: "fp-1",
          request_status: "failed",
          payload: { change: false, summary_short: "needs human" },
        },
      ],
    });

    expect(proposals[0]).toMatchObject({
      change: false,
      summary: "needs human",
    });
  });
});
