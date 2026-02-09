# bot.py
# Telegram -> Google Drive Archiver (Termux friendly)
# aiogram v2.x

import os
import json
import re
import asyncio
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, executor, types

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google_auth_oauthlib.flow import InstalledAppFlow

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


# =======================
# CONFIG
# =======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Export it first: export BOT_TOKEN=xxxxx")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TOKENS_DIR = os.path.join(DATA_DIR, "tokens")
TMP_DIR = os.path.join(DATA_DIR, "tmp")
STORAGE_PATH = os.path.join(DATA_DIR, "storage.json")
CLIENT_SECRET_PATH = os.path.join(BASE_DIR, "client_secret.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TOKENS_DIR, exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

# IMPORTANT: union of scopes we will ever need
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]

DRIVE_ROOT_FOLDER_NAME = "TG Archive"   # top folder in Drive


# =======================
# HELPERS: storage
# =======================
def load_storage() -> dict:
    if not os.path.exists(STORAGE_PATH):
        return {}
    try:
        with open(STORAGE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_storage(data: dict) -> None:
    with open(STORAGE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def chat_key(chat_id: int) -> str:
    return str(chat_id)


def token_path_for_chat(chat_id: int) -> str:
    return os.path.join(TOKENS_DIR, f"token_{chat_id}.json")


# =======================
# HELPERS: google auth
# =======================
_pending_flows = {}  # chat_id -> flow


def build_flow() -> InstalledAppFlow:
    if not os.path.exists(CLIENT_SECRET_PATH):
        raise FileNotFoundError("client_secret.json is missing in project folder.")
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
    # Termux friendly (manual code). This is classic OOB.
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    return flow


def creds_from_file(path: str) -> Credentials | None:
    if not os.path.exists(path):
        return None
    try:
        return Credentials.from_authorized_user_file(path, SCOPES)
    except Exception:
        return None


def save_creds(creds: Credentials, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())


def get_creds(chat_id: int) -> Credentials | None:
    path = token_path_for_chat(chat_id)
    creds = creds_from_file(path)
    if not creds:
        return None

    # refresh if needed
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_creds(creds, path)
        except RefreshError:
            # likely invalid_scope / revoked / etc.
            return None
    return creds


def google_services(creds: Credentials):
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return drive, sheets


# =======================
# HELPERS: Drive
# =======================
def escape_drive_q_value(value: str) -> str:
    # escape backslash + single quotes for Drive query string
    v = value.replace("\\", "\\\\")
    v = v.replace("'", "\\'")
    return v


def find_folder(drive, name: str, parent_id: str | None = None) -> str | None:
    esc = escape_drive_q_value(name)
    q = "mimeType='application/vnd.google-apps.folder' and trashed=false"
    q += f" and name='{esc}'"
    if parent_id:
        q += f" and '{parent_id}' in parents"

    res = drive.files().list(
        q=q,
        spaces="drive",
        fields="files(id,name)",
        pageSize=10
    ).execute()

    files = res.get("files", [])
    return files[0]["id"] if files else None


def create_folder(drive, name: str, parent_id: str | None = None) -> str:
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]

    f = drive.files().create(body=metadata, fields="id").execute()
    return f["id"]


def ensure_folder(drive, name: str, parent_id: str | None = None) -> str:
    fid = find_folder(drive, name, parent_id)
    if fid:
        return fid
    return create_folder(drive, name, parent_id)


def upload_file(drive, folder_id: str, local_path: str, drive_name: str, mime_type: str | None = None) -> tuple[str, str]:
    body = {"name": drive_name, "parents": [folder_id]}
    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
    f = drive.files().create(body=body, media_body=media, fields="id,webViewLink").execute()
    return f["id"], f.get("webViewLink", "")


def ensure_spreadsheet(drive, folder_id: str, title: str = "Archive") -> tuple[str, str]:
    # Search for existing spreadsheet in folder
    esc = escape_drive_q_value(title)
    q = "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    q += f" and name='{esc}' and '{folder_id}' in parents"

    res = drive.files().list(q=q, spaces="drive", fields="files(id,name)").execute()
    files = res.get("files", [])
    if files:
        sid = files[0]["id"]
        return sid, f"https://docs.google.com/spreadsheets/d/{sid}/edit"

    meta = {
        "name": title,
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [folder_id],
    }
    f = drive.files().create(body=meta, fields="id").execute()
    sid = f["id"]
    return sid, f"https://docs.google.com/spreadsheets/d/{sid}/edit"


# =======================
# HELPERS: Sheets
# =======================
def ensure_header_row(sheets, spreadsheet_id: str):
    # Create headers if sheet is empty
    # We'll just append header if A1 is empty.
    try:
        r = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="A1:A1"
        ).execute()
        values = r.get("values", [])
        if values and values[0] and values[0][0].strip():
            return
    except Exception:
        pass

    header = [[
        "timestamp",
        "chat_id",
        "chat_title",
        "from_id",
        "from_name",
        "message_id",
        "text"
    ]]
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range="A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": header}
    ).execute()


