# ruff: noqa
import re
import shutil
import tempfile
from pathlib import Path
from typing import Final

from aiogram import Router, Bot
from aiogram.types import Message

from src.app.handlers.scripts.import_trades import import_trades_from_csv
from src.app.handlers.utils.states import Imp

imp_rt = Router()

ALLOWED_MIME: Final[set[str]] = {
    "text/csv",
    "application/vnd.ms-excel",
    "application/octet-stream",
}

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    stem = Path(name).name
    return _SAFE_RE.sub("_", stem) or "file.csv"


def _looks_like_csv(file_name: str | None, mime: str | None) -> bool:
    fn_ok = (file_name or "").lower().endswith(".csv")
    mime_ok = (mime or "") in ALLOWED_MIME
    return fn_ok or mime_ok


@imp_rt.message(Imp.file)
async def cmd_file(msg: Message, bot: Bot):
    if not msg.document:
        await msg.answer("⚠️ Пришлите файл со сделками!")
        return

    doc = msg.document
    if not _looks_like_csv(doc.file_name, doc.mime_type):
        await msg.reply("⚠️ Пришлите файл с расширением .csv!")
        return

    tmpdir = Path(tempfile.mkdtemp(prefix="csv_import_"))
    dst_path = tmpdir / _safe_filename(doc.file_name or "trades.csv")

    try:
        await bot.download(doc, destination=dst_path)
        notify = await msg.reply("📥 Файл получен! Начинаю импорт в Notion...")

        result = await import_trades_from_csv(
            user_id=msg.from_user.id,
            file_path=str(dst_path),
        )

        if isinstance(result, dict) and result.get("ok"):
            imported = result.get("imported", 0)
            skipped = result.get("skipped", 0)
            # никаких длинных логов в Telegram — всё в консоль
            await notify.edit_text(f"📦 Импорт завершён!\n✅ Добавлено: {imported}\n🚫 Пропущено: {skipped}")
        else:
            err = (result or {}).get("error", "Неизвестная ошибка")
            await notify.edit_text(f"❌ Ошибка: {err}")

    except Exception as e:
        await msg.answer(f"💥 Не удалось обработать файл: `{e}`")
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
