# -*- coding: utf-8 -*-
import re
from datetime import datetime


def sanitize_name(name: str, default: str = "file") -> str:
    """
    Clean file/folder names for Drive
    """
    if not name:
        return default

    # Remove invalid characters
    name = re.sub(r'[\\/*?:"<>|]', "", name)

    # Trim spaces
    name = name.strip()

    return name or default


def now_iso() -> str:
    """
    Return current UTC time in ISO format
    """
    return datetime.utcnow().isoformat()