def append_text_row(sheets, spreadsheet_id: str, row: list[str]):
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range="A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]}
    ).execute()


# =======================
# BOT
# =======================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)


def is_archive_command(text: str) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    # allow /archive, archive, أرشف, ارشف, أرشفة, ارشفة
    return t in {"/archive", "archive", "أرشف", "ارشف", "أرشفة", "ارشفة", "أرشيف", "ارشيف"}


@dp.message_handler(commands=["start", "help"])
async def start_help(message: types.Message):
    msg = (
        "👋 هلا! أنا بوت الأرشفة.\n\n"
        "✅ الأوامر:\n"
        "• /setup  → ربط Google Drive (أول مرة)\n"
        "• /code CODE → لصق كود الربط بعد ما تفتح الرابط\n"
        "• /archive → (ردّ على رسالة) لحفظها\n\n"
        "ملاحظة: النصوص تنحفظ في Google Sheet واحد، والوسائط تنرفع لملفات photos/videos/documents."
    )
    await message.reply(msg)


@dp.message_handler(commands=["setup"])
async def setup_cmd(message: types.Message):
    chat_id = message.chat.id

    # If already has valid creds, just ensure folders/sheet
    creds = get_creds(chat_id)
    if creds:
        drive, sheets = google_services(creds)
        storage = load_storage()
        ck = chat_key(chat_id)
        storage.setdefault(ck, {})

        root_id = ensure_folder(drive, DRIVE_ROOT_FOLDER_NAME)
        chat_folder_id = ensure_folder(drive, f"Chat_{chat_id}", root_id)

        # subfolders for media
        photos_id = ensure_folder(drive, "photos", chat_folder_id)
        videos_id = ensure_folder(drive, "videos", chat_folder_id)
        docs_id = ensure_folder(drive, "documents", chat_folder_id)

        sheet_id, sheet_link = ensure_spreadsheet(drive, chat_folder_id, "Archive")
        ensure_header_row(sheets, sheet_id)

        storage[ck].update({
            "root_id": root_id,
            "chat_folder_id": chat_folder_id,
            "photos_id": photos_id,
            "videos_id": videos_id,
            "docs_id": docs_id,
            "sheet_id": sheet_id,
        })
        save_storage(storage)

        await message.reply(
            "✅ Google Drive مرتبط بالفعل.\n"
            f"📄 جدول النصوص: {sheet_link}"
        )
        return

    # Start auth flow (Termux friendly)
    try:
        flow = build_flow()
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )
        _pending_flows[chat_id] = flow
        await message.reply(
            "🔗 افتح الرابط هذا وسجّل دخولك ثم انسخ CODE:\n"
            f"{auth_url}\n\n"
            "بعدها أرسل:\n"
            "/code CODE"
        )
    except Exception as e:
        await message.reply(f"❌ فشل /setup: {e}")


@dp.message_handler(commands=["code"])
async def code_cmd(message: types.Message):
    chat_id = message.chat.id
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("اكتبها كذا: /code CODE")
        return

    code = parts[1].strip()
    flow = _pending_flows.get(chat_id)
    if not flow:
        # allow creating new flow if bot restarted
        flow = build_flow()

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        save_creds(creds, token_path_for_chat(chat_id))
        _pending_flows.pop(chat_id, None)

        # Ensure folders and sheet now
        drive, sheets = google_services(creds)
        storage = load_storage()
        ck = chat_key(chat_id)
        storage.setdefault(ck, {})

        root_id = ensure_folder(drive, DRIVE_ROOT_FOLDER_NAME)
        chat_folder_id = ensure_folder(drive, f"Chat_{chat_id}", root_id)

        photos_id = ensure_folder(drive, "photos", chat_folder_id)
        videos_id = ensure_folder(drive, "videos", chat_folder_id)
        docs_id = ensure_folder(drive, "documents", chat_folder_id)

        sheet_id, sheet_link = ensure_spreadsheet(drive, chat_folder_id, "Archive")
        ensure_header_row(sheets, sheet_id)

        storage[ck].update({
            "root_id": root_id,
            "chat_folder_id": chat_folder_id,
            "photos_id": photos_id,
            "videos_id": videos_id,
            "docs_id": docs_id,
            "sheet_id": sheet_id,
        })
        save_storage(storage)

        await message.reply(
            "✅ تم ربط Google Drive بنجاح!\n"
            f"📄 جدول النصوص: {sheet_link}"
        )

    except Exception as e:
        await message.reply(f"❌ فشل الربط: {e}\nجرّب /setup من جديد.")


