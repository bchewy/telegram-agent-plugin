# Telegram Agent Plugin

Use your personal Telegram account from your coding agent. The same bundle
works in three kinds of clients:

- **Codex** — installed as a plugin from a local marketplace
- **Grok Bot / Cursor (custom MCP)** — registered as a plain local stdio MCP server
- **Any Agent Plugins 1.0.0 client** — loaded from the portable `plugin.json` + `mcp.json`

This plugin lets your agent:

- summarize chats
- search recent messages live, or search cached chat history for repeat/broad/old lookups
- sync recent chat history into a local cache once, then summarize, search, and aggregate locally to reduce repeated Telegram API calls (see the cache section below — first sync covers the newest N messages; older backfill is not yet exposed)
- draft and send replies
- triage unread threads
- manage groups/channels
- work with media, drafts, reactions, polls, and scheduled messages
<img width="878" height="1071" alt="image" src="https://github.com/user-attachments/assets/6f6322b8-253b-458a-8bc2-cf7e5ed62932" />



## Fast setup

If you just want the shortest path:

1. Create Telegram API credentials at `my.telegram.org/apps`
2. Run the login wizard once (`codex-telegram login`)
3. Hook the server into your client:
   - **Codex**: install the plugin from your local marketplace, then use `@Telegram` in a fresh thread
   - **Grok Bot / Cursor**: add a custom stdio MCP server that runs `uv run --project <repo>/telegram/mcp_server codex-telegram serve`

The exact commands are below. The Python CLI is still named `codex-telegram`
for historical reasons; it is the same server regardless of which client
launches it.

## Requirements

- Python 3.11+
- `uv`
- a real Telegram user account
- your own Telegram `api_id` and `api_hash`
- for the Codex path: Codex CLI / Codex app
- for the Grok Bot / Cursor path: any client that can register a local stdio MCP server

## Step 1: Create Telegram API credentials

