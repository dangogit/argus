"""Typed argus.yaml. Secrets carry an unresolved ref + a resolved value."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

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
    # cross-connector knobs of note:
    #   respond: bool, default False. Respond-back: when a signal from this
    #     source carries a 'reply_to' origin descriptor and its work reaches a
    #     terminal state (PR opened, failed, triaged as ignore), propose a short
    #     reply to that origin (see orchestrator/respond.py). Replies ride the
    #     actions outbox, so outward-channel approval gates still apply.
    # supabase source connector-specific knobs of note (see connectors/supabase.py):
    #   writeback: bool, default False. Append Argus's verdict to the bug row's
    #     notes column after the request goes terminal. Predates and implies
    #     respond-back for the supabase_bug_reports origin kind.
    #   batch: bool, default False. Collapse every open bug row found in one poll
    #     into a single consolidated signal (one PM dispatch, one pipeline run,
    #     one PR) instead of one per bug row. Opt-in, current per-bug behavior
    #     unchanged when unset.


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
    actions: dict[str, AutonomyMode] = Field(default_factory=dict)


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


class CompletionWatchdog(BaseModel):
    enabled: bool = False
    threshold_minutes: int = 45
    sources: List[str] = Field(default_factory=list)
    fingerprint_prefixes: List[str] = Field(default_factory=list)
    destination_ref: Optional[str] = None


class OwnershipCodePolicy(BaseModel):
    auto_ready: bool = False
    auto_merge: bool = False
    allowed_base_branches: List[str] = Field(default_factory=list)
    required_checks: List[str] = Field(default_factory=list)
    deploy_workflow: Optional[str] = None
    live_url: Optional[str] = None
    smoke_paths: List[str] = Field(default_factory=lambda: ["/"])
    deployment_timeout_minutes: int = 30
    blocked_globs: List[str] = Field(default_factory=lambda: [
        ".github/**", "**/.env*", "**/*secret*", "**/*credential*",
        "**/migrations/**", "**/auth/**", "**/billing/**",
        "**/payments/**", "functions/**", "package.json", "package-lock.json",
        "pnpm-lock.yaml", "yarn.lock", "firebase.json", "firestore.rules",
        "storage.rules", "firestore.indexes.json",
    ])

    @field_validator("deployment_timeout_minutes")
    @classmethod
    def deployment_timeout_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("deployment_timeout_minutes must be positive")
        return value

    @model_validator(mode="after")
    def validate_auto_merge_and_deploy(self) -> "OwnershipCodePolicy":
        if self.auto_merge and not self.allowed_base_branches:
            raise ValueError("auto_merge requires allowed_base_branches")
        if self.auto_merge and not self.required_checks:
            raise ValueError("auto_merge requires required_checks")
        if self.deploy_workflow and not self.live_url:
            raise ValueError("deploy_workflow requires live_url")
        return self


class OwnershipSupportPolicy(BaseModel):
    auto_send_low_risk: bool = False
    min_confidence: float = 0.90
    blocked_categories: List[str] = Field(default_factory=lambda: [
        "billing", "refund", "account_access", "security", "privacy",
        "legal", "deletion", "charge_dispute",
    ])

    @field_validator("min_confidence")
    @classmethod
    def min_confidence_must_be_a_probability(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("min_confidence must be between 0 and 1")
        return value


class OwnershipMaintenancePolicy(BaseModel):
    enabled: bool = False
    interval_hours: int = 24
    max_open: int = 1

    @field_validator("interval_hours", "max_open")
    @classmethod
    def values_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("maintenance values must be positive")
        return value


class OwnershipPolicy(BaseModel):
    enabled: bool = False
    cycle_seconds: int = 300
    max_active_obligations: int = 3
    max_attempts: int = 3
    stale_minutes: int = 45
    code: OwnershipCodePolicy = Field(default_factory=OwnershipCodePolicy)
    support: OwnershipSupportPolicy = Field(default_factory=OwnershipSupportPolicy)
    maintenance: OwnershipMaintenancePolicy = Field(
        default_factory=OwnershipMaintenancePolicy)

    @field_validator(
        "cycle_seconds", "max_active_obligations", "max_attempts", "stale_minutes")
    @classmethod
    def values_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("ownership values must be positive")
        return value


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
    setup_timeout_seconds: Optional[int] = None
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
    # Company-wide duplicate-work guard: when a dispatch references a bug id
    # (supabase bug_reports row id, or "bug report <uuid>" in task text) that
    # already has a non-terminal request anywhere in the company, skip the new
    # dispatch instead of opening a second pipeline for the same bug. Off by
    # default so existing installs are unaffected (real incident: the same bug
    # id opened tadam PR #324 and tadam-agents PR #302 on 2026-07-01).
    dedup_bug_dispatch: bool = False
    completion_watchdog: CompletionWatchdog = Field(default_factory=CompletionWatchdog)


class Company(BaseModel):
    name: str
    defaults: Defaults = Field(default_factory=Defaults)
    sources: List[SourceRef] = Field(default_factory=list)


class Project(BaseModel):
    repo: str
    base_branch: str = "main"
    work_branch_prefix: str = "argus"
    setup_cmd: Optional[str] = None
    setup_timeout_seconds: int = 900
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
    # How to get a preview URL of the change.
    # 'vercel': push the branch, poll the Vercel API for the preview build.
    # 'firebase': build the site in the worktree and deploy a Firebase Hosting
    #   preview channel (for repos whose preview is PR-triggered, not push-built,
    #   e.g. luma on Firebase). No branch push needed.
    discovery: Literal["vercel", "firebase"] = "vercel"
    vercel_project_id: Optional[str] = None
    vercel_team_id: Optional[str] = None   # required for team-scoped Vercel projects
    vercel_token_env: str = "VERCEL_TOKEN"
    firebase_project: Optional[str] = None            # e.g. luma-web-ai-staging
    firebase_build_cmd: str = "npm ci && npm run build"
    firebase_channel_expires: str = "1d"
    firebase_build_timeout_seconds: int = 900         # build + deploy can be slow
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


class McpServer(BaseModel):
    """An external MCP server Argus agents may call. stdio: command + args.
    http: url. env lists env var NAMES to pass through (values stay in the
    environment, never in YAML). tools is an optional Claude Code allowlist."""
    name: str
    transport: Literal["stdio", "http"] = "stdio"
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    url: Optional[str] = None
    env: List[str] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)


class McpConfig(BaseModel):
    servers: List[McpServer] = Field(default_factory=list)


class Team(BaseModel):
    name: str
    roles: List[Role]
    pipeline: Pipeline
    engine: Optional[EngineSpec] = None
    autonomy: Optional[Autonomy] = None
    ownership: OwnershipPolicy = Field(default_factory=OwnershipPolicy)
    notifications: Optional[NotificationSettings] = None
    sources: List[SourceRef] = Field(default_factory=list)
    channels: List[ChannelBinding] = Field(default_factory=list)
    project: Optional[Project] = None
    mcp: McpConfig = Field(default_factory=McpConfig)
    # Per-team rolling 24h spend cap (USD). Overrides nothing else; when this
    # team is over it, only this team's new work pauses. None = no team cap.
    max_daily_cost_usd: Optional[float] = None
    # Per-team override of company.defaults.dedup_bug_dispatch. None = inherit
    # the company setting; explicit true/false wins over it either way.
    dedup_bug_dispatch: Optional[bool] = None
    completion_watchdog: Optional[CompletionWatchdog] = None

    def role(self, name: str) -> Role:
        for r in self.roles:
            if r.name == name:
                return r
        raise KeyError(name)


class RetroConfig(BaseModel):
    authority: RetroAuthority = "propose"
    company_change_team: Optional[str] = None


class HermesConfig(BaseModel):
    """Settings for the hermes worker engine (src/argus/hermes/, engine/adapters/hermes.py).

    Optional: a project with no hermes section keeps the legacy
    argus.config.yaml `hermes:` block as a fallback (see
    argus.hermes.settings.hermes_setting).
    """
    toolset: Optional[str] = None
    pm_toolset: Optional[str] = None
    pm_readonly_toolset: Optional[str] = None
    model: Optional[str] = None
    max_turns: Optional[str] = None
    skills_dir: Optional[str] = None
    provider: Optional[str] = None
    home_root: Optional[str] = None


class Config(BaseModel):
    company: Company
    teams: List[Team]
    retro: RetroConfig = Field(default_factory=RetroConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    hermes: Optional[HermesConfig] = None

    def team(self, name: str) -> Team:
        for t in self.teams:
            if t.name == name:
                return t
        raise KeyError(name)
