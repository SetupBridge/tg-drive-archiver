# -*- coding: utf-8 -*-
import os
import uuid
import logging
from typing import Any, Dict, Optional, List

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

from google.oauth2.credentials import Credentials

from config import (
    BOT_TOKEN, APP_NAME, GOOGLE_CLIENT_SECRET_FILE, GOOGLE_SCOPES,
    STATE_PATH, TMP_DIR, DEFAULT_KEYWORDS,
    FOLDER_PHOTOS, FOLDER_VIDEOS, FOLDER_DOCS, FOLDER_AUDIO, FOLDER_VOICE, FOLDER_STICKERS, FOLDER_OTHER, FOLDER_TEXTS
)
from storage import Storage
from i18n import t
from utils import sanitize_name, now_iso
from google_device_flow import start_device_flow, poll_device_flow_token
from drive_api import drive_service, ensure_folder, upload_file
from sheets_api import create_sheet_in_drive_folder, append_row
from tg_media import download_telegram_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("setupbridge")

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)
store = Storage(STATE_PATH)

# in-memory device flows: user_id -> flow dict
DEVICE_FLOWS: Dict[int, Dict[str, Any]] = {}


def _lang_for(user_id: int) -> str:
    return store.get_user_lang(user_id)


def _is_private(msg: types.Message) -> bool:
    return msg.chat.type == types.ChatType.PRIVATE


def _is_group(msg: types.Message) -> bool:
    return msg.chat.type in (types.ChatType.GROUP, types.ChatType.SUPERGROUP)


def kb_home(lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(t(lang, "btn_group_settings"), callback_data="menu:group_settings"),
        InlineKeyboardButton(t(lang, "btn_info"), callback_data="menu:info"),
        InlineKeyboardButton(t(lang, "btn_lang"), callback_data="menu:lang"),
    )
    return kb


def kb_back(lang: str, to: str = "menu:home") -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(t(lang, "btn_back"), callback_data=to))
    return kb


def kb_lang(lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(t(lang, "lang_ar"), callback_data="lang:ar"),
        InlineKeyboardButton(t(lang, "lang_en"), callback_data="lang:en"),
    )
    kb.add(InlineKeyboardButton(t(lang, "btn_back"), callback_data="menu:home"))
    return kb


def _get_user_creds(user_id: int) -> Optional[Credentials]:
    creds_file = store.get_user_creds_file(user_id)
    if not creds_file or not os.path.exists(creds_file):
        return None
    try:
        return Credentials.from_authorized_user_file(creds_file, scopes=GOOGLE_SCOPES)
    except Exception:
        return None


def _save_user_creds(user_id: int, token_json: Dict[str, Any]) -> str:
    os.makedirs("data", exist_ok=True)
    path = f"data/creds_{user_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        import json
        json.dump(token_json, f, ensure_ascii=False, indent=2)
    store.set_user_creds(user_id, path, email=None)
    return path


def _pending_group_for(user_id: int) -> Optional[int]:
    pend = store.data.get("pending", {}).get(str(user_id))
    if isinstance(pend, dict) and pend.get("chat_id"):
        try:
            return int(pend["chat_id"])
        except Exception:
            return None
    return None


def _ensure_group_structure(creds: Credentials, chat_id: int, chat_title: str) -> Dict[str, Any]:
    g = store.get_group(chat_id)

    # must have drive user set
    drive_user_id = g.get("drive_user_id")
    if not drive_user_id:
        g["drive_user_id"] = None
        store.set_group(chat_id, g)
        return g

    # build drive
    drive = drive_service(creds)

    # root folder
    if not g.get("root_folder_id"):
        root_name = f"{APP_NAME} - {sanitize_name(chat_title, str(chat_id))}"
        g["root_folder_id"] = ensure_folder(drive, root_name, None)

    # type folders
    folders = g.get("folders", {})
    if not isinstance(folders, dict):
        folders = {}
    mapping = {
        "photos": FOLDER_PHOTOS,
        "videos": FOLDER_VIDEOS,
        "docs": FOLDER_DOCS,
        "audio": FOLDER_AUDIO,
        "voice": FOLDER_VOICE,
        "stickers": FOLDER_STICKERS,
        "other": FOLDER_OTHER,
    }
    for k, nm in mapping.items():
        if not folders.get(k):
            folders[k] = ensure_folder(drive, nm, g["root_folder_id"])
    g["folders"] = folders

    # sheet for texts
    if not g.get("sheet_id"):
        sheet_title = f"{FOLDER_TEXTS} - {sanitize_name(chat_title, str(chat_id))}"
        g["sheet_id"] = create_sheet_in_drive_folder(creds, drive, g["root_folder_id"], sheet_title)

    store.set_group(chat_id, g)
    return g


