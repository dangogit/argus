# Assistant And Calendar

Assistant memory is stored in Postgres:

```bash
argus assistant memory refresh
argus assistant memory show
```

Calendar commands use the v2 Google Calendar client:

```bash
argus calendar ping
argus calendar list --days 7
argus calendar create --title "Call" --start 2026-06-18T09:00:00+03:00 --duration 30
argus calendar update --id EVENT_ID --title "New title"
argus calendar delete --id EVENT_ID
```

Calendar requires `ARGUS_GCAL_CALENDAR_ID` and either
`ARGUS_GCAL_ACCESS_TOKEN` or a service-account key at `ARGUS_GCAL_SA_KEY`.
