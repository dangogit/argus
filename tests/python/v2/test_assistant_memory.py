from dataclasses import dataclass

from argus.v2.assistant import memory


@dataclass
class Result:
    text: str


def test_refresh_noops_without_history(conn):
    assert memory.refresh() is False
    assert memory.show() == ""


def test_refresh_writes_note_and_watermark(tmp_path, monkeypatch, conn):
    memory.append_history("owner", "ship v2")
    last_id = memory.append_history("argus", "working on it")
    contract = tmp_path / "memory-contract.md"
    contract.write_text("Prev:\n<<<PREV>>>\nConv:\n<<<CONV>>>\n", encoding="utf-8")
    seen = {}

    def fake_run_agent(engine, prompt):
        seen["engine"] = engine
        seen["prompt"] = prompt
        seen["tools"] = memory.os.environ.get("ARGUS_CLAUDE_TOOLS")
        seen["sandbox"] = memory.os.environ.get("ARGUS_CODEX_SANDBOX")
        return Result("Ongoing topics: v2\nRecent decisions: none\n")

    monkeypatch.setenv("ARGUS_ASSISTANT_MEMORY_ENGINE", "echo")
    monkeypatch.setattr(memory, "run_agent", fake_run_agent)

    assert memory.refresh(tmp_path, contract) is True
    assert "ship v2" in seen["prompt"]
    assert seen["engine"] == "echo"
    assert seen["tools"] == ""
    assert seen["sandbox"] == "read-only"
    with conn.cursor() as cur:
        cur.execute("SELECT watermark, content FROM assistant_memory WHERE name='default'")
        watermark, content = cur.fetchone()
    assert watermark == last_id
    assert "Ongoing topics: v2" in content


def test_refresh_skips_when_watermark_is_current(tmp_path, monkeypatch, conn):
    row_id = memory.append_history("owner", "x")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO assistant_memory (name, content, watermark)
            VALUES ('default', 'already current', %s)
            """,
            (row_id,),
        )
    conn.commit()
    monkeypatch.setattr(memory, "run_agent", lambda *_: (_ for _ in ()).throw(AssertionError()))

    assert memory.refresh(tmp_path, tmp_path / "missing.md") is False


def test_show_and_inject_clip_note(conn, monkeypatch):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO assistant_memory (name, content, watermark, updated_at)
            VALUES ('default', %s, 1, clock_timestamp())
            """,
            ("abcdef",),
        )
    conn.commit()
    monkeypatch.setenv("ARGUS_ASSISTANT_MEMORY_CHARS", "3")

    assert memory.show() == "abcdef"
    assert memory.inject().endswith("abc\n")
