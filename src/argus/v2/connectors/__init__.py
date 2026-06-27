"""Importing this package registers every connector."""
from argus.v2.connectors import (  # noqa: F401
    branch_drift,
    email_imap,
    fake,
    fly,
    firebase,
    github,
    openapi,
    postgres,
    posthog,
    sentry,
    supabase,
    uptime,
    vercel,
    webhook,
)
