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

export async function readDashboardState(options = {}) {
  const errors = [];
  const [alerts, proposals, ops, agents] = await Promise.all([
    capture("alerts", () => readAlerts({ ...options, query: options.alertsQuery })),
    capture("proposed fixes", () => readProposals({ ...options, query: options.proposalsQuery })),
    capture("operations", () => readOpsStats({ ...options, query: options.opsQuery })),
    capture("workers", () => readAgents({ ...options, query: options.agentsQuery })),
  ]);
  for (const result of [alerts, proposals, ops, agents]) {
    if (result.error) errors.push(result.error);
  }
  const allWorkers = agents.value || [];
  const workerList = filterAgents(allWorkers, options.agentFilter);
  const requestedAgentId = options.selectedAgentId || "";
  const selectedAgentId = workerList.some((agent) => agent.id === requestedAgentId)
    ? requestedAgentId
    : workerList[0]?.id || "";
  const details = selectedAgentId
    ? await capture("worker detail", () =>
        readAgentDetails({ ...options, agentId: selectedAgentId, query: options.agentDetailsQuery })
      )
    : { value: emptyAgentDetails("") };
  if (details.error) errors.push(details.error);
  return {
    alerts: alerts.value || [],
    proposals: proposals.value || [],
    ops: ops.value || emptyOpsStats(options.dbDsn),
    agents: workerList,
    allAgents: allWorkers,
    allAgentCount: allWorkers.length,
    selectedAgentId,
    selectedAgent: workerList.find((agent) => agent.id === selectedAgentId) || workerList[0] || null,
    agentDetails: details.value || emptyAgentDetails(selectedAgentId),
    errors,
  };
}

async function capture(source, fn) {
  try {
    return { value: await fn() };
  } catch (err) {
    return { error: dashboardError(source, err) };
  }
}

