"""Typed argus.yaml. Secrets carry an unresolved ref + a resolved value."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

EngineName = Literal["echo", "scripted", "codex", "claude-code", "hermes", "openrouter", "ollama"]
RoleKind = Literal["front", "builder", "judge", "worker"]
ActionRisk = Literal["reversible_internal", "personal_outward", "irreversible_outward"]
AutonomyMode = Literal["auto", "approval"]
QuietHoursDelivery = Literal["hold", "deliver"]
RetroAuthority = Literal["propose", "auto-changes"]


class EngineSpec(BaseModel):
    engine: EngineName
    model: Optional[str] = None
    sandbox: Optional[str] = None
    tools: Optional[str] = None


class SourceRef(BaseModel):
    type: str
    name: str
    scope: Literal["company", "team"] = "team"
    secret_ref: Optional[str] = None
    secret: Optional[str] = None  # filled by the loader
    team: Optional[str] = None    # which team a company-scoped source routes to
    config: dict = Field(default_factory=dict)  # connector-specific params


class ChannelBinding(BaseModel):
    # Validated against the live channel REGISTRY in the loader (single source of
    # truth), not a closed Literal that drifts as channels are added.
    type: str
    role: Literal["control", "outward"] = "control"
    channel_id: str
    secret_ref: Optional[str] = None
    secret: Optional[str] = None
    config: dict = Field(default_factory=dict)


class Autonomy(BaseModel):
    reversible_internal: AutonomyMode = "auto"
    personal_outward: AutonomyMode = "approval"
    irreversible_outward: AutonomyMode = "approval"


class NotificationSettings(BaseModel):
    timezone: str = "UTC"
    quiet_hours: str | bool = "22:30-08:30"
    quiet_hours_delivery: QuietHoursDelivery = "hold"
    # Drive a single self-updating status line through the work lifecycle
    # (receipt -> working -> reviewing -> done) on edit-capable chat channels.
    # Off -> a one-shot receipt only (no in-place edits).
    show_progress: bool = True
    urgent_severities: List[str] = Field(
        default_factory=lambda: [
            "error",
            "critical",
            "urgent",
            "emergency",
            "page",
            "wake",
        ]
    )
    urgent_keywords: List[str] = Field(
        default_factory=lambda: [
            "refund",
            "chargeback",
            "legal",
            "lawyer",
            "gdpr",
            "delete my account",
            "cannot access",
            "can't access",
            "charged",
            "payment failed",
        ]
    )


class Role(BaseModel):
    name: str
    kind: RoleKind
    prompt: str
    tools: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)  # load-on-relevance playbooks; [] => none
    engine: Optional[EngineSpec] = None
    vision: bool = False


class Pipeline(BaseModel):
    stages: List[str]
    max_iters: int = 2
    max_jobs: int = 50


class AutofixDefaults(BaseModel):
    mode: Optional[Literal["propose-pr", "propose-only"]] = None
    draft: Optional[bool] = None
    force_draft_on_fail: Optional[bool] = None


class ProjectPmDefaults(BaseModel):
    daily_limit: Optional[int] = None
    max_rework_attempts: Optional[int] = None
    eval_threshold: Optional[float] = None


class ProjectDefaults(BaseModel):
    base_branch: Optional[str] = None
    work_branch_prefix: Optional[str] = None
    setup_cmd: Optional[str] = None
    test_cmd: Optional[str] = None
    test_timeout_seconds: Optional[int] = None
    remote: Optional[str] = None
    github_repo: Optional[str] = None
    allow_code_mode: Optional[bool] = None
    allow_network: Optional[bool] = None
    autofix: AutofixDefaults = Field(default_factory=AutofixDefaults)
    pm: ProjectPmDefaults = Field(default_factory=ProjectPmDefaults)


class Defaults(BaseModel):
    engine: Optional[EngineSpec] = None
    autonomy: Autonomy = Field(default_factory=Autonomy)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    pipeline: Optional[Pipeline] = None
    project: ProjectDefaults = Field(default_factory=ProjectDefaults)
    support: dict = Field(default_factory=dict)
    webhook_secret: Optional[str] = None
    embedder: Optional[dict] = None
    # Global ceiling on rolling 24h LLM spend (USD). When reached the
    # orchestrator stops opening new work until spend drops; in-flight jobs
    # still finish. None disables the cap (default).
    max_daily_cost_usd: Optional[float] = None


class Company(BaseModel):
    name: str
    defaults: Defaults = Field(default_factory=Defaults)
    sources: List[SourceRef] = Field(default_factory=list)


class Project(BaseModel):
    repo: str
    base_branch: str = "main"
    work_branch_prefix: str = "argus"
    setup_cmd: Optional[str] = None
    test_cmd: Optional[str] = None
    test_timeout_seconds: int = 900
    remote: str = "origin"
    github_repo: Optional[str] = None   # owner/repo override for gh (else from remote)
    allow_code_mode: bool = True        # builder/worker may batch edits via ARGUS_SCRIPT
    allow_network: bool = False         # builder/worker sandbox gets network (gh/git
                                        # fetch/push). Writes stay confined to the
                                        # worktree; only network opens. Off by default.
    autofix: "Autofix" = Field(default_factory=lambda: Autofix())
    pm: "ProjectPm" = Field(default_factory=lambda: ProjectPm())
    browser_verify: "BrowserVerify" = Field(default_factory=lambda: BrowserVerify())


class Autofix(BaseModel):
    mode: Literal["propose-pr", "propose-only"] = "propose-pr"
    draft: bool = False
    force_draft_on_fail: bool = True


class BrowserVerify(BaseModel):
    """Config for the browser_verify stage (docs/browser-verify-design.md).

    Off by default. When enabled and a team has a `browser_verify` judge stage,
    it pushes the work branch, polls the Vercel preview, and runs a browser-use
    agent against it - but only when the diff touches UI files (`ui_globs`).
    """
    enabled: bool = False
    # 'hermes' drives the browser via the hermes `browser` toolset on the Codex
    # subscription (gpt-5.5) - no browser-use install, no metered LLM key.
    # 'browser-use' uses the browser-use library (needs a metered LLM key).
    backend: Literal["hermes", "browser-use"] = "hermes"
    hermes_model: str = "gpt-5.5"   # only model accepted by ChatGPT/Codex accounts
    ui_globs: List[str] = Field(default_factory=lambda: [
        # Extension-based so it catches UI markup/styles across Vue + React/Next +
        # Svelte anywhere, WITHOUT matching backend .ts (Next API routes, edge
        # functions, composables, server logic) which would trigger it spuriously.
        "**/*.vue", "**/*.tsx", "**/*.jsx", "**/*.svelte",
        "**/*.css", "**/*.scss",
    ])
    vercel_project_id: Optional[str] = None
    vercel_team_id: Optional[str] = None   # required for team-scoped Vercel projects
    vercel_token_env: str = "VERCEL_TOKEN"
    build_timeout_seconds: int = 300
    poll_interval_seconds: int = 10
    base_path: str = "/"
    browser_model: str = "claude-sonnet-4-6"
    api_host: Optional[str] = None          # extra allowed_domain (e.g. Supabase host)
    test_login: Optional[dict] = None        # {"phone": ..., "otp": ...} for authed screens
    browser_venv_python: Optional[str] = None  # dedicated venv python w/ browser-use;
                                               # None => import browser-use in-process


class ProjectPm(BaseModel):
    daily_limit: int = 3
    max_rework_attempts: Optional[int] = None
    eval_threshold: Optional[float] = None  # None => eval gate off (CI-only promotion)


class Team(BaseModel):
    name: str
    roles: List[Role]
    pipeline: Pipeline
    engine: Optional[EngineSpec] = None
    autonomy: Optional[Autonomy] = None
    notifications: Optional[NotificationSettings] = None
    sources: List[SourceRef] = Field(default_factory=list)
    channels: List[ChannelBinding] = Field(default_factory=list)
    project: Optional[Project] = None
    # Per-team rolling 24h spend cap (USD). Overrides nothing else; when this
    # team is over it, only this team's new work pauses. None = no team cap.
    max_daily_cost_usd: Optional[float] = None

    def role(self, name: str) -> Role:
        for r in self.roles:
            if r.name == name:
                return r
        raise KeyError(name)


class RetroConfig(BaseModel):
    authority: RetroAuthority = "propose"
    company_change_team: Optional[str] = None


class McpServer(BaseModel):
    """An external MCP server Argus agents may call. stdio: command + args.
    http: url. env lists env var NAMES to pass through (values stay in the
    environment, never in YAML)."""
    name: str
    transport: Literal["stdio", "http"] = "stdio"
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    url: Optional[str] = None
    env: List[str] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)  # optional allowlist


class McpConfig(BaseModel):
    servers: List[McpServer] = Field(default_factory=list)


class Config(BaseModel):
    company: Company
    teams: List[Team]
    retro: RetroConfig = Field(default_factory=RetroConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

    def team(self, name: str) -> Team:
        for t in self.teams:
            if t.name == name:
                return t
        raise KeyError(name)
