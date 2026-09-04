# DeckShots

**Steam Deck → Telegram, автоматически.** 🇬🇧 [English version](README.md)

Маленький агент на деке следит за скриншотами Steam, клипами Game Recording и записями экрана из
десктоп-режима (Spectacle), а небольшой сервер превращает их в сообщения Telegram с названием игры в
подписи. Работает в игровом режиме, переживает перезагрузки и обновления SteamOS и ничего не теряет:
дек удаляет свою копию только после того, как Telegram подтвердил доставку.

```
Steam Deck ──HTTPS──▶ сервер DeckShots ──▶ Telegram Bot API ──▶ ваш чат / группа / канал
 (агент, чистый py)   (FastAPI + ffmpeg)   (локальный: 2 ГБ)     🎮 Elden Ring · 📸 Дек Жоры
```

| Источник | Где это лежит у Steam | Как уходит |
|---|---|---|
| Скриншоты Steam (Steam + R1) | `userdata/<id>/760/remote/<appid>/screenshots/` | фото; несколько за 45 с → альбом |
| Клипы Game Recording | `userdata/<id>/gamerecordings/clips/clip_<gameid>_*/` (куски DASH) | mp4 без перекодирования, с обложкой клипа |
| Записи Spectacle / OBS | `~/Videos/Screencasts`, `~/Videos` | mp4 как есть, webm/mkv перекодируются в H.264 |
| Скриншоты Spectacle | `~/Pictures/Screenshots` | фото |

Названия игр берутся из `appmanifest_*.acf` (игры Steam), `shortcuts.vdf` (non-Steam игры, в том числе
уже удалённые ярлыки, за счёт локального кэша) и из Steam Store API как запасной вариант.

**Надёжность:** очередь SQLite на деке (нет сети → файлы ждут, повторы с нарастающей паузой),
идемпотентная загрузка (sha256), две полосы доставки (быстрая: фото, клипы, mp4; медленная:
перекодирование) с несколькими воркерами, деки чередуются по кругу, видео больше лимита Telegram режется
без перекодирования с подписью `1/2`, `2/2`.

**Команды бота:** 📊 статус (очередь по декам, агент на связи?), 📈 статистика (всего / сегодня / 7 дней,
по типам, топ игр), ⏸ пауза / ▶️ продолжить. Цветные кнопки, ответы форматированным текстом. Админ видит
все деки.

---

## Полный рецепт

Понадобится: машина с Docker, доступная по HTTPS (домашний сервер за реверс-прокси или маленький VPS),
бот Telegram и минут десять.

### 0. Telegram из России: VPN или прокси

Из России до api.telegram.org и серверов Telegram напрямую не достучаться, а сервер DeckShots должен туда
ходить постоянно. Рабочие варианты, от простого к хитрому:

1. **Сервер за границей.** Самый простой путь: любой VPS за 2–3 евро в Европе. Деки шлют на него по HTTPS,
   он спокойно общается с Telegram. Ничего дополнительно настраивать не нужно.
2. **Домашний сервер через VPN-выход.** Если сервер дома, выпустите его наружу через свой VPN: WireGuard до
   зарубежного VPS, Tailscale/Headscale с exit-node, или роутер с раздельной маршрутизацией (OpenWrt +
   mihomo/sing-box, где трафик Telegram уходит в туннель). Для DeckShots это прозрачно: контейнеры просто
   ходят в интернет.
3. **VPN только для контейнеров, через gluetun.** Если весь хост в VPN не хочется, заверните в туннель только
   Telegram-часть. В `docker-compose.yml` добавьте gluetun и пустите через него Bot API:

   ```yaml
   services:
     gluetun:
       image: qmcgaw/gluetun
       cap_add: [NET_ADMIN]
       devices: [/dev/net/tun]
       environment:
         - VPN_SERVICE_PROVIDER=custom
         - VPN_TYPE=wireguard
         - WIREGUARD_PRIVATE_KEY=…
         - WIREGUARD_ADDRESSES=10.0.0.2/32
         - VPN_ENDPOINT_IP=…
         - VPN_ENDPOINT_PORT=51820
         - WIREGUARD_PUBLIC_KEY=…
     telegram-bot-api:
       network_mode: "service:gluetun"     # весь трафик Bot API идёт через туннель
       # …остальное как в примере
   ```

   и в `config.yml` укажите `api_url: http://gluetun:8081` (Bot API теперь виден по имени gluetun).
