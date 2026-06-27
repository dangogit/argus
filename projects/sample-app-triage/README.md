# sample-app-triage

A core fixture project used to exercise the triage loop hermetically. It enables
both v0.1 gatherers (`github`, `uptime`), which read offline fixtures
(`ARGUS_GITHUB_FIXTURE`, `ARGUS_UPTIME_FIXTURE`) rather than the network, so
`argus triage run sample-app-triage` runs with no secrets and no live calls.

It is NOT a template to copy for a real project; copy `projects/sample-app`
instead and turn on the gatherers you want.
