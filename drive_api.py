# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from typing import Optional, Dict, Any

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

from utils import sanitize_name


def drive_service(creds: Credentials):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def ensure_folder(creds: Credentials, parent_id: Optional[str], folder_name: str) -> str:
    drive = drive_service(creds)

    folder_name = sanitize_name(folder_name, "Folder")
    parent = parent_id or "root"

    safe_name = folder_name.replace("'", "")

    query = (
        "mimeType='application/vnd.google-apps.folder' and "
        f"name='{safe_name}' and "
        f"'{parent}' in parents and "
        "trashed=false"
    )

    res = drive.files().list(
        q=query,
        spaces="drive",
        fields="files(id,name)"
    ).execute()

    files = res.get("files", [])
    if files:
        return files[0]["id"]

    body = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent]
    }

    created = drive.files().create(
        body=body,
        fields="id"
    ).execute()

    return created["id"]

def upload_file(
    creds: Credentials,
    parent_id: str,
    local_path: str,
    filename: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> str:
    """
    Upload a file to Drive under parent_id. Returns file_id.
    """
    if not os.path.exists(local_path):
        raise FileNotFoundError(local_path)

    drive = drive_service(creds)
    name = filename or os.path.basename(local_path)
    name = sanitize_name(name, "file")

    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
    body: Dict[str, Any] = {"name": name, "parents": [parent_id]}

    created = drive.files().create(body=body, media_body=media, fields="id").execute()
    return created["id"]
