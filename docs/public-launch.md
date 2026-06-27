# Public Launch Checklist

Use this checklist before switching the repository from private to public. It
separates repo-file readiness from external GitHub, package, and community
settings.

## Repo Files

- README has install, quickstart, security defaults, docs-by-goal, and support
  links.
- `VISION.md` explains public product direction, non-goals, and operational
  success standard.
- `docs/index.md` is ready for GitHub Pages with source set to `/docs`.
- `AGENTS.md`, `llms.txt`, and
  `skills/argus-live-onboarding/SKILL.md` explain how Codex, Claude Code, and
  other coding agents should install and prove Argus.
- `.env.example` lists required secret names only.
- `Dockerfile` builds a local CLI image and `.dockerignore` keeps private local
  state out of Docker build context.
- `THIRD_PARTY_NOTICES.md` lists runtime dependency licenses and notes that the
  Docker image resolves packages at build time.
- `scripts/install.sh` and `scripts/install.ps1` cover Unix-like shells and
  Windows PowerShell for GitHub installs.
- `scripts/public_launch_check.py` turns external GitHub, Actions, release,
  docs, community, and PyPI proof into one command with table and JSON output.
- `MANIFEST.in` keeps the source distribution aligned with public docs,
  examples, installer scripts, prompt files, skills, and legal files.
- No local state, secrets, OS metadata, private configs, or generated runtime
  folders are tracked.
- GitHub community health files exist for support, security, conduct,
  contributing, issue routing, and dependency updates.
- `python scripts/gate.py` passes locally.

## GitHub Settings

- Repository description explains the product in one sentence.
- Topics include the main public discovery terms: `ai-agent`, `ai-agents`,
  `agentic-workflows`, `developer-tools`, `self-hosted`, `slack`, `telegram`,
  `codex`, `claude`, `python`, and `monitoring`.
- GitHub Pages is enabled from branch `main`, folder `/docs`.
  If this repository is still private and the API returns
  `Your current plan does not support GitHub Pages for this repository`, switch
  the repository public first or use a GitHub plan that supports private Pages.
- Actions are able to start runners and show green CI on a pull request after
  billing or spending-limit issues are resolved.
- Security advisories are enabled.
- Discussions are enabled for setup questions. Bugs and feature requests stay
  in issues.

## External Proof Commands

Run these before the public launch announcement. They prove the GitHub surface
that is not stored in git.

```bash
python scripts/public_launch_check.py --repo dangogit/argus --pypi-package argus-agent
python scripts/public_launch_check.py --repo dangogit/argus --pypi-package argus-agent --json

gh repo view dangogit/argus \
  --json description,homepageUrl,isPrivate,hasDiscussionsEnabled,hasIssuesEnabled,latestRelease,repositoryTopics

gh api repos/dangogit/argus --jq '{private, has_pages, homepage}'

gh api repos/dangogit/argus/pages --jq '{html_url,status,source,build_type}'

gh api repos/dangogit/argus/community/profile \
  --jq '{health_percentage, documentation, license: .files.license.spdx_id, readme: .files.readme.html_url}'

gh release view --repo dangogit/argus --json tagName,name,publishedAt,url
```

The script exits `1` when any launch blocker remains and `2` when local tooling
is missing. Codex, Claude Code, and other agent installers should treat
`repo.visibility`, `docs.homepage`, `actions.ci`, `actions.release`, and
`package.pypi` blockers as real launch blockers, not documentation gaps.

Expected public-launch state:

- `isPrivate` and `private` are `false`.
- `homepageUrl` and `homepage` point at the docs site or `/docs` entry.
- `has_pages` is `true`, and the Pages source is branch `main`, path `/docs`,
  unless another docs host is used.
- Community health is `100`.
- Latest release points at the launch commit.
- Actions can start jobs. A pre-step failure for billing or spending limits is
  not a code failure, but it still blocks a public green CI badge.

## Package And Release

- Create a release for the public launch commit.
- Run the Release workflow manually once and confirm `python-dist` artifact
  builds, passes `twine check`, and the installed-wheel and installed-sdist
  smoke jobs migrate disposable pgvector databases outside the source tree.
- Publish `argus-agent` to PyPI only after package metadata, install smoke, and
  wheel and sdist smokes pass outside the source tree.
- Configure PyPI trusted publishing for repository `dangogit/argus`, workflow
  `.github/workflows/release.yml`, environment `pypi`, and package
  `argus-agent`.
- Keep the GitHub install path as the primary install until the PyPI package is
  live:

```bash
curl -fsSL https://raw.githubusercontent.com/dangogit/argus/main/scripts/install.sh | sh
```

## Launch Smoke

Run these on a clean machine or clean virtual environment:

```bash
docker compose up -d postgres
until docker compose exec -T postgres pg_isready -U argus -d argus; do sleep 1; done
argus --version
argus init --config argus.yaml --force
argus validate
argus doctor
```

For a real project, the public onboarding proof is:

```bash
argus onboard project /absolute/path/to/project --mode chat-only \
  --config /absolute/path/to/private/argus.yaml \
  --out-dir /absolute/path/to/private/onboarding \
  --channel slack --channel-id C1234567890

argus doctor --deep --json
argus go-live --mode chat-only --public-url https://argus.example.com/slack
```

Do not describe an install as operational while `go-live` reports
`configured-only` or `blocked`.

## Not Automated

Argus does not create third-party accounts or apps for users. Each operator
creates or supplies credentials for Slack, Telegram, GitHub, Codex, Claude
Code, Hermes, Vercel, Supabase, Sentry, Firebase, PostHog, and other providers.
Argus writes private local config, proves credentials with dry-runs, and fails
closed when a required provider is missing.
