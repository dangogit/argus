# Browser-verify stage (Vercel preview) — design

Confirmed 2026-07-01. Gives Argus a pre-merge browser check of UI changes against
the real Vercel preview, gated on the diff (no browser run for backend-only changes).

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

DONE (feature/browser-verify): `browser/preview.py`, `browser/runner.py`, tests
(12 green). Isolated, not wired in. REMAINING: schema `BrowserVerify`; the
`_advance` change (generalize qa-advance + `_is_browser_verify` branch);
worker.py step; then enablement.

## Rollout (safe)

1. Build on `feature/browser-verify`, all unit tests green. ✅ foundation
2. Do NOT add the stage to any live team in `~/argus-run/v2-argus.yaml` yet.
3. Secrets in `~/argus-run/secrets.env`: `VERCEL_TOKEN` (preview discovery) and
   `ANTHROPIC_API_KEY` (browser-use agent). Install browser-use in the argus venv:
   `pip install "browser-use[core]"` (bundles the chromium runtime). Finalize
   `runner._default_runner` against the installed version (beta vs stable import).
4. Enable for **arvuyot-yashir only** (add the role + stage; set `api_host` to the
   staging Supabase host so login works; `test_login` = staging phone/OTP),
   restart `com.argus.up`, and watch the next UI PR verify against a real preview
   before rolling to other teams.
