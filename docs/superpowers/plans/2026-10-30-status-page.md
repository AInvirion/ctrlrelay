---
title: Status page plan
layout: default
nav_order: 100
nav_exclude: true
---

# Status Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only web page and JSON API, served by a new `ctrlrelay web` process, that show what each agent is doing (phase, last command), what is waiting, what needs an answer, and a feed of what ctrlrelay did and said.

**Architecture:** The daemon gains one thing: best-effort rows in a new `events` table (and WAL mode on `state.db`). A separate `ctrlrelay web` process reads `state.db` read-only, tails agent transcripts, asks GitHub once a minute for the waiting line, and serves `/`, `/api/status`, `/api/events` from the standard-library HTTP server.

**Tech Stack:** Python 3.12, sqlite3, `http.server.ThreadingHTTPServer`, existing `GitHubCLI` (`gh`), typer, pydantic, pytest + pytest-asyncio (explicit `@pytest.mark.asyncio`, not auto mode).

**Spec:** `docs/superpowers/specs/2026-10-30-status-page-design.md`

## Global Constraints

- Read-only: the web process never writes to `state.db`, the filesystem (except its pid file), or GitHub. No endpoint changes state; only `GET` is served, anything else is `405`.
- No new runtime dependency. Standard library HTTP server; no external CDN in the page.
- Daemon changes are limited to: WAL mode, and best-effort event writes that log and swallow every exception and never change, delay or fail the work they describe.
- Unknown is shown as unknown: missing transcript, busy database, failed GitHub call each produce an explicit `"unknown"`/`null` plus a reason, never an empty list that reads as "nothing".
- Config defaults: `host: 127.0.0.1`, `port: 8787`, `history_days: 7`, refresh every 5 s, GitHub checked every 60 s, "looks stuck" after 15 minutes.
- Every timestamp in JSON is a pair: `<name>_ts` (epoch seconds, int) and `<name>` (ISO 8601 UTC string).
- No secrets in output: no environment, no tokens, no full prompts. Commands are truncated to 200 chars.
- Run the suite on Python 3.12: `uv run -p 3.12 --extra dev pytest` and `uv run -p 3.12 --extra dev ruff check src tests`.

## Review Focus

1. An issue title or a shell command containing `<script>` or `"` renders as text on the page, never as HTML (titles come from GitHub, where anyone can file on a public repo). Test in Task 6.
2. A transcript whose last line is half-written, a transcript dir with no `.jsonl`, or a multi-MB transcript gives the last complete step or `unknown`, never an exception, and reads at most the tail. Test in Task 3.
3. `state.db` missing, locked, or older than the `events` table returns `200` with `"error"` fields and `unknown` sections, not a `500`. Test in Task 5.
4. `gh` failing (not logged in, rate limit, timeout) leaves the waiting line `unknown` with the time of the last good answer, not empty. Test in Task 4.
5. A secops session (no issue number, no title) and a `task` pipeline session render without errors. Test in Task 5.

---

## File map

| File | Responsibility |
|---|---|
| `src/ctrlrelay/core/state.py` (modify) | WAL, `events` table, `record_event`, `list_events`, `prune_events` |
| `src/ctrlrelay/core/events.py` (create) | `EventKind`, `emit_event()` best-effort wrapper |
| `src/ctrlrelay/pipelines/dev.py`, `task.py`, `secops.py`, `post_merge.py`, `bridge/server.py`, `cli.py` (modify) | call `emit_event` at existing points |
| `src/ctrlrelay/web/__init__.py` (create) | package marker |
| `src/ctrlrelay/web/transcript.py` (create) | find and tail an agent transcript, classify phase |
| `src/ctrlrelay/web/waiting.py` (create) | compute the waiting line from GitHub + poller rules |
| `src/ctrlrelay/web/status.py` (create) | read-only DB snapshot → JSON contract |
| `src/ctrlrelay/web/server.py` (create) | HTTP server, routes, background GitHub refresh |
| `src/ctrlrelay/web/static/index.html` (create) | the page (layout B) |
| `src/ctrlrelay/core/config.py` (modify) | `WebConfig` |
| `src/ctrlrelay/core/github.py` (modify) | `search_assigned_issues` |
| `src/ctrlrelay/templates/systemd/web.service.template`, `install.py` (create/modify) | third unit |
| `docs/configuration.md`, `docs/operations.md` (modify) | `web:` block, how to enable |

