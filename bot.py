#!/usr/bin/env python3
"""
Hetzner Cloud Manager - Telegram Bot (With Termius-like SSH & SFTP)
Env vars (set by install.sh):
  BOT_TOKEN   توکن ربات تلگرام
  ADMIN_IDS   آیدی عددی ادمین‌ها (با کاما)
  DATA_DIR    مسیر ذخیره پروژه‌ها/توکن‌های هتزنر (پیش‌فرض ./data)
توکن هتزنر از داخل خود ربات (مرحله‌به‌مرحله) گرفته می‌شود.
"""
import asyncio
import html
import io
import json
import logging
import os
import posixpath
import re
import shlex
import stat
import tempfile
import time
from datetime import datetime, timedelta, timezone

import asyncssh
import httpx
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as KM, Update
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hbot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()}


class HErr(Exception):
    pass


class Hetzner:
    BASE = "https://api.hetzner.cloud/v1"

    def __init__(self, token):
        self.c = httpx.AsyncClient(base_url=self.BASE, timeout=60,
                                   headers={"Authorization": f"Bearer {token}"})

    async def req(self, method, path, **kw):
        r = await self.c.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                m = r.json()["error"]
                raise HErr(f"{m.get('code')}: {m.get('message')}")
            except (ValueError, KeyError):
                raise HErr(f"HTTP {r.status_code}")
        return r.json() if r.content else {}

    async def paged(self, path, key, **params):
        out, page = [], 1
        while True:
            d = await self.req("GET", path, params={**params, "page": page, "per_page": 50})
            out += d[key]
            if not d.get("meta", {}).get("pagination", {}).get("next_page"):
                return out
            page += 1

    async def wait(self, action_id, timeout=120):
        t = time.time()
        while time.time() - t < timeout:
            a = (await self.req("GET", f"/actions/{action_id}"))["action"]
            if a["status"] == "success":
                return a
            if a["status"] == "error":
                raise HErr(a["error"]["message"])
            await asyncio.sleep(2)
        raise HErr("timeout waiting for action")


DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PFILE = os.path.join(DATA_DIR, "projects.json")
SFILE = os.path.join(DATA_DIR, "ssh_vault.json")
APIS = {}
TOKENS = {}
SSH_VAULT = {}


def load_projects():
    try:
        with open(PFILE, encoding="utf-8") as f:
            for n, t in json.load(f).items():
                TOKENS[n] = t
                APIS[n] = Hetzner(t)
    except FileNotFoundError:
        pass


