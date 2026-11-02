#!/usr/bin/env python3
"""
Hetzner Cloud Manager - Telegram Bot
Env vars (set by install.sh):
  BOT_TOKEN   توکن ربات تلگرام
  ADMIN_IDS   آیدی عددی ادمین‌ها (با کاما)
  DATA_DIR    مسیر ذخیره پروژه‌ها/توکن‌های هتزنر (پیش‌فرض ./data)
توکن هتزنر از داخل خود ربات (مرحله‌به‌مرحله) گرفته می‌شود.
"""
import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
import asyncssh
import pyte
import secrets
import io
import stat as pystat
from cryptography.fernet import Fernet
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as KM, Update
from telegram.error import BadRequest, RetryAfter
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
APIS = {}
TOKENS = {}


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


load_projects()


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
            return await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb,
                                             disable_web_page_preview=True)
        return await u.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb,
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


# ---------------------------------------------------------------- menu
async def menu(u, c):
    c.user_data.pop("await", None)
    if not APIS:
        return await show(u, "👋 <b>خوش اومدی!</b>\nبرای شروع باید اکانت هتزنر را به ربات وصل کنی (فقط دو مرحله).",
                          [[B("🔗 اتصال اکانت هتزنر", callback_data="pa")]])
    rows = [
        [B("🖥 سرورها", callback_data="m:srv"), B("➕ ساخت سرور", callback_data="cs")],
        [B("📸 Snapshot/Backup", callback_data="m:img"), B("🔑 SSH Keys", callback_data="m:key")],
        [B("🛡 Firewall", callback_data="m:fw"), B("💾 Volume", callback_data="m:vol")],
        [B("🌐 Floating IP", callback_data="m:fip"), B("🔗 Network/LB/IP", callback_data="m:misc")],
        [B("💻 SSH / ترمینال", callback_data="x:menu"), B("📁 پروژه‌ها", callback_data="m:proj")],
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
    rows = [[B(f"{st_icon(s['status'])} {s['name']} · {ip4(s)}", callback_data=f"s:{s['id']}")] for s in ss]
    rows.append([B("➕ ساخت سرور", callback_data="cs")])
    rows.append(HOME)
    await show(u, f"🖥 سرورها ({len(ss)})", rows)


async def srv_view(u, c, sid):
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
        [B("💻 SSH / ترمینال", callback_data=f"x:srv:{i}")],
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
        extra = f"\n🔑 پسورد جدید: <code>{e(r['root_password'])}</code>"
    elif act == "enable_rescue":
        r = await h.req("POST", base + "enable_rescue", json={"type": "linux64"})
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
        txt += f"\n🔑 root: <code>{e(r['root_password'])}</code>"
    await show(u, txt, [[B("🖥 مدیریت سرور", callback_data=f"s:{s['id']}")], HOME])


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
    if not APIS and not (k in ("pa", "x") or (k == "m" and a[0] in ("menu", "proj"))):
        return await menu(u, c)
    h = api(c) if APIS else None
    if k == "x":
        await ssh_route(u, c, a)
    elif k == "pa":
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
            txt += f"\n🔑 root: <code>{e(r['root_password'])}</code>"
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
    except Exception as ex:
        log.exception("unexpected")
        await show(u, f"❌ خطای غیرمنتظره:\n<code>{e(ex)}</code>", [HOME])


# ---------------------------------------------------------------- text input
async def on_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not ok(u):
        return
    st = c.user_data.pop("await", None)
    if not st:
        sess = SESS.get(u.effective_user.id)
        if sess:
            await term_input(u, c, sess)
        return
    kind, t = st["kind"], u.message.text.strip()
    if kind in SSH_KINDS:
        return await ssh_text(u, c, kind, st, u.message.text)
    h = api(c) if APIS else None
    try:
        if kind == "pname":
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
            r = await h.req("PUT", f"/servers/{st['id']}", json={"name": t})
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
            import json
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


# ================================================================ SSH / Terminal (Termius-like)
COLS, ROWS = 56, 22
IDLE_SECONDS = 1800
SSH_FILE = os.path.join(DATA_DIR, "ssh.json")
DEFAULT_SNIPPETS = [
    {"name": "Update & Upgrade", "cmd": "apt update && apt upgrade -y"},
    {"name": "Disk", "cmd": "df -h"},
    {"name": "Memory", "cmd": "free -h"},
    {"name": "Top CPU", "cmd": "ps aux --sort=-%cpu | head -n 10"},
]
DB = {"hosts": {}, "keys": {}, "snippets": list(DEFAULT_SNIPPETS)}
SESS = {}
_F = None


def db_load():
    try:
        with open(SSH_FILE, encoding="utf-8") as f:
            DB.update(json.load(f))
    except FileNotFoundError:
        pass


def db_save():
    os.makedirs(DATA_DIR, exist_ok=True)
    fd = os.open(SSH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(DB, f)


db_load()


def fer():
    global _F
    if _F is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        kf = os.path.join(DATA_DIR, "enc.key")
        if not os.path.exists(kf):
            fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        with open(kf, "rb") as f:
            _F = Fernet(f.read())
    return _F


def enc(s):
    return fer().encrypt(s.encode()).decode()


def dec(s):
    return fer().decrypt(s.encode()).decode()


class HostKeyChanged(Exception):
    pass


async def ssh_connect(hid):
    h = DB["hosts"][hid]
    kw = dict(host=h["host"], port=h["port"], username=h["user"], connect_timeout=15,
              keepalive_interval=30, client_keys=None, config=[])
    if h["auth"] == "password":
        kw.update(password=dec(h["secret"]), preferred_auth=("password", "keyboard-interactive"))
    else:
        k = DB["keys"][h["key"]]
        kw.update(client_keys=[asyncssh.import_private_key(dec(k["priv"]))], password=None,
                  preferred_auth=("publickey",))
    pat = h["host"] if h["port"] == 22 else f"[{h['host']}]:{h['port']}"
    kw["known_hosts"] = asyncssh.import_known_hosts(f"{pat} {h['hostkey']}") if h.get("hostkey") else None
    try:
        conn = await asyncssh.connect(**kw)
    except asyncssh.HostKeyNotVerifiable:
        raise HostKeyChanged()
    if not h.get("hostkey"):
        key = getattr(conn, "get_server_host_key", None)
        key = key() if key else conn._server_host_key
        h["hostkey"] = key.export_public_key().decode().strip()
        h["fp"] = key.get_fingerprint()
        db_save()
    return conn


class Sess:
    def __init__(s, bot, uid, hid, name, chat, mid, conn, proc):
        s.bot, s.uid, s.hid, s.name, s.chat, s.mid, s.conn, s.proc = bot, uid, hid, name, chat, mid, conn, proc
        s.screen = pyte.Screen(COLS, ROWS)
        s.stream = pyte.Stream(s.screen)
        s.dirty = s.closed = False
        s.last = ""
        s.last_in = time.time()
        s.lock = asyncio.Lock()
        s.tasks = []


TERM_KB = [
    [B("⏎", callback_data="x:t:enter"), B("^C", callback_data="x:t:c"), B("^D", callback_data="x:t:d"),
     B("^Z", callback_data="x:t:z"), B("Tab", callback_data="x:t:tab")],
    [B("↑", callback_data="x:t:up"), B("↓", callback_data="x:t:down"), B("←", callback_data="x:t:left"),
     B("→", callback_data="x:t:right"), B("Esc", callback_data="x:t:esc")],
    [B("^X", callback_data="x:t:x"), B("^O", callback_data="x:t:o"), B("^A", callback_data="x:t:a"),
     B("^E", callback_data="x:t:e"), B("^L", callback_data="x:t:l")],
    [B("Home", callback_data="x:t:home"), B("End", callback_data="x:t:end"),
     B("PgUp", callback_data="x:t:pgup"), B("PgDn", callback_data="x:t:pgdn"), B("Del", callback_data="x:t:del")],
    [B("📜 Snippets", callback_data="x:snip"), B("🔄", callback_data="x:t:refresh"),
     B("❌ بستن", callback_data="x:close")],
]
KEYMAP = {"enter": "\r", "c": "\x03", "d": "\x04", "z": "\x1a", "tab": "\t", "esc": "\x1b",
          "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
          "x": "\x18", "o": "\x0f", "a": "\x01", "e": "\x05", "l": "\x0c",
          "home": "\x1b[H", "end": "\x1b[F", "pgup": "\x1b[5~", "pgdn": "\x1b[6~", "del": "\x1b[3~"}


def render(s):
    lines = [l.rstrip() for l in s.screen.display]
    while lines and not lines[-1]:
        lines.pop()
    while True:
        body = e("\n".join(lines)) or " "
        out = f"<b>💻 {e(s.name)}</b>\n<pre>{body}</pre>"
        if len(out) <= 3900 or len(lines) <= 1:
            return out
        lines = lines[1:]


async def push(s, force=False):
    async with s.lock:
        txt = render(s)
        if txt == s.last and not force:
            return
        try:
            await s.bot.edit_message_text(chat_id=s.chat, message_id=s.mid, text=txt, parse_mode="HTML",
                                          reply_markup=KM(TERM_KB))
            s.last = txt
        except BadRequest as ex:
            if "not modified" in str(ex).lower():
                s.last = txt
            else:
                log.warning("push: %s", ex)
        except RetryAfter as ex:
            await asyncio.sleep(ex.retry_after + 0.5)
        except Exception:
            log.exception("push failed")


async def reader(s):
    try:
        while True:
            d = await s.proc.stdout.read(4096)
            if not d:
                break
            s.stream.feed(d)
            s.dirty = True
    except Exception:
        pass
    await term_close(s.uid, "🔌 ارتباط توسط سرور بسته شد")


async def pump(s):
    while not s.closed:
        await asyncio.sleep(0.8)
        if s.dirty:
            s.dirty = False
            await push(s)
        if time.time() - s.last_in > IDLE_SECONDS:
            await term_close(s.uid, "⏱ به‌خاطر عدم فعالیت (۳۰ دقیقه) بسته شد")
            return


async def term_close(uid, note="🔌 ارتباط بسته شد"):
    s = SESS.pop(uid, None)
    if not s:
        return
    s.closed = True
    cur = asyncio.current_task()
    for t in s.tasks:
        if t is not cur:
            t.cancel()
    try:
        s.proc.close()
        s.conn.close()
    except Exception:
        pass
    try:
        await s.bot.edit_message_text(chat_id=s.chat, message_id=s.mid, text=render(s) + f"\n\n{note}",
                                      parse_mode="HTML", reply_markup=KM([[B("💻 SSH", callback_data="x:menu")]]))
    except Exception:
        pass


async def term_start(u, c, hid):
    uid = u.effective_user.id
    await term_close(uid)
    h = DB["hosts"].get(hid)
    if not h:
        return await show(u, "❌ سرور پیدا نشد.", [back("x:menu")])
    c.user_data.pop("await", None)
    m = await show(u, f"⏳ اتصال به <b>{e(h['name'])}</b> ...")
    mid = m.message_id if hasattr(m, "message_id") else u.callback_query.message.message_id
    try:
        conn = await ssh_connect(hid)
        proc = await conn.create_process(term_type="xterm", term_size=(COLS, ROWS), encoding="utf-8",
                                         errors="replace")
    except HostKeyChanged:
        return await show(u, "🚨 <b>کلید (fingerprint) سرور عوض شده!</b>\nاگر سرور را ریبیلد کرده‌ای عادی است؛ "
                             "وگرنه ممکن است حمله MITM باشد. اتصال انجام نشد.",
                          [[B("🔓 قبول کلید جدید", callback_data=f"x:rf:{hid}")], back("x:menu")])
    except Exception as ex:
        return await show(u, f"❌ اتصال ناموفق:\n<code>{e(ex)}</code>",
                          [[B("🔁 تلاش دوباره", callback_data=f"x:c:{hid}")], back("x:menu")])
    s = Sess(c.bot, uid, hid, h["name"], u.effective_chat.id, mid, conn, proc)
    SESS[uid] = s
    s.tasks = [asyncio.create_task(reader(s)), asyncio.create_task(pump(s))]
    await asyncio.sleep(1)
    await push(s, force=True)


async def term_send(s, data):
    s.proc.stdin.write(data)
    s.last_in = time.time()
    await asyncio.sleep(0.7)
    await push(s)


async def term_input(u, c, s):
    raw = u.message.text
    try:
        await u.message.delete()
    except Exception:
        pass
    m = re.fullmatch(r"\^([A-Za-z\[\]\\])", raw.strip())
    data = chr(ord(m.group(1).upper()) - 64) if m else raw.replace("\n", "\r") + "\r"
    await term_send(s, data)


def gen_key(name):
    k = asyncssh.generate_private_key("ssh-ed25519")
    try:
        k.set_comment("hetzner-bot")
    except Exception:
        pass
    kid = secrets.token_hex(3)
    DB["keys"][kid] = {"name": name, "priv": enc(k.export_private_key().decode()),
                       "pub": k.export_public_key().decode().strip()}
    db_save()
    return kid


async def ssh_menu(u, c):
    s = SESS.get(u.effective_user.id)
    rows = []
    if s:
        rows.append([B(f"🟢 بازگشت به ترمینال ({s.name})", callback_data="x:term")])
    for hid, h in DB["hosts"].items():
        rows.append([B(f"💻 {h['name']} · {h['host']}", callback_data=f"x:h:{hid}")])
    rows += [[B("➕ سرور جدید", callback_data="x:ha"), B("🔑 کلیدها", callback_data="x:ks")],
             [B("📜 Snippets", callback_data="x:sn"), B("⚡ اجرا روی چند سرور", callback_data="x:mx")], HOME]
    await show(u, "💻 <b>SSH</b>\nترمینال زنده با دکمه‌های کنترلی، آپلود/دانلود فایل (SFTP)، Snippet و اجرای گروهی.", rows)


async def host_view(u, c, hid):
    h = DB["hosts"][hid]
    auth = "🔐 پسورد" if h["auth"] == "password" else f"🔑 کلید ({e(DB['keys'].get(h.get('key'), {}).get('name', '?'))})"
    fp = h.get("fp")
    txt = (f"💻 <b>{e(h['name'])}</b>\n<code>{e(h['user'])}@{e(h['host'])}:{h['port']}</code>\n"
           f"احراز هویت: {auth}\nFingerprint: <code>{e(fp) if fp else 'هنوز ثبت نشده (اولین اتصال ثبت می‌شود)'}</code>")
    rows = [[B("🔌 اتصال", callback_data=f"x:c:{hid}")],
            [B("🔓 ریست fingerprint", callback_data=f"x:rf:{hid}"), B("🗑 حذف", callback_data=f"x:hd:{hid}")],
            back("x:menu")]
    await show(u, txt, rows)


async def ask_user(u, c):
    c.user_data["await"] = {"kind": "huser"}
    await show(u, "👤 یوزرنیم SSH را بفرست (یا دکمه root):", [[B("root", callback_data="x:hu:root")], HOME])


async def ask_auth(u, c):
    c.user_data.pop("await", None)
    rows = [[B(f"🔑 {k['name']}", callback_data=f"x:au:k:{kid}")] for kid, k in DB["keys"].items()]
    rows += [[B("✨ ساخت کلید جدید (پیشنهادی)", callback_data="x:au:gen")],
             [B("📝 چسباندن Private Key", callback_data="x:au:paste")],
             [B("🔐 پسورد", callback_data="x:au:pw")], HOME]
    await show(u, "🔐 روش احراز هویت:", rows)


async def save_host(u, c):
    n = c.user_data.pop("nh")
    hid = secrets.token_hex(3)
    DB["hosts"][hid] = {"name": n["name"], "host": n["host"], "port": n["port"], "user": n["user"],
                        "auth": n["auth"], "key": n.get("key"), "secret": n.get("secret"), "hostkey": None, "fp": None}
    db_save()
    extra = ""
    rows = [[B("🔌 اتصال", callback_data=f"x:c:{hid}")], back("x:menu")]
    if n.get("new_key"):
        k = DB["keys"][n["key"]]
        extra = (f"\n\n🔑 کلید عمومی ساخته‌شده:\n<code>{e(k['pub'])}</code>\n"
                 "این کلید باید روی سرور در <code>~/.ssh/authorized_keys</code> باشد. "
                 "برای سرورهای جدید، آن را به Hetzner اضافه کن و موقع ساخت سرور انتخابش کن.")
        if APIS:
            rows.insert(1, [B("➕ افزودن به Hetzner SSH Keys", callback_data=f"x:kh:{n['key']}")])
    await show(u, f"✅ ذخیره شد: <b>{e(n['name'])}</b>{extra}", rows)


async def ssh_text(u, c, kind, st, raw):
    t = raw.strip()
    try:
        if kind == "hhost":
            host, port = t, 22
            if t.count(":") == 1:
                host, p = t.split(":")
                port = int(p)
            if not host:
                raise ValueError
            c.user_data["nh"] = {"host": host, "port": port, "name": host}
            await ask_user(u, c)
        elif kind == "huser":
            c.user_data["nh"]["user"] = t
            await ask_auth(u, c)
        elif kind in ("hpw", "hpaste", "kpaste"):
            try:
                await u.message.delete()
            except Exception:
                pass
            if kind == "hpw":
                n = c.user_data["nh"]
                n.update(auth="password", secret=enc(raw))
                await save_host(u, c)
            else:
                k = asyncssh.import_private_key(raw)
                kid = secrets.token_hex(3)
                DB["keys"][kid] = {"name": f"key-{kid}", "priv": enc(raw),
                                   "pub": k.export_public_key().decode().strip()}
                db_save()
                if kind == "hpaste":
                    n = c.user_data["nh"]
                    n.update(auth="key", key=kid)
                    await save_host(u, c)
                else:
                    await show(u, "✅ کلید ذخیره شد.", [back("x:ks")])
        elif kind == "snname":
            c.user_data["await"] = {"kind": "sncmd", "name": t}
            await show(u, "حالا دستور را بفرست:")
        elif kind == "sncmd":
            DB["snippets"].append({"name": st["name"], "cmd": raw.strip()})
            db_save()
            await show(u, "✅ Snippet ذخیره شد.", [back("x:sn")])
        elif kind == "mxcmd":
            await run_multi(u, c, raw.strip())
    except asyncssh.KeyImportError:
        await show(u, "❌ کلید نامعتبر است (کلید با passphrase پشتیبانی نمی‌شود).", [back("x:menu")])
    except ValueError:
        await show(u, "❌ ورودی نامعتبر.", [back("x:menu")])


async def run_multi(u, c, cmd):
    sel = list(c.user_data.pop("mx", set()))
    await show(u, f"⏳ اجرا روی {len(sel)} سرور ...")
    sem = asyncio.Semaphore(5)
    per = max(200, 3000 // max(1, len(sel)))

    async def one(hid):
        h = DB["hosts"][hid]
        async with sem:
            try:
                conn = await ssh_connect(hid)
                try:
                    r = await conn.run(cmd, check=False, timeout=60)
                    out = ((r.stdout or "") + (r.stderr or "")).strip()
                    return f"<b>{e(h['name'])}</b> (exit {r.exit_status})\n<pre>{e(out[-per:]) or ' '}</pre>"
                finally:
                    conn.close()
            except HostKeyChanged:
                return f"<b>{e(h['name'])}</b> 🚨 fingerprint عوض شده"
            except Exception as ex:
                return f"<b>{e(h['name'])}</b> ❌ {e(str(ex)[:150])}"

    res = await asyncio.gather(*(one(x) for x in sel))
    await show(u, "\n\n".join(res), [back("x:menu")])


async def ssh_route(u, c, a):
    uid = u.effective_user.id
    t, r = a[0], a[1:]
    s = SESS.get(uid)
    if t == "menu":
        await ssh_menu(u, c)
    elif t == "h":
        await host_view(u, c, r[0])
    elif t == "c":
        await term_start(u, c, r[0])
    elif t == "srv":
        sv = (await api(c).req("GET", f"/servers/{r[0]}"))["server"]
        ip = ip4(sv)
        hid = next((i for i, h in DB["hosts"].items() if h["host"] == ip), None)
        if hid:
            await term_start(u, c, hid)
        else:
            c.user_data["nh"] = {"host": ip, "port": 22, "name": sv["name"]}
            await ask_user(u, c)
    elif t == "ha":
        c.user_data["await"] = {"kind": "hhost"}
        await show(u, "➕ آدرس سرور را بفرست (IP یا دامنه، اختیاری <code>:پورت</code>):", [back("x:menu")])
    elif t == "hu":
        c.user_data["nh"]["user"] = r[0]
        await ask_auth(u, c)
    elif t == "au":
        n = c.user_data["nh"]
        if r[0] == "k":
            n.update(auth="key", key=r[1])
            await save_host(u, c)
        elif r[0] == "gen":
            n.update(auth="key", key=gen_key(f"bot-{n['name']}"), new_key=True)
            await save_host(u, c)
        elif r[0] == "paste":
            c.user_data["await"] = {"kind": "hpaste"}
            await show(u, "📝 Private Key (بدون passphrase) را کامل بفرست.\n🔒 پیامت بعد از خواندن پاک می‌شود.",
                       [HOME])
        elif r[0] == "pw":
            c.user_data["await"] = {"kind": "hpw"}
            await show(u, "🔐 پسورد SSH را بفرست.\n🔒 پیامت بعد از خواندن پاک می‌شود و رمزنگاری‌شده ذخیره می‌شود.",
                       [HOME])
    elif t == "hd":
        await confirm(u, f"حذف سرور «{DB['hosts'][r[0]]['name']}» از لیست SSH", f"x:hdy:{r[0]}", f"x:h:{r[0]}")
    elif t == "hdy":
        DB["hosts"].pop(r[0], None)
        db_save()
        await ssh_menu(u, c)
    elif t == "rf":
        DB["hosts"][r[0]]["hostkey"] = DB["hosts"][r[0]]["fp"] = None
        db_save()
        await host_view(u, c, r[0])
    # --- keys
    elif t == "ks":
        rows = [[B(f"🔑 {k['name']}", callback_data=f"x:k:{kid}")] for kid, k in DB["keys"].items()]
        rows += [[B("✨ ساخت کلید", callback_data="x:kg"), B("📝 چسباندن کلید", callback_data="x:kp")],
                 back("x:menu")]
        await show(u, "🔑 کلیدهای SSH (رمزنگاری‌شده ذخیره می‌شوند)", rows)
    elif t == "kg":
        kid = gen_key(f"key-{len(DB['keys']) + 1}")
        await ssh_route(u, c, ["k", kid])
    elif t == "kp":
        c.user_data["await"] = {"kind": "kpaste"}
        await show(u, "📝 Private Key را بفرست (پیامت پاک می‌شود):", [back("x:ks")])
    elif t == "k":
        k = DB["keys"][r[0]]
        rows = ([[B("➕ افزودن به Hetzner", callback_data=f"x:kh:{r[0]}")]] if APIS else [])
        rows += [[B("🗑 حذف", callback_data=f"x:kd:{r[0]}")], back("x:ks")]
        await show(u, f"🔑 <b>{e(k['name'])}</b>\n<code>{e(k['pub'])}</code>", rows)
    elif t == "kh":
        k = DB["keys"][r[0]]
        await api(c).req("POST", "/ssh_keys", json={"name": f"bot-{k['name']}", "public_key": k["pub"]})
        await show(u, "✅ به SSH Keys هتزنر اضافه شد.", [back(f"x:k:{r[0]}")])
    elif t == "kd":
        await confirm(u, "حذف این کلید (سرورهایی که با آن تنظیم شده‌اند دیگر وصل نمی‌شوند)",
                      f"x:kdy:{r[0]}", f"x:k:{r[0]}")
    elif t == "kdy":
        DB["keys"].pop(r[0], None)
        db_save()
        await ssh_route(u, c, ["ks"])
    # --- snippets
    elif t == "sn":
        rows = [[B(f"📜 {x['name']}", callback_data=f"x:sd:{i}")] for i, x in enumerate(DB["snippets"])]
        rows += [[B("➕ Snippet جدید", callback_data="x:sa")], back("x:menu")]
        await show(u, "📜 Snippets (دستورهای ذخیره‌شده؛ داخل ترمینال با یک تپ اجرا می‌شوند)", rows)
    elif t == "sa":
        c.user_data["await"] = {"kind": "snname"}
        await show(u, "نام Snippet را بفرست:", [back("x:sn")])
    elif t == "sd":
        x = DB["snippets"][int(r[0])]
        await show(u, f"📜 <b>{e(x['name'])}</b>\n<code>{e(x['cmd'])}</code>",
                   [[B("🗑 حذف", callback_data=f"x:sdy:{r[0]}")], back("x:sn")])
    elif t == "sdy":
        DB["snippets"].pop(int(r[0]))
        db_save()
        await ssh_route(u, c, ["sn"])
    # --- multi run
    elif t in ("mx", "mt"):
        sel = c.user_data.setdefault("mx", set())
        if t == "mt":
            sel.symmetric_difference_update({r[0]})
        rows = [[B(("✅ " if hid in sel else "⬜ ") + h["name"], callback_data=f"x:mt:{hid}")]
                for hid, h in DB["hosts"].items()]
        rows += [[B("▶️ ادامه", callback_data="x:mg")], back("x:menu")]
        await show(u, "⚡ سرورهایی که می‌خواهی دستور رویشان اجرا شود:", rows)
    elif t == "mg":
        if not c.user_data.get("mx"):
            return await ssh_route(u, c, ["mx"])
        c.user_data["await"] = {"kind": "mxcmd"}
        await show(u, "دستور را بفرست (حداکثر ۶۰ ثانیه، بدون حالت تعاملی):", [back("x:menu")])
    # --- live terminal
    elif t == "t":
        if not s:
            return await show(u, "⛔ نشستی باز نیست.", [[B("💻 SSH", callback_data="x:menu")]])
        if r[0] == "refresh":
            await push(s, force=True)
        else:
            await term_send(s, KEYMAP[r[0]])
    elif t == "close":
        await term_close(uid, "🔌 بسته شد")
    elif t == "term":
        if not s:
            return await ssh_menu(u, c)
        old = s.mid
        m = await u.effective_message.reply_text("⏳")
        s.mid, s.last = m.message_id, ""
        try:
            await c.bot.delete_message(s.chat, old)
        except Exception:
            pass
        await push(s, force=True)
    elif t == "snip":
        if not s:
            return
        rows = [[B(x["name"], callback_data=f"x:sr:{i}")] for i, x in enumerate(DB["snippets"])]
        await u.effective_message.reply_text("📜 یک Snippet انتخاب کن:", reply_markup=KM(rows or None))
    elif t == "sr":
        if s:
            await term_send(s, DB["snippets"][int(r[0])]["cmd"] + "\r")


async def on_get(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not ok(u):
        return
    s = SESS.get(u.effective_user.id)
    path = " ".join(c.args or [])
    if not s or not path:
        return await u.message.reply_text("ابتدا به یک سرور وصل شو. استفاده: /get مسیر_فایل")
    try:
        async with s.conn.start_sftp_client() as sftp:
            size = (await sftp.stat(path)).size or 0
            if size > 50 * 1024 * 1024:
                return await u.message.reply_text("❌ حداکثر حجم 50MB")
            async with sftp.open(path, "rb") as fh:
                data = await fh.read()
        await u.message.reply_document(io.BytesIO(data), filename=os.path.basename(path) or "file")
    except Exception as ex:
        await u.message.reply_text(f"❌ {ex}")


async def on_ls(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not ok(u):
        return
    s = SESS.get(u.effective_user.id)
    if not s:
        return await u.message.reply_text("ابتدا به یک سرور وصل شو.")
    path = " ".join(c.args or []) or "."
    try:
        async with s.conn.start_sftp_client() as sftp:
            full = str(await sftp.realpath(path))
            items = [x for x in await sftp.readdir(path) if x.filename not in (".", "..")]
        items.sort(key=lambda x: (not pystat.S_ISDIR(x.attrs.permissions or 0), str(x.filename).lower()))
        lines = [f"{'📁' if pystat.S_ISDIR(x.attrs.permissions or 0) else '📄'} {x.filename}"
                 + ("" if pystat.S_ISDIR(x.attrs.permissions or 0) else f"  ({hb(x.attrs.size or 0)})") for x in items]
        txt = f"<b>{e(full)}</b>\n<pre>{e(chr(10).join(lines)) or '(خالی)'}</pre>"
        await u.message.reply_text(txt[:4000], parse_mode="HTML")
    except Exception as ex:
        await u.message.reply_text(f"❌ {ex}")


async def on_doc(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not ok(u):
        return
    s = SESS.get(u.effective_user.id)
    if not s:
        return
    d = u.message.document
    if d.file_size and d.file_size > 20 * 1024 * 1024:
        return await u.message.reply_text("❌ حداکثر حجم آپلود 20MB")
    dest = (u.message.caption or ".").strip()
    try:
        f = await d.get_file()
        data = bytes(await f.download_as_bytearray())
        async with s.conn.start_sftp_client() as sftp:
            if await sftp.isdir(dest):
                dest = dest.rstrip("/") + "/" + (d.file_name or "upload.bin")
            async with sftp.open(dest, "wb") as fh:
                await fh.write(data)
            full = str(await sftp.realpath(dest))
        await u.message.reply_text(f"✅ آپلود شد: {full}")
    except Exception as ex:
        await u.message.reply_text(f"❌ {ex}")


SSH_KINDS = {"hhost", "huser", "hpw", "hpaste", "kpaste", "snname", "sncmd", "mxcmd"}


async def on_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not ok(u):
        return await u.message.reply_text(f"⛔ دسترسی نداری. آیدی تو: {u.effective_user.id}")
    await menu(u, c)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "menu"], on_start))
    app.add_handler(CommandHandler("get", on_get))
    app.add_handler(CommandHandler("ls", on_ls))
    app.add_handler(MessageHandler(filters.Document.ALL, on_doc))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot started. Projects: %s", ", ".join(APIS))
    app.run_polling()


if __name__ == "__main__":
    main()
