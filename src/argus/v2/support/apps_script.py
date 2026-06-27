"""Apps Script support transport used by v2 support cycles."""
from __future__ import annotations

from dataclasses import dataclass


class AppsScriptTransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmailSummary:
    thread_id: str
    sender: str
    subject: str
    snippet: str


class AppsScriptTransport:
    def __init__(self, *, url: str, key: str, timeout: float = 30):
        self.url = url
        self.key = key
        self.timeout = timeout

    @staticmethod
    def normalize_list(raw, limit: int) -> list[EmailSummary]:
        items = raw.get("emails", raw) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        out: list[EmailSummary] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            thread_id = str(item.get("thread_id") or item.get("threadId") or "")
            if not thread_id:
                continue
            out.append(EmailSummary(
                thread_id=thread_id,
                sender=str(item.get("from") or ""),
                subject=str(item.get("subject") or ""),
                snippet=str(item.get("snippet") or ""),
            ))
        return out

    def list_unread(self, limit: int) -> list[EmailSummary]:  # pragma: no cover
        import httpx

        data = self._get_json(httpx, "list", {"maxResults": str(limit)})
        if isinstance(data, dict) and data.get("error"):
            raise AppsScriptTransportError(f"apps-script list failed: {data['error']}")
        return self.normalize_list(data, limit)

    def search(self, query: str, limit: int) -> list[EmailSummary]:  # pragma: no cover
        import httpx

        data = self._get_json(httpx, "search", {
            "q": query,
            "query": query,
            "maxResults": str(limit),
        })
        if isinstance(data, dict) and data.get("error"):
            raise AppsScriptTransportError(f"apps-script search failed: {data['error']}")
        return self.normalize_list(data, limit)

    def read(self, thread_id: str) -> str:  # pragma: no cover
        import httpx

        data = self._get_json(httpx, "read", self._ids(thread_id))
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            if "text" in data:
                return str(data["text"])
            if "body" in data:
                return str(data["body"])
            return _thread_to_text(data)
        return ""

    def mark_read(self, thread_id: str) -> None:  # pragma: no cover
        import httpx

        self._get_json(httpx, "markRead", self._ids(thread_id))

    def archive(self, thread_id: str) -> None:  # pragma: no cover
        import httpx

        self._get_json(httpx, "archive", self._ids(thread_id))

    def reply(self, thread_id: str, body: str) -> None:  # pragma: no cover
        import httpx

        params = self._ids(thread_id)
        params["body"] = body
        self._get_json(httpx, "reply", params)

    def _get_json(self, httpx, action: str, params: dict[str, str]):
        # The deployed Gmail Apps Script reads e.parameter and checks params.key,
        # so auth + args go as form-encoded fields, not a header or JSON body.
        # POST (not GET) keeps long reply bodies off the URL length limit.
        response = httpx.post(
            self.url,
            data={"key": self.key, "action": action, **params},
            timeout=self.timeout,
            follow_redirects=True,
        )
        try:
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {status}" if status else type(exc).__name__
            raise AppsScriptTransportError(
                f"apps-script {action} failed: {detail}") from exc

    @staticmethod
    def _ids(thread_id: str) -> dict[str, str]:
        return {"threadId": thread_id, "id": thread_id}


def _thread_to_text(data: dict) -> str:
    messages = data.get("messages")
    if not isinstance(messages, list):
        return str(data)
    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        sender = msg.get("from") or msg.get("sender") or ""
        body = msg.get("text") or msg.get("body") or msg.get("snippet") or ""
        parts.append(f"From: {sender}\n{body}".strip())
    return "\n\n".join(part for part in parts if part)