async def ensure_ready(message: types.Message) -> tuple[Credentials | None, dict | None, str | None]:
    """Return creds, storage_for_chat, error_message"""
    chat_id = message.chat.id
    creds = get_creds(chat_id)
    if not creds:
        return None, None, "لازم تسوي /setup أول (أو التوكن انتهى)."

    storage = load_storage()
    ck = chat_key(chat_id)
    if ck not in storage or "chat_folder_id" not in storage[ck] or "sheet_id" not in storage[ck]:
        # rebuild folders/sheet
        drive, sheets = google_services(creds)
        root_id = ensure_folder(drive, DRIVE_ROOT_FOLDER_NAME)
        chat_folder_id = ensure_folder(drive, f"Chat_{chat_id}", root_id)
        photos_id = ensure_folder(drive, "photos", chat_folder_id)
        videos_id = ensure_folder(drive, "videos", chat_folder_id)
        docs_id = ensure_folder(drive, "documents", chat_folder_id)
        sheet_id, _ = ensure_spreadsheet(drive, chat_folder_id, "Archive")
        ensure_header_row(sheets, sheet_id)

        storage.setdefault(ck, {})
        storage[ck].update({
            "root_id": root_id,
            "chat_folder_id": chat_folder_id,
            "photos_id": photos_id,
            "videos_id": videos_id,
            "docs_id": docs_id,
            "sheet_id": sheet_id,
        })
        save_storage(storage)

    return creds, storage[ck], None


@dp.message_handler(lambda m: m.text and is_archive_command(m.text))
async def archive_cmd(message: types.Message):
    # must be reply
    if not message.reply_to_message:
        await message.reply("لازم ترد (Reply) على الرسالة وبعدين اكتب /archive")
        return

    status_msg = await message.reply("⏳ جاري الأرشفة...")

    creds, st, err = await ensure_ready(message)
    if err:
        await status_msg.edit_text(f"❌ {err}")
        return

    chat_id = message.chat.id
    try:
        drive, sheets = google_services(creds)

        src = message.reply_to_message
        chat_title = message.chat.title or ""
        from_user = src.from_user
        from_id = str(from_user.id) if from_user else ""
        from_name = ""
        if from_user:
            from_name = (from_user.full_name or "").strip()

        # timestamp ISO
        ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

        # 1) TEXT
        if src.text or src.caption:
            text_value = src.text if src.text else src.caption
            text_value = text_value.strip()

            ensure_header_row(sheets, st["sheet_id"])
            row = [
                ts,
                str(chat_id),
                chat_title,
                from_id,
                from_name,
                str(src.message_id),
                text_value
            ]
            append_text_row(sheets, st["sheet_id"], row)
            sheet_link = f"https://docs.google.com/spreadsheets/d/{st['sheet_id']}/edit"
            await status_msg.edit_text(f"✅ تم حفظ النص في الجدول.\n📄 {sheet_link}")
            return

        # 2) PHOTO
        if src.photo:
            photo = src.photo[-1]  # best quality
            file_info = await bot.get_file(photo.file_id)
            local_path = os.path.join(TMP_DIR, f"photo_{src.message_id}.jpg")
            await bot.download_file(file_info.file_path, local_path)

            drive_name = f"photos_file_{src.message_id}.jpg"
            _, link = upload_file(drive, st["photos_id"], local_path, drive_name, "image/jpeg")
            try:
                os.remove(local_path)
            except Exception:
                pass

            await status_msg.edit_text(f"✅ تم رفع الصورة.\n🔗 {link}")
            return

        # 3) VIDEO
        if src.video:
            v = src.video
            file_info = await bot.get_file(v.file_id)
            ext = ".mp4"
            local_path = os.path.join(TMP_DIR, f"video_{src.message_id}{ext}")
            await bot.download_file(file_info.file_path, local_path)

            drive_name = f"videos_file_{src.message_id}{ext}"
            _, link = upload_file(drive, st["videos_id"], local_path, drive_name, "video/mp4")
            try:
                os.remove(local_path)
            except Exception:
                pass

            await status_msg.edit_text(f"✅ تم رفع الفيديو.\n🔗 {link}")
            return

        # 4) DOCUMENT (pdf, zip, etc)
        if src.document:
            d = src.document
            file_info = await bot.get_file(d.file_id)
            # keep original filename when possible
            orig = d.file_name or f"document_{src.message_id}"
            safe_orig = re.sub(r"[^\w\-. ()\[\]]+", "_", orig)
            local_path = os.path.join(TMP_DIR, f"{src.message_id}_{safe_orig}")
            await bot.download_file(file_info.file_path, local_path)

            mime = d.mime_type or "application/octet-stream"
            drive_name = f"documents_file_{src.message_id}_{safe_orig}"
            _, link = upload_file(drive, st["docs_id"], local_path, drive_name, mime)
            try:
                os.remove(local_path)
            except Exception:
                pass

            await status_msg.edit_text(f"✅ تم رفع المستند.\n🔗 {link}")
            return

        # fallback
        await status_msg.edit_text("❌ نوع الرسالة غير مدعوم حالياً (نص/صورة/فيديو/مستند).")

    except RefreshError as e:
        # very common with invalid_scope / revoked
        # delete token and force setup
        try:
            os.remove(token_path_for_chat(chat_id))
        except Exception:
            pass
        await status_msg.edit_text(
            "❌ صلاحيات Google انتهت/تغيّرت (invalid_scope غالباً).\n"
            "حلّها: سوِّ /setup من جديد."
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ خطأ أثناء الأرشفة: {e}")


if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
