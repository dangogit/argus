import json
from pathlib import Path

from argus.v2.config import loader
from argus.v2.connectors import driver
from argus.v2.connectors.github import GitHubConnector

RAW = json.loads(
    (Path(__file__).parent / "fixtures" / "connectors" / "github_issues.json").read_text()
)


def test_parse_open_issues_only_and_keeps_body():
    signals, cursor = GitHubConnector.parse(RAW, {}, project="courses-clubs")

    assert {s.fingerprint for s in signals} == {
        "github-courses-clubs-12",
        "github-courses-clubs-15",
    }
    assert signals[0].payload["message"] == (
        "open issue #12: Login button fails\n\nClicking the button does nothing."
    )
    assert signals[0].payload["url"] == "https://github.com/example/app/issues/12"
    assert signals[1].payload["message"] == "open issue #15: Checkout copy is confusing"
    assert cursor["seen"] == ["github-courses-clubs-12", "github-courses-clubs-15"]


def test_parse_seen_cursor_dedups_open_issues():
    _, cursor = GitHubConnector.parse(RAW, {}, project="courses-clubs")
    again, new_cursor = GitHubConnector.parse(RAW, cursor, project="courses-clubs")

    assert again == []
    assert new_cursor == cursor


def test_driver_ingests_github_signals(conn, tmp_path, monkeypatch):
    class FakeGitHub(GitHubConnector):
        def fetch(self, source, state):
            return RAW

    monkeypatch.setitem(driver.REGISTRY, "github", FakeGitHub)
    cfg_file = tmp_path / "argus.yaml"
    cfg_file.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        "    - name: github-courses\n"
        "      type: github\n"
        "      scope: company\n"
        "      team: courses-clubs\n"
        "      config: { project: courses-clubs, repo: dangogit/courses-clubs }\n"
        "teams:\n"
        "  - name: courses-clubs\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
    )
    cfg = loader.load(cfg_file)

    assert driver.poll_once(conn, cfg) == 2
    assert driver.poll_once(conn, cfg) == 0


def _src(cfg):
    return type("S", (), {"config": cfg, "secret": "tok", "team": "p", "name": "g"})()


def test_fetch_no_labels_is_noop_no_network():
    # Open issues are not auto-work: with no `labels` configured the connector
    # ingests nothing (and never hits the network -- the guard returns first).
    assert GitHubConnector().fetch(_src({"repo": "o/r", "project": "p"}), {}) == []
    assert GitHubConnector().fetch(_src({"repo": "o/r", "labels": []}), {}) == []


def test_parse_still_filters_prs_and_closed():
    raw = [
        {"number": 1, "title": "real", "state": "open"},
        {"number": 2, "title": "a pr", "state": "open", "pull_request": {"url": "x"}},
        {"number": 3, "title": "closed", "state": "closed"},
    ]
    sigs, _ = GitHubConnector.parse(raw, {}, project="p")
    assert [s.payload["number"] for s in sigs] == [1]


def test_fetch_uses_shared_client_and_still_parses(monkeypatch):
    """Smoke test: fetch() now goes through connectors.client.fetch_json instead
    of a hand-rolled httpx.get call, but the request shape and the parsed result
    must be unchanged."""
    import httpx

    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        request = httpx.Request("GET", url)
        return httpx.Response(200, json=RAW, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)
    source = _src({"repo": "example/app", "labels": ["argus"], "project": "courses-clubs"})

    data = GitHubConnector().fetch(source, {})

    assert data == RAW
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://api.github.com/repos/example/app/issues"
    assert call["params"] == {"state": "open", "per_page": 20, "labels": "argus"}
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["timeout"] == 15.0

    signals, _ = GitHubConnector.parse(data, {}, project="courses-clubs")
    assert {s.fingerprint for s in signals} == {
        "github-courses-clubs-12",
        "github-courses-clubs-15",
    }
