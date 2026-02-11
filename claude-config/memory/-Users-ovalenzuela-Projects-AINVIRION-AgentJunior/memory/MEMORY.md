# AgentJunior - Project Memory

## Current Status (2026-02-09)
- **Week 1: COMPLETE** — Backend deployed to DO Apps
- **Week 2: COMPLETE** — Agents SDK, Orchestrator, Chat API deployed
- **Week 3: COMPLETE** — Agent tools wired to DB + Google APIs
- **Week 4: COMPLETE** — GitHub Agent + delete tools + tier gating (79 tests)
- **Live URL**: https://agent-junior.com (custom domain)
- **DO App ID**: ad233c31-6837-4c54-892d-83370ed0fbd5
- **Week 5-6: COMPLETE** — React frontend dashboard (68 source files, tsc+lint clean)
- **Frontend deployed**: Login + dashboard working end-to-end on agent-junior.com
- **Next**: Test chat with agents, integrations UI, settings page

## What's Deployed / Implemented
- FastAPI backend with 21 endpoints (auth, users, chat, integrations incl. GitHub, health)
- Strands Agents SDK v1.25 with OpenAI provider (agents-as-tools orchestrator)
- Tasks + Contacts tools → PostgreSQL CRUD (incl. delete) with RLS via get_tool_db_session
- Email tools → Gmail REST API via GoogleApiClient
- Calendar tools → Google Calendar REST API via GoogleApiClient
- GitHub tools → GitHub REST API via GitHubApiClient (PAT-based, list/get/create/update/close issues)
- Content tools still stubs (content uses GPT-4o directly)
- Integration endpoints: Gmail/Calendar connect/callback/disconnect/status + GitHub connect/disconnect/status
- Login auth stores Google OAuth tokens in OAuthToken table
- user_id + subscription_tier threaded from orchestrator → specialist agents → tools via invocation_state
- Subscription tier gating: Starter=email+calendar, Pro=+tasks+contacts+github, Ultimate=+content
- Chat API: send message, get history, clear history, list sessions
- JWT blacklist via Redis with jti claim (graceful degradation without Redis)
- PostgreSQL dev database with Alembic migrations applied (10 tables + RLS)
- 39 tests passing, lint clean
- User still needs to configure: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, OPENAI_API_KEY

## Key Architecture Decisions (Approved 2026-02-08)
- **Deployment**: DigitalOcean Apps + Managed PostgreSQL + Managed Redis
- **Backend**: Python 3.11+ / FastAPI
- **Agent Framework**: Strands Agents (AWS OSS, v1.25+) - NOT "Strand-Agents"
- **LLM**: OpenAI GPT-4o-mini (primary), GPT-4o (escalation for content/complex)
- **Data Isolation**: PostgreSQL Row-Level Security (NOT SQLCipher per-user)
- **Frontend**: React 19 + TypeScript + Vite 7 + Tailwind CSS v4 + shadcn/ui (New York)
- **Auth**: Google OAuth 2.0 with PKCE
- **Payments**: Stripe
- **CI/CD**: GitHub Actions -> auto-deploy to DO Apps

## Project Structure
```
/ (repo root)
├── Dockerfile              # Production (copies from backend/)
├── requirements.txt        # For DO auto-detection
├── .do/app.yaml            # DO App Spec (matches cliquey pattern)
├── .github/workflows/ci.yml
├── backend/
│   ├── app/
│   │   ├── main.py         # FastAPI app, lifespan, CORS, routes
│   │   ├── config.py       # Pydantic Settings + async_database_url property
│   │   ├── database.py     # Async SQLAlchemy engine + session
│   │   ├── api/auth.py     # Google OAuth, JWT refresh, logout
│   │   ├── api/users.py    # GET/PUT /users/me
│   │   ├── models/         # 10 SQLAlchemy models (see below)
│   │   ├── middleware/      # auth.py (JWT+RLS), rate_limit.py
│   │   └── utils/          # jwt.py, encryption.py (Fernet)
│   ├── alembic/            # 001_initial_schema.py (tables + RLS)
│   ├── tests/              # test_health.py
│   ├── Dockerfile          # Dev/local (kept for docker-compose)
│   ├── docker-compose.yml  # Local dev (PG16 + Redis7)
│   └── pyproject.toml      # Dependencies, ruff, mypy config
├── MVP_SPEC.md             # v2.1 - Approved spec
└── MVP_PLAN.md             # v2.1 - 12-week plan with progress
```