Go to [https://my.telegram.org/apps](https://my.telegram.org/apps)

This is the exact flow:

1. Log in with your phone number
2. Enter the Telegram login code
3. Open `API development tools`
4. Fill the form with something sane, for example:
  - `App title`: `Telegram Agent Plugin`
  - `Short name`: `codextelegramplugin`
  - `Platform`: `Desktop`
  - `URL`: `https://github.com/bchewy/telegram-agent-plugin`
  - `Description`: `Personal Telegram MCP`
5. Submit
6. Copy:
  - `api_id`
  - `api_hash`

Important:

- this is **not** BotFather
- you want `my.telegram.org/apps`
- the plugin expects **your** API credentials, not a shared developer key

## Step 2: Log in once

This plugin uses Telegram user-account MTProto auth, not the Bot API. Login is
a one-time CLI step; after it succeeds, the session is stored locally (keyring
or encrypted file) and the MCP server does **not** need `api_id`/`api_hash` in
its environment.

From a repo checkout:

```bash
uv run --project ./telegram/mcp_server codex-telegram login
```

You will be prompted for:

1. `Telegram API ID`
2. `Telegram API hash`
3. phone number in E.164 format
4. the Telegram login code
5. 2FA password, if your account uses it

Successful output looks like:

```text
{'ok': True, 'storage': 'keyring', 'user_id': 123, 'username': 'yourname', 'display_name': 'Your Name', 'phone': '+15555555555'}
```

If you already installed the plugin into Codex and want to authenticate the
installed bundle instead of a checkout, see the Codex section below.

---

## Using with Codex

### Codex marketplace model

A Codex marketplace is a catalog of plugins. Its `interface.displayName` is the
dropdown label in Codex, while this plugin's `interface.displayName` is the
installable item shown inside that marketplace.

For a single local selector, keep all local plugin entries in one user-level
marketplace at `~/.agents/plugins/marketplace.json`. Do not keep a repo-local
`.agents/plugins/marketplace.json` active for this checkout unless you
intentionally want Codex to show this repository as a separate marketplace.

This repo's plugin bundle is `telegram/`. In the shared `Local Plugins`
marketplace, the plugin should be installed as:

```bash
codex plugin add telegram@local
```

The matching WhatsApp plugin uses the same model: one `Local Plugins`
marketplace, separate `telegram` and `whatsapp` plugin entries.

For checkouts under `~/dev`, the relevant `plugins` entries look like this.
Preserve any other plugins already present in your local marketplace file.

```json
{
  "name": "local",
  "interface": {
    "displayName": "Local Plugins"
  },
  "plugins": [
    {
      "name": "telegram",
      "source": {
        "source": "local",
        "path": "./dev/telegram-agent-plugin/telegram"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Productivity"
    },
    {
      "name": "whatsapp",
      "source": {
        "source": "local",
        "path": "./dev/codex-whatsapp-plugin/whatsapp"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Productivity"
    }
  ]
}
```

### Install the plugin in Codex

This repo is designed to be installed from the shared local marketplace instead
of registering itself as a separate marketplace.

If your `~/.agents/plugins/marketplace.json` already includes this checkout,
install the plugin with:

```bash
codex plugin add telegram@local
```

If the local marketplace does not include it yet, add a `telegram` entry that
points at this repo's `telegram/` directory, then rerun the command above. Keep
the marketplace name as `local` and the display name as `Local Plugins` if you
want it grouped with your other local plugins.

Open a fresh Codex session after installing. Old threads can miss newly
installed plugin, skill, and MCP context.

### Verify the plugin is installed

In Codex:

1. Open `/plugins`
2. Open `Telegram`
3. Make sure it looks like the screenshot above
4. Make sure the bundled skills are enabled

You should see:

- `Telegram Login`
- `Telegram Summarize`
- `Telegram Triage Unread`
- `Telegram Search`
- `Telegram Aggregate`
- `Telegram Send`
- `Telegram Manage Groups`
- `Telegram Media Inspect`
- bundled MCP server: `telegram_personal`

Important: start a **fresh thread** after installing. Old threads can miss newly installed plugin/skill context.

### Authenticate the installed bundle

If you skipped the checkout login in Step 2 and want to log in against the
bundle Codex installed, run this exact command:

```bash
uv run --project "$(
python3 - <<'PY'
from pathlib import Path

candidates = [
    p for p in Path.home().glob('.codex/plugins/cache/*/telegram/*/mcp_server')
    if p.is_dir()
]
if not candidates:
    raise SystemExit('No installed Telegram plugin bundle found under ~/.codex/plugins/cache')
print(max(candidates, key=lambda p: p.stat().st_mtime))
PY
)" codex-telegram login
```

### Test it in Codex

Start a fresh Codex thread and try one of these:

```text
@Telegram summarize my unread Telegram messages from today
```

```text
@Telegram find Telegram messages from Alice about the launch
```

```text
@Telegram cache the Design chat, then search it for launch blockers from last month
```

```text
@Telegram draft a Telegram reply to the design thread
```

```text
@Telegram watch this Telegram bubble and transcribe what it says
```

Or call the bundled skills directly:

```text
$telegram:telegram-summarize summarize my unread Telegram messages from today
```

```text
$telegram:telegram-search find messages from Alice about launch
```

```text
$telegram:telegram-aggregate show cached weekly message volume for the Design chat this quarter
```

```text
$telegram:telegram-send draft a reply to the latest message in Saved Messages
```

### Bundled skills

Codex registers the skills under the plugin namespace:

- `telegram:telegram-login`
- `telegram:telegram-summarize`
- `telegram:telegram-triage-unread`
- `telegram:telegram-search`
- `telegram:telegram-aggregate`
- `telegram:telegram-media-inspect`
- `telegram:telegram-send`
- `telegram:telegram-manage-groups`

---

## Using with Grok Bot / Cursor (custom MCP, stdio)

The bundled server is a normal local stdio MCP server, so any client that
supports custom MCP servers (Grok Bot, Cursor, Claude Desktop, etc.) can use it
directly — no Codex plugin machinery required.

### 1. Clone the repo and sync the server

```bash
git clone https://github.com/bchewy/telegram-agent-plugin.git
cd telegram-agent-plugin
uv sync --project ./telegram/mcp_server
```

### 2. Log in once

```bash
uv run --project ./telegram/mcp_server codex-telegram login
```

Same wizard as Step 2 above: API ID and hash from `my.telegram.org/apps`,
phone number, login code, 2FA password if set. The session lands in the OS
keyring or in `~/.config/codex-telegram/session.enc`.

### 3. Register the custom MCP server

Add a local **stdio** MCP server in your client with:

- **command**: `uv`
- **args**: `run --project /absolute/path/to/telegram-agent-plugin/telegram/mcp_server codex-telegram serve`
- **env** (optional):
  - `CODEX_TELEGRAM_MASTER_KEY` — only if your session is stored in the
    encrypted file (no OS keyring) and the key is not otherwise available to
    the server process
  - `TELEGRAM_MTPROXY_HOST` / `TELEGRAM_MTPROXY_PORT` / `TELEGRAM_MTPROXY_SECRET`
    — only on networks that block direct Telegram MTProto (see troubleshooting)

Telegram API credentials (`TG_API_ID` / `TG_API_HASH`) are **not** required in
the MCP env after login; the stored session already carries them.

In JSON-config clients (for example Cursor's `mcp.json`), the equivalent entry
looks like:

```json
{
  "mcpServers": {
    "telegram_personal": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/absolute/path/to/telegram-agent-plugin/telegram/mcp_server",
        "codex-telegram",
        "serve"
      ]
    }
  }
}
```

Use an absolute path for `--project`. Custom MCP servers are often spawned
with an unrelated working directory, so relative paths break.

### 4. Verify

From the shell:

```bash
uv run --project ./telegram/mcp_server codex-telegram whoami
```

From the agent, in a fresh conversation, ask it to call the `get_me` tool (or
`telegram_diagnostics` for a fuller runtime/auth/cache report). You should see
your own account's user id, username, and display name.

### Troubleshooting custom MCP setups

- **Tools error with "no session found"**: the server subprocess cannot see
  the session that `login` stored. Make sure the client runs the server as the
  same OS user that ran `login`. If the session went to the encrypted file
  instead of the keyring, set `CODEX_TELEGRAM_MASTER_KEY` in the server's env
  (and `CODEX_TELEGRAM_CONFIG_DIR` too, if you overrode it during login).
- **Server registered but tools don't show up**: start a fresh thread /
  conversation after adding the MCP server. Most clients only load new servers
  into new sessions.
- **Connection hangs or times out on restricted networks**: some hosted or
  sandboxed environments block direct Telegram MTProto connections (Telegram's
  data centers are raw-TCP endpoints, not HTTPS). In that case Telethon needs a
  proxy path. This server supports an MTProxy configured entirely via env vars:
  set `TELEGRAM_MTPROXY_HOST`, `TELEGRAM_MTPROXY_PORT`, and
  `TELEGRAM_MTPROXY_SECRET` together (on both the `login` command and the MCP
  server env). Treat the proxy secret like a credential: pass it through your
  client's env configuration, never commit it. For SOCKS or other proxy types,
  see the [Telethon proxy docs](https://docs.telethon.dev/en/stable/basic/signing-in.html#signing-in-behind-a-proxy).
- **First `uv run` is slow**: `uv` resolves and builds the project virtualenv
  on first launch. If your client enforces a short startup timeout, run
  `uv sync --project ./telegram/mcp_server` once beforehand.

---

## Shared backend: one Telegram connection for many agents

Telegram allows exactly one MTProto client per auth key. If two processes use
the same stored session concurrently, Telegram revokes the key
(`AUTH_KEY_DUPLICATED`) and you have to log in again. That is exactly what
happens when several agent sessions (or hosts) each spawn their own
`codex-telegram serve`.

To prevent this, `codex-telegram serve` is a thin stdio proxy by default:

- It holds **no** Telegram connection. It relays JSON-RPC from the MCP host to
  a shared backend over a unix socket (created `0600` inside a `0700`
  `run/` dir next to the session file).
- The shared backend (`codex-telegram backend`) owns the **only** Telethon
  client. It is started on demand by the first proxy that needs it and is
  single-instance via an `flock` on `run/backend.lock` — concurrent starts
  collapse to one process, extra backends exit immediately.
- If the backend dies, the next request from any proxy restarts it (stale
  sockets are cleaned up by the new lock holder, so clients never overlap).
- At startup each proxy also terminates leftover *legacy* per-agent
  `codex-telegram serve` processes from before this design, matched
  structurally against `/proc/<pid>/cmdline` and the process environment so it
  can never hit an unrelated process, another proxy, or a serve bound to a
  different config dir. Set `CODEX_TELEGRAM_LEGACY_CLEANUP=0` to disable the
  sweep.

Nothing changes in how you register the server: clients still launch
`codex-telegram serve` over stdio.

Environment variables (all optional):

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODEX_TELEGRAM_SHARED_BACKEND` | `1` | Set to `0` to run `serve` as a direct stdio server with its own Telegram client (old behavior). Only safe when exactly one agent host uses the session. `TELEGRAM_MCP_SHARED_BACKEND` is an accepted alias. |
| `CODEX_TELEGRAM_BACKEND_SOCK` | `<config dir>/run/backend.sock` | Unix socket the backend listens on. |
| `CODEX_TELEGRAM_BACKEND_LOCK` | `<config dir>/run/backend.lock` | Single-instance lock file; holds the backend pid. |
| `CODEX_TELEGRAM_BACKEND_LOG` | `<config dir>/run/backend.log` | Backend stdout/stderr log. |
| `CODEX_TELEGRAM_BACKEND_START_TIMEOUT` | `30` | Seconds a proxy waits for the backend to come up before failing the request. |
| `CODEX_TELEGRAM_PROXY_TIMEOUT` | `600` | Per-request timeout (seconds) between proxy and backend. |
| `CODEX_TELEGRAM_LEGACY_CLEANUP` | `1` | Set to `0` to skip the legacy-serve sweep at proxy startup. |

The backend inherits the environment (config dir, master key, API
credentials) of whichever proxy starts it, so give every host the same
`CODEX_TELEGRAM_*` configuration.

### Troubleshooting the shared backend

- **Restart the backend**: kill the pid stored in the lock file; the next
  tool call respawns it automatically.

  ```bash
  kill "$(cat ~/.config/codex-telegram/run/backend.lock)"
  ```

- **Verify there is exactly one backend / Telegram connection**:

  ```bash
  cat ~/.config/codex-telegram/run/backend.lock   # backend pid
  pgrep -af 'codex_telegram backend'              # must list exactly one process
  ss -xlp | grep backend.sock                     # exactly one listener
  ```

- **Backend fails to start**: the proxy reports
  `telegram backend did not start within 30s; check .../run/backend.log`.
  Read that log — typical causes are a missing session (run `login`) or a
  blocked MTProto connection (see the MTProxy section).

---

## Using with Agent Plugins clients

The installable package is the `telegram/` directory. It ships two manifest
sets side by side, so the same directory loads in Codex and in any client that
implements the portable [Agent Plugins 1.0.0](https://agent-plugins.org/specification)
format:

| File | Consumer | Purpose |
| --- | --- | --- |
| `plugin.json` | Agent Plugins clients | Portable manifest (`$schema` = `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`, closed field set) |
| `mcp.json` | Agent Plugins clients | Portable MCP config (`stdio` server launched via `uv`, `${PLUGIN_ROOT}`/`${PLUGIN_DATA}` placeholders) |
| `skills/<name>/SKILL.md` | Both | Agent Skills, discovered as immediate children of `skills/` by both formats |
| `.codex-plugin/plugin.json` | Codex | Codex-native manifest, including marketplace `interface` metadata (display name, logo, screenshots, prompts) |
| `.mcp.json` | Codex | Codex-native MCP config, including Codex-only fields (`env_vars` passthrough allowlist, `startup_timeout_sec`, `tool_timeout_sec`) |

Notes on the split:

- Codex does not currently document an Agent Plugins `extensions` namespace,
  so its UI/marketplace metadata stays in `.codex-plugin/plugin.json` instead
  of being duplicated under an invented `extensions` key. If Codex publishes a
  reverse-domain namespace later, that metadata can move into the portable
  manifest's `extensions` field.
- The portable `mcp.json` declares no secret values. Telegram credentials come
  from the login wizard (keyring / encrypted session file), not from the
  package. The `TG_API_ID`-style environment overrides listed below reach the
  server through Codex's `env_vars` passthrough; other clients control their
  own base subprocess environment, and the server works without them once you
  have logged in.
- The portable config sets `UV_PROJECT_ENVIRONMENT=${PLUGIN_DATA}/uv-env` so
  `uv` builds the virtualenv in the client-managed writable data directory
  instead of inside the (possibly read-only) installed package.
- Shared metadata (`name`, `version`, `author`, `license`, ...) must stay
  identical across `plugin.json` and `.codex-plugin/plugin.json`, and the two
  MCP configs must launch the same server command. The test suite enforces
  this (`tests/telegram_mcp/test_plugin_structure.py`), and also validates the
  portable files against the vendored Agent Plugins 1.0.0 schemas.

An Agent Plugins client loads the package by reading root `plugin.json`,
discovering skills under `skills/`, and starting `telegram_personal` from root
`mcp.json`. Codex keeps using `.codex-plugin/plugin.json` and `.mcp.json`
exactly as before; nothing about the Codex install flow changed.

---

## Useful commands

### Repo checkout flow

```bash
# show storage status
uv run --project ./telegram/mcp_server codex-telegram storage

# inspect the authenticated account
uv run --project ./telegram/mcp_server codex-telegram whoami

# log out / clear the stored session
uv run --project ./telegram/mcp_server codex-telegram logout

# run tests (run from telegram/mcp_server so the pytest config and coverage
# settings in its pyproject.toml are picked up; this also writes
# telegram/coverage/coverage.xml for Plugin Eval)
cd telegram/mcp_server && uv run pytest
```

### Installed-bundle flow (Codex)

```bash
uv run --project "$(
python3 - <<'PY'
from pathlib import Path
candidates = [p for p in Path.home().glob('.codex/plugins/cache/*/telegram/*/mcp_server') if p.is_dir()]
if not candidates:
    raise SystemExit('No installed Telegram plugin bundle found under ~/.codex/plugins/cache')
print(max(candidates, key=lambda p: p.stat().st_mtime))
PY
)" codex-telegram whoami
```

## How sessions are stored

The login wizard stores the Telegram session in:

1. the OS keyring, if available
2. otherwise an encrypted file at `~/.config/codex-telegram/session.enc`

`CODEX_TELEGRAM_SESSION` also exists, but it is intended for test/CI use only. It injects a raw `StringSession` directly and bypasses the normal keyring / encrypted-file flow.

If the OS keyring is unavailable:

```bash
# preferred: let the login flow prompt if keyring is unavailable
uv run --project ./telegram/mcp_server codex-telegram login
```

or:

```bash
read -rsp "Telegram session master key: " CODEX_TELEGRAM_MASTER_KEY; echo
export CODEX_TELEGRAM_MASTER_KEY
uv run --project ./telegram/mcp_server codex-telegram login
unset CODEX_TELEGRAM_MASTER_KEY
```

Do not pass the master key as a CLI flag. It ends up in shell history and `ps`.

## Environment variables


| Variable                           | Purpose                                                                                                                                       |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `TG_API_ID`                        | Telegram API ID used for login/session bootstrap.                                                                                             |
| `TG_API_HASH`                      | Telegram API hash used for login/session bootstrap.                                                                                           |
| `CODEX_TELEGRAM_MASTER_KEY`        | Encrypts/decrypts the fallback session file when the OS keyring is unavailable.                                                               |
| `CODEX_TELEGRAM_SESSION`           | Test/CI-only raw `StringSession` injection. Avoid using this for normal local installs.                                                       |
| `CODEX_TELEGRAM_CACHE_ENCRYPT`     | Optional: set to `1` to encrypt the local SQLite message cache. Requires `pysqlcipher3` plus `CODEX_TELEGRAM_MASTER_KEY`.                    |
| `CODEX_TELEGRAM_ALLOW_DESTRUCTIVE` | Must be set to `1` plus `confirm=True` on the tool call before destructive tools like `delete_chat`, `delete_messages`, or `logout` will run. |
| `CODEX_TELEGRAM_UPLOAD_DIR`        | Upload sandbox for `send_*` and `set_profile_photo`. Files outside this directory require `allow_arbitrary_path=True`.                        |
| `CODEX_TELEGRAM_CONFIG_DIR`        | Optional: overrides the config directory that stores the encrypted session file (default `~/.config/codex-telegram`).                         |
| `CODEX_TELEGRAM_MEDIA_SCRIPTS_DIR` | Optional: overrides the directory containing the `telegram-media-inspect` scripts used by `inspect_message_media`.                            |
| `TELEGRAM_MTPROXY_HOST`            | Optional: MTProxy hostname for networks that block direct MTProto. All three `TELEGRAM_MTPROXY_*` vars must be set together.                   |
| `TELEGRAM_MTPROXY_PORT`            | Optional: MTProxy port. All three `TELEGRAM_MTPROXY_*` vars must be set together.                                                              |
| `TELEGRAM_MTPROXY_SECRET`          | Optional: MTProxy secret. Treat it like a credential — env only, never commit it. All three `TELEGRAM_MTPROXY_*` vars must be set together.    |
| `XDG_CACHE_HOME`                   | Optional (standard XDG var): changes where the local message cache lives (default `~/.cache/codex-telegram/cache.db`).                        |


## Local cache: use it for broad, old, repeated, or aggregate work

The local SQLite cache lives at `~/.cache/codex-telegram/cache.db` and is per chat.
Use live search for quick recent lookups or when you do not know the dialog yet. Use the cache when the dialog is known and the task is broad, old, repeated, exhaustive, a summary, or an aggregate.

- `cache_status` shows which chats are cached, message counts, and last sync times.
- `sync_chat_cache(chat_ref)` incrementally adds new messages for one chat. Use `full=True` only when you intentionally want to rebuild that chat’s cache. The first sync of a chat bootstraps by iterating messages with `iter_messages(limit=max_messages_per_batch)` (default 5000) — for chats larger than that batch, the response sets `older_history_uncached: true` and exposes `oldest_fetched_id` so callers can detect the cap; older history is not currently backfilled by this tool.
- `search_cache(chat_ref, query, from_user, min_date, max_date, auto_sync_seconds=600, compact=True)` searches locally and can auto-sync the chat first if the cache is missing or stale. Use `compact=True` for token-efficient previews; use `next_offset` to continue paginated results.
- `summarize_chat_history(chat_ref, min_date, max_date, chunk_index=0)` returns SQL-paginated cache-backed chunks for map-reduce summaries.
- `aggregate_cache(chat_ref, min_date, max_date, group_by="day|week|sender")` returns local counts without re-querying Telegram.

For unknown-dialog searches, first use live search or `list_dialogs` to identify candidate chats, then sync/search those chats through the cache.

If you want the cache encrypted at rest, install `pysqlcipher3`, set `CODEX_TELEGRAM_CACHE_ENCRYPT=1`, and provide `CODEX_TELEGRAM_MASTER_KEY`.

## Troubleshooting

### Check runtime, auth, and cache diagnostics

Use the `telegram_diagnostics` MCP tool when the plugin may be running from a stale installed bundle, when auth is unclear, or when you want to verify whether the local message cache is encrypted. It returns the package version, plugin root, session-storage state, cache path, cache size, and cache encryption status without requiring a working Telegram login. Pass `include_account=True` if you also want it to attempt an authenticated account check.

### I installed the plugin but `@Telegram` does not show up (Codex)

Open a fresh Codex thread first. Plugin and skill context can lag in older threads.

### The plugin page is there, but I am not logged in

Run the login command from Step 2.

### `codex-telegram storage` says no session found

You have not completed the login wizard yet, or you logged in under a different environment/user.

### Connections fail or hang on restricted networks

Direct Telegram MTProto (raw TCP to Telegram's data centers) is blocked in
some hosted agent environments and corporate networks. If `login` or the MCP
server cannot connect, route through an MTProxy by setting
`TELEGRAM_MTPROXY_HOST`, `TELEGRAM_MTPROXY_PORT`, and
`TELEGRAM_MTPROXY_SECRET` together in the environment of both the login
command and the MCP server. Keep the secret out of committed files. For other
proxy types (SOCKS5, HTTP), see the
[Telethon proxy docs](https://docs.telethon.dev/en/stable/basic/signing-in.html#signing-in-behind-a-proxy).

### `AUTH_KEY_DUPLICATED` or timeouts when several agents are connected

Two MTProto clients used the same session concurrently and Telegram revoked
the auth key. Current versions prevent this: `serve` proxies every host to one
shared backend (see the shared backend section above). If you hit this error,
make sure no host sets `CODEX_TELEGRAM_SHARED_BACKEND=0` while others use the
same session, stop any pre-shared-backend `codex-telegram serve` processes,
then re-run `login` once to create a fresh session.

### `FLOOD_WAIT_X`

Telegram is rate-limiting the action. Short waits retry automatically. Longer waits surface as tool errors.

### `PEER_FLOOD`

Telegram restricted the account for spammy behavior. That is an account-level Telegram restriction, not a plugin crash.

### The plugin MCP server does not appear in `codex mcp list` (Codex)

Do not rely on that alone. Plugin-bundled MCP servers can be present at runtime even if `codex mcp list` is incomplete or another manual MCP server name collides. The stronger checks are:

1. `/plugins` shows `Telegram` installed
2. the plugin page lists `telegram_personal`
3. a fresh thread can use `@Telegram`

### Keyring issues

If the OS keyring fails, rerun login and let it prompt, or pre-set `CODEX_TELEGRAM_MASTER_KEY`.

## What your agent sees

When you invoke a Telegram skill or MCP tool, your agent receives raw chat content and metadata from the response payload. That can include message text, sender names, captions, usernames, reactions, and file metadata.

If you would not paste the content into an agent prompt directly, do not summarize or process it through this plugin.

## Security / Telegram caveats

- This is a user-account integration. A leaked `StringSession` is effectively full account access.
- If you think the session leaked, revoke it from an official Telegram client immediately.
- Telegram can rate-limit or restrict accounts using aggressive third-party automation.
- Read-only summarization of your own chats is the lowest-risk use case.
- QR login is intentionally not implemented here.

## Repo layout

- `telegram/`: the plugin bundle (single source of truth)
  - `plugin.json`: portable Agent Plugins 1.0.0 manifest
  - `mcp.json`: portable Agent Plugins 1.0.0 MCP server declaration
  - `mcp_server/`: Python package and MCP server
  - `skills/`: skill files loaded by the plugin (both formats discover `skills/<name>/SKILL.md`)
  - `assets/`: icon, logo, screenshots referenced by the Codex manifest
  - `.codex-plugin/plugin.json`: Codex-native plugin manifest (marketplace `interface` metadata lives here)
  - `.mcp.json`: Codex-native MCP server declaration (Codex-only `env_vars`/timeout fields live here)
- `tests/telegram_mcp/`: server tests plus `test_plugin_structure.py`, which
  validates the portable manifests against the vendored Agent Plugins schemas
  and keeps the Codex and portable files in sync
- Local marketplace registration lives outside this repo in `~/.agents/plugins/marketplace.json`
