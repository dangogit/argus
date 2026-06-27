# sample-app

The example project Argus ships so a new user has something concrete to point
at. It declares only a manifest (`project.yaml`) right now.

## Copy it for your own project

```bash
cp -r projects/sample-app projects/my-app
$EDITOR projects/my-app/project.yaml   # set name, repo, triage.enabled, autofix.mode
argus validate                          # confirm the manifest is well formed
```

`name` must be unique across core and overlay projects. `contract` must target
the current Argus extension contract (same major). `autofix.mode` defaults to
`propose-only`; `autonomous` is an explicit opt-in (design 5.7).