def save_projects():
    os.makedirs(DATA_DIR, exist_ok=True)
    fd = os.open(PFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(TOKENS, f)


def load_ssh():
    global SSH_VAULT
    try:
        with open(SFILE, encoding="utf-8") as f:
            SSH_VAULT = json.load(f)
    except FileNotFoundError:
        SSH_VAULT = {}


def save_ssh():
    os.makedirs(DATA_DIR, exist_ok=True)
    fd = os.open(SFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(SSH_VAULT, f)


def save_srv_pass(sid, password):
    if not password:
        return
    sid = str(sid)
    cfg = SSH_VAULT.get(sid, {"user": "root", "port": 22})
    cfg["password"] = password
    SSH_VAULT[sid] = cfg
    save_ssh()


load_projects()
load_ssh()


def proj(c):
    p = c.user_data.get("proj")
    if p in APIS:
        return p
    return next(iter(APIS)) if APIS else "—"


def api(c):
    return APIS[proj(c)]


# ---------------------------------------------------------------- helpers
def e(x):
    return html.escape(str(x))


HOME = [B("🏠 منوی اصلی", callback_data="m:menu")]


def back(cb, label="↩️ بازگشت"):
    return [B(label, callback_data=cb)]


def ok(u):
    return u.effective_user and u.effective_user.id in ADMINS


def gb(x):
    return f"{(x or 0) / 1e9:.1f} GB"


def hb(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def st_icon(s):
    return {"running": "🟢", "off": "🔴"}.get(s, "🟡")


def ip4(s):
    return ((s.get("public_net") or {}).get("ipv4") or {}).get("ip", "—")


async def show(u, text, rows=None):
    kb = KM(rows) if rows else None
    q = u.callback_query
    try:
        if q:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb,
                                      disable_web_page_preview=True)
        else:
            await u.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb,
                                                 disable_web_page_preview=True)
    except BadRequest as ex:
        if "not modified" not in str(ex).lower():
            raise


async def confirm(u, text, yes, no):
    await show(u, f"⚠️ {text}\n\nمطمئنی؟",
               [[B("✅ بله", callback_data=yes), B("❌ خیر", callback_data=no)]])


def grid(items, prefix, per=2, width=30):
    rows = []
    for j in range(0, len(items), per):
        rows.append([B(l[:width], callback_data=f"{prefix}:{v}") for l, v in items[j:j + per]])
    return rows


# ---------------------------------------------------------------- SSH & SFTP (Termius Module)
SNIPPETS = {
    "sys": ("📊 منابع سیستم", "uptime && echo '---' && free -h && echo '---' && df -h /"),
    "top": ("🔥 پردازش‌های سنگین", "ps aux --sort=-%cpu | head -n 11"),
    "dock": ("🐳 کانتینرهای Docker", "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"),
    "ports": ("🌐 پورت‌های باز", "ss -tulpn | grep LISTEN"),
    "net": ("📡 تست شبکه و پینگ", "ip -br a && echo '---' && ping -c 2 1.1.1.1"),
    "apt": ("🔄 آپدیت پکیج‌ها", "DEBIAN_FRONTEND=noninteractive apt-get update && apt-get upgrade -y"),
}


def get_ssh_state(c, sid):
    sid = str(sid)
    ss = c.user_data.setdefault("ssh_state", {})
    if sid not in ss:
        ss[sid] = {"cwd": "/root", "ip": None, "name": f"#{sid}"}
    return ss[sid]


async def connect_ssh(c, sid):
    sid = str(sid)
    st = get_ssh_state(c, sid)
    if not st.get("ip") or st["ip"] == "—":
        s = (await api(c).req("GET", f"/servers/{sid}"))["server"]
        st["ip"] = ip4(s)
        st["name"] = s["name"]
    if st["ip"] == "—":
        raise HErr("این سرور IPv4 عمومی ندارد.")

    cfg = SSH_VAULT.get(sid, {})
    user = cfg.get("user", "root")
    port = int(cfg.get("port", 22))
    password = cfg.get("password")
    pkey_str = cfg.get("pkey") or SSH_VAULT.get("_bot_key")

    client_keys = []
    if pkey_str:
        try:
            client_keys.append(asyncssh.import_private_key(pkey_str))
        except Exception:
            pass

    kw = {
        "host": st["ip"],
        "port": port,
        "username": user,
        "known_hosts": None,
        "connect_timeout": 10,
    }
    if password:
        kw["password"] = password
    if client_keys:
        kw["client_keys"] = client_keys

    return await asyncssh.connect(**kw)


def term_kb(sid):
    return [
        [B("📂 فایل‌منیجر (SFTP)", callback_data=f"sf:{sid}"),
         B("⚡ اسنیپت‌های آماده", callback_data=f"sn:{sid}")],
        [B("📥 دانلود با مسیر", callback_data=f"sdl:{sid}"),
         B("🔑 تنظیمات اتصال SSH", callback_data=f"sc:{sid}")],
        [B("🧹 پاکسازی مسیر (cd ~)", callback_data=f"scd:{sid}"),
         B("🔌 خروج از ترمینال", callback_data=f"s:{sid}")],
    ]


async def ssh_home(u, c, sid):
    sid = str(sid)
    s = (await api(c).req("GET", f"/servers/{sid}"))["server"]
    st = get_ssh_state(c, sid)
    st["ip"] = ip4(s)
    st["name"] = s["name"]

    cfg = SSH_VAULT.get(sid, {})
    has_auth = bool(cfg.get("password") or cfg.get("pkey") or SSH_VAULT.get("_bot_key"))

    c.user_data["await"] = {"kind": "ssh_cmd", "id": sid}
    auth_badge = "✅ آماده اتصال" if has_auth else "⚠️ نیازمند تنظیم پسورد/کلید"

    txt = (
        f"💻 <b>ترمینال SSH (Termius)</b> — <b>{e(st['name'])}</b>\n"
        f"🌐 آدرس: <code>{e(cfg.get('user', 'root'))}@{e(st['ip'])}:{cfg.get('port', 22)}</code>\n"
        f"🔐 وضعیت احراز هویت: {auth_badge}\n"
        f"📁 مسیر فعلی: <code>{e(st['cwd'])}</code>\n\n"
        f"⌨️ <b>حالت ترمینال فعال است:</b>\n"
        f"• هر دستوری بنویسی مستقیماً در مسیر <code>{e(st['cwd'])}</code> اجرا می‌شود.\n"
        f"• هر فایلی در تلگرام بفرستی مستقیماً در همین پوشه آپلود می‌شود."
    )
    await show(u, txt, term_kb(sid))


async def ssh_exec_and_reply(u, c, sid, cmd):
    sid = str(sid)
    st = get_ssh_state(c, sid)
    c.user_data["await"] = {"kind": "ssh_cmd", "id": sid}

    msg = u.effective_message
    wait_msg = await msg.reply_text(f"⏳ <code>$ {e(cmd)}</code>", parse_mode="HTML")

    marker = f"__CWD_{int(time.time())}__"
    wrapped = f"cd {shlex.quote(st['cwd'])} 2>/dev/null || cd /; {cmd}; echo ''; echo '{marker}'; pwd"

    try:
        async with await connect_ssh(c, sid) as conn:
            res = await asyncio.wait_for(conn.run(wrapped, check=False), timeout=60)
    except asyncio.TimeoutError:
        return await wait_msg.edit_text("⏱ زمان اجرای دستور بیش از ۶۰ ثانیه شد (Timeout).",
                                        reply_markup=KM(term_kb(sid)))
    except Exception as ex:
        return await wait_msg.edit_text(
            f"❌ <b>خطای اتصال SSH:</b>\n<code>{e(ex)}</code>\n\n"
            f"از بخش «🔑 تنظیمات اتصال SSH» پسورد یا کلید سرور را بررسی کن.",
            parse_mode="HTML", reply_markup=KM(term_kb(sid)))

    out = (res.stdout or "") + (f"\n[stderr]\n{res.stderr}" if res.stderr else "")
    if marker in out:
        parts = out.rsplit(marker, 1)
        out = parts[0].rstrip()
        new_cwd = parts[1].strip().splitlines()
        if new_cwd:
            st["cwd"] = new_cwd[-1].strip()

    if not out.strip():
        out = "(بدون خروجی)"

    header = f"💻 <b>{e(st['name'])}</b>:<code>{e(st['cwd'])}</code>\n<code>$ {e(cmd)}</code>\n\n"
    if len(out) > 3500:
        await wait_msg.delete()
        bio = io.BytesIO(out.encode("utf-8", errors="replace"))
        bio.name = f"output-{sid}.txt"
        await msg.reply_document(
            document=bio,
            caption=f"{header}📄 خروجی طولانی بود و در قالب فایل ارسال شد.",
            parse_mode="HTML",
            reply_markup=KM(term_kb(sid))
        )
    else:
        await wait_msg.edit_text(
            f"{header}<pre>{e(out)}</pre>",
            parse_mode="HTML",
            reply_markup=KM(term_kb(sid))
        )


async def ssh_config_menu(u, c, sid):
    sid = str(sid)
    cfg = SSH_VAULT.get(sid, {})
    has_pw = "✅ ثبت شده" if cfg.get("password") else "❌ ندارد"
    has_pk = "✅ ثبت شده" if cfg.get("pkey") else ("🔑 کلید عمومی ربات" if SSH_VAULT.get("_bot_key") else "❌ ندارد")
    txt = (
        f"🔑 <b>تنظیمات اتصال SSH سرور #{sid}</b>\n"
        f"👤 کاربر: <code>{e(cfg.get('user', 'root'))}</code>\n"
        f"🔌 پورت: <code>{cfg.get('port', 22)}</code>\n"
        f"🔒 پسورد: {has_pw}\n"
        f"🗝 کلید خصوصی: {has_pk}"
    )
    rows = [
        [B("🔒 تنظیم پسورد", callback_data=f"sc_pw:{sid}"),
         B("🗝 ارسال کلید خصوصی (Private Key)", callback_data=f"sc_pk:{sid}")],
        [B("👤 تغییر یوزر و پورت", callback_data=f"sc_up:{sid}")],
        [B("⚡ ساخت و ثبت کلید خودکار ربات در هتزنر", callback_data=f"sc_gen:{sid}")],
        back(f"ssh:{sid}", "↩️ بازگشت به ترمینال"),
    ]
    await show(u, txt, rows)


async def ssh_gen_bot_key(u, c, sid):
    key = asyncssh.generate_private_key("ssh-ed25519")
    priv = key.export_private_key().decode("utf-8")
    pub = key.export_public_key().decode("utf-8").strip()
    SSH_VAULT["_bot_key"] = priv
    save_ssh()
    try:
        await api(c).req("POST", "/ssh_keys", json={"name": f"hbot-auto-{int(time.time())}", "public_key": pub})
        msg = "✅ کلید اختصاصی ربات ساخته شد و در SSH Keys اکانت هتزنر ثبت گردید!\n\n"
    except HErr as ex:
        msg = f"✅ کلید ساخته شد (خطا در ثبت خودکار هتزنر: {e(ex)}).\n\n"
    msg += f"برای افزودن به سرورهای فعلی، این دستور را یک‌بار در سرور اجرا کن:\n<code>mkdir -p ~/.ssh && echo '{e(pub)}' >> ~/.ssh/authorized_keys</code>"
    await show(u, msg, [back(f"sc:{sid}")])


async def sftp_browser(u, c, sid):
    sid = str(sid)
    st = get_ssh_state(c, sid)
    c.user_data["await"] = {"kind": "ssh_cmd", "id": sid}
    await show(u, f"⏳ در حال خواندن فایل‌های <code>{e(st['cwd'])}</code> ...")
    try:
        async with await connect_ssh(c, sid) as conn:
            async with conn.start_sftp_client() as sftp:
                items = await sftp.readdir(st["cwd"])
    except Exception as ex:
        return await show(u, f"❌ خطای SFTP:\n<code>{e(ex)}</code>", [back(f"ssh:{sid}")])

    dirs, files = [], []
    for it in items:
        name = it.filename
        if name in (".", ".."):
            continue
        mode = it.attrs.permissions or 0
        if stat.S_ISDIR(mode):
            dirs.append(name)
        else:
            files.append((name, it.attrs.size or 0))

    dirs.sort()
    files.sort(key=lambda x: x[0])
    st["sftp_dirs"] = dirs[:30]
    st["sftp_files"] = [f[0] for f in files[:30]]

    rows = []
    if st["cwd"] != "/":
        rows.append([B("⬆️ پوشه بالاتر (..)", callback_data=f"sfd:{sid}:..")])

    for idx, d in enumerate(st["sftp_dirs"]):
        rows.append([B(f"📁 {d}/", callback_data=f"sfd:{sid}:{idx}")])

    for idx, (fname, fsize) in enumerate(files[:30]):
        rows.append([
            B(f"📄 {fname[:22]} ({hb(fsize)})", callback_data=f"sff:{sid}:{idx}"),
            B("🗑", callback_data=f"sfrm:{sid}:{idx}")
        ])

    rows.append([B("🔄 رفرش", callback_data=f"sf:{sid}"),
                 B("💻 بازگشت به ترمینال", callback_data=f"ssh:{sid}")])

    txt = (
        f"📂 <b>فایل‌منیجر SFTP</b> — <b>{e(st['name'])}</b>\n"
        f"📍 مسیر: <code>{e(st['cwd'])}</code>\n\n"
        f"• برای <b>دانلود</b> روی نام فایل بزن.\n"
        f"• برای <b>آپلود در این پوشه</b>، کافیست فایل را همین‌جا در چت بفرستی!"
    )
    await show(u, txt, rows)


async def sftp_download_file(u, c, sid, remote_path):
    sid = str(sid)
    msg = u.effective_message
    wait_msg = await msg.reply_text(f"📥 در حال دریافت <code>{e(remote_path)}</code> از سرور ...", parse_mode="HTML")
    try:
        async with await connect_ssh(c, sid) as conn:
            async with conn.start_sftp_client() as sftp:
                st_attr = await sftp.stat(remote_path)
                if (st_attr.size or 0) > 49 * 1024 * 1024:
                    return await wait_msg.edit_text("❌ حجم فایل بیشتر از محدودیت ۵۰ مگابایت بات تلگرام است.")
                with tempfile.NamedTemporaryFile(delete=False) as tf:
                    tmp_path = tf.name
                await sftp.get(remote_path, tmp_path)
        await wait_msg.delete()
        with open(tmp_path, "rb") as f:
            await msg.reply_document(
                document=f,
                filename=posixpath.basename(remote_path),
                caption=f"📥 <code>{e(remote_path)}</code>",
                parse_mode="HTML"
            )
        os.remove(tmp_path)
    except Exception as ex:
        await wait_msg.edit_text(f"❌ خطا در دانلود فایل:\n<code>{e(ex)}</code>", parse_mode="HTML")


async def on_document(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not ok(u):
        return
    st_await = c.user_data.get("await") or {}
    if st_await.get("kind") not in ("ssh_cmd", "ssh_pkey"):
        return

    sid = st_await["id"]
    doc = u.message.document

    if st_await.get("kind") == "ssh_pkey":
        f = await doc.get_file()
        raw = await f.download_as_bytearray()
        cfg = SSH_VAULT.get(str(sid), {"user": "root", "port": 22})
        cfg["pkey"] = raw.decode("utf-8", errors="replace")
        SSH_VAULT[str(sid)] = cfg
        save_ssh()
        c.user_data.pop("await", None)
        return await show(u, "✅ کلید خصوصی با موفقیت ذخیره شد.", [back(f"sc:{sid}")])

    st = get_ssh_state(c, sid)
    remote_path = posixpath.join(st["cwd"], doc.file_name)
    wait_msg = await u.message.reply_text(f"📤 در حال آپلود به <code>{e(remote_path)}</code> ...", parse_mode="HTML")
    try:
        tg_file = await doc.get_file()
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tmp_path = tf.name
        await tg_file.download_to_drive(tmp_path)

        async with await connect_ssh(c, sid) as conn:
            async with conn.start_sftp_client() as sftp:
                await sftp.put(tmp_path, remote_path)
        os.remove(tmp_path)
        await wait_msg.edit_text(
            f"✅ فایل با موفقیت در سرور آپلود شد!\n📍 مسیر: <code>{e(remote_path)}</code>",
            parse_mode="HTML",
            reply_markup=KM(term_kb(sid))
        )
    except Exception as ex:
        await wait_msg.edit_text(f"❌ خطا در آپلود SFTP:\n<code>{e(ex)}</code>", parse_mode="HTML")


# ---------------------------------------------------------------- menu
async def menu(u, c):
    c.user_data.pop("await", None)
    if not APIS:
        return await show(u, "👋 <b>خوش اومدی!</b>\nبرای شروع باید اکانت هتزنر را به ربات وصل کنی (فقط دو مرحله).",
                          [[B("🔗 اتصال اکانت هتزنر", callback_data="pa")]])
    rows = [
        [B("🖥 سرورها (و ترمینال SSH)", callback_data="m:srv"), B("➕ ساخت سرور", callback_data="cs")],
        [B("📸 Snapshot/Backup", callback_data="m:img"), B("🔑 SSH Keys", callback_data="m:key")],
        [B("🛡 Firewall", callback_data="m:fw"), B("💾 Volume", callback_data="m:vol")],
        [B("🌐 Floating IP", callback_data="m:fip"), B("🔗 Network/LB/IP", callback_data="m:misc")],
        [B("📁 پروژه‌ها", callback_data="m:proj")],
    ]
    await show(u, f"🤖 <b>مدیریت Hetzner</b>\nپروژه فعال: <b>{e(proj(c))}</b>", rows)


async def proj_menu(u, c):
    rows = [[B(("✅ " if n == proj(c) else "▫️ ") + n, callback_data=f"pj:{i}"),
             B("🗑", callback_data=f"pd:{i}")] for i, n in enumerate(APIS)]
    rows.append([B("➕ افزودن اکانت/پروژه", callback_data="pa")])
    rows.append(HOME)
    await show(u, "📁 <b>پروژه‌ها</b>\nهر توکن API هتزنر = یک پروژه. روی اسم بزن تا فعال شود.", rows)


# ---------------------------------------------------------------- servers
async def srv_list(u, c):
    ss = await api(c).paged("/servers", "servers")
    rows = [[B(f"{st_icon(s['status'])} {s['name']} · {ip4(s)}", callback_data=f"s:{s['id']}"),
             B("💻 SSH", callback_data=f"ssh:{s['id']}")] for s in ss]
    rows.append([B("➕ ساخت سرور", callback_data="cs")])
    rows.append(HOME)
    await show(u, f"🖥 سرورها ({len(ss)})", rows)


async def srv_view(u, c, sid):
    c.user_data.pop("await", None)
    s = (await api(c).req("GET", f"/servers/{sid}"))["server"]
    pn = s.get("public_net") or {}
    t = s["server_type"]
    loc = (s.get("location") or (s.get("datacenter") or {}).get("location") or {}).get("name", "?")
    img = (s.get("image") or {}).get("description") or "—"
    prot = (s.get("protection") or {})
    txt = (
        f"{st_icon(s['status'])} <b>{e(s['name'])}</b>  <code>#{s['id']}</code>\n"
        f"وضعیت: {e(s['status'])}\n"
        f"IPv4: <code>{e(ip4(s))}</code>\n"
        f"IPv6: <code>{e((pn.get('ipv6') or {}).get('ip', '—'))}</code>\n"
        f"پلن: {e(t['name'])} · {t['cores']} vCPU · {t['memory']:g} GB RAM · {t['disk']} GB\n"
        f"لوکیشن: {e(loc)}\nایمیج: {e(img)}\n"
        f"ترافیک: ⬇️ {gb(s.get('ingoing_traffic'))} ⬆️ {gb(s.get('outgoing_traffic'))} "
        f"(سهمیه {gb(s.get('included_traffic'))})\n"
        f"بکاپ: {'روشن' if s.get('backup_window') else 'خاموش'} · "
        f"Rescue: {'روشن' if s.get('rescue_enabled') else 'خاموش'} · "
        f"محافظت: {'🔒' if prot.get('delete') else '—'}"
    )
    i = sid
    rows = [
        [B("💻 ترمینال SSH (Termius)", callback_data=f"ssh:{i}"),
         B("📂 مدیریت فایل SFTP", callback_data=f"sf:{i}")],
        [B("▶️ روشن", callback_data=f"a:{i}:poweron"), B("⏻ خاموش (نرم)", callback_data=f"y:{i}:shutdown"),
         B("⛔ خاموش (اجباری)", callback_data=f"y:{i}:poweroff")],
        [B("🔄 ریبوت", callback_data=f"y:{i}:reboot"), B("⚡ ریست سخت", callback_data=f"y:{i}:reset")],
        [B("🔑 ریست پسورد", callback_data=f"y:{i}:reset_password"),
         B("🛟 Rescue " + ("خاموش" if s.get("rescue_enabled") else "روشن"),
           callback_data=f"y:{i}:" + ("disable_rescue" if s.get("rescue_enabled") else "enable_rescue"))],
        [B("💿 ریبیلد", callback_data=f"rb:{i}"), B("📐 تغییر پلن", callback_data=f"rs:{i}")],
        [B("📸 اسنپ‌شات", callback_data=f"y:{i}:snapshot"),
         B("💽 بکاپ " + ("خاموش" if s.get("backup_window") else "روشن"),
           callback_data=f"y:{i}:" + ("disable_backup" if s.get("backup_window") else "enable_backup"))],
        [B("✏️ تغییر نام", callback_data=f"rn:{i}"), B("📊 متریک", callback_data=f"mt:{i}"),
         B("🔒 Protection", callback_data=f"a:{i}:protect")],
        [B("🔃 رفرش", callback_data=f"s:{i}"), B("🗑 حذف", callback_data=f"y:{i}:delete")],
        back("m:srv", "↩️ لیست سرورها"),
    ]
    await show(u, txt, rows)


ACT_FA = {
    "shutdown": "خاموش کردن نرم (ACPI)", "poweroff": "خاموش کردن اجباری", "reboot": "ریبوت نرم",
    "reset": "ریست سخت", "reset_password": "ریست پسورد root", "enable_rescue": "فعال‌سازی Rescue",
    "disable_rescue": "غیرفعال‌سازی Rescue", "snapshot": "ساخت اسنپ‌شات (هزینه دارد)",
    "enable_backup": "فعال‌سازی بکاپ (۲۰٪ هزینه سرور)", "disable_backup": "غیرفعال‌سازی بکاپ (بکاپ‌ها حذف می‌شوند)",
    "delete": "حذف کامل سرور — غیرقابل بازگشت",
}


async def do_action(u, c, sid, act):
    h = api(c)
    base = f"/servers/{sid}/actions/"
    extra = ""
    if act in ("poweron", "poweroff", "shutdown", "reboot", "reset"):
        r = await h.req("POST", base + act)
        await show(u, "⏳ در حال اجرا ...")
        await h.wait(r["action"]["id"], 90)
    elif act == "reset_password":
        r = await h.req("POST", base + "reset_password")
        await h.wait(r["action"]["id"], 60)
        save_srv_pass(sid, r.get("root_password"))
        extra = f"\n🔑 پسورد جدید (در SSH هم ذخیره شد): <code>{e(r['root_password'])}</code>"
    elif act == "enable_rescue":
        r = await h.req("POST", base + "enable_rescue", json={"type": "linux64"})
        save_srv_pass(sid, r.get("root_password"))
        extra = f"\n🛟 پسورد Rescue: <code>{e(r['root_password'])}</code>\nبرای ورود، سرور را ریست سخت کن."
    elif act == "disable_rescue":
        await h.req("POST", base + "disable_rescue")
    elif act == "snapshot":
        await h.req("POST", base + "create_image",
                    json={"type": "snapshot", "description": f"bot-{datetime.now():%Y%m%d-%H%M}"})
        extra = "\n📸 اسنپ‌شات در حال ساخت است (از منوی Snapshot ببین)."
    elif act in ("enable_backup", "disable_backup"):
        await h.req("POST", base + act)
    elif act == "protect":
        s = (await h.req("GET", f"/servers/{sid}"))["server"]
        new = not s["protection"]["delete"]
        r = await h.req("POST", base + "change_protection", json={"delete": new, "rebuild": new})
        await h.wait(r["action"]["id"], 30)
    elif act == "delete":
        await h.req("DELETE", f"/servers/{sid}")
        SSH_VAULT.pop(str(sid), None)
        save_ssh()
        await show(u, "🗑 سرور حذف شد.", [back("m:srv", "↩️ لیست سرورها")])
        return
    if extra:
        await show(u, f"✅ انجام شد.{extra}", [back(f"s:{sid}", "↩️ سرور")])
    else:
        await srv_view(u, c, sid)


async def ensure_off(h, sid):
    s = (await h.req("GET", f"/servers/{sid}"))["server"]
    if s["status"] == "off":
        return
    try:
        await h.req("POST", f"/servers/{sid}/actions/shutdown")
        for _ in range(30):
            await asyncio.sleep(3)
            if (await h.req("GET", f"/servers/{sid}"))["server"]["status"] == "off":
                return
    except HErr:
        pass
    r = await h.req("POST", f"/servers/{sid}/actions/poweroff")
    await h.wait(r["action"]["id"], 60)


async def server_arch(c, sid):
    s = (await api(c).req("GET", f"/servers/{sid}"))["server"]
    return s["server_type"]["architecture"]


async def image_picker(u, c, arch, prefix, kind, switch_cb, back_cb, title):
    h = api(c)
    if kind == "system":
        imgs = await h.paged("/images", "images", type="system", architecture=arch, status="available")
        imgs = [i for i in imgs if not i.get("deprecated")]
        imgs.sort(key=lambda i: (i.get("os_flavor") or "", i.get("name") or ""))
        items = [(i.get("description") or i["name"], i["name"]) for i in imgs]
    else:
        imgs = await h.paged("/images", "images", type="snapshot", architecture=arch)
        items = [(i.get("description") or str(i["id"]), str(i["id"])) for i in imgs]
    rows = grid(items, prefix)
    rows.append([B("📸 اسنپ‌شات‌ها" if kind == "system" else "💿 ایمیج‌های سیستمی", callback_data=switch_cb)])
    rows.append(back(back_cb))
    await show(u, title + ("" if items else "\n(موردی یافت نشد)"), rows)


async def metrics(u, c, sid):
    h = api(c)
    now = datetime.now(timezone.utc)
    f = "%Y-%m-%dT%H:%M:%SZ"
    r = await h.req("GET", f"/servers/{sid}/metrics",
                    params={"type": "cpu,network", "start": (now - timedelta(hours=1)).strftime(f),
                            "end": now.strftime(f)})
    ts = r["metrics"]["time_series"]
    lines = ["📊 <b>یک ساعت اخیر</b>"]
    for name, d in ts.items():
        vals = [float(v[1]) for v in d["values"] if v[1] is not None]
        if not vals:
            continue
        last, avg = vals[-1], sum(vals) / len(vals)
        if name == "cpu":
            lines.append(f"CPU: الان {last:.1f}% · میانگین {avg:.1f}%")
        elif "bandwidth" in name:
            lines.append(f"{e(name)}: الان {hb(last)}/s · میانگین {hb(avg)}/s")
        elif "pps" in name:
            lines.append(f"{e(name)}: الان {last:.0f} · میانگین {avg:.0f}")
    await show(u, "\n".join(lines), [back(f"s:{sid}", "↩️ سرور")])


# ---------------------------------------------------------------- create server wizard
async def new_start(u, c, image=None, arch=None):
    n = {"keys": [], "backups": False, "ud": None}
    if image:
        n["image"], n["arch"] = image, arch
    c.user_data["new"] = n
    c.user_data["await"] = {"kind": "newname"}
    await show(u, "➕ <b>ساخت سرور</b>\nنام سرور را بفرست (انگلیسی، عدد، خط تیره):", [HOME])


async def new_loc(u, c):
    locs = (await api(c).req("GET", "/locations"))["locations"]
    rows = [[B(f"{l['city']}, {l['country']} ({l['name']})", callback_data=f"nl:{l['name']}")] for l in locs]
    rows.append(HOME)
    await show(u, "📍 لوکیشن را انتخاب کن:", rows)


async def new_types(u, c):
    n = c.user_data["new"]
    loc = n["loc"]
    ts = await api(c).paged("/server_types", "server_types")
    out = []
    for t in ts:
        if t.get("deprecated"):
            continue
        if n.get("arch") and t["architecture"] != n["arch"]:
            continue
        locs = t.get("locations")
        if locs and loc not in [l.get("name") for l in locs]:
            continue
        pr = next((p for p in t["prices"] if p["location"] == loc), None)
        if not pr:
            continue
        out.append((float(pr["price_monthly"]["gross"]), t))
    out.sort(key=lambda x: x[0])
    rows = [[B(f"{t['name']} · {t['cores']}c/{t['memory']:g}G/{t['disk']}G · €{p:.2f}",
               callback_data=f"nt:{t['name']}")] for p, t in out]
    rows.append(HOME)
    await show(u, f"📐 پلن را انتخاب کن ({e(loc)}) — قیمت ماهانه با مالیات:", rows)


async def new_image_step(u, c, kind="system"):
    n = c.user_data["new"]
    await image_picker(u, c, n["arch"], "ni", kind, "nik:snapshot" if kind == "system" else "nik:system",
                       "m:menu", "💿 ایمیج را انتخاب کن:")


async def new_keys(u, c):
    h = api(c)
    n = c.user_data["new"]
    keys = await h.paged("/ssh_keys", "ssh_keys")
    if not keys:
        return await new_confirm(u, c)
    rows = [[B(("✅ " if k["id"] in n["keys"] else "⬜ ") + k["name"], callback_data=f"nk:{k['id']}")] for k in keys]
    rows.append([B("➡️ ادامه", callback_data="nk:done")])
    await show(u, "🔑 SSH Key(ها) را انتخاب کن (اگر هیچ‌کدام، پسورد root ساخته می‌شود):", rows)


async def new_confirm(u, c):
    n = c.user_data["new"]
    txt = (f"✅ <b>تأیید نهایی</b>\nنام: <code>{e(n['name'])}</code>\nلوکیشن: {e(n['loc'])}\n"
           f"پلن: {e(n['type'])}\nایمیج: {e(n['image'])}\nSSH Key: {len(n['keys'])} عدد\n"
           f"بکاپ: {'✅' if n['backups'] else '❌'}\ncloud-init: {'✅' if n['ud'] else '❌'}")
    rows = [[B("💽 بکاپ " + ("خاموش" if n["backups"] else "روشن"), callback_data="nb"),
             B("☁️ cloud-init", callback_data="nu")],
            [B("🚀 ساخت سرور", callback_data="ng")], HOME]
    await show(u, txt, rows)


async def new_go(u, c):
    n = c.user_data["new"]
    body = {"name": n["name"], "server_type": n["type"], "image": n["image"], "location": n["loc"],
            "ssh_keys": n["keys"], "start_after_create": True, "backups": n["backups"]}
    if n["ud"]:
        body["user_data"] = n["ud"]
    await show(u, "⏳ در حال ساخت سرور ...")
    r = await api(c).req("POST", "/servers", json=body)
    s = r["server"]
    c.user_data.pop("new", None)
    txt = (f"🎉 سرور ساخته شد!\n<b>{e(s['name'])}</b>\nIPv4: <code>{e(ip4(s))}</code>\n"
           f"IPv6: <code>{e(((s.get('public_net') or {}).get('ipv6') or {}).get('ip', '—'))}</code>")
    if r.get("root_password"):
        save_srv_pass(s["id"], r["root_password"])
        txt += f"\n🔑 root (ذخیره شده در SSH): <code>{e(r['root_password'])}</code>"
    await show(u, txt, [[B("💻 ورود به SSH", callback_data=f"ssh:{s['id']}"),
                         B("🖥 مدیریت سرور", callback_data=f"s:{s['id']}")], HOME])


# ---------------------------------------------------------------- images
async def img_list(u, c):
    imgs = await api(c).paged("/images", "images", type=["snapshot", "backup"], sort="created:desc")
    rows = [[B(f"{'📸' if i['type'] == 'snapshot' else '💽'} {(i.get('description') or i['id'])} · "
               f"{(i.get('image_size') or 0):.1f}GB", callback_data=f"g:{i['id']}")] for i in imgs]
    rows.append(HOME)
    await show(u, f"📸 Snapshot / Backup ({len(imgs)})", rows)


async def img_view(u, c, iid):
    i = (await api(c).req("GET", f"/images/{iid}"))["image"]
    txt = (f"{'📸' if i['type'] == 'snapshot' else '💽'} <b>{e(i.get('description') or i['id'])}</b>\n"
           f"ID: <code>{i['id']}</code>\nنوع: {i['type']} · {i['architecture']}\n"
           f"حجم: {(i.get('image_size') or 0):.2f} GB · دیسک: {i.get('disk_size')} GB\n"
           f"ساخته‌شده: {e(i['created'])}\nوضعیت: {i['status']}")
    rows = [[B("🖥 ساخت سرور از این ایمیج", callback_data=f"gn:{iid}")],
            [B("🗑 حذف", callback_data=f"dl:g:{iid}")], back("m:img")]
    await show(u, txt, rows)


# ---------------------------------------------------------------- ssh keys
async def key_list(u, c):
    ks = await api(c).paged("/ssh_keys", "ssh_keys")
    rows = [[B(f"🔑 {k['name']}", callback_data=f"k:{k['id']}")] for k in ks]
    rows.append([B("➕ افزودن کلید", callback_data="ka")])
    rows.append(HOME)
    await show(u, f"🔑 SSH Keys ({len(ks)})", rows)


async def key_view(u, c, kid):
    k = (await api(c).req("GET", f"/ssh_keys/{kid}"))["ssh_key"]
    await show(u, f"🔑 <b>{e(k['name'])}</b>\nFingerprint: <code>{e(k['fingerprint'])}</code>\n"
                  f"<code>{e(k['public_key'][:200])}</code>",
               [[B("🗑 حذف", callback_data=f"dl:k:{kid}")], back("m:key")])


# ---------------------------------------------------------------- firewalls
async def fw_list(u, c):
    fs = await api(c).paged("/firewalls", "firewalls")
    rows = [[B(f"🛡 {f['name']} ({len(f['rules'])} rule)", callback_data=f"f:{f['id']}")] for f in fs]
    rows.append([B("➕ فایروال جدید", callback_data="fc")])
    rows.append(HOME)
    await show(u, f"🛡 Firewalls ({len(fs)})", rows)


async def fw_view(u, c, fid):
    h = api(c)
    f = (await h.req("GET", f"/firewalls/{fid}"))["firewall"]
    names = {s["id"]: s["name"] for s in await h.paged("/servers", "servers")}
    lines = []
    for r in f["rules"]:
        ips = r.get("source_ips") if r["direction"] == "in" else r.get("destination_ips")
        lines.append(f"{'⬇️' if r['direction'] == 'in' else '⬆️'} {r['protocol']} {r.get('port') or ''} "
                     f"{', '.join(ips or [])}")
    applied = [a["server"]["id"] for a in f["applied_to"] if a["type"] == "server"]
    txt = (f"🛡 <b>{e(f['name'])}</b>\n" + ("\n".join(e(x) for x in lines) or "بدون قانون") +
           "\n\nاعمال‌شده روی: " + (", ".join(e(names.get(i, i)) for i in applied) or "—"))
    rows = [[B(f"➖ {names.get(i, i)}", callback_data=f"fr:{fid}:{i}")] for i in applied]
    rows += [[B("➕ اعمال روی سرور", callback_data=f"fa:{fid}"), B("✏️ قوانین (JSON)", callback_data=f"fj:{fid}")],
             [B("🗑 حذف", callback_data=f"dl:f:{fid}")], back("m:fw")]
    await show(u, txt, rows)


# ---------------------------------------------------------------- volumes
async def vol_list(u, c):
    vs = await api(c).paged("/volumes", "volumes")
    rows = [[B(f"💾 {v['name']} · {v['size']}GB", callback_data=f"v:{v['id']}")] for v in vs]
    rows.append([B("➕ ساخت Volume", callback_data="vc")])
    rows.append(HOME)
    await show(u, f"💾 Volumes ({len(vs)})", rows)


async def vol_view(u, c, vid):
    v = (await api(c).req("GET", f"/volumes/{vid}"))["volume"]
    txt = (f"💾 <b>{e(v['name'])}</b>\nحجم: {v['size']} GB · لوکیشن: {e(v['location']['name'])}\n"
           f"متصل به سرور: {v['server'] or '—'}\nدیوایس: <code>{e(v.get('linux_device'))}</code>")
    rows = [[B("🔌 اتصال به سرور", callback_data=f"va:{vid}"), B("⏏️ جدا کردن", callback_data=f"vd:{vid}")],
            [B("📏 افزایش حجم", callback_data=f"vr:{vid}"), B("🗑 حذف", callback_data=f"dl:v:{vid}")],
            back("m:vol")]
    await show(u, txt, rows)


# ---------------------------------------------------------------- floating IPs
async def fip_list(u, c):
    fs = await api(c).paged("/floating_ips", "floating_ips")
    rows = [[B(f"🌐 {f['ip']} → {f['server'] or '—'}", callback_data=f"i:{f['id']}")] for f in fs]
    rows.append([B("➕ Floating IP جدید", callback_data="ic")])
    rows.append(HOME)
    await show(u, f"🌐 Floating IPs ({len(fs)})", rows)


async def fip_view(u, c, fid):
    f = (await api(c).req("GET", f"/floating_ips/{fid}"))["floating_ip"]
    txt = (f"🌐 <code>{e(f['ip'])}</code> ({f['type']})\nنام: {e(f.get('name'))}\n"
           f"لوکیشن: {e(f['home_location']['name'])}\nمتصل به سرور: {f['server'] or '—'}")
    rows = [[B("🔌 اتصال", callback_data=f"ia:{fid}"), B("⏏️ جدا کردن", callback_data=f"iu:{fid}")],
            [B("🗑 حذف", callback_data=f"dl:i:{fid}")], back("m:fip")]
    await show(u, txt, rows)


async def misc(u, c):
    h = api(c)
    nets = await h.paged("/networks", "networks")
    lbs = await h.paged("/load_balancers", "load_balancers")
    pips = await h.paged("/primary_ips", "primary_ips")
    t = "🔗 <b>Networks</b>\n" + ("\n".join(f"• {e(n['name'])} {e(n['ip_range'])}" for n in nets) or "—")
    t += "\n\n⚖️ <b>Load Balancers</b>\n" + ("\n".join(
        f"• {e(l['name'])} {e(((l.get('public_net') or {}).get('ipv4') or {}).get('ip', ''))}" for l in lbs) or "—")
    t += "\n\n📍 <b>Primary IPs</b>\n" + ("\n".join(
        f"• {e(p['name'])} <code>{e(p['ip'])}</code> → {p.get('assignee_id') or '—'}" for p in pips) or "—")
    await show(u, t, [HOME])


async def server_picker(u, c, title, prefix, back_cb):
    ss = await api(c).paged("/servers", "servers")
    rows = [[B(f"{st_icon(s['status'])} {s['name']}", callback_data=f"{prefix}:{s['id']}")] for s in ss]
    rows.append(back(back_cb))
    await show(u, title, rows)


# ---------------------------------------------------------------- delete generic
RES = {"k": ("ssh_keys", "SSH Key", "m:key"), "f": ("firewalls", "Firewall", "m:fw"),
       "v": ("volumes", "Volume", "m:vol"), "i": ("floating_ips", "Floating IP", "m:fip"),
       "g": ("images", "Image", "m:img")}


# ---------------------------------------------------------------- router
async def route(u, c, k, a):
    if not APIS and not (k == "pa" or (k == "m" and a[0] in ("menu", "proj"))):
        return await menu(u, c)
    h = api(c) if APIS else None
    if k == "pa":
        c.user_data["await"] = {"kind": "pname"}
        await show(u, "🔗 <b>مرحله ۱ از ۲</b>\nیک اسم دلخواه برای این اکانت/پروژه بفرست (انگلیسی، مثلاً <code>main</code>):",
                   [HOME])
    elif k == "pd":
        name = list(APIS)[int(a[0])]
        await confirm(u, f"حذف پروژه «{name}» از ربات (به سرورهای هتزنر کاری ندارد؛ فقط توکن از ربات پاک می‌شود)",
                      f"pdy:{a[0]}", "m:proj")
    elif k == "pdy":
        name = list(APIS)[int(a[0])]
        await APIS.pop(name).c.aclose()
        TOKENS.pop(name, None)
        save_projects()
        c.user_data.pop("proj", None)
        await proj_menu(u, c)
    elif k == "m":
        await {"menu": menu, "srv": srv_list, "img": img_list, "key": key_list, "fw": fw_list,
               "vol": vol_list, "fip": fip_list, "misc": misc, "proj": proj_menu}[a[0]](u, c)
    elif k == "pj":
        c.user_data["proj"] = list(APIS)[int(a[0])]
        await menu(u, c)
    # --- SSH & SFTP routes
    elif k == "ssh":
        await ssh_home(u, c, a[0])
    elif k == "scd":
        get_ssh_state(c, a[0])["cwd"] = "/root"
        await ssh_home(u, c, a[0])
    elif k == "sc":
        await ssh_config_menu(u, c, a[0])
    elif k == "sc_pw":
        c.user_data["await"] = {"kind": "ssh_pw", "id": a[0]}
        await show(u, "🔒 پسورد SSH سرور را بفرست (پیام تو بلافاصله حذف می‌شود):", [back(f"sc:{a[0]}")])
    elif k == "sc_pk":
        c.user_data["await"] = {"kind": "ssh_pkey", "id": a[0]}
        await show(u, "🗝 متن کلید خصوصی (<code>-----BEGIN OPENSSH PRIVATE KEY-----</code>) را بفرست یا فایل آن را آپلود کن:",
                   [back(f"sc:{a[0]}")])
    elif k == "sc_up":
        c.user_data["await"] = {"kind": "ssh_up", "id": a[0]}
        await show(u, "👤 یوزر و پورت را به فرمت <code>user:port</code> بفرست (مثلاً <code>root:22</code> یا <code>ubuntu:2222</code>):",
                   [back(f"sc:{a[0]}")])
    elif k == "sc_gen":
        await ssh_gen_bot_key(u, c, a[0])
    elif k == "sn":
        sid = a[0]
        rows = [[B(title, callback_data=f"snr:{sid}:{key}")] for key, (title, _) in SNIPPETS.items()]
        rows.append(back(f"ssh:{sid}", "↩️ بازگشت به ترمینال"))
        await show(u, "⚡ <b>اسنیپت‌های آماده (Termius Snippets):</b>", rows)
    elif k == "snr":
        sid, key = a[0], a[1]
        _, cmd = SNIPPETS[key]
        await ssh_exec_and_reply(u, c, sid, cmd)
    elif k == "sdl":
        c.user_data["await"] = {"kind": "ssh_dlpath", "id": a[0]}
        await show(u, "📥 مسیر کامل فایل روی سرور را بفرست (مثلاً <code>/etc/nginx/nginx.conf</code>):",
                   [back(f"ssh:{a[0]}")])
    elif k == "sf":
        await sftp_browser(u, c, a[0])
    elif k == "sfd":
        sid, target = a[0], a[1]
        st = get_ssh_state(c, sid)
        if target == "..":
            st["cwd"] = posixpath.dirname(st["cwd"].rstrip("/")) or "/"
        else:
            dname = st.get("sftp_dirs", [])[int(target)]
            st["cwd"] = posixpath.join(st["cwd"], dname)
        await sftp_browser(u, c, sid)
    elif k == "sff":
        sid, idx = a[0], int(a[1])
        st = get_ssh_state(c, sid)
        fname = st.get("sftp_files", [])[idx]
        await sftp_download_file(u, c, sid, posixpath.join(st["cwd"], fname))
    elif k == "sfrm":
        sid, idx = a[0], int(a[1])
        st = get_ssh_state(c, sid)
        fname = st.get("sftp_files", [])[idx]
        await confirm(u, f"حذف فایل {fname} از سرور", f"sfrmy:{sid}:{idx}", f"sf:{sid}")
    elif k == "sfrmy":
        sid, idx = a[0], int(a[1])
        st = get_ssh_state(c, sid)
        fname = st.get("sftp_files", [])[idx]
        rpath = posixpath.join(st["cwd"], fname)
        async with await connect_ssh(c, sid) as conn:
            async with conn.start_sftp_client() as sftp:
                await sftp.remove(rpath)
        await sftp_browser(u, c, sid)
    # --- servers
    elif k == "s":
        await srv_view(u, c, a[0])
    elif k == "a":
        await do_action(u, c, a[0], a[1])
    elif k == "y":
        sid, act = a
        await confirm(u, ACT_FA[act], f"a:{sid}:{act}", f"s:{sid}")
    elif k == "mt":
        await metrics(u, c, a[0])
    elif k == "rn":
        c.user_data["await"] = {"kind": "rename", "id": a[0]}
        await show(u, "✏️ نام جدید سرور را بفرست:", [back(f"s:{a[0]}")])
    elif k == "rb":
        arch = await server_arch(c, a[0])
        c.user_data["arch_tmp"] = arch
        await image_picker(u, c, arch, f"rbc:{a[0]}", "system", f"rbk:{a[0]}:snapshot", f"s:{a[0]}",
                           "💿 ایمیج جدید برای ریبیلد (همه دیتا پاک می‌شود):")
    elif k == "rbk":
        arch = c.user_data.get("arch_tmp") or await server_arch(c, a[0])
        other = "system" if a[1] == "snapshot" else "snapshot"
        await image_picker(u, c, arch, f"rbc:{a[0]}", a[1], f"rbk:{a[0]}:{other}", f"s:{a[0]}",
                           "💿 ایمیج را انتخاب کن:")
    elif k == "rbc":
        await confirm(u, f"ریبیلد با ایمیج {a[1]} — تمام دیتای سرور پاک می‌شود", f"rbd:{a[0]}:{a[1]}", f"s:{a[0]}")
    elif k == "rbd":
        img = int(a[1]) if a[1].isdigit() else a[1]
        await show(u, "⏳ در حال ریبیلد ...")
        r = await h.req("POST", f"/servers/{a[0]}/actions/rebuild", json={"image": img})
        await h.wait(r["action"]["id"], 240)
        txt = "✅ ریبیلد انجام شد."
        if r.get("root_password"):
            save_srv_pass(a[0], r["root_password"])
            txt += f"\n🔑 root (ذخیره شده در SSH): <code>{e(r['root_password'])}</code>"
        await show(u, txt, [back(f"s:{a[0]}", "↩️ سرور")])
    elif k == "rs":
        sid = a[0]
        s = (await h.req("GET", f"/servers/{sid}"))["server"]
        arch = s["server_type"]["architecture"]
        loc = (s.get("location") or (s.get("datacenter") or {}).get("location") or {}).get("name")
        ts = [t for t in await h.paged("/server_types", "server_types")
              if t["architecture"] == arch and not t.get("deprecated")]
        rows = []
        for t in sorted(ts, key=lambda t: (t["cores"], t["memory"])):
            pr = next((p for p in t["prices"] if p["location"] == loc), None)
            rows.append([B(f"{t['name']} · {t['cores']}c/{t['memory']:g}G/{t['disk']}G"
                           + (f" · €{float(pr['price_monthly']['gross']):.2f}" if pr else ""),
                           callback_data=f"rst:{sid}:{t['name']}")])
        rows.append(back(f"s:{sid}"))
        await show(u, f"📐 پلن جدید (فعلی: {e(s['server_type']['name'])}) — سرور موقتاً خاموش می‌شود:", rows)
    elif k == "rst":
        sid, name = a
        await show(u, f"پلن جدید: <b>{e(name)}</b>\nدیسک هم بزرگ شود؟ (بعدش دیگر نمی‌شود کوچک‌تر کرد)",
                   [[B("فقط CPU/RAM (قابل برگشت)", callback_data=f"rsx:{sid}:{name}:0")],
                    [B("همراه دیسک (برگشت‌ناپذیر)", callback_data=f"rsx:{sid}:{name}:1")], back(f"s:{sid}")])
    elif k == "rsx":
        sid, name, up = a
        await show(u, "⏳ خاموش کردن و تغییر پلن ...")
        await ensure_off(h, sid)
        r = await h.req("POST", f"/servers/{sid}/actions/change_type",
                        json={"server_type": name, "upgrade_disk": up == "1"})
        await h.wait(r["action"]["id"], 300)
        r = await h.req("POST", f"/servers/{sid}/actions/poweron")
        await h.wait(r["action"]["id"], 90)
        await srv_view(u, c, sid)
    # --- create wizard
    elif k == "cs":
        await new_start(u, c)
    elif k == "nl":
        c.user_data["new"]["loc"] = a[0]
        await new_types(u, c)
    elif k == "nt":
        n = c.user_data["new"]
        t = (await h.req("GET", "/server_types", params={"name": a[0]}))["server_types"][0]
        n["type"], n["arch"] = t["name"], t["architecture"]
        await (new_keys(u, c) if n.get("image") else new_image_step(u, c))
    elif k == "nik":
        await new_image_step(u, c, a[0])
    elif k == "ni":
        c.user_data["new"]["image"] = int(a[0]) if a[0].isdigit() else a[0]
        await new_keys(u, c)
    elif k == "nk":
        n = c.user_data["new"]
        if a[0] == "done":
            await new_confirm(u, c)
        else:
            kid = int(a[0])
            n["keys"].remove(kid) if kid in n["keys"] else n["keys"].append(kid)
            await new_keys(u, c)
    elif k == "nb":
        n = c.user_data["new"]
        n["backups"] = not n["backups"]
        await new_confirm(u, c)
    elif k == "nu":
        c.user_data["await"] = {"kind": "newud"}
        await show(u, "☁️ متن cloud-init (مثلاً #cloud-config ...) را بفرست:", [back("nback")])
    elif k == "nback":
        await new_confirm(u, c)
    elif k == "ng":
        await new_go(u, c)
    elif k == "gn":
        i = (await h.req("GET", f"/images/{a[0]}"))["image"]
        await new_start(u, c, image=i["id"], arch=i["architecture"])
        c.user_data["new"]["image"] = i["id"]
    # --- images
    elif k == "g":
        await img_view(u, c, a[0])
    # --- ssh keys
    elif k == "k":
        await key_view(u, c, a[0])
    elif k == "ka":
        c.user_data["await"] = {"kind": "keyname"}
        await show(u, "🔑 نام کلید را بفرست:", [back("m:key")])
    # --- firewalls
    elif k == "f":
        await fw_view(u, c, a[0])
    elif k == "fc":
        c.user_data["await"] = {"kind": "fwname"}
        await show(u, "🛡 نام فایروال جدید را بفرست (قوانین پیش‌فرض: SSH/HTTP/HTTPS/ICMP):", [back("m:fw")])
    elif k == "fa":
        await server_picker(u, c, "روی کدام سرور اعمال شود؟", f"fas:{a[0]}", f"f:{a[0]}")
    elif k in ("fas", "fr"):
        act = "apply_to_resources" if k == "fas" else "remove_from_resources"
        key = "apply_to" if k == "fas" else "remove_from"
        await h.req("POST", f"/firewalls/{a[0]}/actions/{act}",
                    json={key: [{"type": "server", "server": {"id": int(a[1])}}]})
        await fw_view(u, c, a[0])
    elif k == "fj":
        c.user_data["await"] = {"kind": "fwrules", "id": a[0]}
        await show(u, "✏️ آرایه JSON قوانین را بفرست. مثال:\n<code>[{\"direction\":\"in\",\"protocol\":\"tcp\","
                      "\"port\":\"22\",\"source_ips\":[\"0.0.0.0/0\",\"::/0\"]}]</code>", [back(f"f:{a[0]}")])
    # --- volumes
    elif k == "v":
        await vol_view(u, c, a[0])
    elif k == "vc":
        c.user_data["await"] = {"kind": "volname"}
        await show(u, "💾 نام Volume را بفرست:", [back("m:vol")])
    elif k == "vcl":
        v = c.user_data.pop("vol")
        r = await h.req("POST", "/volumes", json={"name": v["name"], "size": v["size"], "location": a[0],
                                                   "format": "ext4"})
        await vol_view(u, c, r["volume"]["id"])
    elif k == "va":
        await server_picker(u, c, "به کدام سرور وصل شود؟ (هم‌لوکیشن)", f"vas:{a[0]}", f"v:{a[0]}")
    elif k == "vas":
        await h.req("POST", f"/volumes/{a[0]}/actions/attach", json={"server": int(a[1]), "automount": False})
        await vol_view(u, c, a[0])
    elif k == "vd":
        await h.req("POST", f"/volumes/{a[0]}/actions/detach")
        await vol_view(u, c, a[0])
    elif k == "vr":
        c.user_data["await"] = {"kind": "volresize", "id": a[0]}
        await show(u, "📏 حجم جدید (GB) را بفرست (باید بیشتر از فعلی باشد):", [back(f"v:{a[0]}")])
    # --- floating ips
    elif k == "i":
        await fip_view(u, c, a[0])
    elif k == "ic":
        await show(u, "نوع IP:", [[B("IPv4", callback_data="ict:ipv4"), B("IPv6", callback_data="ict:ipv6")],
                                  back("m:fip")])
    elif k == "ict":
        locs = (await h.req("GET", "/locations"))["locations"]
        rows = [[B(f"{l['city']} ({l['name']})", callback_data=f"icl:{a[0]}:{l['name']}")] for l in locs]
        await show(u, "لوکیشن:", rows + [back("m:fip")])
    elif k == "icl":
        r = await h.req("POST", "/floating_ips", json={"type": a[0], "home_location": a[1]})
        await fip_view(u, c, r["floating_ip"]["id"])
    elif k == "ia":
        await server_picker(u, c, "به کدام سرور وصل شود؟", f"ias:{a[0]}", f"i:{a[0]}")
    elif k == "ias":
        await h.req("POST", f"/floating_ips/{a[0]}/actions/assign", json={"server": int(a[1])})
        await fip_view(u, c, a[0])
    elif k == "iu":
        await h.req("POST", f"/floating_ips/{a[0]}/actions/unassign")
        await fip_view(u, c, a[0])
    # --- generic delete
    elif k == "dl":
        _, label, bk = RES[a[0]]
        await confirm(u, f"حذف {label} #{a[1]}", f"dy:{a[0]}:{a[1]}", bk)
    elif k == "dy":
        path, label, bk = RES[a[0]]
        await h.req("DELETE", f"/{path}/{a[1]}")
        await show(u, f"🗑 {label} حذف شد.", [back(bk), HOME[0]])


async def on_cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if not ok(u):
        return
    p = q.data.split(":")
    try:
        await route(u, c, p[0], p[1:])
    except HErr as ex:
        await show(u, f"❌ خطا:\n<code>{e(ex)}</code>", [HOME])
    except httpx.HTTPError as ex:
        await show(u, f"❌ خطای شبکه:\n<code>{e(ex)}</code>", [HOME])
    except (KeyError, IndexError):
        log.exception("state error")
        await show(u, "❌ وضعیت منقضی شده، از اول شروع کن.", [HOME])


# ---------------------------------------------------------------- text input
async def on_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not ok(u):
        return
    st = c.user_data.pop("await", None)
    if not st:
        return
    kind, t = st["kind"], u.message.text.strip()
    h = api(c) if APIS else None
    try:
        if kind == "ssh_cmd":
            await ssh_exec_and_reply(u, c, st["id"], t)
        elif kind == "ssh_pw":
            try:
                await u.message.delete()
            except Exception:
                pass
            save_srv_pass(st["id"], t)
            await ssh_config_menu(u, c, st["id"])
        elif kind == "ssh_pkey":
            try:
                await u.message.delete()
            except Exception:
                pass
            cfg = SSH_VAULT.get(str(st["id"]), {"user": "root", "port": 22})
            cfg["pkey"] = u.message.text.strip() + "\n"
            SSH_VAULT[str(st["id"])] = cfg
            save_ssh()
            await ssh_config_menu(u, c, st["id"])
        elif kind == "ssh_up":
            user, port = (t.split(":", 1) + ["22"])[:2]
            cfg = SSH_VAULT.get(str(st["id"]), {})
            cfg["user"] = user.strip() or "root"
            cfg["port"] = int(port.strip())
            SSH_VAULT[str(st["id"])] = cfg
            save_ssh()
            await ssh_config_menu(u, c, st["id"])
        elif kind == "ssh_dlpath":
            c.user_data["await"] = {"kind": "ssh_cmd", "id": st["id"]}
            await sftp_download_file(u, c, st["id"], t)
        elif kind == "pname":
            if not re.fullmatch(r"[A-Za-z0-9_\-]{1,20}", t) or t in APIS:
                c.user_data["await"] = st
                return await show(u, "❌ اسم نامعتبر یا تکراری است (فقط انگلیسی/عدد/_/-). دوباره بفرست:")
            c.user_data["await"] = {"kind": "ptoken", "name": t}
            await show(u, "🔗 <b>مرحله ۲ از ۲</b>\nتوکن API هتزنر را بفرست.\n\n"
                          "طرز گرفتن توکن:\n1) وارد console.hetzner.cloud شو و پروژه را باز کن\n"
                          "2) Security ← API Tokens ← Generate API Token\n"
                          "3) دسترسی را <b>Read &amp; Write</b> بزن و توکن را کپی کن\n\n"
                          "🔒 پیام تو بعد از خواندن توکن به‌صورت خودکار پاک می‌شود.", [HOME])
        elif kind == "ptoken":
            try:
                await u.message.delete()
            except Exception:
                pass
            cand = Hetzner(t)
            try:
                await cand.req("GET", "/servers", params={"per_page": 1})
            except (HErr, httpx.HTTPError) as ex:
                await cand.c.aclose()
                c.user_data["await"] = st
                return await show(u, f"❌ توکن قبول نشد:\n<code>{e(ex)}</code>\nتوکن درست را دوباره بفرست:")
            APIS[st["name"]] = cand
            TOKENS[st["name"]] = t
            save_projects()
            c.user_data["proj"] = st["name"]
            await show(u, f"✅ پروژه <b>{e(st['name'])}</b> وصل شد!", [[B("🏠 ورود به منوی اصلی", callback_data="m:menu")]])
        elif kind == "newname":
            if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9\-\.]{0,61}[A-Za-z0-9])?", t):
                c.user_data["await"] = st
                return await show(u, "❌ نام نامعتبر. دوباره بفرست:")
            c.user_data["new"]["name"] = t
            await new_loc(u, c)
        elif kind == "newud":
            c.user_data["new"]["ud"] = t
            await new_confirm(u, c)
        elif kind == "rename":
            await h.req("PUT", f"/servers/{st['id']}", json={"name": t})
            await srv_view(u, c, st["id"])
        elif kind == "keyname":
            c.user_data["await"] = {"kind": "keypub", "name": t}
            await show(u, "حالا public key را بفرست (ssh-ed25519 AAAA...):")
        elif kind == "keypub":
            await h.req("POST", "/ssh_keys", json={"name": st["name"], "public_key": t})
            await key_list(u, c)
        elif kind == "fwname":
            rules = [{"direction": "in", "protocol": "tcp", "port": p, "source_ips": ["0.0.0.0/0", "::/0"]}
                     for p in ("22", "80", "443")]
            rules.append({"direction": "in", "protocol": "icmp", "source_ips": ["0.0.0.0/0", "::/0"]})
            r = await h.req("POST", "/firewalls", json={"name": t, "rules": rules})
            await fw_view(u, c, r["firewall"]["id"])
        elif kind == "fwrules":
            await h.req("POST", f"/firewalls/{st['id']}/actions/set_rules", json={"rules": json.loads(t)})
            await fw_view(u, c, st["id"])
        elif kind == "volname":
            c.user_data["vol"] = {"name": t}
            c.user_data["await"] = {"kind": "volsize"}
            await show(u, "اندازه (GB، حداقل 10):")
        elif kind == "volsize":
            c.user_data["vol"]["size"] = int(t)
            locs = (await h.req("GET", "/locations"))["locations"]
            rows = [[B(f"{l['city']} ({l['name']})", callback_data=f"vcl:{l['name']}")] for l in locs]
            await show(u, "لوکیشن Volume:", rows)
        elif kind == "volresize":
            await h.req("POST", f"/volumes/{st['id']}/actions/resize", json={"size": int(t)})
            await vol_view(u, c, st["id"])
    except (HErr, httpx.HTTPError) as ex:
        await show(u, f"❌ خطا:\n<code>{e(ex)}</code>", [HOME])
    except ValueError:
        await show(u, "❌ ورودی نامعتبر.", [HOME])


async def on_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not ok(u):
        return await u.message.reply_text(f"⛔ دسترسی نداری. آیدی تو: {u.effective_user.id}")
    await menu(u, c)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "menu"], on_start))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot started. Projects: %s", ", ".join(APIS))
    app.run_polling()


if __name__ == "__main__":
    main()
