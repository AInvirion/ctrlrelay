# Kenos Orchestrator - Memory

## Key Facts
- **App ID:** `39e3ec98-fd60-4d7a-99e6-7b90d1784776`
- **Live URL:** `https://kenos-nkitf.ondigitalocean.app`
- **Test account:** admin@semcl.one / KenosAdmin2026
- **API routes:** Do NOT include `/api` prefix (ingress strips it)
- **main.py location:** `api/main.py` (not `api/app/main.py`)
- **Deployment:** Push to main → auto-deploy (2-5 min). Check with `doctl apps list-deployments <app-id>`
- **Auth:** JWT bearer tokens. Login: `POST /v1/auth/login`

## Implementation Status (2026-02-10)
- **Phase 0-3:** COMPLETE — Infrastructure, products, scanning, CVE, enrichment
- **Phase 4:** COMPLETE — AI Analysis (OpenAI gpt-4o-mini, auto-enqueue after scan)
- **Phase 5:** COMPLETE — tanu-discovery integration (25+ sources)
- **Phase 6:** PARTIAL — SBOM export done (CycloneDX 1.5 + SPDX 2.3), compliance quizzes/rubrics pending
- **Sprint B:** COMPLETE — Credit/licensing (license CRUD, credit ledger, scan debit, dashboard)
- **Sprint C:** COMPLETE — Self-reg + public risk dashboard (domain matching, pending approvals)
- **Other pending:** Circuit breakers, email sending (Sprint D), Redis rate limiting
- **See:** `NEXT_STEPS.md` for full gap analysis and roadmap

## Architecture
- **4 scanners:** osslili, binarysniffer, syft, src2purl
- **SCANNER_MAP:** `worker/tools/__init__.py` maps product_type → scanner list
- **Pipeline:** download → file inventory → scanners → store → CVE → enrichment → rules → findings → status
- **Rule engine:** 12 deterministic rules + OSPAC policy evaluator
- **CVE:** OSV.dev batch API + individual endpoint, cve_cache with 24h TTL
- **Enrichment:** ClearlyDefined (extensible provider architecture)
- **Export:** `GET /v1/scans/{id}/export?format=cyclonedx_json|spdx_json`
- **Stats:** `GET /v1/stats/dashboard` (aggregated counts + recent scans)
- **Discovery:** `POST /v1/companies/{id}/discover`, 3-tier confidence classification
- **AI Analysis:** `GET /v1/scans/{id}/analysis`, `POST /v1/scans/{id}/analyze`
  - Module: `worker/ai_analyzer.py`, auto-enqueues after scan (priority 5)
  - Tables: `analysis_jobs`, `analysis_results`, `token_usage` (from migration 001)
  - Skips silently if OPENAI_API_KEY not set
- **Licenses:** `POST/PUT/GET /v1/licenses`, `POST /v1/credits/grant`, `GET /v1/vaults/{id}/credits`
  - Tables: `licenses`, `credit_transactions` (from migration 001)
  - Credit check in scans: `_check_and_debit_credit()` with `FOR UPDATE` locking
  - Enterprise tier unlimited, SEMCLONERs bypass
- **Self-reg:** `POST /v1/auth/register` (domain match → VIEWER, no match → pending)
  - `GET /v1/auth/verify-email?token=...`, rate limited 5/min/IP
  - Bootstrap renamed to `POST /v1/auth/bootstrap-register`
  - Migration 007: `email_verified`, `domain`, `verification_tokens`, `pending_registrations`
- **Public API:** `GET /v1/public/companies`, `GET /v1/public/companies/{id}/summary`
  - Company-level aggregates only (no vault IDs, CVEs, components)

## Key Patterns
- Routers in `api/main.py` with `prefix="/v1"`
- RBAC: `current_user.role.upper() == "SEMCLONER"` / `"VAULT_ADMIN"`
- Job queue: raw SQL INSERT, `SELECT FOR UPDATE SKIP LOCKED`
- Metadata: `metadata_json` and `details_json` JSONB columns
- Pydantic schemas MUST match details_json keys or fields are silently stripped
- CVE + enrichment steps are non-fatal (try/except)

## Critical Gotchas
- `dict.get('key', default)` returns None when key exists with None value — use `or`
- `json.loads(strict=False)` needed for binarysniffer output (embedded control chars)
- Tailwind: ALWAYS rebuild CSS after template changes (`cd dashboard && npm run build:css`)
- `.do/app.yaml` changes need `doctl apps update --spec`, not just git push
- Build artifacts outside `/workspace/<source_dir>/` are NOT persisted to runtime
- `pip install --upgrade pip` can break `git+https://` auth (pip 26+ issue)
- osslili: NEVER use `license_files_only=False` — freezes on large repos
- binarysniffer: pre-filters via symlinked temp dir (configurable env vars)
