import Link from "next/link";
import {
  readAlerts,
  attentionAlerts,
  readProposals,
} from "./lib/data.js";

// Always read fresh on each request: this is a live operational console.
export const dynamic = "force-dynamic";

function sevClass(severity) {
  if (severity === "critical") return "sev sev-critical";
  if (severity === "error") return "sev sev-error";
  if (severity === "warn") return "sev sev-warn";
  return "sev sev-info";
}

function prNumber(url) {
  const m = String(url).match(/\/pull\/(\d+)/);
  return m ? `#${m[1]}` : "PR";
}

export default async function AttentionFeed() {
  const alerts = attentionAlerts(await readAlerts());
  const proposals = await readProposals();

  return (
    <div>
      <div className="nav-links">
        <Link href="/details">See all alerts and proposed fixes</Link>
      </div>

      <h2>Proposed fixes</h2>
      {proposals.length === 0 ? (
        <div className="all-quiet">No proposed fixes yet.</div>
      ) : (
        <ul className="alert-list">
          {proposals.map((p, i) => (
            <li className="alert-row" key={p.project + p.fingerprint + i}>
              <div className="meta">
                <span className="project">{p.project}</span>
                <span className="ts">{p.fingerprint}</span>
                {p.change === false ? (
                  <span className="verdict">no change, needs human</span>
                ) : (
                  <>
                    {p.senior ? <span className="verdict">senior: {p.senior}</span> : null}
                    {p.qa ? <span className="verdict">qa: {p.qa}</span> : null}
                  </>
                )}
              </div>
              <div className="message">
                {p.change === false ? (
                  <span>no fix produced for this finding</span>
                ) : p.pr ? (
                  <a href={p.pr} target="_blank" rel="noreferrer">
                    draft {prNumber(p.pr)} {p.pr}
                  </a>
                ) : (
                  <span>{p.summary || p.title || p.status || "(pending)"}</span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      <h2>Attention feed</h2>
      {alerts.length === 0 ? (
        <div className="all-quiet">All quiet. No warn, error, or critical alerts.</div>
      ) : (
        <ul className="alert-list">
          {alerts.map((a, i) => (
            <li className="alert-row" key={a.fingerprint ? a.fingerprint + i : i}>
              <div className="meta">
                <span className={sevClass(a.severity)}>{a.severity}</span>
                <span className="project">{a.project}</span>
                <span className="ts">{a.ts}</span>
              </div>
              <div className="message">{a.message}</div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
