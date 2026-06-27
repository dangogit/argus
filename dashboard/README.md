# Argus dashboard

A read-only Next.js 15 console for Argus. Two pages:

- `/` attention feed: warn, error, and critical alerts, most recent first.
- `/details` everything: all alerts plus proposed fixes.

It reads v2 state from Postgres at request time. It never mutates anything.
There is no WhatsApp webhook and no write API here.

## Data source

The dashboard reads `alerts`, `actions`, and `requests` through `ARGUS_DB_DSN`
or `DATABASE_URL`.

## Develop

```bash
export ARGUS_DASHBOARD_TOKEN=dev-token
npm install
npm run dev      # http://localhost:3000
```

## Test and build

```bash
npm run test     # vitest, no server or secrets needed
npm run build    # Next production build
npm start        # serve the production build
```

Run these locally before changing dashboard code. The Python `argus verify`
gate stays fast and does not build the dashboard.
