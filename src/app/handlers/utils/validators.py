import re
from urllib.parse import urlparse


def validate_notion_token(token: str | None) -> tuple[bool, str]:
    """
    Проверка API-ключа Notion.
    Возвращает (ok, msg)
    """
    if not token or not token.strip():
        return False, "⚠️ API-ключ Notion пустой. Укажите его в меню!"
    if not (token.startswith("secret_") or token.startswith("ntn_")):
        return False, "⚠️ API-ключ Notion некорректный (ожидается формат secret_xxx или ntn_xxx)!"
    if len(token) < 40:  # у новых ключей обычно >40 символов
        return False, "⚠️ API-ключ Notion слишком короткий!"
    return True, ""


def validate_notion_url(url: str | None) -> tuple[bool, str]:
    if not url or not url.strip():
        return False, "⚠️ Ссылка на страницу Notion пустая. Укажите её в настройках!"
    if not url.startswith("http"):
        return False, "⚠️ Ссылка на страницу Notion должна начинаться с http/https."
    if "notion.so" not in url:
        return False, "⚠️ Ссылка не похожа на Notion-страницу."

    # убираем query (?source=copy_link и т.п.)
    clean_url = url.split("?", 1)[0]

    # ищем 32 hex-символа подряд (с дефисами или без)
    page_id_match = re.search(r"([0-9a-f]{32})", clean_url.replace("-", ""), re.IGNORECASE)
    if not page_id_match:
        return False, "⚠️ Ссылка не содержит корректного идентификатора страницы Notion."
    return True, ""
