"""Google Calendar service-account client for v2."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode


API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar"
KNOWN = {"ping", "list", "get", "create", "update", "delete"}


@dataclass(frozen=True)
class CalendarError(Exception):
    message: str
    code: int = 1

    def __str__(self) -> str:
        return self.message


def build_event_body(params: dict) -> dict:
    body: dict = {}
    if params.get("title"):
        body["summary"] = params["title"]
    if params.get("description") is not None:
        body["description"] = params["description"]
    if params.get("location") is not None:
        body["location"] = params["location"]
    guests = params.get("guests")
    if guests:
        if isinstance(guests, str):
            guests = [item.strip() for item in guests.split(",")]
        body["attendees"] = [{"email": item} for item in guests if item]
    if params.get("start"):
        all_day = bool(params.get("all_day"))
        if all_day:
            start = _date_only(params["start"])
            end = _date_only(params.get("end") or _add_day(start))
            body["start"] = {"date": start}
            body["end"] = {"date": end}
        else:
            tz = {"timeZone": params["tz"]} if params.get("tz") else {}
            end = params.get("end")
            if not end:
                minutes = int(params.get("duration_min") or params.get("durationMin") or 30)
                end = (datetime.fromisoformat(_iso(params["start"])) + timedelta(minutes=minutes)).isoformat()
            body["start"] = {"dateTime": params["start"], **tz}
            body["end"] = {"dateTime": end, **tz}
    elif params.get("end"):
        body["end"] = {"dateTime": params["end"], **({"timeZone": params["tz"]} if params.get("tz") else {})}
    return body


def build_api_request(command: str, params: dict, cal_id: str, *, now_ms: int | None = None) -> dict:
    validate(command, params, cal_id)
    cal = quote(cal_id, safe="")
    send_updates = "sendUpdates=all"
    if command == "ping":
        return {"method": "GET", "url": f"{API}/calendars/{cal}"}
    if command == "list":
        now_ms = now_ms or int(time.time() * 1000)
        time_min = _to_google_time(params.get("from") or datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat())
        days = max(1, int(params.get("days") or 7))
        time_max_raw = params.get("to") or (
            datetime.fromtimestamp(now_ms / 1000, timezone.utc) + timedelta(days=days)
        ).isoformat()
        query = urlencode({
            "timeMin": time_min,
            "timeMax": _to_google_time(time_max_raw),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "50",
        })
        return {"method": "GET", "url": f"{API}/calendars/{cal}/events?{query}"}
    if command == "get":
        return {"method": "GET", "url": f"{API}/calendars/{cal}/events/{quote(params['id'], safe='')}"}
    if command == "create":
        return {
            "method": "POST",
            "url": f"{API}/calendars/{cal}/events?{send_updates}",
            "body": build_event_body(params),
        }
    if command == "update":
        return {
            "method": "PATCH",
            "url": f"{API}/calendars/{cal}/events/{quote(params['id'], safe='')}?{send_updates}",
            "body": build_event_body(params),
        }
    if command == "delete":
        return {
            "method": "DELETE",
            "url": f"{API}/calendars/{cal}/events/{quote(params['id'], safe='')}?{send_updates}",
        }
    raise CalendarError(f"unknown command: {command}", 2)


def validate(command: str, params: dict, cal_id: str) -> None:
    if command not in KNOWN:
        raise CalendarError(f"unknown command: {command or '(none)'}", 2)
    if not cal_id:
        raise CalendarError("calendar not configured: set ARGUS_GCAL_CALENDAR_ID", 2)
    if command == "create":
        if not params.get("title"):
            raise CalendarError("create needs --title", 2)
        if not params.get("start"):
            raise CalendarError("create needs --start", 2)
    if command in {"get", "delete", "update"} and not params.get("id"):
        raise CalendarError(f"{command} needs --id", 2)


def run(command: str, params: dict, *, json_output: bool = False, httpx_module=None) -> str:
    import httpx

    httpx_module = httpx_module or httpx
    cal_id = os.environ.get("ARGUS_GCAL_CALENDAR_ID", "")
    request = build_api_request(command, params, cal_id)
    token = access_token(httpx_module=httpx_module)
    response = httpx_module.request(
        request["method"],
        request["url"],
        headers={"authorization": f"Bearer {token}", **({"content-type": "application/json"} if request.get("body") else {})},
        json=request.get("body"),
        timeout=float(os.environ.get("ARGUS_GCAL_TIMEOUT", "30")),
    )
    if request["method"] == "DELETE" and response.status_code in {200, 204}:
        data = {"ok": True, "deleted": params["id"]}
    else:
        try:
            data = response.json()
        except Exception as exc:
            raise CalendarError(f"gcal: non-JSON response: {exc}") from exc
        if response.status_code >= 400 or data.get("error"):
            err = data.get("error")
            msg = err.get("message") if isinstance(err, dict) else str(err or response.status_code)
            raise CalendarError(f"gcal: {msg}")
    return json.dumps(data, separators=(",", ":")) if json_output else render_human(command, data)


def access_token(*, httpx_module) -> str:
    token = os.environ.get("ARGUS_GCAL_ACCESS_TOKEN")
    if token:
        return token
    key_path = Path(os.environ.get("ARGUS_GCAL_SA_KEY", str(Path.home() / ".config" / "argus" / "calendar-sa.json"))).expanduser()
    try:
        sa = json.loads(key_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CalendarError(f"calendar not configured: SA key not readable at {key_path}", 2) from exc
    now = int(time.time())
    token_uri = sa.get("token_uri") or "https://oauth2.googleapis.com/token"
    header = _b64url_json({"alg": "RS256", "typ": "JWT"})
    claim = _b64url_json({
        "iss": sa["client_email"],
        "scope": SCOPE,
        "aud": token_uri,
        "iat": now,
        "exp": now + 3600,
    })
    signed = f"{header}.{claim}"
    signature = _openssl_sign(signed.encode("utf-8"), sa["private_key"])
    jwt = f"{signed}.{_b64url(signature)}"
    response = httpx_module.post(
        token_uri,
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt},
        timeout=float(os.environ.get("ARGUS_GCAL_TIMEOUT", "30")),
    )
    data = response.json()
    if not data.get("access_token"):
        raise CalendarError(f"token exchange failed: {json.dumps(data)[:200]}")
    return str(data["access_token"])


def render_human(command: str, data: dict) -> str:
    if command == "ping":
        return f"calendar ok: {data.get('summary') or data.get('id') or '(calendar)'}"
    if command == "delete":
        return "Deleted."
    if command == "list":
        events = [map_event(item) for item in data.get("items", [])]
        if not events:
            return "No events in that window."
        return f"{len(events)} event(s):\n" + "\n".join(f"- {_fmt_event(event)}" for event in events)
    prefix = "Created:\n" if command == "create" else ""
    return prefix + _fmt_event(map_event(data))


def map_event(event: dict) -> dict:
    start = event.get("start") or {}
    end = event.get("end") or {}
    all_day = bool(start.get("date") and not start.get("dateTime"))
    return {
        "id": event.get("id") or "",
        "title": event.get("summary") or "",
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "allDay": all_day,
        "location": event.get("location") or "",
        "description": event.get("description") or "",
        "guests": [item.get("email") for item in event.get("attendees", []) if item.get("email")],
        "htmlLink": event.get("htmlLink") or "",
    }


def _fmt_event(event: dict) -> str:
    when = f"{event['start']} (all day)" if event["allDay"] else f"{event['start']} -> {event['end']}"
    extra = []
    if event["location"]:
        extra.append(f"@ {event['location']}")
    if event["guests"]:
        extra.append(f"with {', '.join(event['guests'])}")
    suffix = f"  {'  '.join(extra)}" if extra else ""
    return f"{when}  {event['title']}{suffix}\n  id: {event['id']}"


def _openssl_sign(payload: bytes, private_key: str) -> bytes:
    if not shutil_which("openssl"):
        raise CalendarError("openssl not found for service-account signing")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as key_file:
        key_file.write(private_key)
        key_file.flush()
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_file.name],
            input=payload,
            capture_output=True,
            check=True,
        )
    return proc.stdout


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def _b64url_json(value: dict) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _date_only(value: str) -> str:
    return str(value)[:10]


def _add_day(ymd: str) -> str:
    return (datetime.fromisoformat(f"{ymd}T00:00:00+00:00") + timedelta(days=1)).date().isoformat()


def _iso(value: str) -> str:
    raw = str(value)
    return raw[:-1] + "+00:00" if raw.endswith("Z") else raw


def _to_google_time(value: str) -> str:
    return datetime.fromisoformat(_iso(value)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