@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    lang = _lang_for(msg.from_user.id)
    if not _is_private(msg):
        # ignore in groups to avoid noise
        return

    text = "\n".join([
        f"<b>{t(lang, 'welcome_title')}</b>",
        t(lang, "welcome_brief", app=APP_NAME),
        "",
        f"<b>{t(lang, 'how_to')}</b>",
        t(lang, "step1"),
        t(lang, "step2"),
        t(lang, "step3"),
        t(lang, "step4"),
    ])
    await msg.answer(text, reply_markup=kb_home(lang))


@dp.message_handler(commands=["setup"])
async def cmd_setup(msg: types.Message):
    lang = _lang_for(msg.from_user.id)

    if not _is_group(msg):
        await msg.reply(t(lang, "only_groups"))
        return

    member = await bot.get_chat_member(msg.chat.id, msg.from_user.id)
    if member.status not in ("administrator", "creator"):
        await msg.reply(t(lang, "need_admin"))
        return

    # Save pending mapping: user -> chat
    store.set_pending(msg.from_user.id, msg.chat.id)

    # Send as BUTTON (not plain link)
    deep = f"https://t.me/{(await bot.me).username}?start=setup_{msg.chat.id}"
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(t(lang, "open_private_btn"), url=deep))
    await msg.reply(t(lang, "setup_ready"), reply_markup=kb)


@dp.message_handler(lambda m: _is_private(m) and m.text and m.text.startswith("/start setup_"))
async def cmd_start_setup_private(msg: types.Message):
    lang = _lang_for(msg.from_user.id)
    # parse chat_id from /start setup_<id>
    try:
        payload = msg.text.split("setup_", 1)[1].strip()
        chat_id = int(payload)
    except Exception:
        await msg.answer(t(lang, "pending_not_found"))
        return

    store.set_pending(msg.from_user.id, chat_id)
    await show_group_settings(msg.from_user.id, msg.chat.id, edit_message_id=None)


async def show_group_settings(user_id: int, private_chat_id: int, edit_message_id: Optional[int]):
    lang = _lang_for(user_id)
    chat_id = _pending_group_for(user_id)
    if not chat_id:
        await bot.send_message(private_chat_id, t(lang, "pending_not_found"))
        return

    chat = await bot.get_chat(chat_id)
    g = store.get_group(chat_id)

    linked = bool(g.get("drive_user_id"))
    enabled = bool(g.get("enabled", True))

    text = "\n".join([
        f"<b>{t(lang,'group_settings_title')}</b>",
        "",
        t(lang, "status_group",
          title=sanitize_name(chat.title or str(chat_id), str(chat_id)),
          drive=t(lang, "drive_linked") if linked else t(lang, "drive_not_linked"),
          arch=t(lang, "arch_on") if enabled else t(lang, "arch_off")),
    ])

    kb = InlineKeyboardMarkup(row_width=1)
    if linked:
        kb.add(InlineKeyboardButton(t(lang, "btn_drive_unlink"), callback_data="drive:unlink"))
    else:
        kb.add(InlineKeyboardButton(t(lang, "btn_drive_link"), callback_data="drive:link"))

    kb.add(
        InlineKeyboardButton(t(lang, "btn_toggle_arch"), callback_data="group:toggle_arch"),
        InlineKeyboardButton(t(lang, "btn_choose_folder"), callback_data="group:ensure_folders"),
        InlineKeyboardButton(t(lang, "btn_advanced"), callback_data="menu:advanced"),
        InlineKeyboardButton(t(lang, "btn_back"), callback_data="menu:home"),
    )

    if edit_message_id:
        await bot.edit_message_text(text, private_chat_id, edit_message_id, reply_markup=kb)
    else:
        await bot.send_message(private_chat_id, text, reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("menu:"))
