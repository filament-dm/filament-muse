# Filament agents MCP — protocol facts

Verified 30 Sep 2026 from inside this VM. Enough detail here to re-derive `bin/filament`.

## Endpoint
- Base: `https://api.filament.dm/mcp/agents`. MCP Streamable HTTP, JSON-RPC 2.0, single POST to the base.
- Headers: `Authorization: Bearer <token>`, `Content-Type: application/json`, `Accept: application/json, text/event-stream`.
- Handshake once per process: `initialize` (protocolVersion `"2025-03-26"`; the server answers `2024-11-05`, which is fine) → `notifications/initialized` (empty 2xx) → `tools/call`. No session id is issued today; if an `Mcp-Session-Id` header or `result._sessionId` ever appears, echo it back on later requests.
- Responses are JSON today. If a response has `Content-Type: text/event-stream`, read the `data:` lines and use the last complete JSON object.
- Tool results are `result.content[0].text` containing a JSON string. Server rejections arrive as HTTP 200 with a JSON-RPC `error` object: check `error` on every response. `-32001` = token invalid/revoked. `-32002` = agent still reserved (owner has not finished naming it). HTTP 401/403 = same as -32001.

## OAuth (how the credential is obtained)
- An unauthenticated POST returns 401 with `WWW-Authenticate: MCP realm="filament-agents", resource="https://api.filament.dm/.well-known/oauth-protected-resource/mcp/agents", scope="filament:agent:control"`.
- That protected-resource metadata names the authorization server `https://api.filament.dm/mcp/agents/oauth`, whose metadata (`/.well-known/oauth-authorization-server` under it) advertises: `authorization_code` and token-exchange grants, PKCE `S256`, `token_endpoint_auth_methods_supported: ["none"]` (public clients), dynamic client registration at `/mcp/agents/oauth/register` (RFC 7591), scope `filament:agent:control`.
- The authorize step sends the user to Filament's login, then to a select-agent page where they pick or create the agent, then a token is issued. Access tokens have **no expiry**; a 401/403 means revocation.

## poll_work
`poll_work(cursor?, ack?, wait_seconds=30, max_items=10)`: blocks until work arrives or `wait_seconds` elapses (server max 60). Returns `{work: [{channel_id, thread_id, is_backchannel, messages: [{event_id, sender, body, ts}], reply_with: {tool, args} | null}], cursor, next_poll_ms, truncated, acknowledged}`.

- **Work stays outstanding until it is replied to or acked — best-effort.** The `cursor` only narrows the server's scan and never consumes anything. So an item that was fetched but not answered comes back on the next poll, whatever cursor is passed. This is the crash-safety guarantee; the skill does not need its own. BUT server-side consumption is unreliable: the server's record of issued work appears to be in memory per worker process (short life) and the API runs several workers, so a reply can post without consuming the item, which then comes back on the next poll — observed twice 30 Sep 2026 (~09:54–09:56 and ~10:04 EDT; in the second case a reply whose consumption was verified same-session AND cross-session resurfaced 4 minutes later). The skill therefore keeps its own record: `state/replied.json`, the last 1000 replied `event_id`s with timestamps (no expiry — an id answered once is never answered again while recorded).
- One poll returns **one item per channel aggregating all outstanding messages**; `reply_with` targets the latest message in the item (verified 30 Sep 2026: four outstanding messages arrived as one item, `in_reply_to` = the newest). Reply once per item, addressing everything in it.
- Replying via `reply_with` USUALLY marks the item read (verified same-session and cross-session on 30 Sep), **but only best-effort** — see the first bullet. **A second reply to the same item is refused by the server.** So a reply is never retried, and before replying to any item the CLI checks whether every `event_id` in that item is already in `state/replied.json`; if so it **acks** the item instead of replying. Ids are recorded in `replied.json` BEFORE the reply is sent: an unknown reply outcome (timeout, connection reset) counts as replied, so a repeat of the item is acked, not answered twice.
- `listen` additionally filters client-side: an item whose every message id is in `state/replied.json` never ends the listen (it is skipped and its ids are offered as `ack` on subsequent polls). Without this, a resurfaced-but-answered item would make every `listen` exit 0 immediately in a tight loop.
- `ack` takes message event ids for work decided NOT to answer. In testing (30 Sep 2026, twice, on genuinely outstanding ids) the server returned `acknowledged: 0` and the items kept coming back — the `ack` parameter appears non-functional against this beta. The client-side `replied.json` guard is the reliable duplicate suppression; the CLI still passes `ack` opportunistically.
- `mark_read` called by the agent fails with JSON-RPC `-32603` ("Tool execution failed"); it is not part of the consumption flow.
- `wait_seconds` must be an **integer**; fractional values are rejected with JSON-RPC `-32602` (the CLI casts to int).
- Items with `reply_with: null` are consumed by the server on delivery. Skip them.
- `truncated: true` means more work is pending: poll again immediately with the returned cursor (`next_poll_ms` will be 0).
- When `poll_work` delivers work, the server sets a "reading a new message" status in that room. The agent's next `poll_work` call clears it; it expires by itself after 60 s. The reply does not clear it.
- Verified: 30 s and 60 s waits both hold through Meta's egress proxy; a message sent to the backchannel returned from a waiting poll within ~1 s and the reply posted 6 s after the message.

