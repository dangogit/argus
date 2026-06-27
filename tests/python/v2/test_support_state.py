from argus.v2.support import state


def test_register_draft_records_ready_state(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))

    draft = state.register_draft("luma", "T1", "u@example.com", "Help", "Reply", "apps-script")

    assert draft.path.name == draft.id
    assert state.has_ready_draft("luma", "T1") is True
    assert state.latest_action("luma", "T1") == "draft_ready"
    with conn.cursor() as cur:
        cur.execute("SELECT reply, transport FROM support_drafts WHERE id=%s", (draft.id,))
        assert cur.fetchone() == ("Reply", "apps-script")


def test_draft_list_get_and_status(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))

    draft = state.register_draft("luma", "T1", "u@example.com", "Help", "Reply", "apps-script")

    assert state.draft_get("luma", draft.id)["reply"] == "Reply"
    assert [row["id"] for row in state.draft_list("luma")] == [draft.id]
    state.draft_set_status("luma", draft.id, "sent")
    assert state.draft_get("luma", draft.id)["status"] == "sent"
    assert state.draft_list("luma") == []
    assert [row["id"] for row in state.draft_list("luma", status="sent")] == [draft.id]