async def menu_handler(call: types.CallbackQuery):
    lang = _lang_for(call.from_user.id)
    if call.message.chat.type != types.ChatType.PRIVATE:
        await call.answer(t(lang, "only_private"), show_alert=True)
        return

    action = call.data.split(":", 1)[1]

    if action == "home":
        text = "\n".join([
            f"<b>{t(lang, 'welcome_title')}</b>",
            t(lang, "welcome_brief", app=APP_NAME),
            "",
            f"<b>{t(lang, 'how_to')}</b>",
            t(lang, "step1"),
            t(lang, "step2"),
            t(lang, "step3"),
            t(lang, "step4"),
        ])
        await call.message.edit_text(text, reply_markup=kb_home(lang))
        await call.answer()
        return

    if action == "info":
        text = "\n".join([
            f"<b>{APP_NAME}</b>",
            t(lang, "welcome_brief", app=APP_NAME),
            "",
            t(lang, "step2"),
            t(lang, "step3"),
            t(lang, "step4"),
        ])
        await call.message.edit_text(text, reply_markup=kb_back(lang))
        await call.answer()
        return

    if action == "lang":
        await call.message.edit_text(t(lang, "choose_lang"), reply_markup=kb_lang(lang))
        await call.answer()
        return

    if action == "group_settings":
        await show_group_settings(call.from_user.id, call.message.chat.id, call.message.message_id)
        await call.answer()
        return

    if action == "advanced":
        await show_advanced(call)
        return

    await call.answer()


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("lang:"))
async def lang_handler(call: types.CallbackQuery):
    lang_new = call.data.split(":", 1)[1]
    store.set_user_lang(call.from_user.id, lang_new)
    lang = _lang_for(call.from_user.id)
    await call.message.edit_text(t(lang, "saved"), reply_markup=kb_home(lang))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data in ("group:toggle_arch", "group:ensure_folders"))
async def group_actions(call: types.CallbackQuery):
    lang = _lang_for(call.from_user.id)
    chat_id = _pending_group_for(call.from_user.id)
    if not chat_id:
        await call.answer(t(lang, "pending_not_found"), show_alert=True)
        return

    g = store.get_group(chat_id)

    if call.data == "group:toggle_arch":
        g["enabled"] = not bool(g.get("enabled", True))
        store.set_group(chat_id, g)
        await show_group_settings(call.from_user.id, call.message.chat.id, call.message.message_id)
        await call.answer()
        return

    if call.data == "group:ensure_folders":
        if not g.get("drive_user_id"):
            await call.answer(t(lang, "drive_not_linked"), show_alert=True)
            return
        creds = _get_user_creds(int(g["drive_user_id"]))
        if not creds:
            await call.answer(t(lang, "drive_not_linked"), show_alert=True)
            return
        chat = await bot.get_chat(chat_id)
        _ensure_group_structure(creds, chat_id, chat.title or str(chat_id))
        await show_group_settings(call.from_user.id, call.message.chat.id, call.message.message_id)
        await call.answer(t(lang, "saved"), show_alert=False)
        return


