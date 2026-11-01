#!/usr/bin/env bash
# Hetzner-Robot installer
# bash <(curl -Ls https://raw.githubusercontent.com/hayousef68/Hetzner-Robot/main/install.sh)
set -euo pipefail

REPO="hayousef68/Hetzner-Robot"
BRANCH="${BRANCH:-main}"
RAW="https://raw.githubusercontent.com/${REPO}/${BRANCH}"
APP_DIR="/opt/hetzner-bot"
SERVICE="hetzner-bot"
SVC_USER="hbot"

R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; N=$'\e[0m'
say() { echo "${G}==>${N} $*"; }
die() { echo "${R}خطا:${N} $*" >&2; exit 1; }
ask() { local v; read -r -p "$1" v </dev/tty || true; echo "$v"; }

[ "$(id -u)" -eq 0 ] || die "با root اجرا کن (sudo -i)"
command -v systemctl >/dev/null || die "systemd لازم است"

say "نصب پیش‌نیازها ..."
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y -qq
  apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates
elif command -v dnf >/dev/null; then
  dnf install -y -q python3 python3-pip curl ca-certificates
elif command -v yum >/dev/null; then
  yum install -y -q python3 python3-pip curl ca-certificates
else
  die "توزیع پشتیبانی نمی‌شود (apt/dnf/yum)"
fi

mkdir -p "$APP_DIR/data"

# ---------- config ----------
ENV_FILE="$APP_DIR/.env"
KEEP="${KEEP_CONFIG:-0}"
if [ -f "$ENV_FILE" ] && [ "$KEEP" != "1" ]; then
  a=$(ask "تنظیمات قبلی پیدا شد. نگهش دارم؟ [Y/n]: ")
  case "${a:-Y}" in n|N) rm -f "$ENV_FILE" ;; *) KEEP=1 ;; esac
fi

if [ ! -f "$ENV_FILE" ]; then
  echo
  echo "${Y}1) توکن ربات تلگرام را از @BotFather بگیر${N}"
  while true; do
    BOT_TOKEN=$(ask "   Bot Token: ")
    if curl -fsS "https://api.telegram.org/bot${BOT_TOKEN}/getMe" 2>/dev/null | grep -q '"ok":true'; then
      say "توکن معتبر است ✔"; break
    fi
    echo "${R}   توکن نامعتبر است، دوباره امتحان کن.${N}"
  done
  echo
  echo "${Y}2) آیدی عددی تلگرام خودت را بده (از @userinfobot بگیر؛ چند نفر با کاما)${N}"
  while true; do
    ADMIN_IDS=$(ask "   Admin ID: ")
    ADMIN_IDS="${ADMIN_IDS// /}"
    [[ "$ADMIN_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]] && break
    echo "${R}   فقط عدد (یا چند عدد با کاما).${N}"
  done
  ( umask 077; printf 'BOT_TOKEN=%s\nADMIN_IDS=%s\n' "$BOT_TOKEN" "$ADMIN_IDS" > "$ENV_FILE" )
fi

# ---------- app ----------
say "دانلود فایل‌ها ..."
curl -fsSL "$RAW/bot.py"           -o "$APP_DIR/bot.py"
curl -fsSL "$RAW/requirements.txt" -o "$APP_DIR/requirements.txt"

say "ساخت virtualenv و نصب کتابخانه‌ها ..."
[ -d "$APP_DIR/venv" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

id "$SVC_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER"
chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR"
chmod 700 "$APP_DIR/data"; chmod 600 "$ENV_FILE"

cat > "/etc/systemd/system/${SERVICE}.service" <<UNIT
[Unit]
Description=Hetzner Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
User=${SVC_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=DATA_DIR=${APP_DIR}/data
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/bot.py
Restart=always
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT

# ---------- helper command ----------
cat > /usr/local/bin/hetzner-bot <<CLI
#!/usr/bin/env bash
case "\${1:-}" in
  status)    systemctl status ${SERVICE} --no-pager ;;
  logs)      journalctl -u ${SERVICE} -f -n 50 ;;
  restart)   systemctl restart ${SERVICE} && echo restarted ;;
  update)    curl -fsSL ${RAW}/install.sh | KEEP_CONFIG=1 bash ;;
  uninstall) bash <(curl -fsSL ${RAW}/uninstall.sh) ;;
  *) echo "usage: hetzner-bot {status|logs|restart|update|uninstall}" ;;
esac
CLI
chmod +x /usr/local/bin/hetzner-bot

systemctl daemon-reload
systemctl enable --now "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 2

if systemctl is-active --quiet "$SERVICE"; then
  echo
  say "نصب شد و ربات در حال اجراست ✅"
  echo "   حالا در تلگرام به ربات برو و ${Y}/start${N} بزن؛ ربات مرحله‌به‌مرحله توکن هتزنر را می‌گیرد."
  echo "   مدیریت: hetzner-bot {status|logs|restart|update|uninstall}"
else
  die "سرویس بالا نیامد. لاگ: journalctl -u ${SERVICE} -n 50"
fi
