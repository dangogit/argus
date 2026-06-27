import pytest

from argus.v2.support.apps_script import AppsScriptTransport, AppsScriptTransportError


def test_normalize_list_accepts_wrapped_and_camel_case():
    raw = {"emails": [
        {"threadId": "T1", "from": "u@example.com", "subject": "Help", "snippet": "stuck"},
        {"thread_id": "T2", "from": "v@example.com", "subject": "Billing", "snippet": "charged"},
    ]}

    rows = AppsScriptTransport.normalize_list(raw, 1)

    assert len(rows) == 1
    assert rows[0].thread_id == "T1"
    assert rows[0].sender == "u@example.com"


def test_transport_posts_form_encoded_key_and_action():
    # The deployed Apps Script reads e.parameter and checks params.key, so auth
    # and args must travel as form-encoded fields (data=), not a header/JSON body.
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class Httpx:
        @staticmethod
        def post(url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    transport = AppsScriptTransport(url="https://example.com/app", key="secret", timeout=3)
    transport._get_json(Httpx, "reply", {"threadId": "T1", "id": "T1", "body": "Hi"})

    url, kwargs = calls[0]
    assert url == "https://example.com/app"
    assert "headers" not in kwargs
    assert "json" not in kwargs
    assert kwargs["data"]["key"] == "secret"
    assert kwargs["data"]["action"] == "reply"
    assert kwargs["data"]["body"] == "Hi"
    assert kwargs["data"]["threadId"] == "T1"
    assert kwargs["follow_redirects"] is True


def test_transport_search_posts_query_and_normalizes_results(monkeypatch):
    import sys

    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"emails": [
                {"threadId": "T9", "from": "eesha@example.com",
                 "subject": "OpenClaw", "snippet": "billing details"}
            ]}

    class Httpx:
        @staticmethod
        def post(url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setitem(sys.modules, "httpx", Httpx)

    transport = AppsScriptTransport(url="https://example.com/app", key="secret", timeout=3)
    rows = transport.search("from:eesha@example.com", 5)

    assert rows[0].thread_id == "T9"
    data = calls[0][1]["data"]
    assert data["action"] == "search"
    assert data["q"] == "from:eesha@example.com"
    assert data["query"] == "from:eesha@example.com"
    assert data["maxResults"] == "5"


def test_transport_http_failure_is_sanitized():
    class Response:
        status_code = 404

    class HttpError(Exception):
        response = Response()

    class FailingResponse:
        def raise_for_status(self):
            raise HttpError("raw url should not leak")

    class Httpx:
        @staticmethod
        def post(_url, **_kwargs):
            return FailingResponse()

    transport = AppsScriptTransport(url="https://example.com/app", key="secret", timeout=3)

    with pytest.raises(AppsScriptTransportError) as exc:
        transport._get_json(Httpx, "list", {"maxResults": "1"})

    assert str(exc.value) == "apps-script list failed: HTTP 404"