async def show_advanced(call: types.CallbackQuery):
    lang = _lang_for(call.from_user.id)
    chat_id = _pending_group_for(call.from_user.id)
    if not chat_id:
        await call.answer(t(lang, "pending_not_found"), show_alert=True)
        return

    g = store.get_group(chat_id)
    mode = g.get("mode", "reply")
    auto = g.get("auto", {}) if isinstance(g.get("auto"), dict) else {"enabled": False, "notify_interval_h": 6}
    kw = g.get("keywords") or DEFAULT_KEYWORDS
    kw_s = ", ".join(kw)

    text = "\n".join([
        f"<b>{t(lang,'advanced_title')}</b>",
        "",
        t(lang, "adv_status",
          mode=t(lang, "mode_auto") if mode == "auto" else t(lang, "mode_reply"),
          auto=t(lang, "auto_on") if auto.get("enabled") else t(lang, "auto_off"),
          n=int(auto.get("notify_interval_h") or 6),
          kw=kw_s),
    ])

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(t(lang, "btn_mode_toggle"), callback_data="adv:mode"),
        InlineKeyboardButton(t(lang, "btn_auto_toggle"), callback_data="adv:auto"),
        InlineKeyboardButton(t(lang, "btn_notify_interval"), callback_data="adv:interval"),
        InlineKeyboardButton(t(lang, "btn_keywords"), callback_data="adv:keywords"),
        InlineKeyboardButton(t(lang, "btn_back"), callback_data="menu:group_settings"),
    )
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("adv:"))
async def adv_handler(call: types.CallbackQuery):
    lang = _lang_for(call.from_user.id)
    chat_id = _pending_group_for(call.from_user.id)
    if not chat_id:
        await call.answer(t(lang, "pending_not_found"), show_alert=True)
        return

    g = store.get_group(chat_id)
    auto = g.get("auto", {})
    if not isinstance(auto, dict):
        auto = {"enabled": False, "notify_interval_h": 6}

    action = call.data.split(":", 1)[1]
    if action == "mode":
        g["mode"] = "auto" if g.get("mode") != "auto" else "reply"
        store.set_group(chat_id, g)
        await show_advanced(call)
        return

    if action == "auto":
        auto["enabled"] = not bool(auto.get("enabled"))
        g["auto"] = auto
        store.set_group(chat_id, g)
        await show_advanced(call)
        return

    if action == "interval":
        # ask user to send number
        await call.message.edit_text("🕒 " + t(lang, "btn_notify_interval") + "\n\n" + "1 - 24",
                                    reply_markup=kb_back(lang, "menu:advanced"))
        store.data.setdefault("pending", {})
        store.data["pending"][str(call.from_user.id)] = {"chat_id": str(chat_id), "await": "interval"}
        store.save()
        await call.answer()
        return

    if action == "keywords":
        await call.message.edit_text(t(lang, "keywords_help"), reply_markup=kb_back(lang, "menu:advanced"))
        store.data.setdefault("pending", {})
        store.data["pending"][str(call.from_user.id)] = {"chat_id": str(chat_id), "await": "keywords"}
        store.save()
        await call.answer()
        return


