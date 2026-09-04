#!/bin/sh
# Builds a self-contained installer per device: pkg/DeckShots-<name>.sh (+ .tar.gz that keeps the exec bit).
# Run on the server host next to docker-compose.yml:  sh tools/mkpkg.sh
# Packages are served at  <server>/shots/dl/<dl_key>/DeckShots-<name>.tar.gz  (dl_key from config.yml).
set -e
cd "$(dirname "$0")/.."
mkdir -p pkg
SERVER="${DECKSHOTS_PUBLIC_URL:-$(docker exec deckshots python3 -c 'from app import config as C; print(C.CFG.get("public_url","https://plugins.geekcom.org/shots"))')}"

docker exec deckshots python3 -c 'import yaml
c=yaml.safe_load(open("/config/config.yml"))
for d in c["devices"]: print(d["name"], d["token"], d.get("label", d["name"]))' | while read -r name token label; do
  f="pkg/DeckShots-$name.sh"
  cat > "$f" <<SH
#!/bin/bash
# DeckShots installer for "$label" ($name). Steam Deck, Desktop Mode: double-tap -> Execute.
# Installs the agent as a user service (autostarts in Game Mode too) and reports to Telegram.
TOKEN='$token'
NAME='$name'
SERVER='$SERVER'

if [ ! -t 1 ] && [ -n "\${DISPLAY:-}\${WAYLAND_DISPLAY:-}" ] && command -v konsole >/dev/null 2>&1; then
  exec konsole -e bash -c "bash '\$0'; echo; read -rp 'Готово. Нажми Enter, чтобы закрыть.'" "\$0"
fi

echo "== DeckShots: установка на \$NAME (\$(uname -n))"
if ! curl -fsS -m 15 "\$SERVER/health" >/dev/null; then
  echo "!! Сервер \$SERVER недоступен. Проверь интернет и попробуй снова."; exit 1
fi
curl -fsSL "\$SERVER/install.sh" | DECKSHOTS_SERVER="\$SERVER" bash -s -- "\$TOKEN" "\$NAME"
sleep 2
if systemctl --user is-active --quiet deckshots.service; then
  curl -fsS -m 15 -X POST "\$SERVER/hello" -H "X-Device-Token: \$TOKEN" -H "X-Host: \$(uname -n)" >/dev/null \\
    && echo "== Уведомление в Telegram отправлено." || echo "!! Агент работает, но уведомление не ушло (сервер/сеть)."
  echo "== Всё готово: скриншоты и клипы будут уходить в Telegram автоматически."
else
  echo "!! Сервис не запустился. Лог: journalctl --user -u deckshots -n 30"; exit 1
fi
SH
  chmod 755 "$f"
  tar -C pkg -czf "pkg/DeckShots-$name.tar.gz" "DeckShots-$name.sh"
  echo "built $f + tar.gz"
done
ls -la pkg
