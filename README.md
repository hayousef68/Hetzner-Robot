# Hetzner-Robot

ربات تلگرام برای مدیریت کامل اکانت Hetzner Cloud (ساخت سرور، پاور، ریبیلد، اسنپ‌شات، فایروال، Volume، Floating IP و ...).

## نصب (تک‌دستوری)
```bash
bash <(curl -Ls https://raw.githubusercontent.com/hayousef68/Hetzner-Robot/main/install.sh)
```
نصب‌کننده فقط **توکن ربات تلگرام** و **آیدی عددی تو** را می‌پرسد. بعد از نصب، در تلگرام `/start` بزن؛
ربات مرحله‌به‌مرحله اسم پروژه و **توکن API هتزنر** را می‌گیرد (چند پروژه هم می‌شود اضافه کرد).

## حذف
```bash
bash <(curl -Ls https://raw.githubusercontent.com/hayousef68/Hetzner-Robot/main/uninstall.sh)
```

## دستورات سرور
`hetzner-bot status | logs | restart | update | uninstall`
