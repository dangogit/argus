import { describe, it, expect } from "vitest";
import {
  readAlerts,
  readProposals,
  attentionAlerts,
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
      senior: "low",
      qa: "limit:100",
      summary: "patched login",
      requestStatus: "done",
    });
  });
});
