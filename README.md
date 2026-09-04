# DeckShots

**Steam Deck → Telegram, automatically.** 🇷🇺 [Русская версия](README.ru.md)

A tiny agent on the Deck watches Steam screenshots, Game Recording clips and desktop recordings (Spectacle);
a small server turns them into Telegram messages with the game name in the caption. Works in Game Mode,
survives reboots and SteamOS updates, and never loses a file: the Deck deletes its copy only after Telegram
has confirmed delivery.

```
Steam Deck ──HTTPS──▶ DeckShots server ──▶ Telegram Bot API ──▶ your chat / group / channel
 (agent, stdlib py)   (FastAPI + ffmpeg)    (local server: 2 GB)   🎮 Elden Ring · 📸 George's Deck
```

| Source | Where Steam keeps it | Sent as |
|---|---|---|
| Steam screenshots (Steam + R1) | `userdata/<id>/760/remote/<appid>/screenshots/` | photo; several within 45 s → album |
| Game Recording clips | `userdata/<id>/gamerecordings/clips/clip_<gameid>_*/` (DASH pieces) | mp4 (stream copy, no re-encode) with the clip thumbnail |
| Spectacle / OBS recordings | `~/Videos/Screencasts`, `~/Videos` | mp4 as-is, webm/mkv transcoded to H.264 |
| Spectacle screenshots | `~/Pictures/Screenshots` | photo |

Game names come from `appmanifest_*.acf` (Steam games), `shortcuts.vdf` (non-Steam games, including
shortcuts you deleted later, thanks to a local cache) and the Steam Store API as a fallback.

**Reliability:** SQLite queue on the Deck (offline → files wait, exponential backoff), idempotent uploads
(sha256), two delivery lanes (fast: photos/clips/mp4, slow: transcodes) with several workers, round-robin
between Decks, videos above the Telegram limit split with stream copy (`1/2`, `2/2`).

**Bot commands:** 📊 status (queue per Deck, agent online?), 📈 stats (total / today / 7 days, by type,
top games), ⏸ pause / ▶️ resume. Colored inline buttons, rich-text replies. Admin sees every Deck.

---

## Full recipe

You need: a machine with Docker that is reachable over HTTPS (a home server behind a reverse proxy, or a
small VPS), a Telegram bot, and 10 minutes.

### 1. Telegram

