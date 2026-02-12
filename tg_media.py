# -*- coding: utf-8 -*-
import os
from typing import Optional, Tuple

import aiohttp
from aiogram import Bot
from utils import sanitize_name


async def download_telegram_file(bot: Bot, file_id: str, tmp_dir: str, suggested_name: str) -> Tuple[str, Optional[str]]:
    """
    returns (local_path, mime_type_guess)
    """
    f = await bot.get_file(file_id)
    file_path = f.file_path
    url = f"https://api.telegram.org/file/bot{bot.token}/{file_path}"

    os.makedirs(tmp_dir, exist_ok=True)
    name = sanitize_name(suggested_name, "file")
    local = os.path.join(tmp_dir, name)

    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=120) as r:
            r.raise_for_status()
            with open(local, "wb") as out:
                out.write(await r.read())

            ct = r.headers.get("Content-Type")
            return local, ct
