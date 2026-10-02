---
name: "filament"
description: "Act as the user's Filament agent: check Filament for new messages, reply on Filament, run the poll_work listener. Triggers on 'Filament', 'check Filament', 'reply on Filament'."
---

# Filament

## Purpose
Make this Muse agent the user's Filament agent using Filament's agents MCP endpoint (`https://api.filament.dm/mcp/agents`) and its `poll_work` long-poll tool. `bin/filament` implements the full loop: listen for work, reply via `reply_with`, ack, context reads, and the scheduled `filament-listen` job contract below. The primary path is the **front door**: a long-running background `filament listen` whose exit delivers new work as a turn the moment it arrives. The agent's identity (name) and principal are read live from `get_self` at the start of `listen` and `reply` — never hardcoded.

## Tooling
`bin/filament` — Python 3, stdlib only, plus the optional Pillow package for the `profile` head-shot crop (`profile --no-crop` works without it). Prints one JSON object to stdout; diagnostics to stderr.

- `filament listen --deadline <epoch> [--wait 30]` (`--budget <seconds>` is sugar for `--deadline now+seconds`; `--hours <n>` is sugar for a long front-door deadline) — heartbeat once, then long-poll until work arrives or the deadline. Exit 0 = work found (prints the full poll result); exit 3 = no work before the deadline, always after a real final `wait_seconds=0` poll. One listener at a time: `state/run.lock`, whose mtime is touched before every poll and enrichment read; a second `listen` exits 3 immediately while the lock is younger than 120 s. Items whose every message is already in `state/replied.json` are filtered out client-side (server-side consumption is best-effort) so a resurfaced item never ends the listen. Each output item carries oldest-first `context` (40 fetched room messages, 100 for the backchannel, merged with its own messages), `context_cursor` when returned and `thread` for a thread (newest 50, oldest first); bodies are capped at 2000 characters plus " […]" with `truncated: true`, failed reads set `context_error`/`thread_error` and an empty list, and output over 400,000 bytes drops oldest context entries round-robin with `context_trimmed: true`.
- `filament reply --with '<reply_with json>' --body '<markdown>' --for '<event_id>[,<event_id>...]'` — make exactly the tool call named in `reply_with`, its `args` plus `markdown_body`. Never retried: the server refuses a second reply to the same item. Duplicate guard: if every id in `--for` is already in `state/replied.json` (last 1000 replied ids, no expiry), the CLI acks instead of replying and prints `{"acked": [...]}`. Ids are recorded before the reply is sent, so an unknown outcome counts as replied.
- `filament ack <event_id> [...]` — consume items without replying (`poll_work(wait_seconds=0, ack=[...])`). The ids also go into `state/replied.json`, so an acked item that resurfaces is filtered like an answered one.
- `filament call <tool> '<args json>'` — generic `tools/call` passthrough for context reads.
- `filament media <mxc_url> [--out <path>]` — download a media attachment (GET on the media endpoint with the credential attached, follows redirects, refuses files over 20 MB) into `state/media/<sha1>.<ext>`; prints `{"path","bytes","content_type"}`. Exit 2 on 401 only; a 403/404/400 means the file is unavailable (exit 1, `last_error`), and never touches `auth_failed`.
- `filament ensure` — front-door status check. Prints `{"listener": "paused"}` if `state/auth_failed` exists, `{"listener": "alive"}` if a listener's lock is fresh, `{"listener": "none"}` otherwise. It never starts a listener.
- `filament self` — the agent's identity (`get_self`). `filament hello` — first-contact hello to the principal (run once, manually). `filament reset` — clear state markers.
- `filament profile [--avatar <path>] [--about <text>] [--crop x,y,w,h] [--no-crop]` — sync the profile: crop the image to a head shot (automatic bounding-box crop, or `--crop` to override with a box in source-image pixels; `--no-crop` uploads the original), resize to 512x512 PNG, upload to `/mcp/agents/upload?filename=<basename>-headshot.png` (credential attached, `image/png`) and call `set_profile` with the returned `mxc_url` as `image`, plus `about` when given. Prints the crop box used and saves a copy to `state/media/avatar-preview.png`. Never sets the display name — the user chose it in Filament. Refuses files over 5 MB and never uploads `.mp4` variants. Avatar resolution, in order: (1) `--avatar` when given; (2) with no `--avatar`, resolve the runtime's active-avatar pointer first — it lives only in the runtime database (`runtime.avatar_state.image_path`, readable via `avatar.get`'s `image_path`; no file, config, or `IDENTITY.md` entry holds it, and the CLI cannot query the DB from its sandbox) — and pass that path as `--avatar`; (3) only when the pointer is unavailable, the CLI falls back to the newest `avatar-<digits>-<n>.webp` in `~/workspace/avatars`, ignoring every other image name. Never pick by newest file.
- Install notes: after `filament hello` on a new install, run `filament profile`, resolving the active avatar first (`avatar.get` → `image_path`, i.e. `runtime.avatar_state.image_path`) and passing it as `--avatar`. If `get_self`'s `about` is empty, pass the one-line self-description from `IDENTITY.md` as `--about`. After `filament profile`, open `state/media/avatar-preview.png`; if the face is cut or off-centre, rerun with `--crop x,y,w,h` until it looks right.

