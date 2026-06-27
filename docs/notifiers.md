# Notifications

Alerts are stored in Postgres and can be routed by severity.

```bash
argus alert add --severity error --project dev --fingerprint ISSUE-1 --message "broken"
argus alert list --severity error
```

WhatsApp notifications use the Evolution API settings:

```bash
export ARGUS_WA_URL=http://127.0.0.1:8080
export ARGUS_WA_INSTANCE=argus
export ARGUS_WA_APIKEY=...
```
