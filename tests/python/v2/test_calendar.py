from argus.v2 import calendar


def test_build_event_body_with_duration_and_guests():
    body = calendar.build_event_body({
        "title": "Call",
        "start": "2026-06-18T09:00:00+03:00",
        "duration_min": 45,
        "guests": "a@example.com,b@example.com",
        "tz": "Asia/Jerusalem",
    })

    assert body["summary"] == "Call"
    assert body["start"] == {
        "dateTime": "2026-06-18T09:00:00+03:00",
        "timeZone": "Asia/Jerusalem",
    }
    assert body["end"]["dateTime"].startswith("2026-06-18T09:45:00")
    assert body["attendees"] == [{"email": "a@example.com"}, {"email": "b@example.com"}]


def test_build_event_body_all_day_defaults_end():
    body = calendar.build_event_body({
        "title": "Offsite",
        "start": "2026-06-18",
        "all_day": True,
    })

    assert body["start"] == {"date": "2026-06-18"}
    assert body["end"] == {"date": "2026-06-19"}


def test_build_api_request_covers_verbs():
    cal = "owner@example.com"

    assert calendar.build_api_request("ping", {}, cal)["method"] == "GET"
    assert "/events/evt1" in calendar.build_api_request("get", {"id": "evt1"}, cal)["url"]
    assert calendar.build_api_request("create", {
        "title": "Call",
        "start": "2026-06-18T09:00:00+03:00",
    }, cal)["method"] == "POST"
    assert calendar.build_api_request("update", {"id": "evt1", "title": "New"}, cal)["method"] == "PATCH"
    assert calendar.build_api_request("delete", {"id": "evt1"}, cal)["method"] == "DELETE"
    assert "timeMin=" in calendar.build_api_request("list", {"days": 1}, cal, now_ms=0)["url"]


def test_build_api_request_validates_calendar_id():
    try:
        calendar.build_api_request("list", {}, "")
    except calendar.CalendarError as exc:
        assert exc.code == 2
        assert "calendar not configured" in exc.message
    else:
        raise AssertionError("expected CalendarError")


def test_validate_rejects_unknown_and_missing_args():
    for command, params, message in [
        ("nope", {}, "unknown command"),
        ("create", {"title": "x"}, "create needs --start"),
        ("get", {}, "get needs --id"),
    ]:
        try:
            calendar.build_api_request(command, params, "owner@example.com")
        except calendar.CalendarError as exc:
            assert message in exc.message
            assert exc.code == 2
        else:
            raise AssertionError("expected CalendarError")


def test_run_uses_access_token_and_renders_human(monkeypatch):
    monkeypatch.setenv("ARGUS_GCAL_ACCESS_TOKEN", "token")
    monkeypatch.setenv("ARGUS_GCAL_CALENDAR_ID", "owner@example.com")
    seen = {}

    class Response:
        status_code = 200

        def json(self):
            return {"summary": "Owner calendar", "id": "owner@example.com"}

    class Httpx:
        @staticmethod
        def request(method, url, headers, json=None, timeout=30):
            seen["method"] = method
            seen["url"] = url
            seen["headers"] = headers
            return Response()

    out = calendar.run("ping", {}, httpx_module=Httpx)

    assert out == "calendar ok: Owner calendar"
    assert seen["headers"]["authorization"] == "Bearer token"
    assert "/calendars/owner%40example.com" in seen["url"]


def test_run_delete_success(monkeypatch):
    monkeypatch.setenv("ARGUS_GCAL_ACCESS_TOKEN", "token")
    monkeypatch.setenv("ARGUS_GCAL_CALENDAR_ID", "owner@example.com")

    class Response:
        status_code = 204

        def json(self):
            raise AssertionError("delete should not need json")

    class Httpx:
        @staticmethod
        def request(method, url, headers, json=None, timeout=30):
            return Response()

    assert calendar.run("delete", {"id": "evt1"}, httpx_module=Httpx) == "Deleted."


def test_run_api_error(monkeypatch):
    monkeypatch.setenv("ARGUS_GCAL_ACCESS_TOKEN", "token")
    monkeypatch.setenv("ARGUS_GCAL_CALENDAR_ID", "owner@example.com")

    class Response:
        status_code = 403

        def json(self):
            return {"error": {"message": "forbidden"}}

    class Httpx:
        @staticmethod
        def request(method, url, headers, json=None, timeout=30):
            return Response()

    try:
        calendar.run("ping", {}, httpx_module=Httpx)
    except calendar.CalendarError as exc:
        assert "forbidden" in exc.message
    else:
        raise AssertionError("expected CalendarError")


def test_access_token_missing_key_reports_not_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("ARGUS_GCAL_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("ARGUS_GCAL_SA_KEY", str(tmp_path / "missing.json"))

    try:
        calendar.access_token(httpx_module=object())
    except calendar.CalendarError as exc:
        assert exc.code == 2
        assert "not readable" in exc.message
    else:
        raise AssertionError("expected CalendarError")


def test_run_returns_json_for_list(monkeypatch):
    monkeypatch.setenv("ARGUS_GCAL_ACCESS_TOKEN", "token")
    monkeypatch.setenv("ARGUS_GCAL_CALENDAR_ID", "owner@example.com")

    class Response:
        status_code = 200

        def json(self):
            return {"items": []}

    class Httpx:
        @staticmethod
        def request(method, url, headers, json=None, timeout=30):
            return Response()

    assert calendar.run("list", {"days": 1}, json_output=True, httpx_module=Httpx) == '{"items":[]}'


def test_render_human_list_and_map_event():
    text = calendar.render_human("list", {
        "items": [{
            "id": "evt1",
            "summary": "Call",
            "start": {"dateTime": "2026-06-18T09:00:00+03:00"},
            "end": {"dateTime": "2026-06-18T10:00:00+03:00"},
            "location": "Zoom",
            "attendees": [{"email": "a@example.com"}],
        }],
    })

    assert "1 event(s)" in text
    assert "with a@example.com" in text
