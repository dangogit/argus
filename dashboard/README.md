# Argus dashboard

A read-only Next.js 15 console for Argus. Main surfaces:

- `/` operational summary, proposed fixes, and attention feed.
- `/details` all alerts plus proposed fixes.
- `/login` browser token form for setting the protected cookie.

It reads v2 state from Postgres at request time. It never mutates anything.
There is no WhatsApp webhook and no write API here.

## Data source

The dashboard reads `alerts`, `actions`, and `requests` through `ARGUS_DB_DSN`
or `DATABASE_URL`. It also summarizes `events`, `jobs`, and `runs` when the
full v2 schema is present. DB read failures render as dashboard errors instead
of crashing the page.

The dashboard only ever runs `SELECT`. Do not point `ARGUS_DB_DSN` at the
full-privilege operator DSN used by the orchestrator; a compromised dashboard
process would then have full read/write access to the database. Instead,
point it at a read-only login user:

1. Migration `0023_dashboard_readonly_role.sql` creates a `NOLOGIN` role
   `argus_dashboard` with `SELECT` on all current and future tables in the
   `public` schema.
2. As a one-time operator step (not in a migration, since passwords do not
   belong in migrations), create a login user in that role:

   ```sql
   CREATE ROLE dashboard_login LOGIN PASSWORD '<generated-secret>' IN ROLE argus_dashboard;
   ```

3. Set the dashboard's `ARGUS_DB_DSN` to that user's connection string, e.g.
   `postgresql://dashboard_login:<generated-secret>@host:port/dbname`.

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