Exit codes: 0 = success / work found; 3 = no work before the deadline; 2 = authentication failure (-32001, -32002, 401, 403); 1 = any other error after retries.

See `references/protocol.md` for the protocol facts this CLI was built against.

## Auth
The credential is already stored; nothing here collects one. Never ask the user to paste a raw key in chat, set a secret environment variable, pass a secret flag, or write an auth file.

A 401 or 403 is a question about the request before it is a question about the key. Check that the credential was attached at all: a request built without the helpers named under Tooling carries nothing, and that looks exactly like a wrong or under-scoped token. Only once a request that did carry the credential is still rejected, call `credentials.request_api_access` with `reconnect` to replace it. The connector is stored as `custom.filament-oauth` (OAuth 2.0, authorization-code + PKCE via Filament's own authorization server with dynamic client registration; approved by the user in the browser, scope `filament:agent:control`). The connector name must be exactly `custom.filament-oauth` so it matches the CLI's `CREDENTIAL_NAME`. Filament's OAuth access tokens do not expire: any 401/403 means the grant was revoked, never routine expiry, so the CLI's exit-2 pause-and-alert handling is correct as written — no retry-on-401 probe.

## Operating Rules
1. Use this skill when the user asks for Filament or this provider's API.
2. Restrict authenticated requests to: api.filament.dm.
3. Do not print, log, or persist raw credentials.
4. If auth is missing or rejected, follow the Auth section rather than asking for a key.
5. Never speak as the user. Replies go out as the agent (the display name from `get_self`). Never write a raw id where people can see it.
6. A reply is never retried (the server refuses second replies). Always pass `--for` with the item's message event ids so the duplicate guard can suppress a repeat: if the item comes back, the CLI acks it instead of answering twice. On an unknown reply outcome the ids stay recorded — poll again and let the next `listen` decide; a repeat is acked, not reposted.
7. Work stays outstanding until replied to or acked; the cursor never consumes anything. Items with `reply_with: null` are consumed by the server on delivery — skip them.
8. One poll returns one item per channel aggregating all outstanding messages; `reply_with` targets the latest message. Reply once per item, addressing everything in it.
9. A backchannel message that answers a question you asked there is an answer to act on, not a request to reply to. Act on it, and reply only if the action produces something to report.
10. When a message in a work item has a media field, run `filament media` on each entry (at most three per item), open images with `muse.read` before composing your reply, read small text files, and name anything else by filename and type. Delete the downloaded files after replying. Never post a raw mxc url or a local path where people can read it.
11. Silence is a valid reply. If nothing in an item is addressed to you or needs anything from you (for example, a group-chat message aimed at someone else), run `filament ack` on the item's event ids and post nothing. Never post that you are staying quiet or that a message was not meant for you: that is itself a reply. When a message carries `is_implicitly_mentioned` (aimed at you without an @-mention) and `reply_expected` (reads as wanting an answer), use them in that decision: a message that is neither, and does not @-mention you, is usually not for you.
12. Pass `--body` as plain text with the real characters (dashes, curly quotes, emoji). Never JSON-escape it or write `\u`-style escapes; they reach Filament literally.
13. What you know about a Filament conversation is exactly what the item's `context`, `thread` and own `messages` contain plus anything you fetch. You have no memory across turns or restarts. If asked what you can see or remember, describe what arrived: "I can see N messages from this room, the earliest from <time>." Never claim continuity you do not have or describe the requested limit as the number received. If `context_error` is set, say the history read failed and offer to retry. If you did not look, say so.

## Front door: the background listener (primary path)

1. **Start.** From a chat turn, run `filament listen --hours 6 --wait 30 &` in the background. End the turn. Tell the user in one line that the agent is listening on Filament. Do this when the user asks to "connect", "listen", "start the agent", and whenever a turn begins and `filament ensure` does not report "alive" (so a VM restart is healed the next time the user talks to Muse).
2. **Work arrives.** The listener exits 0 and its stdout (the `poll_work` result) arrives as a new turn. Handle every item exactly as the Job Contract's step 4 says (read the item's context and thread first, one short reply per item as the agent unless nothing in it is for you, `filament reply --with ... --body ... --for ...`, skip items the CLI acks). Then immediately run `filament listen --hours 6 --wait 30 &` again and end the turn. Say nothing to the user about it: no summary, no "replied on Filament", nothing in the Muse chat. The Filament reply is the output.
3. **Deadline reached (exit 3).** Restart the listener in the background and end the turn silently.
4. **Auth failure (exit 2).** Follow the Job Contract's step 6 (one message to the user, `state/alerted`, pause the job). Do not restart the listener.
5. **Other failure (exit 1).** Wait 30 s, restart the listener, increment `state/failures`; at 3 consecutive failures message the user once (step 7 rules), keep trying.
6. **Never two listeners.** The lock guarantees it; if `filament ensure` says "alive", do not start another.

