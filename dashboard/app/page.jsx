import Link from "next/link";
import { attentionAlerts, normalizeProjectFilter, normalizeRoleFilter, readDashboardState } from "./lib/data.js";

export const dynamic = "force-dynamic";

const STATUSES = {
  clear: "Clear",
  working: "Working",
  waiting: "Waiting",
  blocked: "Blocked",
};

const ROLE_FILTERS = [
  { value: "", label: "All" },
  { value: "manager", label: "PMs" },
  { value: "developer", label: "Developers" },
  { value: "qa", label: "QA" },
  { value: "senior", label: "Senior" },
];

function count(section, keys) {
  return keys.reduce((sum, key) => sum + Number(section?.[key] || 0), 0);
}

function total(section) {
  return Object.values(section || {}).reduce((sum, n) => sum + Number(n || 0), 0);
}

function opsStatus(ops, errors) {
  if (errors.length > 0) return { label: "DB read failed", tone: "blocked" };
  if (!ops.dbConfigured) return { label: "DB missing", tone: "waiting" };
  const failed = count(ops.sections.jobs, ["failed", "dead"]) + count(ops.sections.requests, ["failed"]);
  if (failed > 0) return { label: "Attention", tone: "blocked" };
  return { label: "Live", tone: "clear" };
}

function shortTime(value) {
  if (!value) return "no signal";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("en", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function prNumber(url) {
  const m = String(url).match(/\/pull\/(\d+)/);
  return m ? `#${m[1]}` : "PR";
}

function severityClass(severity) {
  if (severity === "critical") return "tone-critical";
  if (severity === "error") return "tone-blocked";
  if (severity === "warn") return "tone-waiting";
  return "tone-muted";
}

function nodePosition(index, totalNodes) {
  const top = Math.min(5, Math.max(1, Math.ceil(totalNodes / 4)));
  const bottom = Math.min(5, Math.max(0, Math.floor(totalNodes / 4)));
  const remaining = Math.max(0, totalNodes - top - bottom);
  const right = Math.ceil(remaining / 2);
  const left = remaining - right;
  const spread = (slot, slots, start, end) => {
    if (slots <= 1) return (start + end) / 2;
    return start + (slot * (end - start)) / (slots - 1);
  };

  if (index < top) {
    return {
      "--x": `${spread(index, top, 13, 87)}%`,
      "--y": "11%",
    };
  }
  if (index < top + right) {
    const slot = index - top;
    return {
      "--x": "89%",
      "--y": `${spread(slot, right, 20, 80)}%`,
    };
  }
  if (index < top + right + bottom) {
    const slot = index - top - right;
    return {
      "--x": `${spread(slot, bottom, 87, 13)}%`,
      "--y": "89%",
    };
  }
  const slot = index - top - right - bottom;
  return {
    "--x": "11%",
    "--y": `${spread(slot, left, 80, 20)}%`,
  };
}

function hrefFor(params = {}) {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value) qs.set(key, value);
  }
  const query = qs.toString();
  return query ? `/?${query}` : "/";
}

function workerHref(agent, filter) {
  return hrefFor({ role: filter.role, project: filter.project, agent: agent.id });
}

function WorkerNode({ agent, filter, index, totalNodes, selected }) {
  const pending = agent.pendingJobs + agent.waitingActions + agent.failedJobs + agent.failedActions + agent.runOutages;
  return (
    <Link
      aria-label={`${agent.label}, ${STATUSES[agent.status] || agent.status}, ${pending} pending`}
      className={`worker-node ${agent.status} ${selected ? "selected" : ""}`}
      href={workerHref(agent, filter)}
      style={nodePosition(index, totalNodes)}
    >
      <span className="worker-glyph">{agent.glyph}</span>
      <span className="worker-copy">
        <strong>{agent.team}</strong>
        <span>{agent.roleLabel}</span>
      </span>
      <span className="worker-badge" aria-label={`${pending} pending items`}>
        {pending}
      </span>
    </Link>
  );
}