---

### Task 1: WAL mode and the events table

**Files:**
- Modify: `src/ctrlrelay/core/state.py` (`SCHEMA` :15-179, `__init__` :189-202, new methods near `list_pr_watches` :1160)
- Create: `src/ctrlrelay/core/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Produces:
  - `StateDB.record_event(kind: str, *, repo: str | None = None, issue_number: int | None = None, session_id: str | None = None, detail: dict | None = None, ts: int | None = None) -> None` (commits)
  - `StateDB.list_events(*, since_ts: int = 0, limit: int = 200) -> list[dict]` newest first; `detail` decoded from JSON
  - `StateDB.prune_events(older_than_ts: int) -> int`
  - `events.EventKind` (`StrEnum`): `DISPATCHED="dispatched"`, `DONE="done"`, `BLOCKED="blocked"`, `FAILED="failed"`, `PR_OPENED="pr_opened"`, `MERGED="merged"`, `CI_STARTED="ci_started"`, `CI_FINISHED="ci_finished"`, `REVIEW_STARTED="review_started"`, `REVIEW_FINISHED="review_finished"`, `MESSAGE_SENT="message_sent"`, `QUESTION_POSTED="question_posted"`, `QUESTION_HELD="question_held"`, `ALERT_SENT="alert_sent"`, `RESTART="restart"`
  - `events.emit_event(state_db: StateDB | None, kind: EventKind, **fields) -> None`: no-op when `state_db` is None; any exception is logged via `log_event(_logger, "events.write_failed", kind=..., reason=type(e).__name__)` and swallowed.

- [ ] **Step 1: Write the failing tests** in `tests/test_events.py`:
  - `test_state_db_uses_wal`: `StateDB(tmp_path/"s.db")`; `db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"`.
  - `test_record_and_list_events_newest_first`: record `dispatched` at ts=100 and `merged` at ts=200 with `detail={"pr": 5}`; `list_events()` returns kinds `["merged","dispatched"]` and `[0]["detail"] == {"pr": 5}`; `list_events(since_ts=150)` returns only `merged`.
  - `test_prune_events`: rows at 100 and 200, `prune_events(150) == 1`, one row left.
  - `test_existing_db_gains_events_table`: create a DB with the old schema by running `SCHEMA` minus the events DDL on a raw `sqlite3` connection, then open with `StateDB`; `record_event` works.
  - `test_emit_event_swallows_failure`: a `StateDB` whose `record_event` raises `sqlite3.OperationalError("database is locked")` (monkeypatch); `emit_event(db, EventKind.DONE, repo="a/b")` returns `None`, and `caplog` holds `events.write_failed`.
  - `test_emit_event_none_db_is_noop`.

- [ ] **Step 2: Run** `uv run -p 3.12 --extra dev pytest tests/test_events.py -v`. Expected: FAIL (no `record_event`, no `ctrlrelay.core.events`).

- [ ] **Step 3: Implement.** In `__init__`, right after connect: `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000`. Add to `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    repo TEXT,
    issue_number INTEGER,
    session_id TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, ts);
```

`detail` is `json.dumps(detail)` or NULL. `emit_event` lives in `core/events.py`.

- [ ] **Step 4: Run** the same command. Expected: PASS. Then the full suite; expected: same count as main plus the new tests, 0 failed.

- [ ] **Step 5: Commit** `git add src/ctrlrelay/core/state.py src/ctrlrelay/core/events.py tests/test_events.py && git commit -m "feat(state): events table and WAL mode for the status page (#209)"`

