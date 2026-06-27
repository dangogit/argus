"""Assemble an agent's conversational context: last-24h raw messages + recent
daily summaries (memory of past days). `now` is passed in for testability."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class ContextBundle:
    recent_messages: list = field(default_factory=list)  # [(received_at, text)]
    summaries: list = field(default_factory=list)         # [(day, summary)]
    knowledge: list = field(default_factory=list)         # [{title, content}]

    def as_prompt(self) -> str:
        lines = []
        if self.knowledge:
            lines.append("KNOWLEDGE:")
            lines += [f"- {k['title']}: {k['content']}" for k in self.knowledge]
        if self.summaries:
            lines.append("RECENT DAYS:")
            lines += [f"- {day}: {s}" for day, s in self.summaries]
        if self.recent_messages:
            lines.append("LAST 24H:")
            lines += [f"- {text}" for _, text in self.recent_messages]
        return "\n".join(lines)


def assemble(conn, *, team_id, conversation_id, now: datetime,
             hours: int = 24, summary_days: int = 7, max_messages: int = 50,
             cfg=None, query: str = "") -> ContextBundle:
    since = now - timedelta(hours=hours)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT received_at, payload->>'text' FROM events
               WHERE team_id=%s AND kind='message' AND received_at >= %s
                 AND (%s::uuid IS NULL OR conversation_id = %s::uuid)
               ORDER BY received_at LIMIT %s""",
            (team_id, since, conversation_id, conversation_id, max_messages))
        msgs = [(r[0], r[1] or "") for r in cur.fetchall()]
        cur.execute(
            """SELECT day, summary FROM conversation_summaries
               WHERE team_id=%s AND (%s::uuid IS NULL OR conversation_id = %s::uuid)
               ORDER BY day DESC LIMIT %s""",
            (team_id, conversation_id, conversation_id, summary_days))
        summaries = [(r[0], r[1]) for r in cur.fetchall()]
    know = []
    if cfg is not None and query:
        try:
            from argus.v2.knowledge import store as kstore
            know = kstore.search(conn, cfg, team_id=team_id, query=query, k=3)
        except Exception:
            know = []   # knowledge is optional; never break context assembly
    return ContextBundle(recent_messages=msgs, summaries=list(reversed(summaries)),
                         knowledge=know)
