# Plan: Slack-native staged "thinking" status (live progress)

## Goal
Instead of posting a sequence of separate chat messages, **edit one message in
place** through the lifecycle - the Claude-in-Slack feel:

```
👀 Got it - looking into this...     (posted on receipt)
        ↓ edit same message
🛠️ On it - working on a fix...        (pipeline opened)
        ↓ edit
🔍 Reviewing the change...            (qa / senior stage)
        ↓ edit
✅ PR ready: <url>                    (terminal)   |   the answer (converse)
```

This is the **richer half** of the chat-receipt work already on main (#12). #12
posts the receipt; this makes it a single, self-updating status line.

## Why it's cheap (mechanics already in place)
- `slack/telegram/discord .send()` already **return the posted message id**, which
  the executor stores as `actions.provider_ref`. So the status message is
  addressable for edits with zero new plumbing to capture the id.
- Stage transitions are discrete, existing code points (route_events,
  enqueue_stage, on_job_done/_approve_done/_fail, _handle_converse).
- Editable channels: **slack, telegram, discord**. whatsapp/email can't edit -
  they keep today's behavior (receipt + final message, no intermediate edits).

## Design

### 1. Channel edit seam
- Add optional `update(self, binding, message_id, text) -> str` to
  `slack.py` (`chat.update`), `telegram.py` (`editMessageText`),
  `discord.py` (`PATCH /channels/{id}/messages/{mid}`).
- Add `edit(cfg, destination_ref, message_id, text)` to `channels/send.py`,
  mirroring `deliver()`. Returns None when the resolved adapter has no `update`
  (non-editable channel) so callers can fall back.
- Protocol (`base.py`): `update` stays optional (duck-typed via `getattr`), so
  whatsapp/email/fake need no change.

### 2. One status message per user turn, keyed by event
- New action type **`status`**, idempotency key `status:<event_id>`
  (one inbound message = one event = one status lifecycle; resolvable from every
  downstream stage via `requests.event_id` / `job.event_id`).
- First drain: `send()` posts the line, stores message id in `provider_ref`.
- Each later stage calls `_set_status(conn, event_id, text)`:
  overwrite the action's payload text + re-arm it (`status='proposed'`).
- Executor: a `status` action **with** a `provider_ref` executes as an **edit**
  (`send.edit(... provider_ref ...)`), not a resend; without one, as the first
  send. "Latest text wins" - if stages advance between drains, only the newest
  line shows (no flicker, monotonic by sweep order).

### 3. Stage -> line map (pure helper `status_line`)
| Stage point | Line |
|---|---|
| receipt (route_events) | `👀 Got it - looking into this...` |
| pipeline opened (dispatch) | `🛠️ On it - working on a fix...` |
| qa / senior stage | `🔍 Reviewing the change...` |
| PR ready (`_approve_done`) | `✅ PR ready: <url>` |
| no-fix / blocked / failed | `⚠️ <short reason>` |
| converse answer | the answer text (status line becomes the reply) |

### 4. Hook points (all already have conn + event/conversation in scope)
- `reconcile.route_events`: replace `_emit_ack` -> post initial `status` (editable
  channels) / keep plain reply (non-editable).
- `pipeline.enqueue_stage`: set "working" / "reviewing" by stage role.
- `pipeline._approve_done | _no_fix_close | _fail`: terminal line.
- `pipeline._handle_converse` (answer): final edit = answer.

### 5. Config + fallback
- `notifications.show_progress: true` default; false -> behave exactly like #12
  (single receipt, no edits).
- Non-editable channel (whatsapp/email): `_set_status` posts the **terminal** line
  as a normal message, skips intermediate edits (no edit API, avoid spam).

## Risks / decisions
- **Re-arm of the `status` action** for repeated edits is the one subtle part:
  the executor's "provider_ref set => idempotent done" short-circuit must NOT
  apply to `status` (for status it means "edit instead"). Carve a clear branch.
- **Edit failure** (message deleted, token lost): fall back to a fresh send,
  update provider_ref. Transient httpx errors already retry via the executor
  savepoint path.
- **Ordering**: monotonic because the orchestrator sweeps sequentially; latest
  text wins is correct.

## Tasks
- [x] `channels/{slack,telegram,discord,fake}.py`: add `update()` (network seams pragma'd)
- [x] `channels/send.py`: add `edit()` + `channel_supports_edit()`
- [x] `actions/executor.py`: `status` action type -> first-send / subsequent-edit branch
- [x] `orchestrator/status.py` (new): `post_initial` / `set_status` / `stage_line`
- [x] `orchestrator/reconcile.py`: `_emit_ack` posts a status line on editable channels
- [x] `orchestrator/pipeline.py`: `set_status` calls at stage + terminal hooks
- [x] `config/schema.py`: `notifications.show_progress` (default true)
- [x] Tests (13): edit seam, lifecycle (receipt->working->reviewing->done in place),
      non-editable + toggle-off fallback, idempotent re-sweep, best-effort edit failure
- [x] CHANGELOG entry; `ARGUS_GATE=1` gate green (795 passed, 83.13%)

## Scope guard (not in this change)
- True token-by-token reasoning stream (needs engine-adapter streaming) - separate, larger.
- Threaded replies / reactions as progress - not now.

## Review
- Shipped the staged status line as an **additive, best-effort** layer: it never
  touches the substantive replies/notifies, so the existing flows (answer,
  PR-ready, blocked) are byte-identical - the status is a separate self-editing
  line that resolves to ✅/⚠️. Lowest-risk way to get the Claude-in-Slack feel.
- The one subtle invariant (executor: `status` + existing `provider_ref` => EDIT,
  not idempotent-done) is carved as a dedicated branch before the generic
  short-circuit; `set_status` only re-arms on a real text change, so idempotent
  sweeps and same-line stages (qa->senior) don't re-edit.
- Edge cases covered by tests: receipt sent once then edited in place; toggle
  off + non-editable channel both fall back to the one-shot receipt; failed edit
  is best-effort (drain never breaks); set_status is a no-op with no status msg.
- Follow-up (not here): true token-by-token reasoning stream (needs engine
  streaming); per-stage detail/links inside the status line.