1. Create a bot in [@BotFather](https://t.me/BotFather) (`/newbot`), keep the token.
2. Find the `chat_id` of every recipient: message [@userinfobot](https://t.me/userinfobot), or use a
   group/channel id (`-100…`). Every person must press **Start** in your bot once, otherwise the bot
   cannot write to them.
3. Optional, for files above 50 MB (clips easily are): get `api_id` / `api_hash` at
   [my.telegram.org](https://my.telegram.org) → *API development tools*. This lets you run the bundled
   local Bot API server with a 2 GB limit.

### 2. Server

```bash
git clone https://github.com/Nospire/deckshots && cd deckshots/server
cp config.example.yml config.yml
cp .env.example .env
```

`.env`:

```
BOT_TOKEN=123456:ABC-DEF          # from BotFather
TELEGRAM_API_ID=…                 # only for the local Bot API (step 1.3)
TELEGRAM_API_HASH=…
TZ=Europe/Berlin
```

`config.yml`, the parts that matter:

```yaml
api_url: http://telegram-bot-api:8081     # bundled local Bot API …
# api_url: https://api.telegram.org       # … or the public one (50 MB limit)
# local_files: false                      #     (set this too for the public API)
public_url: https://example.com/shots     # where the Decks will reach the server (step 3)
admin_chat_id: 123456789                  # you: "deck connected" notices, admin view in the bot
dl_key: <openssl rand -hex 12>            # secret path for the click-to-install packages
devices:
  - name: deck-alice
    label: "Alice's Deck"
    token: <openssl rand -hex 16>         # one per Deck
    chat_id: 123456789                    # where Alice's shots go
```

Start it:

```bash
docker compose --profile local-api up -d --build     # with the bundled 2 GB Bot API
# docker compose up -d --build                        # public API only
docker compose logs -f deckshots                      # "worker fast#1 started", "command bot @… started"
```

The server listens on `:8790` and serves everything under `path_prefix` (default `/shots`).

### 3. HTTPS

Only `/shots/...` has to be public, so a location on a site you already have is enough.

*Nginx Proxy Manager* → your proxy host → **Custom locations** → `/shots` → `http://<server-ip>:8790`, and
in the location's advanced config:

```
client_max_body_size 2000m;
proxy_request_buffering off;
proxy_read_timeout 1800s;
proxy_send_timeout 1800s;
```

*Caddy* (keep the `/shots` prefix, so `handle`, not `handle_path`):

```
example.com {
    handle /shots/* {
        reverse_proxy <server-ip>:8790
        request_body { max_size 2000MB }
    }
}
```

Check: `curl https://example.com/shots/health` → `{"ok":true,...}`. Put that URL into `public_url`.

### 4. Deck

On every Deck, Desktop Mode → Konsole (token from `config.yml`):

```bash
curl -fsSL https://example.com/shots/install.sh | DECKSHOTS_SERVER=https://example.com/shots bash -s -- <DEVICE_TOKEN> alice-deck
```

Or build click-to-install packages for the whole family (run on the server):

```bash
sh tools/mkpkg.sh
# -> server/pkg/DeckShots-<name>.tar.gz, served at https://example.com/shots/dl/<dl_key>/DeckShots-<name>.tar.gz
```

On the Deck: download the tar.gz, double-tap to extract (the exec bit is preserved), double-tap the `.sh`
→ **Execute**. A Konsole window shows progress and a "deck connected" message lands in Telegram.

What the installer does: `~/.local/bin/deckshots-agent.py` (pure stdlib Python, ships with SteamOS),
user unit `deckshots.service` in `~/.config/systemd/user` (starts in Game Mode too), linger enabled,
`~/.config/deckshots/config.json`. Everything lives in `/home`, so SteamOS updates don't touch it.
Files older than the install are skipped; set `"ignore_before": 0` in the config to send the backlog.
Logs: `journalctl --user -u deckshots -f`. Re-running the installer updates the agent.

Tip: in Spectacle set the recording format to **MP4 (H.264)**, then desktop recordings go out in seconds
instead of being transcoded on the server.

### 5. Bot commands

The delivery bot answers `/status`, `/stats`, `/pause`, `/resume`, `/help` with buttons, but only to
chats listed in `devices` and to the admin. If the bot token is shared with another service that already
reads its updates (webhook or polling), set `commands_on_main_bot: false` and put a second bot's token
into `COMMAND_BOT_TOKEN`.

### 6. Day-to-day

- Add a Deck: new entry in `devices`, `docker compose restart deckshots`, `sh tools/mkpkg.sh`.
- Update the server: `git pull && docker compose up -d --build`.
- Update Decks: re-run the installer (or the package), config is kept.
- Storage: files are deleted after delivery; `data/` only holds what's in flight.

### Troubleshooting

| Symptom | Look at |
|---|---|
| Nothing arrives | `journalctl --user -u deckshots -n 50` on the Deck: `queued` → `uploaded` → `delivered`? |
| `upload HTTP 401` | token in `config.json` ≠ token in `config.yml` |
| `upload HTTP 413` / connection reset on clips | reverse proxy body limit (step 3) |
| `telegram: 403 Forbidden: bot can't initiate conversation` | the recipient hasn't pressed Start in the bot |
| `telegram: 400 Request Entity Too Large` | using api.telegram.org with clips > 50 MB → local Bot API or `max_part_mb: 49` |
| `command bot cannot start: … webhook is active` | the token is used elsewhere → `commands_on_main_bot: false` + second bot |

## Layout

```
server/app/main.py     HTTP API (upload, status, ping, hello, dl, agent.py, install.sh)
server/app/worker.py   lanes, round-robin, albums, sending, janitor
server/app/media.py    ffmpeg: clip remux, transcode, split
server/app/bot.py      command bot with inline buttons
server/app/store.py    SQLite
agent/agent.py         Steam Deck agent
agent/install.sh       installer / updater
tools/mkpkg.sh         per-device click-to-install packages
```

## License

MIT
