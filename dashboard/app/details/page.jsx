import Link from "next/link";
import { readAlerts, readProposals } from "../lib/data.js";

export const dynamic = "force-dynamic";

function sevClass(severity) {
  if (severity === "critical") return "sev sev-critical";
  if (severity === "error") return "sev sev-error";
  if (severity === "warn") return "sev sev-warn";
  return "sev sev-info";
}

export default async function Details() {
  const alerts = await readAlerts();
  const proposals = await readProposals({ limit: 200 });

  return (
    <div>
      <div className="nav-links">
        <Link href="/">Back to the attention feed</Link>
      </div>

      <h2>All alerts ({alerts.length})</h2>
      {alerts.length === 0 ? (
        <div className="all-quiet">No alerts recorded yet.</div>
      ) : (
        <ul className="alert-list">
          {alerts.map((a, i) => (
            <li className="alert-row" key={a.fingerprint ? a.fingerprint + i : i}>
              <div className="meta">
                <span className={sevClass(a.severity)}>{a.severity}</span>
                <span className="project">{a.project}</span>
                <span className="ts">{a.ts}</span>
                <span className="channel">{a.channel}</span>
              </div>
              <div className="message">{a.message}</div>
            </li>
          ))}
        </ul>
      )}

      <h2>Proposed fixes ({proposals.length})</h2>
      {proposals.length === 0 ? (
        <div className="all-quiet">No proposed fixes yet.</div>
      ) : (
        <ul className="alert-list">
          {proposals.map((p, i) => (
            <li className="alert-row" key={p.project + p.fingerprint + i}>
              <div className="meta">
                <span className="project">{p.project}</span>
                <span className="ts">{p.ts}</span>
                <span className="channel">{p.status}</span>
              </div>
              <div className="message">
                {p.pr ? (
                  <a href={p.pr} target="_blank" rel="noreferrer">{p.pr}</a>
                ) : (
                  p.summary || p.title || p.fingerprint || "(pending)"
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