## Database Models (10 tables)
users, oauth_tokens, subscriptions, tasks, contacts, conversations,
agent_memory, usage_logs, agent_configs, github_configs
All user-data tables have RLS policies via `app.current_user_id`.

## Agents (6 for MVP)
1. Email Agent (Gmail API)
2. Calendar Agent (Google Calendar API)
3. Tasks Agent (own PostgreSQL - NOT Todoist)
4. Contact Agent (own PostgreSQL - NOT Airtable)
5. Content Creator Agent (Tavily + GPT-4o)
6. GitHub Agent (simple issue tracking)

## Interfaces (3)
- Web Dashboard (React)
- Telegram Bot
- Email Interface (inbound parse)

## Pricing (Tiered Bundles)
- Starter: $5/mo (Email + Calendar + Telegram)
- Pro: $10/mo (+ Tasks + Contacts + GitHub)
- Ultimate: $15/mo (+ Content Creator)

## Post-MVP Features (Months 4-6)
- User-created personal prompts & custom agents
- Platform custom tools via MCP (designed by AInvirion)

## Lessons Learned
- Original docs were AI-generated with hallucinated framework name ("Strand-Agents")
- SQLCipher per-user is impractical on PaaS (ephemeral disk)
- GPT-4o-mini is ~10x cheaper than GPT-4o, margins jump from 62% to 88%+
- DO Apps auto-detection requires Dockerfile/requirements.txt at REPO ROOT
- `dockerfile_path` in .do/app.yaml is relative to `source_dir`
- DO provides `postgresql://` but SQLAlchemy async needs `postgresql+asyncpg://` — use `async_database_url` property
- Match cliquey pattern: github section in app.yaml with deploy_on_push
- `doctl apps update <id> --spec file.json` to update app config via CLI
- `doctl apps list-deployments <id>` to monitor deploy progress
- **CRITICAL**: Dockerfile copies `backend/requirements.txt`, NOT root `requirements.txt` — keep BOTH in sync!
- asyncpg doesn't support `sslmode=` parameter, must convert to `ssl=` (DO URLs include `?sslmode=require`)
- DO managed Redis requires production tier (~$15/mo min), dev tier not available for Redis
- DO kaniko builder caches Docker layers aggressively — changing RUN command string busts cache
- SSH key issues with git push? Use `gh auth setup-git && git -c credential.helper='!gh auth git-credential' push`
- Strands Agents SDK: `@tool` decorator auto-generates tool specs from type hints + docstrings
- Strands Agents SDK: agents-as-tools pattern = wrap specialist agent calls as `@tool` functions for orchestrator
- Strands SDK: `@tool(context=True)` injects `ToolContext` into param named `tool_context`
- Strands SDK: `invocation_state` passes custom kwargs from `agent(msg, key=val)` to tool context
- Strands SDK: async tool functions are awaited natively (line 600-601 in decorator.py)
- Strands SDK: `__wrapped__` attribute on decorated tools gives access to underlying function for testing
- Agent tools can't use FastAPI DI — they run inside Strands SDK event loop, use `get_tool_db_session()` instead
- Integration OAuth (Gmail/Calendar) needs separate redirect URIs from login OAuth
- Tailwind v4: use `@import "tailwindcss"` + `@theme inline {}` (no tailwind.config.ts)
- Tailwind v4: use `tw-animate-css` (NOT `tailwindcss-animate` which is v3 only)
- shadcn/ui v3.8+: may install components to literal `@/` directory — move to `src/` after install
- React 19 eslint: `set-state-in-effect` rule — compute initial state outside useEffect instead
- DO static sites: use `catchall_document: index.html` for SPA routing
- DO ingress: more-specific path rules first (/api, /health, /docs), catch-all (/) last
- DO ingress: `preserve_path_prefix: true` required or backend never sees `/api` prefix
- DO env vars: service-level overrides app-level — watch for stale duplicates (OPENAI_API_KEY, GOOGLE_CLIENT_ID)
- PostgreSQL SET LOCAL doesn't accept bind params ($1) — use f-string with system-generated UUIDs
- RLS: unauthenticated endpoints (auth callback) must manually SET LOCAL app.current_user_id
- Backend API returns wrapped objects (e.g. `{sessions: [...]}`) — frontend must unwrap before .map()