---

### Task 2: Emit events from the daemon

**Files:**
- Modify: `src/ctrlrelay/pipelines/dev.py` (running INSERT :1282-1291, final UPDATE :1503-1508, failed :1605, `add_pending_resume` :1517, PR number read :1362, `_run_verify` :1045-1105, `_review_and_comment` :904-933), `src/ctrlrelay/pipelines/task.py` (:375, :431, :471), `src/ctrlrelay/pipelines/secops.py` (:746, :895, :945, :992, `_question_hold` :122), `src/ctrlrelay/pipelines/post_merge.py` (:355-359), `src/ctrlrelay/bridge/server.py` (SEND :467-478, ASK :545-550), `src/ctrlrelay/cli.py` (`deliver_relay_alert` after `mark_dispatch_alert_delivered` :1140; `poller_start` after `scheduler.started`; `_run_question_expiry_sweeper` :3020)
- Test: `tests/test_event_emission.py`

**Interfaces:**
- Consumes: `emit_event`, `EventKind`, `StateDB.list_events`, `StateDB.prune_events` (Task 1).
- Produces: event rows with these `detail` keys, which Task 5 reads:
  - `dispatched`: `{"pipeline": str}`
  - `done`/`failed`/`blocked`: `{"pipeline": str, "exit_code": int|None, "question": str|None}` (question truncated to 300 chars)
  - `pr_opened`, `merged`: `{"pr_number": int, "pr_url": str|None}`
  - `ci_started`/`ci_finished`, `review_started`/`review_finished`: `{"pr_number": int}`, finished adds `{"ok": bool}`
  - `message_sent`: `{"request_id": str|None, "chars": int}`; `question_posted`: `{"request_id": str|None}`
  - `question_held`: `{"held_until": int}`; `alert_sent`: `{"consecutive_failures": int|None}`; `restart`: `{"process": "poller"|"bridge", "version": str}`

- [ ] **Step 1: Write the failing tests** in `tests/test_event_emission.py`, each driving the real function with the repo's existing fakes (follow the fixtures already used by `tests/test_dev_pipeline*.py`, `tests/test_secops*.py`, `tests/test_post_merge*.py`, `tests/test_bridge_server.py`) and asserting on `state_db.list_events()`:
  - `test_dev_run_records_dispatched_pr_ci_review_done`: a dev run that opens PR 7 and passes CI and review yields kinds in time order `dispatched, pr_opened, ci_started, ci_finished, review_started, review_finished, done`, all with the session's `session_id`, `pr_opened.detail["pr_number"] == 7`.
  - `test_dev_blocked_records_blocked_with_question`.
  - `test_task_and_secops_record_dispatched_and_outcome`: secops rows have `issue_number is None`.
  - `test_secops_hold_records_question_held` with `detail["held_until"]` equal to the hold time.
  - `test_pr_watch_merge_records_merged`.
  - `test_bridge_send_and_ask_record_events`.
  - `test_event_write_failure_does_not_change_the_run`: patch `StateDB.record_event` to raise; the dev run result and its `sessions` row equal those of the same run without the patch.
  - `test_hourly_sweep_prunes_events_past_history`: events at now-8d and now-1d; after the sweep only the 1-day one remains (window: `config.web.history_days`, default 7 → use 7 here since Task 6 adds the config; read it with `getattr(getattr(config, "web", None), "history_days", 7)`).

- [ ] **Step 2: Run** `uv run -p 3.12 --extra dev pytest tests/test_event_emission.py -v`. Expected: FAIL (no rows).

- [ ] **Step 3: Implement.** Add one `emit_event(...)` call at each point listed under Files, after the work it describes has happened. The prune goes in `_run_question_expiry_sweeper` before its early return on missing TTL, in its own `try` that logs and continues, so it runs even with no chat transport.

- [ ] **Step 4: Run** the new file, then the full suite. Expected: PASS, no existing test changed.