@dp.message_handler(lambda m: _is_private(m) and m.text)
async def private_text_router(msg: types.Message):
    lang = _lang_for(msg.from_user.id)
    pend = store.data.get("pending", {}).get(str(msg.from_user.id))
    if not isinstance(pend, dict) or "await" not in pend:
        return

    chat_id = _pending_group_for(msg.from_user.id)
    if not chat_id:
        await msg.reply(t(lang, "pending_not_found"))
        return

    g = store.get_group(chat_id)
    await_key = pend.get("await")

    if await_key == "interval":
        try:
            n = int(msg.text.strip())
            if not (1 <= n <= 24):
                raise ValueError()
        except Exception:
            await msg.reply(t(lang, "bad_interval"))
            return
        auto = g.get("auto", {})
        if not isinstance(auto, dict):
            auto = {"enabled": False, "notify_interval_h": 6}
        auto["notify_interval_h"] = n
        g["auto"] = auto
        store.set_group(chat_id, g)
        pend.pop("await", None)
        store.data["pending"][str(msg.from_user.id)] = {"chat_id": str(chat_id)}
        store.save()
        await msg.reply(t(lang, "saved"), reply_markup=kb_home(lang))
        return

    if await_key == "keywords":
        parts = [p.strip() for p in msg.text.split(",")]
        parts = [p for p in parts if p]
        g["keywords"] = parts[:20] if parts else None
        store.set_group(chat_id, g)
        pend.pop("await", None)
        store.data["pending"][str(msg.from_user.id)] = {"chat_id": str(chat_id)}
        store.save()
        await msg.reply(t(lang, "saved"), reply_markup=kb_home(lang))
        return


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("drive:"))
async def drive_menu(call: types.CallbackQuery):
    lang = _lang_for(call.from_user.id)
    chat_id = _pending_group_for(call.from_user.id)
    if not chat_id:
        await call.answer(t(lang, "pending_not_found"), show_alert=True)
        return
    g = store.get_group(chat_id)

    action = call.data.split(":", 1)[1]
    if action == "unlink":
        g["drive_user_id"] = None
        g["root_folder_id"] = None
        g["folders"] = {}
        g["sheet_id"] = None
        store.set_group(chat_id, g)
        await show_group_settings(call.from_user.id, call.message.chat.id, call.message.message_id)
        await call.answer(t(lang, "unlink_ok"), show_alert=False)
        return

    if action == "link":
        # Start device flow for this user
        try:
            flow = await start_device_flow(GOOGLE_CLIENT_SECRET_FILE, GOOGLE_SCOPES)
            DEVICE_FLOWS[call.from_user.id] = flow
        except Exception as e:
            await call.answer(str(e), show_alert=True)
            return

        code = flow.get("user_code")
        verification_url = flow.get("verification_url") or flow.get("verification_uri")
        device_code = flow.get("device_code")
        interval = int(flow.get("interval") or 5)

        if not (code and verification_url and device_code):
            await call.answer("device flow response invalid", show_alert=True)
            return

        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton(t(lang, "btn_open_google"), url=verification_url),
            InlineKeyboardButton(t(lang, "btn_verify"), callback_data="drive:verify"),
            InlineKeyboardButton(t(lang, "btn_back"), callback_data="menu:group_settings"),
        )
        await call.message.edit_text(
            f"<b>{t(lang,'device_code_title')}</b>\n\n" + t(lang, "device_code_body", code=code),
            reply_markup=kb
        )
        await call.answer()
        return

    if action == "verify":
        flow = DEVICE_FLOWS.get(call.from_user.id)
        if not flow:
            await call.answer(t(lang, "linked_fail"), show_alert=True)
            return
        await call.answer(t(lang, "verifying"), show_alert=True)

        try:
            token = await poll_device_flow_token(
                GOOGLE_CLIENT_SECRET_FILE,
                device_code=flow["device_code"],
                interval=int(flow.get("interval") or 5),
                timeout_sec=180,
            )
        except Exception:
            await call.answer(t(lang, "linked_fail"), show_alert=True)
            return

        # save creds for user
        _save_user_creds(call.from_user.id, token)

        # bind group to this user creds
        g["drive_user_id"] = call.from_user.id
        store.set_group(chat_id, g)

        # ensure folders + sheet now
        creds = _get_user_creds(call.from_user.id)
        if creds:
            chat = await bot.get_chat(chat_id)
            _ensure_group_structure(creds, chat_id, chat.title or str(chat_id))

        await show_group_settings(call.from_user.id, call.message.chat.id, call.message.message_id)
        await call.answer(t(lang, "linked_ok"), show_alert=True)
        return


def _match_archive_keyword(g: Dict[str, Any], text: str) -> bool:
    kw = g.get("keywords") or DEFAULT_KEYWORDS
    text = (text or "").strip().lower()
    return any(k.lower() == text for k in kw)


@dp.message_handler(commands=["archive"])
async def archive_cmd(msg: types.Message):
    if not _is_group(msg):
        return
    g = store.get_group(msg.chat.id)
    lang = store.get_user_lang(msg.from_user.id)

    if not g.get("enabled", True):
        await msg.reply(t(lang, "not_enabled"))
        return

    if not msg.reply_to_message:
        await msg.reply(t(lang, "reply_required"))
        return

    # Need linked drive
    drive_user_id = g.get("drive_user_id")
    if not drive_user_id:
        await msg.reply(t(lang, "drive_not_linked"))
        return
    creds = _get_user_creds(int(drive_user_id))
    if not creds:
        await msg.reply(t(lang, "drive_not_linked"))
        return

    try:
        # ensure structure
        g = _ensure_group_structure(creds, msg.chat.id, msg.chat.title or str(msg.chat.id))
        drive = drive_service(creds)

        r = msg.reply_to_message
        sender = r.from_user.full_name if r.from_user else "Unknown"
        sender_id = r.from_user.id if r.from_user else ""
        group_title = msg.chat.title or str(msg.chat.id)

        # TEXT -> append to sheet (NOT folder spam)
        if r.text or r.caption:
            text = r.text or r.caption or ""
            row = [now_iso(), group_title, sender, str(sender_id), str(r.message_id), text]
            if not g.get("sheet_id"):
                # create sheet if missing
                g = _ensure_group_structure(creds, msg.chat.id, group_title)
            append_row(creds, g["sheet_id"], row)

        # MEDIA -> upload into its folder
        uploaded_any = False

        async def _upload(file_id: str, folder_key: str, name: str):
            nonlocal uploaded_any
            local, mime = await download_telegram_file(bot, file_id, TMP_DIR, name)
            try:
                upload_file(drive, local, name, g["folders"][folder_key], mime_type=mime)
                uploaded_any = True
            finally:
                try:
                    os.remove(local)
                except Exception:
                    pass

        if r.photo:
            p = r.photo[-1]
            await _upload(p.file_id, "photos", f"photo_{p.file_unique_id}.jpg")

        if r.video:
            v = r.video
            await _upload(v.file_id, "videos", f"video_{v.file_unique_id}.mp4")

        if r.document:
            d = r.document
            await _upload(d.file_id, "docs", sanitize_name(d.file_name or f"doc_{d.file_unique_id}", "document"))

        if r.audio:
            a = r.audio
            await _upload(a.file_id, "audio", sanitize_name(a.file_name or f"audio_{a.file_unique_id}.mp3", "audio"))

        if r.voice:
            v = r.voice
            await _upload(v.file_id, "voice", f"voice_{v.file_unique_id}.ogg")

        if r.sticker:
            s = r.sticker
            ext = "webp" if s.is_animated is False else "tgs"
            await _upload(s.file_id, "stickers", f"sticker_{s.file_unique_id}.{ext}")

        # fallback (nothing)
        await msg.reply(t(lang, "archive_done"))

    except Exception as e:
        log.exception("archive failed")
        await msg.reply(t(lang, "archive_failed", err=str(e)))


