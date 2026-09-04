# DeckShots

Steam Deck → Telegram, automatically. A tiny agent on the Deck watches Steam screenshots, Game Recording
clips and desktop recordings (Spectacle), a small server turns them into Telegram messages with the game
name in the caption. Works in Game Mode, survives reboots and SteamOS updates, and never loses a file:
the Deck deletes its copy only after Telegram has confirmed delivery.

```
Steam Deck ──HTTPS──▶ DeckShots server ──▶ Telegram Bot API ──▶ your chat / channel
 (agent, stdlib py)   (FastAPI + ffmpeg)    (local server: 2 GB)   🎮 Elden Ring · 📸 George's Deck
```

**What gets sent**

| Source | Where Steam keeps it | Sent as |
|---|---|---|
| Steam screenshots (Steam + R1) | `userdata/<id>/760/remote/<appid>/screenshots/` | photo, several within 45 s → album |
| Game Recording clips | `userdata/<id>/gamerecordings/clips/clip_<gameid>_*/` (DASH pieces) | remuxed mp4, stream copy, clip thumbnail |
| Spectacle / OBS recordings | `~/Videos/Screencasts`, `~/Videos` | mp4 as-is, webm/mkv transcoded to H.264 |
| Spectacle screenshots | `~/Pictures/Screenshots` | photo |

Game names come from `appmanifest_*.acf` (Steam games), `shortcuts.vdf` (non-Steam games, incl. deleted
shortcuts thanks to a local cache) and the Steam Store API as a fallback. 64-bit gameids of non-Steam
games are mapped to their 32-bit appid automatically.

**Reliability**

- SQLite queue on the Deck: no internet → files wait, exponential backoff, FIFO.
- Deck deletes a file only after the server reports `sent`; the server deletes only after Telegram returned a `message_id`.
- Idempotent uploads (sha256): a reboot mid-upload never produces duplicates.
- Two delivery lanes (fast: photos, clips, mp4; slow: transcodes) with several workers each; devices are served round-robin so one Deck's backlog never blocks another.
- Videos above the Telegram limit are split with stream copy and captioned `1/2`, `2/2`.

## Server

Requirements: Docker. Any small box works; transcoding is the only CPU-heavy part (set Spectacle to MP4 and it disappears).

```bash
git clone https://github.com/Nospire/deckshots && cd deckshots/server
cp config.example.yml config.yml && cp .env.example .env
# edit both: bot token, admin chat id, one device per Deck (token = openssl rand -hex 16), chat_id per device
docker compose --profile local-api up -d --build     # with bundled local Bot API (2 GB files)
# or: docker compose up -d --build                    # api.telegram.org (50 MB limit, set api_url + local_files: false)
```

Publish `http://<host>:8790` under HTTPS. Only the `path_prefix` (default `/shots`) needs to be reachable,
so a **custom location on an existing site** is enough (Nginx Proxy Manager: add location `/shots` →
`http://<host>:8790`, with `client_max_body_size 2000m; proxy_request_buffering off; proxy_read_timeout 1800s;`).
Put the resulting URL into `public_url`.

Where the files go: `chat_id` per device can be a user (they must `/start` the bot once), a group or a
channel; `thread_id` targets a forum topic. `admin_chat_id` receives a copy of every "deck connected" notice.

### Command bot (optional)

Create a **second** bot in @BotFather and put its token into `COMMAND_BOT_TOKEN`. It answers only to chats
listed in `devices` (and the admin) with colored inline buttons:

- 📊 **Status** — server queue per Deck, whether the agent is online (heartbeat every 5 min), agent version
- 📈 **Stats** — sent total / today / 7 days, by type, size, top-5 games (admin sees every Deck)
- ⏸ **Pause** / ▶️ **Resume** — hold delivery for your Decks; files keep queuing, nothing is lost

Why a second bot: the delivery bot may already be used by another service with webhooks, and only one
consumer can read a bot's updates.

## Deck

One-liner in Desktop Mode (Konsole), token from `config.yml`:

```bash
curl -fsSL https://YOUR-SERVER/shots/install.sh | bash -s -- <DEVICE_TOKEN> my-deck
```

Or build click-to-install packages for everyone in the family:

```bash
sh tools/mkpkg.sh          # -> server/pkg/DeckShots-<name>.tar.gz, served at <public_url>/dl/<dl_key>/
```

Download the tar.gz on the Deck, double-tap to extract (the exec bit is preserved), double-tap the `.sh`
→ *Execute*. A Konsole window shows progress and a "deck connected" message lands in Telegram.

What the installer does: puts `deckshots-agent.py` (pure stdlib, Python 3 ships with SteamOS) into
`~/.local/bin`, a user unit `deckshots.service` into `~/.config/systemd/user` (starts in Game Mode too),
enables linger, writes `~/.config/deckshots/config.json`. Files older than the install are skipped;
set `"ignore_before": 0` to send the backlog. Logs: `journalctl --user -u deckshots -f`.

Everything lives in `/home`, so SteamOS updates don't touch it. Re-running the installer updates the agent.

## Layout

```
server/app/main.py     HTTP API (upload, status, ping, hello, dl, agent.py, install.sh)
server/app/worker.py   lanes, round-robin, albums, sending
server/app/media.py    ffmpeg: clip remux, transcode, split
server/app/bot.py      command bot with inline buttons
server/app/store.py    SQLite
agent/agent.py         Steam Deck agent
agent/install.sh       installer / updater
tools/mkpkg.sh         per-device click-to-install packages
```

## License

MIT
