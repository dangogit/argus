# Browser-verify stage (Vercel preview) — design

Confirmed 2026-07-01. Gives Argus a pre-merge browser check of UI changes against
the real Vercel preview, gated on the diff (no browser run for backend-only changes).

## Backend (how the browser is driven) — DEFAULT: hermes on the Codex sub

Two backends behind an injectable runner (config `browser_verify.backend`):
- **`hermes` (default)** — drives the browser via the hermes `browser` toolset on
  the **openai-codex** provider (the Codex/ChatGPT subscription, `gpt-5.5`). NO
  browser-use install, NO metered LLM key. `_hermes_runner` shells
  `hermes -z <task> -t browser --yolo -m gpt-5.5`; `_extract_hermes_verdict` pulls
  the final PASS/FAIL line. e2e-proven 2026-07-01 (verdict pass on the live arvuyot
  preview, zero metered spend).
- **`browser-use`** — the browser-use library (needs a metered OpenAI/Anthropic key
  or a paid browser-use-cloud key). Kept as a fallback. Runs in a dedicated venv via
  subprocess (`browser_venv_python`) so the argus runtime never imports it.

Note: the Codex subscription cannot be exposed as a raw OpenAI API (hermes `proxy`
only fronts nous/xai), which is why browser-use itself can't use the sub - but
hermes CAN, because it calls the Codex responses backend directly and has its own
browser toolset. That is the whole reason `hermes` is the default backend.

## Goal / decisions (locked)

- Verify target: **Vercel preview** (the deployed artifact), pre-merge.
- Trigger: **deterministic diff gate** — run the browser check only when the diff
  touches UI files (`*.vue`, `src/views/`, `src/components/`, styles). Backend-only
  diffs skip it (auto-pass). This is the "PM/QA decide depending on the changes".
- Architecture **A**: a `browser_verify` **judge** stage inserted between `qa` and
  `senior` (`developer → qa → browser_verify → senior → open_pr`). It pushes the
  work branch itself (Vercel builds a preview on any branch push), polls the
  preview, runs Playwright, returns a verdict. `senior` still opens the PR at the
  end (re-push is idempotent). A fail reuses the existing judge→rework→
  `force_draft_on_fail` machinery (same as a qa fail).

## Core state-machine change (affects ALL teams — must be verdict-safe)

`pipeline._advance`:
- Today `qa` hardcodes its successor as `senior` (`_index(team,"senior")`).
  Generalize: qa advances to the **next stage** (`job.stage+1`). For a team with
  no browser_verify stage this is still `senior` — behavior unchanged.
- Add `_is_browser_verify(team, role)` (judge kind + name `browser_verify`) and an
  `elif` branch mirroring qa: `verdict = parsed.get("verdict")` → pass advances to
  next stage (senior); fail `_loop_back(to_role="developer")`. Without this branch
  a browser_verify judge falls into the generic linear advance and its verdict is
  **ignored** (silent pass) — the bug this section prevents.
- `_checks_summary` / `_build_checks`: add a `Browser: pass|fail` line.

## Config (schema.py)

`class BrowserVerify(BaseModel)` on `Project` + `ProjectDefaults`:
- `enabled: bool = False`
- `ui_globs: List[str] = ["**/*.vue","src/views/**","src/components/**","**/*.css","src/styles/**"]`
- `vercel_project_id: Optional[str]` (else discover from repo)
- `vercel_token_env: str = "VERCEL_TOKEN"`
- `build_timeout_seconds: int = 300`
- `poll_interval_seconds: int = 10`
- `base_path: str = "/"` (entry path to open)

- `browser_model: str = "claude-sonnet-4-6"` (browser-use agent LLM)
- `api_host: Optional[str]` (extra allowed_domain, e.g. the Supabase host so
  login/API works)

Team gets a `browser_verify` role (kind: judge) and the stage added to
`pipeline.stages`. The verification itself is run by **browser-use** (below), not
by a scripted Playwright skill.

## Preview discovery module (new: src/argus/v2/browser/preview.py)

`discover_preview_url(project, branch, commit, *, http=<injectable>) -> str`
- Vercel API: `GET https://api.vercel.com/v6/deployments?projectId={id}
  &meta-githubCommitRef={branch}&limit=1`, auth `Bearer $VERCEL_TOKEN`.
- Poll until `readyState == "READY"` (or `state == "READY"`); return
  `https://{url}`. Raise `PreviewTimeout` on build_timeout, `PreviewFailed` on
  `ERROR`/`CANCELED`. `http` injectable so tests mock it (no network).

## Worker wiring (worker.py, parallel to the test_cmd block)

When role is the browser_verify judge and `project.browser_verify.enabled`:
1. `diff = workspace.diff(...)`. If NOT `diff_touches_ui(diff, ui_globs)` →
   short-circuit: finalize the job with `parsed.verdict = "pass"` and
   `browser_skipped = true` (no push, no agent, no browser). Cheap + safe.
