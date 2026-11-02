# Hetzner-Robot

ربات تلگرام برای مدیریت کامل Hetzner Cloud + ترمینال SSH زنده (شبیه Termius).

## نصب (تک‌دستوری)
```bash
bash <(curl -Ls https://raw.githubusercontent.com/hayousef68/Hetzner-Robot/main/install.sh)
```
نصب‌کننده فقط **توکن ربات تلگرام** و **آیدی عددی تو** را می‌پرسد. بعد از نصب `/start` بزن؛
ربات مرحله‌به‌مرحله **توکن API هتزنر** را می‌گیرد.

## حذف
```bash
bash <(curl -Ls https://raw.githubusercontent.com/hayousef68/Hetzner-Robot/main/uninstall.sh)
```

## امکانات SSH
- ترمینال زنده (PTY واقعی) داخل یک پیام تلگرام؛ htop/nano/vim هم نمایش داده می‌شوند
- دکمه‌ها: Enter, Ctrl+C/D/Z, Tab, Esc, جهت‌ها؛ تایپ `^X` = Ctrl+X
- ذخیره سرورها (پسورد یا کلید؛ رمزنگاری‌شده)، ساخت کلید ed25519 و افزودن به Hetzner
- Snippet (دستورهای ذخیره‌شده با یک تپ)
- اجرای یک دستور روی چند سرور
- SFTP: `/ls [مسیر]`، `/get مسیر` (دانلود تا 50MB)، ارسال فایل داخل چت = آپلود (کپشن = مسیر مقصد، تا 20MB)
- تأیید Host Key (TOFU): اگر fingerprint سرور عوض شود اتصال قطع و هشدار داده می‌شود
- بسته شدن خودکار نشست بعد از ۳۰ دقیقه بی‌فعالیتی

## دستورات سرور
`hetzner-bot status | logs | restart | update | uninstall`