- [ ] **Step 5: Commit** `git commit -m "feat(events): record dispatch, PR, CI, review, merge and message events (#209)"` with the files listed above staged by name.

---

### Task 3: Agent transcript reader

**Files:**
- Create: `src/ctrlrelay/web/__init__.py`, `src/ctrlrelay/web/transcript.py`
- Create: `tests/fixtures/transcript_sample.jsonl` (real rows, see Step 1)
- Test: `tests/test_web_transcript.py`

**Interfaces:**
- Produces:
  - `Phase` (`StrEnum`): `READING="reading"`, `CODING="coding"`, `TESTING="testing"`, `CI="ci"`, `REVIEW="review"`, `PR="pr"`, `UNKNOWN="unknown"`
  - `@dataclass(frozen=True) AgentActivity: phase: Phase; last_command: str | None; last_step_ts: int | None; transcript: str | None; reason: str | None`
  - `transcript_dir_for(worktree_path: str, home: Path) -> Path`: `home/".claude"/"projects"/re.sub(r"[/._]", "-", worktree_path)`
  - `classify(tool_name: str, tool_input: dict) -> Phase | None`
  - `read_activity(worktree_path: str | None, *, home: Path | None = None, tail_bytes: int = 262_144) -> AgentActivity`

- [ ] **Step 1: Capture a real fixture.** On alkaturbo, copy ~30 consecutive lines from a finished dev transcript under `~/.claude/projects/-home-ovalenzuela--ctrlrelay-worktrees-*/` that include `Read`, `Edit`, a `Bash` running `pytest`, and a `Bash` running `git push`. Replace repo-private text in `content` strings with `"..."`; keep keys, `type`, `timestamp`, tool names and inputs. Write failing tests:
  - `test_transcript_dir_matches_measured_layout`: `transcript_dir_for("/home/u/.ctrlrelay/worktrees/AInvirion-yt-dev-215-49a5", Path("/home/u"))` == `Path("/home/u/.claude/projects/-home-u--ctrlrelay-worktrees-AInvirion-yt-dev-215-49a5")`.
  - `test_classify`: `("Read",{})→READING`, `("Edit",{})→CODING`, `("Bash",{"command":"uv run pytest -q"})→TESTING`, `("Bash",{"command":"npx playwright test"})→TESTING`, `("Bash",{"command":"git push -u origin x"})→PR`, `("Bash",{"command":"gh pr create --fill"})→PR`, `("Bash",{"command":"sed -n '1,60p' f.py"})→READING`, `("TodoWrite",{})→None`.
  - `test_read_activity_from_real_fixture`: last step's phase, `last_command`, `last_step_ts` match the fixture's last `tool_use`.
  - `test_half_written_last_line_is_ignored`: fixture + a truncated JSON line → same result as without it.
  - `test_no_transcript_is_unknown`: empty dir → `phase=UNKNOWN`, `reason="no transcript"`; `worktree_path=None` → `UNKNOWN`, `reason="no worktree"`.
  - `test_reads_only_the_tail`: a 5 MB file whose last line is a `Bash pytest` step returns `TESTING`, and monkeypatched `open().read` is never asked for more than `tail_bytes`.
  - `test_last_command_truncated_to_200`.

- [ ] **Step 2: Run** `uv run -p 3.12 --extra dev pytest tests/test_web_transcript.py -v`. Expected: FAIL (module missing).

- [ ] **Step 3: Implement.** Newest `*.jsonl` by mtime in the dir. Seek to `max(0, size - tail_bytes)`, drop the first (partial) line and any line that fails `json.loads`. Walk `assistant` rows' `message.content` lists for `type == "tool_use"`. `last_command` is `input.command`, else `input.file_path`, else `input.pattern`, newline-collapsed, max 200 chars. Phase is `classify` of the last tool use that classifies to non-None. Test-runner patterns: `pytest`, `npm test`, `npm run test`, `npx playwright`, `go test`, `cargo test`, `jest`, `vitest`. PR patterns: `git push`, `gh pr create`. Read-only shell (everything else under `Bash`) → `READING`.

