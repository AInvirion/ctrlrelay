# AgentJunior - Project Memory

## Current Status (2026-02-11)
- **Weeks 1-6: COMPLETE** — Full stack deployed and working
- **Week 7+: IN PROGRESS** — CRUD pages, import features, new features, polish
- **Live URL**: https://agent-junior.com (custom domain)
- **DO App ID**: ad233c31-6837-4c54-892d-83370ed0fbd5
- **129 backend tests passing**, lint + tsc + eslint clean
- **Production env**: All DO console vars configured, Google OAuth tested e2e, working
- **Redis**: SKIPPED (optional, app degrades gracefully, not worth $15/mo for MVP)

## What's Deployed / Implemented
- FastAPI backend with 35+ endpoints (auth, users, chat, contacts, tasks, integrations, github, telegram, usage, health)
- Strands Agents SDK v1.25 with OpenAI provider (agents-as-tools orchestrator)
- Tasks: REST API (CRUD) + management UI (no longer chat-based)
- Contacts: REST API (CRUD + CSV import + Google Contacts import) + directory UI
- GitHub Issues: REST API (CRUD proxy to GitHub API) + management UI (no longer chat-based)
- Content Creator Agent: real tools (Tavily web search + GPT-4o blog generation)
- Telegram Bot: infrastructure ready (bot module, webhook endpoint, account linking)
- Onboarding Wizard: 4-step dialog on first login (Gmail → Calendar → GitHub → Done)
- Email tools → Gmail REST API via GoogleApiClient
- Calendar tools → Google Calendar REST API via GoogleApiClient
- GitHub tools → GitHub REST API via GitHubApiClient (PAT-based)
- Integration endpoints: Gmail/Calendar OAuth + GitHub OAuth connect/disconnect/status
- Usage API: GET /api/usage/current + GET /api/usage/history (real implementation)
- Login auth: Google OAuth → JWT (access + refresh tokens)
- Subscription tier gating: Starter=email+calendar, Pro=+tasks+contacts+github, Ultimate=+content
- Chat API: send message, get history, clear history, list sessions
- PostgreSQL with Alembic migrations (10 tables + RLS) + migration 002 (telegram_user_id)
- React 19 frontend: login, dashboard, chat, all agent pages, settings, integrations, onboarding

## What's Still Missing / Stub
- Stripe billing: config placeholders only, no API endpoints
- Email Interface (inbound parse): not started
- Background workers/scheduler: not started
- Frontend tests: 0 test files (no Vitest/Playwright)
- Email/Calendar pages: still chat-based (Tasks + Contacts + GitHub converted to CRUD)
- Telegram Bot: needs TELEGRAM_BOT_TOKEN env var + end-to-end testing

## Key Architecture Decisions (Approved 2026-02-08)
- **Deployment**: DigitalOcean Apps + Managed PostgreSQL (Redis skipped)
- **Backend**: Python 3.11+ / FastAPI
- **Agent Framework**: Strands Agents (AWS OSS, v1.25+) - NOT "Strand-Agents"
- **LLM**: OpenAI GPT-4o-mini (primary), GPT-4o (escalation for content/complex)
- **Data Isolation**: PostgreSQL Row-Level Security
- **Frontend**: React 19 + TypeScript + Vite 7 + Tailwind CSS v4 + shadcn/ui (New York)
- **Auth**: Google OAuth 2.0 with PKCE
- **Payments**: Stripe (not yet implemented)
- **CI/CD**: GitHub Actions → auto-deploy to DO Apps

## Database Models (10 tables)
users, oauth_tokens, subscriptions, tasks, contacts, conversations,
agent_memory, usage_logs, agent_configs, github_configs
All user-data tables have RLS policies via `app.current_user_id`.
users table now has `telegram_user_id` column (migration 002).

## Agents (6 for MVP)
1. Email Agent (Gmail API) — working
2. Calendar Agent (Google Calendar API) — working
3. Tasks Agent (PostgreSQL CRUD) — working + REST API + UI
4. Contact Agent (PostgreSQL CRUD) — working + REST API + UI + import
5. Content Creator Agent (Tavily web search + GPT-4o blog generation) — working
6. GitHub Agent (GitHub REST API) — working + REST API + UI

## Pricing (Tiered Bundles)
- Starter: $5/mo (Email + Calendar + Telegram)
- Pro: $10/mo (+ Tasks + Contacts + GitHub)
- Ultimate: $15/mo (+ Content Creator)

## Lessons Learned
- See CLAUDE.md for full list
- CSV import: use `csv.DictReader`, accept `UploadFile`, handle utf-8-sig BOM
- Google People API: `personFields=names,emailAddresses,phoneNumbers,organizations`, paginate with `pageToken`
- FormData file upload from frontend: don't set Content-Type header (browser sets multipart boundary)
- `apiFetch` auto-sets Content-Type to JSON when body exists — pass `headers: {}` to override for FormData
- Contacts import dedup: match on email (case-insensitive) to skip existing
- React 19 ESLint: `react-hooks/set-state-in-effect` rule — avoid `setState` in `useEffect`, derive state instead
- Telegram bot: python-telegram-bot v22+ uses `Application.builder()` pattern, async handlers
- Content tools: Tavily import is lazy (inside function) since it's optional dependency