@dp.message_handler(content_types=types.ContentType.ANY)
async def auto_archiver(msg: types.Message):
    # auto mode: archive every incoming message (no reply needed)
    if not _is_group(msg):
        return
    g = store.get_group(msg.chat.id)
    if not g.get("enabled", True):
        return

    if g.get("mode") != "auto":
        return
    auto = g.get("auto", {})
    if not isinstance(auto, dict) or not auto.get("enabled"):
        return

    # do not auto-archive bot commands
    if msg.text and msg.text.startswith("/"):
        return

    # Need linked drive
    drive_user_id = g.get("drive_user_id")
    if not drive_user_id:
        return
    creds = _get_user_creds(int(drive_user_id))
    if not creds:
        return

    # archive this message by calling archive logic on itself as if reply
    try:
        g = _ensure_group_structure(creds, msg.chat.id, msg.chat.title or str(msg.chat.id))
        drive = drive_service(creds)

        sender = msg.from_user.full_name if msg.from_user else "Unknown"
        sender_id = msg.from_user.id if msg.from_user else ""
        group_title = msg.chat.title or str(msg.chat.id)

        # text row
        if msg.text or msg.caption:
            text = msg.text or msg.caption or ""
            row = [now_iso(), group_title, sender, str(sender_id), str(msg.message_id), text]
            append_row(creds, g["sheet_id"], row)

        async def _upload(file_id: str, folder_key: str, name: str):
            local, mime = await download_telegram_file(bot, file_id, TMP_DIR, name)
            try:
                upload_file(drive, local, name, g["folders"][folder_key], mime_type=mime)
            finally:
                try:
                    os.remove(local)
                except Exception:
                    pass

        if msg.photo:
            p = msg.photo[-1]
            await _upload(p.file_id, "photos", f"photo_{p.file_unique_id}.jpg")
        if msg.video:
            v = msg.video
            await _upload(v.file_id, "videos", f"video_{v.file_unique_id}.mp4")
        if msg.document:
            d = msg.document
            await _upload(d.file_id, "docs", sanitize_name(d.file_name or f"doc_{d.file_unique_id}", "document"))
        if msg.audio:
            a = msg.audio
            await _upload(a.file_id, "audio", sanitize_name(a.file_name or f"audio_{a.file_unique_id}.mp3", "audio"))
        if msg.voice:
            v = msg.voice
            await _upload(v.file_id, "voice", f"voice_{v.file_unique_id}.ogg")
        if msg.sticker:
            s = msg.sticker
            ext = "webp" if s.is_animated is False else "tgs"
            await _upload(s.file_id, "stickers", f"sticker_{s.file_unique_id}.{ext}")

    except Exception:
        log.exception("auto archive failed")


if __name__ == "__main__":
    log.info("Bot started")
    executor.start_polling(dp, skip_updates=True)