- [ ] **Step 4: Run** the test file. Expected: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat(web): read an agent's phase from its live transcript (#209)"`

---

### Task 4: Waiting line

**Files:**
- Modify: `src/ctrlrelay/core/github.py` (new method after `list_assigned_issues` :273)
- Create: `src/ctrlrelay/web/waiting.py`
- Test: `tests/test_web_waiting.py`

**Interfaces:**
- Consumes: `IssuePoller._matched_exclude_label`, `_matched_include_label`, `_matched_require_label`, `_issue_is_assigned_to` (static, `core/poller.py:602-669`); `Config.repos[*].automation` (`exclude_labels`, `include_labels`, `require_labels`, `accept_foreign_assignments`).
- Produces:
  - `GitHubCLI.search_assigned_issues(assignee: str, *, limit: int = 200) -> list[dict]`: `gh search issues --assignee <a> --state open --limit <n> --json repository,number,title,labels,assignees,updatedAt`; each dict gets `"repo": repository["nameWithOwner"]`.
  - `@dataclass(frozen=True) WaitingItem: repo: str; issue_number: int; title: str; reason: str; updated_ts: int`
  - `compute_waiting(config: Config, issues: list[dict], *, seen: set[str], running: list[dict], locks: dict[str, str]) -> list[WaitingItem]` (pure)
  - `load_seen(poller_state_file: Path) -> set[str]`: the poller's `seen_issues` from `poller_state.json` (empty set if the file is missing; raises nothing).
  - `class WaitingTracker` with `async def refresh(self) -> None` and `snapshot(self) -> dict` returning `{"items": [...], "status": "ok"|"unknown", "checked": iso, "checked_ts": int, "last_good": iso|None, "last_good_ts": int|None, "error": str|None}`. Username from `gh api user --jq .login` once, retried on next refresh if it failed.

- [ ] **Step 1: Write failing tests** in `tests/test_web_waiting.py`, with issue dicts in the exact shape `gh search issues --json ...` returns (capture one real response on alkaturbo into `tests/fixtures/gh_search_issues.json`):
  - `test_only_configured_repos`.
  - `test_label_rules_match_the_poller`: an issue with an exclude label is dropped; a repo with `require_labels=["ready"]` drops an issue without it; behaviour is asserted against calling the poller's own static methods on the same input.
  - `test_unlabeled_foreign_assignment_needs_accept`: in a repo not in `accept_foreign_assignments`, an issue admitted by assignment only has reason `"waits for the poller to confirm the assignment"`; in an accepting repo it gets the normal reason.
  - `test_seen_and_running_issues_are_dropped`.
  - `test_reasons`: repo locked by session of issue 215 → `"same repo as #215"`; any session running elsewhere → `"poller busy with <repo>#<n>"`; nothing running → `"next poll"`.
  - `test_order_is_config_repo_order_then_github_order`.
  - `test_gh_failure_keeps_last_good` (asyncio): first refresh ok with 2 items, second refresh `GitHubError` → `status == "unknown"`, `items` are the last good 2, `last_good_ts` is the first refresh's, `error` set.
  - `test_never_succeeded_is_unknown_not_empty`: first refresh fails → `status "unknown"`, `items == []`, `last_good is None`.

- [ ] **Step 2: Run** `uv run -p 3.12 --extra dev pytest tests/test_web_waiting.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement** as specified. `seen` keys use the poller's own key format (read it from `IssuePoller.mark_seen` :688). No other GitHub calls.

- [ ] **Step 4: Run.** Expected: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat(web): compute the waiting line from GitHub with the poller's own rules (#209)"`

---

### Task 5: Status snapshot (the JSON contract)

**Files:**
- Create: `src/ctrlrelay/web/status.py`
- Test: `tests/test_web_status.py`