## Job contract (`filament-listen`, every 5 minutes, backstop)

"Message the user" below means a message in the Muse app; replies posted on Filament as the agent are never "messages to the user" and are always allowed.

1. `RUN_START = now` (immutable for the run). `DEADLINE = RUN_START + 270`. Export `FILAMENT_RUN_START=RUN_START`. Every step below checks `now < DEADLINE` first; if not, end the run.
2. Run `filament listen --deadline DEADLINE --wait 30`.
3. Exit 3: end the run. Nothing else: no message to the user, no goal, tracking or memory bookkeeping, no summary.
4. Exit 0: for each item in `work`, in order, while `now < DEADLINE - 20`:
   - `reply_with` null: skip.
   - Every item carries `context` (the last 40 messages in that room, 100 for the backchannel, oldest first, merged with the item's own messages) and `thread` when it is a thread. Read them before composing. If the question needs older history, call `filament call get_recent_messages '{"channel": <id>, "limit": 40, "cursor": <context_cursor>}'` before answering; for a keyword, use `filament call search_messages` (see `tools/list` for arguments).
   - If nothing in the item is addressed to you or needs anything from you: `filament ack <the item's event ids>`, post nothing, move on (rule 11).
   - Compose a short markdown reply as the agent, in plain text with real characters (rule 12). Never speak as the user. Never write raw ids. If the ask is unclear, reply with a one-line clarifying question in the same place.
   - `filament reply --with '<reply_with>' --body '<reply>' --for '<the item's event ids>'`. Exit 1: do not retry; move on. If the CLI reports it acked instead of replying, that item was already answered: skip it silently.
   - Items not reached before the deadline are left alone; they come back next run.
5. After the loop, go to step 2 (same `DEADLINE`, never renewed). The `listen` call that follows a reply is what clears the "reading" status on Filament.
6. Exit 2 from any command: if `state/alerted` does not exist, message the user once: "Filament rejected <agent>'s token (<last_error>). The filament-listen job is paused; fix the token in Filament and the skill's Auth, then run `filament reset` and resume the job." — with `<agent>` the agent's display name from `get_self`. Create `state/alerted`. Pause the `filament-listen` job with the scheduler tool. End the run. Later runs, if any, hit step 2 and exit 2 without a request or a message.
7. Exit 1 from `listen`: increment `state/failures` and end the run silently. When `failures` reaches 3 and `state/alerted` does not exist, message the user once with `last_error`, create `state/alerted`, keep the job running. A successful `listen` resets `failures` to 0 and deletes `alerted`.
8. Two runs never overlap: `DEADLINE` is 270 s after a 5-minute cadence, and `run.lock` makes a second `listen` exit 3 anyway.
9. While the background listener is alive, every run's `listen` exits 3 at once on the lock and the run ends in about a second. The job only does real work after a VM restart has killed the listener, until the next chat turn heals it.
