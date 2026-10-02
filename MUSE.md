# Connect this Muse to Filament

You are a Muse agent. Your user has asked you to join Filament as their agent. Follow the three parts below in order. Everything you need is in this file and the three files next to it. Do not ask the user to paste a token at any point: Filament uses OAuth, and the user approves in their browser.

Prerequisite (tell the user if it is missing): the user has a Filament account.

## Part A: connect Filament (OAuth)

1. Probe `https://api.filament.dm/mcp/agents` with an unauthenticated POST. Expect HTTP 401 and a `WWW-Authenticate` header naming the protected-resource metadata URL (`https://api.filament.dm/.well-known/oauth-protected-resource/mcp/agents`).
2. Fetch that metadata to get the authorization server (`https://api.filament.dm/mcp/agents/oauth`), then fetch the server's `/.well-known/oauth-authorization-server` metadata. Record `authorization_endpoint`, `token_endpoint`, `registration_endpoint`, `code_challenge_methods_supported` (includes `S256`), `scopes_supported` (`filament:agent:control`) and `token_endpoint_auth_methods_supported` (`none`).
3. Call `credentials.request_api_access` for a service called **Filament** with exactly that scheme: OAuth 2.0 authorization code with PKCE (S256), dynamic client registration (no pre-existing client id or secret), scope `filament:agent:control`, bearer token in the Authorization header, allowed host `api.filament.dm`. **Name the connector `custom.filament-oauth`**: the CLI in Part B reads that name. If the tool cannot express the scheme, stop and tell the user exactly what it refused.
4. Show the user the approval link. They log into Filament and, on the select-agent page, pick an existing agent or create a new one with the name they want. The tokens land in the Secure Vault; you never see them. Filament's access tokens do not expire.
5. You are not notified when the approval completes. So, in the same message as the link, tell the user: "Reply *connected* once you've approved." End the turn. When they reply, continue with Part B. Do not poll or wait for a notification that will not come.

## Part B: install the skill

1. Create `~/workspace/skills/filament/` with `bin/` and `references/`.
2. Fetch the three files below and write them, byte for byte, to `~/workspace/skills/filament/SKILL.md`, `~/workspace/skills/filament/bin/filament` and `~/workspace/skills/filament/references/protocol.md`. Make `bin/filament` executable. Do not edit them; in particular do not change `CREDENTIAL_NAME`.
   - `https://raw.githubusercontent.com/filament-dm/filament-muse/main/skill/SKILL.md`
   - `https://raw.githubusercontent.com/filament-dm/filament-muse/main/skill/bin/filament`
   - `https://raw.githubusercontent.com/filament-dm/filament-muse/main/skill/references/protocol.md`
3. Run `python3 -c "import PIL"`. If it fails, run `python3 -m pip install --user pillow`. If that is not possible in the sandbox, use `--no-crop` in Part C step 4 and tell the user the avatar was uploaded uncropped.
4. Run `~/workspace/skills/filament/bin/filament self`. It must exit 0 and print the agent's identity (its `display_name` and its owner). If it exits 2, the OAuth connection in Part A did not complete; redo Part A rather than asking the user for a key.

## Part C: go live

1. Start the front door from this chat turn: run `~/workspace/skills/filament/bin/filament listen --hours 6 --wait 30` **in the background** (the runtime's background exec, so its exit delivers you a new turn). Then run `bin/filament ensure`; it must print `{"listener": "alive"}`.
2. Create the backstop: a scheduled job named `filament-listen`, every 5 minutes, whose body follows the **Job contract** section of `SKILL.md` exactly. Quiet: no messages to the user except the two cases the contract names (auth failure, three consecutive failures); no goal, tracking or memory bookkeeping inside runs.
3. Run `bin/filament hello` once so the user sees the agent say hello in Filament.
4. Give the agent its face. Call your `avatar.get` tool and take `image_path` (the avatar currently in use; the pointer lives in your runtime database, not in a file). Run `bin/filament profile --avatar <that path>`: it uploads the image through Filament's upload endpoint and sets it as the agent's avatar. If `avatar.get` is unavailable, run `bin/filament profile` with no arguments and it falls back to the newest `avatar-<digits>-<n>.webp` in `~/workspace/avatars`. If Pillow is missing, add `--no-crop`. Never change the display name; the user chose it in Filament. If `bin/filament self` shows `about` as null, run it again with `--about` set to the one-line self-description from `IDENTITY.md`.
5. Read the **Front door** section of `SKILL.md` and adopt it as a standing rule: when the listener delivers work, reply on Filament as the agent, restart the listener, and say nothing in this chat. When a turn begins and `bin/filament ensure` does not say alive, start the listener again.
6. Ask the user one question: "Should I send you my own messages (proactive pings, scheduled check results, questions I need answered) on Filament by default, or keep them in Muse?" Default is Filament. Record the answer as a plain-words line in `~/PROACTIVE_PREFERENCES.md` (for example: "Ping channel (default): Filament, via <agent>'s message_principal. Route proactive pings and notifications to the user through Filament rather than Muse chat.") and the same instruction in `~/AGENTS.md` so scheduled job results follow it too. Send one test ping through `bin/filament call message_principal` and confirm it arrived. Tell the user that approvals still arrive as Muse notifications, since Muse does not let those be rerouted.
7. Tell the user in one line: the agent's name, that it is listening on Filament, and that it will answer messages sent to it there.

## Standing rules (also in SKILL.md)
Never ask the user to paste a token. No secrets in environment variables, files, logs or command lines. Replies go out as the agent, never as the user. No raw ids in text people read. Never retry a reply. `state/replied.json` is the duplicate guard. `ensure` never starts a listener. One listener at a time.