**Interfaces:**
- Consumes: `read_activity` (Task 3), `WaitingTracker.snapshot` (Task 4), event kinds and detail keys (Task 2).
- Produces:
  - `open_readonly(db_path: Path) -> sqlite3.Connection`: `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)`, `row_factory = sqlite3.Row`.
  - `build_status(*, db_path: Path, waiting: dict, version: str, host: str, now: int | None = None, stale_after_s: int = 900, history_days: int = 7, home: Path | None = None) -> dict`
  - `list_feed(*, db_path: Path, since_ts: int, limit: int = 200) -> dict` → `{"events": [...], "error": str|None}`

The contract (keys exactly):

```
{
  "version": str, "host": str, "generated": iso, "generated_ts": int,
  "running": [ {"session_id", "pipeline", "repo", "issue_number": int|null,
                "title": str|null, "started", "started_ts", "elapsed_s": int,
                "phase": "reading|coding|testing|ci|review|pr|unknown",
                "phase_source": "transcript|pipeline|none",
                "last_command": str|null, "last_step", "last_step_ts",
                "looks_stuck": bool, "reason": str|null } ],
  "needs_you": [ {"session_id", "pipeline", "repo", "question", "asked", "asked_ts",
                  "held_until", "held_until_ts"} ],
  "waiting": <WaitingTracker.snapshot()>,
  "today": {"dispatched", "pr_opened", "merged", "failed", "blocked", "messages_sent"},
  "feed": [ {"ts", "time", "kind", "repo", "issue_number", "session_id", "detail"} ],
  "errors": [str]
}
```

