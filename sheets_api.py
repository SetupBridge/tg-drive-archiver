# -*- coding: utf-8 -*-
from typing import List, Any, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from utils import sanitize_name


def sheets_service(creds: Credentials):
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def create_sheet_in_drive_folder(
    creds: Credentials,
    drive,
    folder_id: str,
    title: str,
) -> str:
    title = sanitize_name(title, "Text Archive")

    body = {
        "name": title,
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [folder_id],
    }

    created = drive.files().create(
        body=body,
        fields="id"
    ).execute()

    sheet_id = created["id"]

    sheets = sheets_service(creds)

    headers = [[
        "Timestamp",
        "Group",
        "Sender",
        "Sender ID",
        "Message ID",
        "Text"
    ]]

    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="A1:F1",
        valueInputOption="RAW",
        body={"values": headers},
    ).execute()

    return sheet_id


def append_row(
    creds: Credentials,
    spreadsheet_id: str,
    row: List[Any],
) -> None:
    sheets = sheets_service(creds)

    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range="A:F",
        valueInputOption="RAW",
        body={"values": [row]},
    ).execute()
