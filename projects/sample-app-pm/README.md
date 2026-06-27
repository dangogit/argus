# sample-app-pm (fixture)

A core fixture project the PM tests and the `argus verify` pm smoke run against.
Triage is on (github gatherer only), autofix is `propose-only`, and the daily
dispatch cap is 3. It points at no live repo: the runner uses a throwaway git
checkout and the `echo` engine, so a run never reaches a network or a model and
never pushes. Copy `projects/sample-app/` (not this) to start a real project.
