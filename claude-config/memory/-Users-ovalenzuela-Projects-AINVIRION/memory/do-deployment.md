# DigitalOcean Deployment - ainvirion-private-do-deployable-app

## Resources Created (2026-03-07)

| Resource | ID | Notes |
|---|---|---|
| DO App | `24b93cbe-a693-42df-8d47-1d0540dfa0dd` | Name: `ainvirion-app`, Region: sfo3, URL: https://ainvirion-app-jnf72.ondigitalocean.app |
| Managed PostgreSQL | auto-created via app spec | Name: `db`, size: db-s-dev-database, PG 16, dev mode |

## How to Delete

```bash
# Delete the app (includes static site and API service)
doctl apps delete 24b93cbe-a693-42df-8d47-1d0540dfa0dd --force

# Note: Managed database may need separate deletion if not auto-removed
doctl databases list  # Check for orphaned DBs
```

## App Spec Details
- API: `apps-s-1vcpu-0.5gb` (smallest)
- DB: `db-s-dev-database` with `production: false`
- Static site: frontend SPA
- Pre-deploy job: migrations + seeds
- All feature flags disabled
