# Argus Claude Code Guide

Read `AGENTS.md` first. It is the shared install, smoke-test, and contribution
guide for Claude Code, Codex, and other coding agents.

For daily learning work, use `docs/retro.md` as the product contract.
`argus retro run` owns team and company learning. When `retro.authority` is
`auto-changes`, it can open internal PM requests, but must still respect
approval gates for merge, deploy, outward messages, secrets, and destructive
work. It also queues PM digests to project control channels and a CEO retro
brief to the `ceo-brief` control channel unless run with `--no-notify`.