function dashboardError(source, err) {
  return {
    source,
    message: err?.message || String(err),
    code: err?.code || err?.name || "",
  };
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

export async function readOpsStats(options = {}) {
  const query = options.query || queryOpsRows;
  const rows = await query({ dbDsn: options.dbDsn });
  return normalizeOpsStats(rows, options.dbDsn);
}

async function queryOpsRows({ dbDsn }) {
  const dsn = dbDsn || resolveDbDsn();
  if (!dsn) return [];
  const sql = `
    SELECT 'events' AS section, status, count(*)::int AS count FROM events GROUP BY status
    UNION ALL
    SELECT 'requests' AS section, status, count(*)::int AS count FROM requests GROUP BY status
    UNION ALL
    SELECT 'jobs' AS section, status, count(*)::int AS count FROM jobs GROUP BY status
    UNION ALL
    SELECT 'runs' AS section, status, count(*)::int AS count FROM runs GROUP BY status
    UNION ALL
    SELECT 'actions' AS section, status, count(*)::int AS count FROM actions GROUP BY status
    ORDER BY section, status
  `;
  const result = await pgPool(dsn).query(sql);
  return result.rows;
}

export function normalizeOpsStats(rows, dbDsn) {
  const stats = emptyOpsStats(dbDsn);
  for (const row of rows || []) {
    const section = row.section;
    if (!stats.sections[section]) continue;
    stats.sections[section][row.status || "unknown"] = Number(row.count) || 0;
  }
  stats.dbConfigured = hasDbDsn(dbDsn);
  return stats;
}

export function emptyOpsStats(dbDsn) {
  return {
    dbConfigured: hasDbDsn(dbDsn),
    sections: {
      events: {},
      requests: {},
      jobs: {},
      runs: {},
      actions: {},
    },
  };
}

function hasDbDsn(dbDsn) {
  return dbDsn === undefined ? Boolean(resolveDbDsn()) : Boolean(dbDsn);
}

export async function readAgents(options = {}) {
  const query = options.query || queryAgentRows;
  const rows = await query({ dbDsn: options.dbDsn });
  return normalizeAgents(rows);
}

async function queryAgentRows({ dbDsn }) {
  const dsn = dbDsn || resolveDbDsn();
  if (!dsn) return [];
  const sql = `
    SELECT 'job' AS source, team_id, role, status, count(*)::int AS count, max(updated_at) AS last_at
    FROM jobs
    GROUP BY team_id, role, status
    UNION ALL
    SELECT 'run' AS source, j.team_id, r.role, r.status, count(*)::int AS count, max(coalesce(r.ended_at, r.started_at)) AS last_at
    FROM runs r
    JOIN jobs j ON j.id = r.job_id
    GROUP BY j.team_id, r.role, r.status
    UNION ALL
    SELECT 'action' AS source, a.team_id, coalesce(j.role, 'outbox') AS role, a.status, count(*)::int AS count, max(a.updated_at) AS last_at
    FROM actions a
    LEFT JOIN jobs j ON j.id = a.job_id
    GROUP BY a.team_id, coalesce(j.role, 'outbox'), a.status
  `;
  const result = await pgPool(dsn).query(sql);
  return result.rows;
}

export function normalizeAgents(rows) {
  const byId = new Map();
  for (const row of rows || []) {
    const team = row.team_id || "general";
    const role = row.role || "worker";
    const id = agentId(team, role);
    if (!byId.has(id)) {
      byId.set(id, {
        id,
        team,
        role,
        label: `${team} / ${role}`,
        roleLabel: roleLabel(role),
        glyph: roleGlyph(role),
        jobs: {},
        runs: {},
        actions: {},
        lastAt: "",
      });
    }
    const agent = byId.get(id);
    const bucket = row.source === "run" ? "runs" : row.source === "action" ? "actions" : "jobs";
    agent[bucket][row.status || "unknown"] = Number(row.count) || 0;
    const last = row.last_at instanceof Date ? row.last_at.toISOString() : String(row.last_at || "");
    if (last && (!agent.lastAt || last > agent.lastAt)) agent.lastAt = last;
  }
  return Array.from(byId.values())
    .map((agent) => {
      const pendingJobs = countStatuses(agent.jobs, ["pending", "claimed", "running"]);
      const failedJobs = countStatuses(agent.jobs, ["failed", "dead"]);
      const failedActions = countStatuses(agent.actions, ["failed", "rejected"]);
      const waitingActions = countStatuses(agent.actions, ["proposed", "awaiting_approval", "approved", "held"]);
      const runOutages = countStatuses(agent.runs, ["outage", "failed"]);
      const doneJobs = countStatuses(agent.jobs, ["done"]);
      const totalJobs = sumCounts(agent.jobs);
      return {
        ...agent,
        pendingJobs,
        failedJobs,
        failedActions,
        waitingActions,
        runOutages,
        doneJobs,
        totalJobs,
        status: agentStatus({ pendingJobs, failedJobs, failedActions, waitingActions, runOutages }),
        score: pendingJobs * 20 + waitingActions * 12 + (failedJobs + failedActions + runOutages) * 30 + totalJobs,
      };
    })
    .sort((a, b) => b.score - a.score || a.label.localeCompare(b.label));
}

export function filterAgents(agents, filter = {}) {
  const role = normalizeRoleFilter(filter.role);
  const project = normalizeProjectFilter(filter.project);
  return (agents || []).filter((agent) => {
    if (role && agent.role !== role) return false;
    if (project && agent.team !== project) return false;
    return true;
  });
}

export function normalizeRoleFilter(value) {
  const role = String(value || "").trim().toLowerCase();
  const roles = {
    manager: "manager",
    managers: "manager",
    pm: "manager",
    pms: "manager",
    developer: "developer",
    developers: "developer",
    dev: "developer",
    devs: "developer",
    qa: "qa",
    senior: "senior",
    seniors: "senior",
    seniorer: "senior",
    outbox: "outbox",
  };
  return roles[role] || "";
}

export function normalizeProjectFilter(value) {
  return String(value || "").trim();
}

export async function readAgentDetails(options = {}) {
  const query = options.query || queryAgentDetails;
  const [team, role] = parseAgentId(options.agentId);
  if (!team || !role) return emptyAgentDetails(options.agentId || "");
  const rows = await query({ dbDsn: options.dbDsn, team, role, limit: options.detailLimit ?? 8 });
  return normalizeAgentDetails(options.agentId, rows);
}

async function queryAgentDetails({ dbDsn, team, role, limit }) {
  const dsn = dbDsn || resolveDbDsn();
  if (!dsn) return [];
  const max = Math.max(1, Math.min(Number.parseInt(String(limit), 10) || 8, 20));
  const sql = `
    SELECT 'job' AS kind, j.status, j.updated_at AS at, j.role, j.team_id,
      left(coalesce(j.payload->>'text', j.payload::text, ''), 220) AS detail
    FROM jobs j
    WHERE j.team_id = $1 AND j.role = $2
    UNION ALL
    SELECT 'run' AS kind, r.status, coalesce(r.ended_at, r.started_at) AS at, r.role, j.team_id,
      left(coalesce(r.output, r.model, r.engine, ''), 220) AS detail
    FROM runs r
    JOIN jobs j ON j.id = r.job_id
    WHERE j.team_id = $1 AND r.role = $2
    UNION ALL
    SELECT 'action' AS kind, a.status, a.updated_at AS at, coalesce(j.role, 'outbox') AS role, a.team_id,
      left(coalesce(a.payload->>'text', a.payload->>'summary_short', a.provider_ref, a.payload::text, ''), 220) AS detail
    FROM actions a
    LEFT JOIN jobs j ON j.id = a.job_id
    WHERE a.team_id = $1 AND coalesce(j.role, 'outbox') = $2
    ORDER BY at DESC
    LIMIT $3
  `;
  const result = await pgPool(dsn).query(sql, [team, role, max]);
  return result.rows;
}

export function normalizeAgentDetails(agentIdValue, rows) {
  return {
    agentId: agentIdValue,
    items: (rows || []).map((row) => ({
      kind: row.kind || "",
      status: row.status || "",
      at: row.at instanceof Date ? row.at.toISOString() : String(row.at || ""),
      role: row.role || "",
      team: row.team_id || "",
      detail: cleanDetail(row.detail),
    })),
  };
}

export function emptyAgentDetails(agentIdValue) {
  return { agentId: agentIdValue, items: [] };
}

function agentId(team, role) {
  return `${encodeURIComponent(team)}:${encodeURIComponent(role)}`;
}

function parseAgentId(value) {
  const [team = "", role = ""] = String(value || "").split(":");
  return [decodeURIComponent(team), decodeURIComponent(role)];
}

function countStatuses(section, statuses) {
  return statuses.reduce((sum, status) => sum + Number(section?.[status] || 0), 0);
}

function sumCounts(section) {
  return Object.values(section || {}).reduce((sum, n) => sum + Number(n || 0), 0);
}

function agentStatus({ pendingJobs, failedJobs, failedActions, waitingActions, runOutages }) {
  if (failedJobs || failedActions || runOutages) return "blocked";
  if (pendingJobs) return "working";
  if (waitingActions) return "waiting";
  return "clear";
}

function roleLabel(role) {
  const labels = {
    manager: "Manager",
    developer: "Developer",
    qa: "QA",
    senior: "Senior",
    outbox: "Outbox",
  };
  return labels[role] || role;
}

function roleGlyph(role) {
  const glyphs = {
    manager: "PM",
    developer: "DEV",
    qa: "QA",
    senior: "SR",
    outbox: "OUT",
  };
  return glyphs[role] || role.slice(0, 2).toUpperCase();
}

function cleanDetail(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .trim();
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
  const pr = row.provider_ref || payload.pr || "";
  const change = payload.change === false || payload.no_change === true ? false : Boolean(pr || payload.patch || payload.summary_short || payload.title);
  return {
    project: row.team_id ?? "",
    fingerprint: row.fingerprint ?? "",
    status: row.status ?? "",
    requestStatus: row.request_status ?? "",
    change,
    pr,
    patch: payload.patch ?? "",
    senior: payload.risk_summary ?? "",
    qa: payload.checks ?? "",
    title: payload.title ?? "",
    summary: payload.summary_short ?? "",
    ts: row.created_at instanceof Date ? row.created_at.toISOString() : String(row.created_at ?? ""),
  };
}