- [ ] **Step 1: Write failing tests**, building real DBs with `StateDB(tmp_path/"state.db")` and the real write methods:
  - `test_running_dev_session_with_transcript_phase`: a running dev session whose worktree has the Task 3 fixture under a temp `home` → `phase == "pr"` (or the fixture's last phase), `phase_source == "transcript"`.
  - `test_open_ci_event_overrides_transcript`: `ci_started` with no matching `ci_finished` for that session → `phase == "ci"`, `phase_source == "pipeline"`, and `looks_stuck is False` even when the transcript's last step is 2 hours old.
  - `test_looks_stuck_after_threshold`: last step 16 min old, no open pipeline phase → `True`; 14 min → `False`.
  - `test_secops_session_has_no_issue` and `test_task_session_renders`.
  - `test_needs_you_lists_open_unanswered_questions` including `held_until`.
  - `test_today_counts_from_events` using the local day of `now`.
  - `test_missing_db_returns_unknown_sections`: nonexistent path → `running == []`, `errors` names the DB, no exception.
  - `test_locked_db_reports_error`: hold an `EXCLUSIVE` transaction on a second connection → `errors` non-empty, no exception, returns within 3 s.
  - `test_never_writes`: file mtime and size of `state.db` (and `-wal`) unchanged after `build_status`.
  - `test_feed_since`.

- [ ] **Step 2: Run** `uv run -p 3.12 --extra dev pytest tests/test_web_status.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement.** Pipeline phase = latest of `ci_started`/`review_started` for the session without a later matching `*_finished`. Title: from the waiting tracker's last GitHub answer when the issue is there, else `null` (no extra GitHub calls in this task).

- [ ] **Step 4: Run.** Expected: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat(web): build the status JSON from a read-only snapshot (#209)"`

---

### Task 6: Server, page, config, command and systemd unit

**Files:**
- Create: `src/ctrlrelay/web/server.py`, `src/ctrlrelay/web/static/index.html`, `src/ctrlrelay/templates/systemd/web.service.template`
- Modify: `src/ctrlrelay/core/config.py` (new `WebConfig`, field on `Config` :694-700), `src/ctrlrelay/cli.py` (new `web_app` typer group like `poller_app` :2212-2305), `src/ctrlrelay/install.py` (`render_systemd` :154 loop), `tests/test_install.py:68`, `docs/configuration.md`, `docs/operations.md`, `config/orchestrator.yaml.example`, `pyproject.toml` (only if `static/*.html` is not already shipped as package data; check with `uv build` and `unzip -l`)
- Test: `tests/test_web_server.py`, `tests/test_install.py`

**Interfaces:**
- Consumes: `build_status`, `list_feed` (Task 5), `WaitingTracker` (Task 4).
- Produces:
  - `WebConfig(BaseModel)`: `host: str = "127.0.0.1"`, `port: int = Field(8787, ge=1, le=65535)`, `history_days: int = Field(7, ge=1)`, `refresh_seconds: int = Field(5, ge=1)`, `github_check_seconds: int = Field(60, ge=15)`, `stale_after_minutes: int = Field(15, ge=1)`; `Config.web: WebConfig = Field(default_factory=WebConfig)`.
  - `make_server(config: Config, *, waiting: WaitingTracker) -> ThreadingHTTPServer`
  - CLI: `ctrlrelay web start [--config/-c] [--foreground/-F]`, pid file `state_db.parent/"web.pid"`, same daemonize pattern as `poller start`.
  - Unit `ctrlrelay-web.service`: `ExecStart=${CTRLRELAY_BIN} web start --foreground`, logs `append:%h/.ctrlrelay/logs/web.log`.

- [ ] **Step 1: Write failing tests** in `tests/test_web_server.py` (server on port 0 in a thread, real DB from Task 5's helpers):
  - `test_index_served_with_no_external_urls`: `GET /` 200, `text/html`, body contains no `http://` or `https://`.
  - `test_api_status_contract`: `GET /api/status` 200, `application/json`, top-level keys equal the Task 5 contract.
  - `test_api_events_since`; `test_api_events_bad_since_is_400` (`?since=abc`).
  - `test_unknown_path_404`; `test_post_put_delete_are_405` and the DB is unchanged after them.
  - `test_page_escapes_titles_and_commands`: a session titled `<img src=x onerror=alert(1)>` and a last command `"><script>` — load `index.html` and assert the page builds DOM with `textContent` (no `innerHTML` assignment of data anywhere in the file: `assert "innerHTML" not in html` or every `innerHTML` use is a static literal, checked by regex).
  - `test_status_error_is_200_with_errors` when `state_db` is missing.
  - `test_web_config_defaults`: `Config` without a `web` key → `host "127.0.0.1"`, `port 8787`, `history_days 7`.
  - In `tests/test_install.py`: systemd render now writes `ctrlrelay-web.service` with the ExecStart above; update the `{"bridge","poller"}` expectation at :68 only for systemd if launchd is out of scope (keep launchd unchanged).

- [ ] **Step 2: Run** `uv run -p 3.12 --extra dev pytest tests/test_web_server.py tests/test_install.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement.** The waiting tracker refreshes on its own `asyncio` loop in a background thread every `github_check_seconds`; request handlers only read its last snapshot. The page is layout B from the spec: left column running cards (phase bar `reading → coding → testing → CI → review → PR`, last command, how long ago, "looks stuck" mark), "Needs you", "Waiting", "Today"; right column the feed. It polls `/api/status` every `refresh_seconds` with `fetch`, builds every data node with `textContent`, and shows the `errors` and any `unknown` section as visible text. No external URLs.

- [ ] **Step 4: Run** the two test files, then the full suite and ruff. Expected: all pass. Then run it for real: `uv run -p 3.12 ctrlrelay web start -F -c <a test config pointing at a copy of state.db>` and `curl -s localhost:8787/api/status | python -m json.tool | head -40`; expected: the contract with real sessions.

- [ ] **Step 5: Docs.** `docs/configuration.md`: the `web:` block with every field and default. `docs/operations.md`: enabling on a host (set `web.host` to the LAN address, `ctrlrelay install systemd`, `systemctl --user enable --now ctrlrelay-web`), and that it is read-only and must not be exposed to the internet. Run every command written there once.

- [ ] **Step 6: Commit** `git commit -m "feat(web): serve the read-only status page and JSON API (#209)"`
