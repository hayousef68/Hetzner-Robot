#!/usr/bin/env bash
# Hetzner-Robot uninstaller
# bash <(curl -Ls https://raw.githubusercontent.com/hayousef68/Hetzner-Robot/main/uninstall.sh)
set -uo pipefail

APP_DIR="/opt/hetzner-bot"
SERVICE="hetzner-bot"
SVC_USER="hbot"

[ "$(id -u)" -eq 0 ] || { echo "با root اجرا کن"; exit 1; }

if [ "${YES:-0}" != "1" ]; then
  echo "⚠️ ربات، سرویس و همه داده‌ها (شامل توکن‌های ذخیره‌شده هتزنر) حذف می‌شود."
  echo "   به سرورهای هتزنر شما هیچ تغییری داده نمی‌شود."
  read -r -p "ادامه؟ [y/N]: " a </dev/tty
  case "$a" in y|Y) ;; *) echo "لغو شد."; exit 0 ;; esac
fi

systemctl disable --now "$SERVICE" >/dev/null 2>&1
rm -f "/etc/systemd/system/${SERVICE}.service" /usr/local/bin/hetzner-bot
systemctl daemon-reload
rm -rf "$APP_DIR"
id "$SVC_USER" >/dev/null 2>&1 && userdel "$SVC_USER" 2>/dev/null
echo "✅ ربات کامل حذف شد."
echo "پیشنهاد: توکن API هتزنر را از Console هتزنر هم revoke کن."
