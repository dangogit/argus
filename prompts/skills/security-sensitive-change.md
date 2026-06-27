---
name: security-sensitive-change
triggers: [auth, token, secret, password, credential, rls, permission, 401, 403, api key, session, oauth]
roles: [developer]
---
This request touches a security-sensitive surface. Before finishing the change:

- Validate and escape input at the trust boundary; never trust a client-supplied id for authorization decisions.
- Never log, echo, or commit secrets, tokens, or credentials. They stay in env / secret refs.
- Supabase: confirm RLS policies still enforce per-row ownership after the change; prefer a DB constraint over an app-layer check.
- Keep the fix minimal and least-privilege. Do not widen scopes, disable a check, or relax a policy just to make the symptom go away.