function FilterBar({ filter, projects }) {
  return (
    <section className="filter-deck" aria-label="Worker filters">
      <div className="filter-group">
        <span className="filter-label">Role</span>
        <div className="filter-chips">
          {ROLE_FILTERS.map((item) => (
            <Link
              className={`filter-chip ${filter.role === item.value ? "selected" : ""}`}
              href={hrefFor({ role: item.value, project: filter.project })}
              key={item.value || "all"}
            >
              {item.label}
            </Link>
          ))}
        </div>
      </div>

      <form className="filter-group project-filter" action="/" method="get">
        {filter.role ? <input type="hidden" name="role" value={filter.role} /> : null}
        <label className="filter-label" htmlFor="project-filter">Project</label>
        <select id="project-filter" name="project" defaultValue={filter.project}>
          <option value="">All projects</option>
          {projects.map((project) => (
            <option key={project} value={project}>{project}</option>
          ))}
        </select>
        <button type="submit">Apply</button>
        {filter.project ? (
          <Link className="filter-chip" href={hrefFor({ role: filter.role })}>Clear</Link>
        ) : null}
      </form>
    </section>
  );
}

function Metric({ label, value, detail, tone = "" }) {
  return (
    <div className={`metric ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function DetailRow({ item }) {
  return (
    <li className={`detail-row ${item.status}`}>
      <span className="detail-kind">{item.kind}</span>
      <div>
        <strong>{item.status || "unknown"}</strong>
        <p>{item.detail || "No detail recorded."}</p>
      </div>
      <time>{shortTime(item.at)}</time>
    </li>
  );
}

function ProposalRow({ proposal }) {
  return (
    <li className="signal-row">
      <span className="signal-source">{proposal.project || "project"}</span>
      <div>
        <strong>{proposal.summary || proposal.title || proposal.fingerprint || proposal.status || "Proposed fix"}</strong>
        <p>{proposal.senior || proposal.qa || proposal.requestStatus || "No review note recorded."}</p>
      </div>
      {proposal.pr ? (
        <a href={proposal.pr} target="_blank" rel="noreferrer">
          draft {prNumber(proposal.pr)}
        </a>
      ) : (
        <span className="signal-chip">{proposal.change === false ? "needs human" : proposal.status || "pending"}</span>
      )}
    </li>
  );
}

function AlertRow({ alert }) {
  return (
    <li className="signal-row">
      <span className={`signal-source ${severityClass(alert.severity)}`}>{alert.severity}</span>
      <div>
        <strong>{alert.project || "general"}</strong>
        <p>{alert.message}</p>
      </div>
      <time>{shortTime(alert.ts)}</time>
    </li>
  );
}

export default async function ControlRoom({ searchParams }) {
  const params = await searchParams;
  const requestedAgent = typeof params?.agent === "string" ? params.agent : "";
  const filter = {
    role: normalizeRoleFilter(typeof params?.role === "string" ? params.role : ""),
    project: normalizeProjectFilter(typeof params?.project === "string" ? params.project : ""),
  };
  const state = await readDashboardState({ selectedAgentId: requestedAgent, agentFilter: filter });
  const alerts = attentionAlerts(state.alerts);
  const status = opsStatus(state.ops, state.errors);
  const selected = state.selectedAgent;
  const selectedDetail = state.agentDetails.items;
  const projects = Array.from(new Set(state.allAgents.map((agent) => agent.team))).sort((a, b) => a.localeCompare(b));
  const activeWork = count(state.ops.sections.jobs, ["pending", "claimed", "running"]);
  const activeRequests = count(state.ops.sections.requests, ["open", "awaiting_approval"]);
  const pendingActions = count(state.ops.sections.actions, ["proposed", "awaiting_approval", "approved", "held"]);

  return (
    <div className="control-room">
      <section className="command-top">
        <div>
          <p className="system-label">Argus live command</p>
          <h2>Agent operating field</h2>
        </div>
        <div className="top-actions">
          <span className={`system-pill ${status.tone}`}>{status.label}</span>
          <Link href="/details">Open full log</Link>
        </div>
      </section>

      <FilterBar filter={filter} projects={projects} />

      <section className="live-grid">
        <div className="orbital-stage" aria-label="Argus workers">
          <div className="scan-ring ring-one" />
          <div className="scan-ring ring-two" />
          <div className="scan-sweep" />
          <div className="argus-core">
            <img src="/argus-icon.svg" alt="Argus" width="96" height="96" />
            <strong>Argus core</strong>
            <span>{state.agents.length} / {state.allAgentCount} workers shown</span>
          </div>
          {state.agents.map((agent, index) => (
            <WorkerNode
              agent={agent}
              filter={filter}
              index={index}
              key={agent.id}
              selected={agent.id === selected?.id}
              totalNodes={state.agents.length}
            />
          ))}
        </div>

        <aside className="agent-panel" aria-label="Selected worker">
          {selected ? (
            <>
              <div className="agent-heading">
                <span className={`agent-status ${selected.status}`}>{STATUSES[selected.status] || selected.status}</span>
                <h3>{selected.team}</h3>
                <p>{selected.roleLabel} worker</p>
              </div>
              <div className="agent-metrics">
                <Metric label="Jobs" value={selected.totalJobs} detail={`${selected.pendingJobs} pending`} tone={selected.pendingJobs ? "waiting" : ""} />
                <Metric label="Runs" value={total(selected.runs)} detail={`${selected.runOutages} outages`} tone={selected.runOutages ? "blocked" : ""} />
                <Metric label="Actions" value={total(selected.actions)} detail={`${selected.waitingActions} pending`} tone={selected.waitingActions ? "waiting" : ""} />
              </div>
              <div className="detail-head">
                <span>Last signal</span>
                <strong>{shortTime(selected.lastAt)}</strong>
              </div>
              {selectedDetail.length > 0 ? (
                <ul className="detail-list">
                  {selectedDetail.map((item, index) => (
                    <DetailRow item={item} key={`${item.kind}-${item.at}-${index}`} />
                  ))}
                </ul>
              ) : (
                <div className="empty-state">No timeline rows for this worker yet.</div>
              )}
            </>
          ) : (
            <div className="empty-state">No workers found. Start Argus workers or configure `ARGUS_DB_DSN`.</div>
          )}
        </aside>
      </section>

      <section className="mission-metrics" aria-label="Operational summary">
        <Metric label="Requests" value={total(state.ops.sections.requests)} detail={`${activeRequests} active`} />
        <Metric label="Jobs" value={total(state.ops.sections.jobs)} detail={`${activeWork} queued or running`} tone={activeWork ? "waiting" : ""} />
        <Metric label="Actions" value={total(state.ops.sections.actions)} detail={`${pendingActions} pending`} tone={pendingActions ? "waiting" : ""} />
        <Metric label="Alerts" value={state.alerts.length} detail={`${alerts.length} need attention`} tone={alerts.length ? "blocked" : ""} />
      </section>

      {state.errors.length > 0 ? (
        <section className="error-list" aria-label="Dashboard errors">
          {state.errors.map((err) => (
            <div className="error-panel" key={err.source}>
              <strong>{err.source}</strong>
              <span>{err.code ? `${err.code}: ` : ""}{err.message}</span>
            </div>
          ))}
        </section>
      ) : null}

      <section className="signal-grid">
        <div className="signal-panel">
          <div className="panel-title">
            <h3>Proposed fixes</h3>
            <span>{state.proposals.length}</span>
          </div>
          {state.proposals.length > 0 ? (
            <ul className="signal-list">
              {state.proposals.slice(0, 5).map((proposal, index) => (
                <ProposalRow proposal={proposal} key={`${proposal.project}-${proposal.fingerprint}-${index}`} />
              ))}
            </ul>
          ) : (
            <div className="empty-state">No proposed fixes yet.</div>
          )}
        </div>

        <div className="signal-panel">
          <div className="panel-title">
            <h3>Attention feed</h3>
            <span>{alerts.length}</span>
          </div>
          {alerts.length > 0 ? (
            <ul className="signal-list">
              {alerts.slice(0, 5).map((alert, index) => (
                <AlertRow alert={alert} key={`${alert.fingerprint}-${index}`} />
              ))}
            </ul>
          ) : (
            <div className="empty-state">No warn, error, or critical alerts.</div>
          )}
        </div>
      </section>
    </div>
  );
}
