# Jarvis Personal Assistant - Project Memory

## Project State (assessed 2026-02-08)
- **Status**: Scaffolding with local CRUD, but no real AI or Google integrations
- **Backend**: ~65K lines Python/FastAPI, SQLite, 31 tables, 14 route files
- **Frontend**: ~510 lines React/TypeScript, mostly placeholder pages
- **Claims**: 215/215 features "passing" but verification was superficial (endpoint exists = pass)
- **Key gap**: All 5 AI agents are stubs (TODO comments for Google API integration)
- **Key gap**: No API keys configured (.env has no OPENAI_API_KEY, GOOGLE_CLIENT_ID, etc.)
- **Key gap**: Frontend pages for Calendar/Email/Tasks/Contacts/Content are all `Placeholder.tsx`

## Architecture
- Backend: FastAPI + raw sqlite3 (not SQLAlchemy ORM despite spec)
- Frontend: React 19 + Vite + Tailwind + Zustand (minimal usage)
- Auth: Google OAuth 2.0 flow exists, JWT tokens
- Encryption: AES-256 vaults for per-user data (implemented)
- main.py is 6,724 lines (monolith, needs refactoring)

## What Actually Works
- Server boots, Swagger docs serve
- Database CRUD via REST API (local SQLite only)
- OAuth flow structure
- Token encryption/decryption
- API call logging middleware

## What Doesn't Work
- No agent actually processes natural language (all return canned strings)
- No Google Calendar/Gmail/Tasks/Contacts sync
- No OpenAI integration (no API key)
- No Redis, Celery, Docker, or tests
- Frontend is a shell (5/7 dashboard sections are placeholders)