2. Else: `workspace.push(project, workdir)` (triggers the Vercel preview build),
   `url = discover_preview_url(...)`, then `run_browser_check(preview_url=url,
   base_path, changed_files, summary=<request title>, allowed_domains=[preview
   host, api_host], model=browser_model, test_login=<from config>)`. Write the
   returned `verdict` into the job result (`parsed.verdict`).
3. On any preview error (timeout/failed) OR browser run error: verdict `fail`
   with the reason (fail-closed - a broken preview/run never silently passes).

## Browser runner (browser-use) — src/argus/v2/browser/runner.py (DONE)

Uses browser-use (github.com/browser-use/browser-use): an LLM browser agent that
drives the page itself, so we hand it a task + preview URL and read a PASS/FAIL
verdict instead of hand-scripting Playwright.
- `run_browser_check(...) -> BrowserCheckResult{verdict, reason, raw}`. The real
  run is behind an injectable `runner` seam (default: headless browser-use beta
  `Agent(task, llm=ChatAnthropic(model), browser_profile=BrowserProfile(
  headless=True, allowed_domains=[...]))`, `history.final_result()`), so tests
  need no browser/chromium/LLM key.
- `parse_verdict` is FAIL-CLOSED: empty/ambiguous output => fail.
- `allowed_domains` confines the agent to the preview + API host.

`diff_touches_ui(diff_text, globs)` — small helper (fnmatch over changed paths).

## On fail

Judge verdict `fail` → `_loop_back` to developer (bounded by `max_iters`) →
`force_draft_on_fail` opens the PR as draft with the browser findings. Same path as
a qa fail; no new failure plumbing.

## Tests (tests/python/v2/test_browser_verify.py) — 12 passing (DONE)

- `diff_touches_ui`: UI diff true; backend-only diff false; added-file case.
- `discover_preview_url`: mocked http polls BUILDING→READY→url; timeout raises;
  ERROR raises; missing config raises.
- runner: `parse_verdict` pass/fail/fail-closed; `build_task` includes url/files/
  login; `run_browser_check` uses injected runner; fail-closed on runner exception.
- TODO (with the wiring): `_advance` browser_verify pass→senior, fail→developer;
  assert a team WITHOUT the stage is unchanged. Worker skip-path (backend diff →
  pass, no push/http/browser).

## Status

FULLY BUILT + E2E-PROVEN on `feature/browser-verify` (742 unit tests green):
- `browser/preview.py`, `browser/runner.py`, schema `BrowserVerify`, the
  `_advance` change (generalized qa-advance + `_is_browser_verify` branch),
  worker `_run_browser_verify`, `workspace.push`.
- runner finalized against browser-use 0.13.1 (top-level `Agent` +
  `ChatAnthropic`/`ChatOpenAI` + `BrowserProfile(headless, allowed_domains)`).

E2E (2026-07-01, against the real arvuyot Vercel project):
- Preview discovery returned the live dev preview URL. FOUND+FIXED 2 bugs unit
  tests could not: Vercel needs `teamId` for team-scoped projects, and
  non-production branch previews report `target=None` (so `&target=preview`
  dropped them).
- browser-use drove the live preview headless (gpt-4o-mini) through
  `run_browser_check` -> verdict PASS with a real reason. Negative run against a
  dead preview -> fail-closed.

- hermes backend added + e2e-proven: `run_browser_check(backend='hermes')` drove
  the live preview on the Codex subscription (no browser-use, no metered key) ->
  verdict pass. 744 unit tests green.

REMAINING: only enablement (config flip). Not wired to any live team.

## Rollout (safe) — hermes backend needs ZERO new secrets

1. Build on `feature/browser-verify`, all unit tests green. ✅
2. Do NOT add the stage to any live team in `~/argus-run/v2-argus.yaml` yet.
3. With `backend: hermes` (default) NO new secrets and NO install are needed:
   hermes + openai-codex is already set up (the Codex sub). Preview discovery still
   uses `VERCEL_TOKEN` for now (TODO: switch to `gh` deployment-status discovery,
   proven feasible, to drop that too). browser-use backend is optional/fallback
   (needs `pip install browser-use` + a metered key).
4. Enable for **arvuyot-yashir only**: add the `browser_verify` judge role + put it
   in `pipeline.stages` between qa and senior; set `browser_verify.enabled: true`,
   `vercel_project_id: prj_MdRC...`, `vercel_team_id: team_Vse...`, `api_host:
   qpayfsqrcgdwsaeiqmcg.supabase.co`, `test_login: {phone, otp}`. Restart
   `com.argus.up`, watch the next UI PR verify against a real preview before
   rolling to other teams.