4. **HTTP/SOCKS-прокси без локального Bot API.** Если работаете напрямую с `api_url: https://api.telegram.org`,
   достаточно переменных окружения у контейнера `deckshots`: `HTTPS_PROXY=http://user:pass@host:port`
   (или `socks5://…`). Лимит файла при этом 50 МБ, клипы будут резаться на части.

Важно: локальный Bot API-сервер (вариант с лимитом 2 ГБ) сам по себе прокси для связи с Telegram не
поддерживает, поэтому для него подходят только варианты 1–3. Деки же никакого VPN не требуют: они ходят
только на ваш сервер.

### 1. Telegram

1. Создайте бота в [@BotFather](https://t.me/BotFather) (`/newbot`), сохраните токен.
2. Узнайте `chat_id` каждого получателя: напишите [@userinfobot](https://t.me/userinfobot), либо возьмите
   id группы/канала (`-100…`). Каждый получатель должен один раз нажать **Start** у вашего бота, иначе бот
   не сможет ему писать.
3. Для файлов больше 50 МБ (клипы легко больше): получите `api_id` / `api_hash` на
   [my.telegram.org](https://my.telegram.org) → *API development tools*. Это позволит поднять встроенный
   локальный Bot API с лимитом 2 ГБ.

### 2. Сервер

```bash
git clone https://github.com/Nospire/deckshots && cd deckshots/server
cp config.example.yml config.yml
cp .env.example .env
```

`.env`:

```
BOT_TOKEN=123456:ABC-DEF          # от BotFather
TELEGRAM_API_ID=…                 # только для локального Bot API (шаг 1.3)
TELEGRAM_API_HASH=…
TZ=Europe/Moscow
```

`config.yml`, то, что важно:

```yaml
api_url: http://telegram-bot-api:8081     # встроенный локальный Bot API …
# api_url: https://api.telegram.org       # … или публичный (лимит 50 МБ)
# local_files: false                      #     (для публичного укажите и это)
public_url: https://example.com/shots     # по какому адресу деки найдут сервер (шаг 3)
admin_chat_id: 123456789                  # вы: уведомления «дек подключён», админ-вид в боте
dl_key: <openssl rand -hex 12>            # секретный путь к пакетам установки
language: ru
devices:
  - name: deck-alice
    label: "Дек Алисы"
    token: <openssl rand -hex 16>         # свой на каждый дек
    chat_id: 123456789                    # куда уходят её скриншоты
```

Запуск:

```bash
docker compose --profile local-api up -d --build     # со встроенным Bot API на 2 ГБ
# docker compose up -d --build                        # только публичный API
docker compose logs -f deckshots                      # "worker fast#1 started", "command bot @… started"
```

Сервер слушает `:8790` и отдаёт всё под `path_prefix` (по умолчанию `/shots`).

### 3. HTTPS

Наружу нужен только `/shots/...`, так что хватит локации на уже существующем сайте.

*Nginx Proxy Manager* → ваш proxy host → **Custom locations** → `/shots` → `http://<ip-сервера>:8790`,
и в advanced config локации:

```
client_max_body_size 2000m;
proxy_request_buffering off;
proxy_read_timeout 1800s;
proxy_send_timeout 1800s;
```

*Caddy*:

```
example.com {
    handle /shots/* {
        reverse_proxy <ip-сервера>:8790
        request_body { max_size 2000MB }
    }
}
```

Проверка: `curl https://example.com/shots/health` → `{"ok":true,...}`. Этот адрес и есть `public_url`.

### 4. Дек

На каждом деке, десктоп-режим → Konsole (токен из `config.yml`):

```bash
curl -fsSL https://example.com/shots/install.sh | DECKSHOTS_SERVER=https://example.com/shots bash -s -- <DEVICE_TOKEN> alice-deck
```

Или соберите пакеты «скачал и тапнул» на всю семью (на сервере):

```bash
sh tools/mkpkg.sh
# -> server/pkg/DeckShots-<name>.tar.gz, отдаётся по https://example.com/shots/dl/<dl_key>/DeckShots-<name>.tar.gz
```

На деке: скачать tar.gz, даблтап по архиву (бит исполняемости сохраняется), даблтап по `.sh` →
**Выполнить**. Откроется окно Konsole с ходом установки, а в Telegram придёт «дек подключён».

Что делает установщик: кладёт `~/.local/bin/deckshots-agent.py` (чистый Python из стандартной библиотеки,
он уже есть в SteamOS), user-юнит `deckshots.service` в `~/.config/systemd/user` (стартует и в игровом
режиме), включает linger, пишет `~/.config/deckshots/config.json`. Всё живёт в `/home`, обновления SteamOS
это не трогают. Файлы старше момента установки пропускаются; чтобы отправить историю, поставьте в конфиге
`"ignore_before": 0`. Логи: `journalctl --user -u deckshots -f`. Повторный запуск установщика обновляет агент.

Совет: в Spectacle выберите формат записи **MP4 (H.264)**, тогда записи экрана уходят за секунды, а не
перекодируются на сервере.

### 5. Команды бота

Бот доставки отвечает на `/status`, `/stats`, `/pause`, `/resume`, `/help` кнопками, но только чатам из
`devices` и админу. Если токен бота уже используется другим сервисом, который читает его апдейты (вебхук
или поллинг), поставьте `commands_on_main_bot: false` и дайте второго бота через `COMMAND_BOT_TOKEN`.

### 6. Эксплуатация

- Новый дек: запись в `devices`, `docker compose restart deckshots`, `sh tools/mkpkg.sh`.
- Обновить сервер: `git pull && docker compose up -d --build`.
- Обновить деки: повторно запустить установщик (или пакет), конфиг сохраняется.
- Место: файлы удаляются после доставки, в `data/` лежит только то, что в пути.

### Если что-то не так

| Симптом | Куда смотреть |
|---|---|
| Ничего не приходит | на деке `journalctl --user -u deckshots -n 50`: есть `queued` → `uploaded` → `delivered`? |
| `upload HTTP 401` | токен в `config.json` не совпадает с `config.yml` |
| `upload HTTP 413` / обрыв на клипах | лимит тела запроса на реверс-прокси (шаг 3) |
| `telegram: 403 Forbidden: bot can't initiate conversation` | получатель не нажал Start у бота |
| `telegram: 400 Request Entity Too Large` | api.telegram.org и клипы > 50 МБ → локальный Bot API или `max_part_mb: 49` |
| `command bot cannot start: … webhook is active` | токен занят другим сервисом → `commands_on_main_bot: false` + второй бот |
| Сервер не видит Telegram (таймауты) | шаг 0: VPN или прокси; проверяйте изнутри контейнера, а не с хоста |

## Структура

```
server/app/main.py     HTTP API (upload, status, ping, hello, dl, agent.py, install.sh)
server/app/worker.py   полосы, round-robin, альбомы, отправка, уборка
server/app/media.py    ffmpeg: сборка клипов, перекодирование, нарезка
server/app/bot.py      командный бот с кнопками
server/app/store.py    SQLite
agent/agent.py         агент для Steam Deck
agent/install.sh       установщик / обновлятор
tools/mkpkg.sh         пакеты «скачал и тапнул» на каждый дек
```

## Лицензия

MIT