## Reply tools
- `post_message(channel, markdown_body, in_reply_to?)`, `reply_in_thread(message_id, markdown_body)`. `reply_with.args` already carries everything except `markdown_body`.
- Bodies are markdown only; no HTML; no `---` rules. Mentions are `[Name](member:@mxid)`; never write a raw id where people can see it.

## Context tools
`get_recent_messages(channel, limit, cursor?)`, `get_thread(message_id)` where `message_id` is the thread root, i.e. the work item's `thread_id`; `get_user_profile`, `get_self`, `message_principal(markdown_body)`.

## Presence
`POST https://api.filament.dm/mcp/agents/heartbeat` (same bearer, plain HTTP POST, empty body, not a tool) sets presence online for ~30 s. A polling agent cannot stay online; send one heartbeat at the start of each `listen` so presence flickers on.

## Muse runtime constraints
Scheduled jobs fire every 5 minutes on the second; a run may hold at least 10 minutes; background processes **started inside a scheduled run** are SIGTERMed at run end; each run is an agent turn; nothing survives a VM restart except files under `~/workspace`.

Verified 30 Sep 2026: a command started in the background **from the chat** (exec `background: true`) survives between turns, and when it exits Muse receives a new turn carrying its full stdout ("Background exec command tool output"). This is the front door: a long-running `filament listen` in the background delivers work to Muse the moment it arrives. It dies on a VM restart, so the 5-minute job stays as the backstop.

Also verified 30 Sep 2026: a process the CLI detaches itself (the old `filament ensure` detached start, since removed) is NOT tracked by the chat runtime — it runs fine, but its exit does **not** deliver a turn. `filament ensure` is now the status check only (`alive`/`paused`/`none`) and never starts a listener. From a chat turn, the front-door listener must be started via the background-exec form (`filament listen --hours 6 --wait 30` in the background).

## CLI contract (bin/filament)
- `listen` attaches oldest-first `context`: 40 fetched room messages, 100 for the backchannel, deduplicated by `event_id` and merged with the item's own messages (item fields win); `context_cursor` exposes the read cursor when returned. `thread` contains the newest 50 thread messages, oldest first. Bodies are capped at 2000 characters plus " […]" with `truncated: true`; output over 400,000 bytes loses oldest context entries round-robin across items and sets `context_trimmed: true`.
- Reads fail independently: a room failure sets `context: []` and `context_error`, a thread failure sets `thread: []` and `thread_error`; auth errors propagate. Missing `channel_id` sets `context_error: "item has no channel_id"` without a read. Enrichment uses the run deadline, or `now + 20` seconds after the final poll.
- Socket timeout: `wait_seconds + 15` for `poll_work`, 30 s for everything else.
- Retryable errors (HTTP 5xx, 429, connection errors, malformed JSON, JSON-RPC errors other than -32001/-32002): up to 3 retries with 2 s, 4 s, 8 s backoff, never past the deadline; then exit 1 with `state/last_error` written. Auth errors exit 2 immediately and write `state/auth_failed`.
- Exit codes: 0 = success / work found; 3 = no work before the deadline; 2 = auth failure; 1 = other error. When the deadline is under 15 s away, `listen` makes one final `wait_seconds=0` poll rather than exiting blind, so exit 3 always reflects a real server check.
- One listener at a time: `state/run.lock` holds the run start (from `FILAMENT_RUN_START` or now); its **mtime** is touched before every poll and enrichment read. A second `listen` exits 3 immediately when the lock's mtime is younger than 120 s and the content is not its own run start. The lock is removed on exit, including on SIGTERM.
- `state/` holds `run.lock`, `auth_failed` (epoch, only after exit 2), `alerted` (once the user has been told), `failures` (consecutive exit-1 count), `last_error` (text), `replied.json` (last 1000 replied event ids with timestamps, no expiry). No cursor is persisted: every run starts without one.
- `filament listen --hours <n>` is sugar for `--deadline now+n*3600` (front door).
- `filament reply --for '<id>[,<id>...]'`: when every id is already in `replied.json`, the CLI acks instead of replying and prints `{"acked": [...]}`.
- `filament ensure`: `{"listener": "paused"}` if `auth_failed` exists; `{"listener": "alive"}` if the lock's mtime is younger than 120 s; `{"listener": "none"}` otherwise. It never starts a listener.
- 5-second floor: when a poll returns no fresh items but only already-answered (client-side-filtered) items, `listen` sleeps 5 s (capped to the time left) before the next poll instead of `next_poll_ms` (~1 s). Until Filament fixes server-side read-marking, this stops the 1-second hot poll loop.
