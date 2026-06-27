import pg from "pg";

const { Pool } = pg;

export function resolveDbDsn() {
  return process.env.ARGUS_DB_DSN || process.env.DATABASE_URL || "";
}

export async function readAlerts(options = {}) {
  const query = options.query || queryAlertRows;
  const rows = await query({
    limit: options.limit ?? 200,
    project: options.project,
    severity: options.severity,
    dbDsn: options.dbDsn,
  });
  return rows.map(normalizeAlert);
}

async function queryAlertRows({ limit, project, severity, dbDsn }) {
  const dsn = dbDsn || resolveDbDsn();
  if (!dsn) return [];
  const clauses = [];
  const params = [];
  if (project) {
    params.push(project);
    clauses.push(`project = $${params.length}`);
  }
  if (severity) {
    params.push(severity);
    clauses.push(`severity = $${params.length}`);
  }
  const max = Math.max(1, Math.min(Number.parseInt(String(limit), 10) || 200, 500));
  params.push(max);
  const where = clauses.length > 0 ? `WHERE ${clauses.join(" AND ")}` : "";
  const sql = `
    SELECT id, ts, severity, project, fingerprint, message, channel, payload
    FROM alerts
    ${where}
    ORDER BY ts DESC
    LIMIT $${params.length}
  `;
  const result = await pgPool(dsn).query(sql, params);
  return result.rows;
}

function pgPool(dsn) {
  const key = "__argusDashboardPgPools";
  globalThis[key] = globalThis[key] || new Map();
  if (!globalThis[key].has(dsn)) {
    globalThis[key].set(dsn, new Pool(pgPoolConfig(dsn)));
  }
  return globalThis[key].get(dsn);
}

export function pgPoolConfig(dsn) {
  if (dsn.includes("://")) return { connectionString: dsn };
  const parsed = {};
  for (const match of dsn.matchAll(/(\w+)=('[^']*'|"[^"]*"|\S+)/g)) {
    const key = match[1];
    const value = unquoteDsnValue(match[2]);
    if (key === "dbname") parsed.database = value;
    else if (key === "host") parsed.host = value;
    else if (key === "port") parsed.port = Number.parseInt(value, 10);
    else if (key === "user") parsed.user = value;
    else if (key === "password") parsed.password = value;
    else if (key === "sslmode" && value === "require") parsed.ssl = true;
  }
  return parsed;
}

function unquoteDsnValue(value) {
  if (
    (value.startsWith("'") && value.endsWith("'")) ||
    (value.startsWith('"') && value.endsWith('"'))
  ) {
    return value.slice(1, -1);
  }
  return value;
}

function normalizeAlert(row) {
  return {
    id: row.id ?? "",
    ts: row.ts instanceof Date ? row.ts.toISOString() : String(row.ts ?? ""),
    severity: row.severity ?? "info",
    project: row.project ?? "",
    fingerprint: row.fingerprint ?? "",
    message: row.message ?? "",
    channel: row.channel ?? "",
    payload: row.payload ?? {},
  };
}

// attentionAlerts: only warn/error/critical, most recent first (ts descending).
export function attentionAlerts(alerts) {
  const keep = new Set(["warn", "error", "critical"]);
  return alerts
    .filter((a) => keep.has(a.severity))
    .slice()
    .sort((a, b) => String(b.ts).localeCompare(String(a.ts)));
}

export async function readProposals(options = {}) {
  const query = options.query || queryProposalRows;
  const rows = await query({
    limit: options.limit ?? 100,
    dbDsn: options.dbDsn,
  });
  return rows.map(normalizeProposal);
}

async function queryProposalRows({ limit, dbDsn }) {
  const dsn = dbDsn || resolveDbDsn();
  if (!dsn) return [];
  const max = Math.max(1, Math.min(Number.parseInt(String(limit), 10) || 100, 500));
  const sql = `
    SELECT
      a.id,
      a.team_id,
      a.status,
      a.provider_ref,
      a.payload,
      a.created_at,
      r.fingerprint,
      r.status AS request_status
    FROM actions a
    LEFT JOIN requests r ON r.id = a.request_id
    WHERE a.type = 'open_pr'
    ORDER BY a.created_at DESC
    LIMIT $1
  `;
  const result = await pgPool(dsn).query(sql, [max]);
  return result.rows;
}

function normalizeProposal(row) {
  const payload = row.payload && typeof row.payload === "object" ? row.payload : {};
  return {
    project: row.team_id ?? "",
    fingerprint: row.fingerprint ?? "",
    status: row.status ?? "",
    requestStatus: row.request_status ?? "",
    change: true,
    pr: row.provider_ref ?? "",
    patch: "",
    senior: payload.risk_summary ?? "",
    qa: payload.checks ?? "",
    title: payload.title ?? "",
    summary: payload.summary_short ?? "",
    ts: row.created_at instanceof Date ? row.created_at.toISOString() : String(row.created_at ?? ""),
  };
}
