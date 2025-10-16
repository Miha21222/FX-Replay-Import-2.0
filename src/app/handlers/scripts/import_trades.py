# ruff: noqa
import csv
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from notion_client import Client

from src.app.database.requests import get_data  # твоя async-функция

# ----------------------------- logging -----------------------------
logger = logging.getLogger("fxreplay.import")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setLevel(logging.DEBUG)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
    logger.addHandler(_h)

# ----------------------------- globals -----------------------------

_UUID_RE = re.compile(r"([0-9a-fA-F]{32})")
_DASHED_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

RU_WEEKDAY = {
    0: "Понедельник",
    1: "Вторник",
    2: "Среда",
    3: "Четверг",
    4: "Пятница",
    5: "Суббота",
    6: "Воскресенье",
}

SESSION_SET = {"ASIA", "FRANKFURT", "LONDON", "NEW YORK", "OVERLAP"}

# tags like:  30F_E_TF_30m, 30F_SL_No_OTT, etc.
_TAG_RE = re.compile(r"^\s*([A-Za-z0-9]+)_(E|SL)_(.+?)\s*$")

# important for your base: "30m OF" -> "30F"
_SETUP_CODE_OVERRIDES = {
    "30m OF": "30F",
}

DEFAULT_ICON_URL = "https://www.notion.so/icons/numero_gray.svg"


# ----------------------------- helpers -----------------------------

def _page_id_from_url(url: str) -> str:
    m = _DASHED_UUID_RE.search(url)
    if m:
        return m.group(1)
    m = _UUID_RE.search(url)
    if m:
        u = m.group(1).lower()
        return f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}"
    if len(url) in (32, 36) and all(c in "0123456789abcdef-" for c in url.lower()):
        return url
    raise RuntimeError("Не удалось извлечь ID страницы/БД из ссылки")


def collect_child_databases(notion: Client, root_block_id: str, max_depth: int) -> List[str]:
    db_ids: List[str] = []
    stack: List[Tuple[str, int]] = [(root_block_id, 0)]
    while stack:
        block_id, depth = stack.pop()
        if depth > max_depth:
            continue
        cursor = None
        while True:
            resp = notion.blocks.children.list(block_id=block_id, start_cursor=cursor)
            for it in resp.get("results", []):
                if it.get("type") == "child_database":
                    db_ids.append(it["id"])
                if it.get("has_children"):
                    stack.append((it["id"], depth + 1))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
    logger.debug(f"collect_child_databases: found={len(db_ids)} ids (depth≤{max_depth})")
    return db_ids


def _score_journal_candidate(notion: Client, db_id: str) -> int:
    try:
        props = notion.databases.retrieve(db_id)["properties"]
    except Exception:
        return -1
    names = {k.lower(): v for k, v in props.items()}
    expect = ["id", "pair", "date", "type", "bias", "rr", "result", "risk", "day", "session", "entry", "sl"]
    score = 0
    for e in expect:
        if e in names:
            score += 2
    if any(v.get("type") == "title" for v in props.values()):
        score += 3
    logger.debug(f"score_db {db_id[:8]}… = {score} (props={list(props.keys())[:10]} …)")
    return score


def resolve_journal_db_id(notion: Client, root_url: str) -> str:
    page_or_db_id = _page_id_from_url(root_url)
    logger.info(f"resolve: root={page_or_db_id}")

    try:
        notion.databases.retrieve(page_or_db_id)
        logger.info(f"resolve: root is DB -> {page_or_db_id}")
        return page_or_db_id
    except Exception:
        logger.debug("resolve: root is not DB, try as page…")

    candidates: List[str] = []
    try:
        candidates = collect_child_databases(notion, page_or_db_id, max_depth=4)
    except Exception:
        candidates = []

    if not candidates:
        try:
            cursor = None
            while True:
                resp = notion.search(
                    filter={"property": "object", "value": "database"},
                    start_cursor=cursor,
                    page_size=100,
                    sort={"direction": "ascending", "timestamp": "last_edited_time"},
                )
                for it in resp.get("results", []):
                    parent = it.get("parent", {})
                    if parent.get("type") == "page_id" and parent.get("page_id") == page_or_db_id:
                        candidates.append(it["id"])
                if not resp.get("has_more"):
                    break
                cursor = resp.get("next_cursor")
        except Exception:
            pass

    if not candidates:
        raise RuntimeError(
            "resolve_db_failed: не удалось определить ID базы журнала. "
            "Проверь, что страница содержит child-базы/или ссылка на саму БД и что интеграции выдан доступ (Share)."
        )

    best_id, best_score = None, -1
    for did in candidates:
        s = _score_journal_candidate(notion, did)
        if s > best_score:
            best_id, best_score = did, s

    if not best_id:
        raise RuntimeError("на странице найдены БД, но ни одна не похожа на журнал")
    logger.info(f"resolve: best_db={best_id} score={best_score}")
    return best_id


def _detect_session(hour: int) -> str:
    if 15 <= hour < 18:
        return "OVERLAP"
    if 0 <= hour < 8:
        return "ASIA"
    if 8 <= hour < 11:
        return "FRANKFURT"
    if 11 <= hour < 15:
        return "LONDON"
    if 15 <= hour < 22:
        return "NEW YORK"
    return "ASIA"


def _title_prop_name(db_props: Dict[str, Any]) -> str:
    for k, v in db_props.items():
        if v.get("type") == "title":
            return k
    raise RuntimeError("в базе отсутствует title-свойство")


def _parse_title_number(title_val: Dict[str, Any]) -> Optional[int]:
    if not title_val or not title_val.get("title"):
        return None
    text = "".join([t.get("plain_text", "") for t in title_val["title"]]).strip()
    m = re.search(r"(\d+)$", text)
    return int(m.group(1)) if m else None


def _fetch_max_title_number(notion: Client, db_id: str, title_prop: str, limit: int = 1000) -> int:
    max_num = 0
    cursor = None
    scanned = 0
    while True:
        resp = notion.databases.query(database_id=db_id, start_cursor=cursor, page_size=100)
        for page in resp.get("results", []):
            props = page.get("properties", {})
            num = _parse_title_number(props.get(title_prop, {}))
            if isinstance(num, int):
                max_num = max(max_num, num)
            scanned += 1
            if scanned >= limit:
                logger.debug(f"max_title: scanned={scanned}, max={max_num} (early stop)")
                return max_num
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    logger.debug(f"max_title: scanned_all={scanned}, max={max_num}")
    return max_num


def _build_id_filter(db_props: Dict[str, Any], trade_id: Any) -> Dict[str, Any]:
    if "ID" not in db_props:
        return {}
    t = db_props["ID"]["type"]
    if t == "number":
        try:
            val = float(trade_id)
        except Exception:
            return {}
        return {"property": "ID", "number": {"equals": val}}
    if t == "rich_text":
        return {"property": "ID", "rich_text": {"equals": str(trade_id)}}
    if t == "title":
        return {"property": "ID", "title": {"equals": str(trade_id)}}
    return {}


def _to_float(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except Exception:
        return None


def _strip_pair_name(raw: str) -> str:
    s = (raw or "").strip()
    if ":" in s:
        s = s.split(":", 1)[1]
    return s


def _load_relation_map(notion: Client, related_db_id: str, label: str) -> Dict[str, str]:
    name_to_id: Dict[str, str] = {}
    cursor = None
    cnt = 0
    while True:
        resp = notion.databases.query(database_id=related_db_id, start_cursor=cursor)
        for page in resp.get("results", []):
            props = page.get("properties", {})
            for key, val in props.items():
                if val.get("type") == "title" and val["title"]:
                    name = val["title"][0]["plain_text"].strip()
                    if name:
                        name_to_id[name] = page["id"]
                        cnt += 1
                    break
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    logger.info(f"{label}: loaded {cnt} items from related_db={related_db_id}")
    return name_to_id


def _auto_detect_day_session_props(
        notion: Client, db_props: Dict[str, Any]
) -> Tuple[Optional[str], Optional[str], Dict[str, Dict[str, str]]]:
    rel_maps: Dict[str, Dict[str, str]] = {}
    day_key: Optional[str] = None
    session_key: Optional[str] = None

    for prop_name, info in db_props.items():
        if info.get("type") == "relation":
            related_db = info["relation"]["database_id"]
            m = _load_relation_map(notion, related_db, label=f"relmap:{prop_name}")
            rel_maps[prop_name] = m
            names = set(m.keys())
            if not day_key and len(names & set(RU_WEEKDAY.values())) >= 4:
                day_key = prop_name
            if not session_key and len(names & SESSION_SET) >= 3:
                session_key = prop_name

    for prop_name, info in db_props.items():
        if info.get("type") == "select":
            options = {o.get("name") for o in info.get("select", {}).get("options", []) if o.get("name")}
            if not day_key and len(options & set(RU_WEEKDAY.values())) >= 4:
                day_key = prop_name
            if not session_key and len(options & SESSION_SET) >= 3:
                session_key = prop_name

    logger.info(f"day/session detected: day={day_key} session={session_key}")
    return day_key, session_key, rel_maps


# ----------------------------- expectation / direction anchor helpers -----------------------------

def _find_expectation_anchor_prop(db_props: Dict[str, Any]) -> Optional[str]:
    """Ищем колонку-анкёр для матожидания (relation)."""
    info = db_props.get("🧮 Матожидание")
    if info and info.get("type") == "relation":
        return "🧮 Матожидание"
    for name, meta in db_props.items():
        if meta.get("type") != "relation":
            continue
        n = name.casefold()
        if "матож" in n or "ожидан" in n or "expect" in n:
            return name
    return None


def _find_direction_anchor_prop(db_props: Dict[str, Any]) -> Optional[str]:
    """Ищем колонку-анкёр для Direction (relation)."""
    for probe in ("Direction", "Directions", "Направление"):
        info = db_props.get(probe)
        if info and info.get("type") == "relation":
            return probe
    for name, meta in db_props.items():
        if meta.get("type") != "relation":
            continue
        n = name.casefold()
        if "direction" in n or "направл" in n:
            return name
    return None


def _pick_single_page_id(notion: Client, db_id: Optional[str]) -> Optional[str]:
    if not db_id:
        return None
    try:
        resp = notion.databases.query(database_id=db_id, page_size=1)
        items = resp.get("results") or []
        return items[0]["id"] if items else None
    except Exception:
        return None


# ----------------------------- risk -----------------------------

def _estimate_risk_pct(trade: Dict[str, Any]) -> Optional[float]:
    for key in ("riskPercent", "risk_pct", "risk", "risk%"):
        if key in trade and trade[key] not in (None, ""):
            try:
                v = abs(float(trade[key]))
                return None if v <= 0 else min(10.0, v)
            except Exception:
                pass

    R = _to_float(trade.get("avgRiskReward"))
    init_bal = _to_float(trade.get("initialBalance"))
    r_pnl = _to_float(trade.get("rPnL"))
    if init_bal is None or r_pnl is None:
        return None

    profit_pct = (r_pnl / init_bal) * 100.0
    if profit_pct >= 0 and R and R > 0:
        risk_pct = profit_pct / R
    else:
        risk_pct = abs(profit_pct)

    if risk_pct <= 0:
        return None
    return min(10.0, risk_pct)


def _risk_bucket(risk_pct: float) -> str:
    if risk_pct <= 0.5:
        return "0.5%"
    if risk_pct <= 1.0:
        return "1.0%"
    if risk_pct <= 1.5:
        return "1.5%"
    return "2.0%"


# ----------------------------- Entry / SL index -----------------------------

def _cyr2lat_lookalikes(s: str) -> str:
    table = str.maketrans({
        "О": "O", "о": "o",
        "Т": "T", "т": "t",
        "С": "C", "с": "c",
        "Р": "P", "р": "p",
        "А": "A", "а": "a",
        "В": "B", "в": "b",
        "Е": "E", "е": "e",
        "К": "K", "к": "k",
        "М": "M", "м": "m",
        "Н": "H", "н": "h",
        "Х": "X", "х": "x",
    })
    return s.translate(table)


def _normalize_detail(s: str) -> str:
    x = (s or "").strip()
    x = _cyr2lat_lookalikes(x)

    if re.fullmatch(r"(?i)15m", x):
        x = "TF 15m"
    elif re.fullmatch(r"(?i)30m", x):
        x = "TF 30m"

    x = x.replace("_", " ")
    x = re.sub(r"([A-Za-z])(\d)", r"\1 \2", x)
    x = re.sub(r"(\d)([A-Za-z])", r"\1 \2", x)
    x = re.sub(r"([a-z])([A-Z])", r"\1 \2", x)
    x = re.sub(r"(?<=\d),(?=\d)", ".", x)
    x = re.sub(r"\bFul\s*Fill\b", "Fulfill", x, flags=re.I)
    x = re.sub(r"\bTP\s*Fix\s*2\s*RR\b", "TP Fix2RR", x, flags=re.I)

    repl = {
        "FVG 0 5": "FVG 0.5",
        "FVGFulfill": "FVG Fulfill",
        "FVG Ful Fill": "FVG Fulfill",
        "FVGOpen": "FVG Open",
        "TPFix2 RR": "TP Fix2RR",
        "TPFix2RR": "TP Fix2RR",
        "TP Fractal": "TP Fractal",
        "TPFVG": "TP FVG",
        "SLFractal": "SL Fractal",
        "SL Ful Fill": "SL Fulfill",
        "SLFulfill": "SL Fulfill",
        "TF30 m": "TF 30m",
        "TF15 m": "TF 15m",
        "TF 15 m": "TF 15m",
        "TF 30 m": "TF 30m",
        "RulesRR": "Rules RR",
        "No OTT": "No OTT",
        "No_OTT": "No OTT",
        "PDH/PDL (TP/SL)": "PDH/PDL (TP/SL)",
        "3Liquidity": "3 Liquidity",
        "FVG 0,5": "FVG 0.5",
    }
    for a, b in repl.items():
        x = x.replace(a, b)

    x = re.sub(r"\s{2,}", " ", x).strip()
    return x


def _derive_setup_code(setup_name: str) -> str:
    name = setup_name.strip()
    if name in _SETUP_CODE_OVERRIDES:
        return _SETUP_CODE_OVERRIDES[name]
    m = re.search(r"^\s*(\d+)\s*m\s+OF\s*$", name, re.I)
    if m:
        return f"{m.group(1)}F"
    digits = "".join(re.findall(r"\d+", name))
    words = [w for w in re.split(r"\s+", re.sub(r"[^0-9A-Za-z\s]", " ", name)) if w]
    if digits and words:
        last = words[-1]
        return f"{digits}{last[0].upper()}"
    code = re.sub(r"[^0-9A-Za-z]", "", name).upper()
    return code or "GLOBAL"


def _detect_setup_prop_name(notion: Client, detail_db_id: str, label: str) -> Optional[str]:
    props = notion.databases.retrieve(detail_db_id)["properties"]
    for k in props.keys():
        if k.lower() == "setup" and props[k].get("type") == "relation":
            logger.info(f"{label}: found Setup column='{k}'")
            return k
    for k, v in props.items():
        if v.get("type") == "relation":
            logger.info(f"{label}: fallback Setup column='{k}' (first relation)")
            return k
    logger.warning(f"{label}: Setup column NOT found")
    return None


def _index_detail_db_by_setup(
        notion: Client, detail_db_id: str, setup_rel_prop: str, label: str
) -> Dict[Tuple[str, str], str]:
    detail_props = notion.databases.retrieve(detail_db_id)["properties"]
    setup_prop_info = detail_props.get(setup_rel_prop)
    if not setup_prop_info or setup_prop_info.get("type") != "relation":
        logger.warning(f"{label}: property '{setup_rel_prop}' is not relation — indexing aborted")
        return {}

    setup_db_id = setup_prop_info["relation"]["database_id"]

    setup_id_to_code: Dict[str, str] = {}
    cursor = None
    while True:
        resp = notion.databases.query(database_id=setup_db_id, start_cursor=cursor)
        for p in resp.get("results", []):
            stitle = ""
            for k, v in p.get("properties", {}).items():
                if v.get("type") == "title" and v["title"]:
                    stitle = v["title"][0]["plain_text"].strip()
                    break
            if stitle:
                scode = _derive_setup_code(stitle)
                setup_id_to_code[p["id"]] = scode
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")

    logger.info(f"{label}: setup map size={len(setup_id_to_code)} (sample up to 10)")
    for i, (sid, scode) in enumerate(list(setup_id_to_code.items())[:10], 1):
        logger.debug(f"{label}: setup #{i} id={sid[:6]}… code={scode}")

    index: Dict[Tuple[str, str], str] = {}
    cursor = None
    c = 0
    while True:
        resp = notion.databases.query(database_id=detail_db_id, start_cursor=cursor)
        for page in resp.get("results", []):
            props = page.get("properties", {})
            detail_text = ""
            for k, v in props.items():
                if v.get("type") == "title" and v["title"]:
                    detail_text = v["title"][0]["plain_text"].strip()
                    break
            if not detail_text:
                continue
            detail_norm = _normalize_detail(detail_text)

            rel = props.get(setup_rel_prop, {})
            rel_items = rel.get("relation") or []

            if not rel_items:
                index[("GLOBAL", detail_norm)] = page["id"]
                c += 1
                continue

            for it in rel_items:
                scode = setup_id_to_code.get(it["id"]) or "GLOBAL"
                index[(scode, detail_norm)] = page["id"]
                c += 1

        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")

    logger.info(f"{label}: indexed keys={c}, unique={len(index)} (sample up to 12)")
    for i, (k, v) in enumerate(list(index.items())[:12], 1):
        logger.debug(f"{label}: key#{i}={k} -> {v[:6]}…")
    return index


def _make_entry_sl_index(
        notion: Client, db_props: Dict[str, Any]
):
    lower_to_real = {k.lower(): k for k in db_props.keys()}

    preferred_entry_names = ["entry details", "entry", "entry detail"]
    preferred_sl_names = ["mistakes", "sl", "stop loss"]

    entry_prop = next((db_props[n] and n for n in (lower_to_real.get(x) for x in preferred_entry_names) if
                       n and db_props[n].get("type") == "relation"), None)
    sl_prop = next((db_props[n] and n for n in (lower_to_real.get(x) for x in preferred_sl_names) if
                    n and db_props[n].get("type") == "relation"), None)

    def _guess(prop_name: str) -> str:
        info = db_props[prop_name]
        rid = info["relation"]["database_id"]
        meta = notion.databases.retrieve(rid)
        title = meta.get("title", [])
        t = "".join(x.get("plain_text", "") for x in title).lower()
        return t

    if not entry_prop or not sl_prop:
        for name, info in db_props.items():
            if info.get("type") != "relation":
                continue
            t = _guess(name)
            if not entry_prop and any(x in t for x in ["entry", "entry details"]):
                entry_prop = name
            if not sl_prop and any(x in t for x in ["stop loss", "(sl)", "sl "]):
                sl_prop = name

    entry_index, sl_index = {}, {}

    def _build_index(detail_prop_name: str, label: str):
        setup_rel = _detect_setup_prop_name(notion, db_props[detail_prop_name]["relation"]["database_id"], label=label)
        if not setup_rel:
            return {}
        return _index_detail_db_by_setup(notion, db_props[detail_prop_name]["relation"]["database_id"], setup_rel,
                                         label=label)

    if entry_prop:
        entry_index = _build_index(entry_prop, "ENTRY")
    else:
        logger.warning("Entry property not usable: name=None type=None")

    if sl_prop:
        sl_index = _build_index(sl_prop, "SL")
    else:
        logger.warning("SL property not usable: name=None type=None")

    from collections import defaultdict
    entry_detail_setups = defaultdict(set)
    for (scode, detail) in entry_index.keys():
        entry_detail_setups[detail].add(scode)

    sl_detail_setups = defaultdict(set)
    for (scode, detail) in sl_index.keys():
        sl_detail_setups[detail].add(scode)

    return entry_prop, sl_prop, entry_index, sl_index, entry_detail_setups, sl_detail_setups


def _parse_tags(raw: str) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    if not raw:
        return out

    safe = re.sub(r"(?<=_[0-9]),(?=[0-9]\b)", ".", raw)

    parts = [t for t in safe.split(",") if t.strip()]
    for t in parts:
        m = _TAG_RE.match(t)
        if not m:
            logger.debug(f"tag skip (no match): {t!r}")
            continue
        setup_code = m.group(1).upper()
        kind = m.group(2).upper()  # E or SL
        detail_norm = _normalize_detail(m.group(3))
        out.append((setup_code, kind, detail_norm))
    logger.debug(f"parse_tags: {raw!r} -> {out}")
    return out


def _detect_tags_field(header: List[str]) -> Optional[str]:
    cand = [h for h in header if "tag" in h.lower()]
    return cand[0] if cand else None


def _pick_csv_encoding(file_path: str) -> str:
    for enc in ("utf-8-sig", "cp1251", "utf-8", "latin-1"):
        try:
            with open(file_path, "r", encoding=enc) as f:
                _ = f.read(4096)
            return enc
        except UnicodeDecodeError:
            continue
    with open(file_path, "r", encoding="utf-8") as f:
        f.read(1)
    return "utf-8"


# ----------------------------- Pair helpers (NEW) -----------------------------

def _normalize_pair_name(raw: str) -> str:
    """
    Нормализация имени инструмента из CSV.

    Делает:
    - убирает префиксы (FX:, BINANCE:, OANDA:, FXCM:)
    - убирает /, -, пробелы
    - знает алиасы для индексов/металлов/крипты
    - если у индексов присутствует валютный суффикс (…USD/…USDT) — корректно отбрасывает его
    """
    s = (raw or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]  # FX:GBPUSD -> GBPUSD
    s = s.replace("/", "").replace("-", "").replace(" ", "")

    # Базовые алиасы (без валютных суффиксов)
    alias_base = {
        # Индексы
        "SPX500": "US500", "SP500": "US500", "SNP500": "US500", "S&P500": "US500", "US500": "US500",
        "NAS100": "US100", "US100": "US100", "NDX100": "US100",
        "DJ30": "US30", "US30": "US30", "DJI": "US30",
        "FTSE100": "UK100", "UK100": "UK100",
        "DE40": "GER40", "DAX": "GER40", "GER40": "GER40",
        "DAX30": "GER30", "DE30": "GER30", "GER30": "GER30",
        "NIKKEI225": "JP225", "NI225": "JP225", "JP225": "JP225",

        # Металлы
        "GOLD": "XAUUSD", "XAU": "XAUUSD", "XAUUSD": "XAUUSD",
        "SILVER": "XAGUSD", "XAG": "XAGUSD", "XAGUSD": "XAGUSD",

        # Крипта (по умолчанию к USDT)
        "BTC": "BTCUSDT", "BTCUSD": "BTCUSDT", "BTCUSDT": "BTCUSDT",
        "ETH": "ETHUSDT", "ETHUSD": "ETHUSDT", "ETHUSDT": "ETHUSDT",
    }

    # Если точное попадание в базовые алиасы — вернём сразу
    if s in alias_base:
        return alias_base[s]

    # Индекс + валютный суффикс (SPX500USD, US500USD, GER40USD, US100USD и т.п.)
    for suff in ("USD", "USDT"):
        if s.endswith(suff):
            base = s[: -len(suff)]
            if base in alias_base:
                return alias_base[base]
            # если это уже «нормализованная» форма индекса — оставляем базу
            if base in ("US500", "US100", "US30", "UK100", "GER40", "GER30", "JP225"):
                return base
            # металлы уже описаны отдельно, а форекс-пары оставим ниже
            break

    # Классические форекс-пары вида EURUSD, GBPUSD, USDJPY и т.п. — оставляем как есть
    if len(s) in (6, 7) and any(
            s.endswith(x) for x in ("USD", "USDT", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD")):
        return s

    # XAU/XAG без суффикса
    if s == "XAU":
        return "XAUUSD"
    if s == "XAG":
        return "XAGUSD"

    # Крипта без суффикса — считаем к USDT
    if s in ("BTC", "ETH"):
        return s + "USDT"

    return s


def _find_pair_prop_name(notion: Client, db_props: Dict[str, Any]) -> Optional[str]:
    """Ищем property пары (relation/select), допускаем русские/вариации."""
    lower_to_real = {k.casefold(): k for k in db_props.keys()}
    probes = ["pair", "пара", "symbol", "инструмент"]
    for p in probes:
        name = lower_to_real.get(p)
        if name and db_props[name].get("type") in ("relation", "select"):
            return name

    # эвристика: relation, чья связанная БД по названию похожа на «Пары/Pairs/Symbols»
    for name, info in db_props.items():
        if info.get("type") == "relation":
            try:
                rid = info["relation"]["database_id"]
                meta = notion.databases.retrieve(rid)
                title = meta.get("title", [])
                t = "".join(x.get("plain_text", "") for x in title).lower()
                if any(x in t for x in ("pair", "пары", "symbols", "инструменты")):
                    return name
            except Exception:
                pass

    if "Pair" in db_props and db_props["Pair"].get("type") in ("relation", "select"):
        return "Pair"
    return None


def _build_normalized_pair_map(
        notion: Client, db_props: Dict[str, Any], pair_prop_name: str
) -> Tuple[Optional[str], Dict[str, str]]:
    """
    Возвращает (pair_prop_type, norm_map), где norm_map: normalized_name -> page_id.
    Работает если колонка пары — relation. Если select — карта пустая (fallback).
    """
    info = db_props[pair_prop_name]
    ptype = info.get("type")
    if ptype != "relation":
        return ptype, {}

    related_db_id = info["relation"]["database_id"]
    raw_map = _load_relation_map(notion, related_db_id, label="pair_map")

    norm_map: Dict[str, str] = {}
    for display_name, page_id in raw_map.items():
        # исходное имя из БД
        norm = _normalize_pair_name(display_name)
        norm_map[norm] = page_id

        # альтернатива: убираем разделители и снова нормализуем
        alt = display_name.replace("/", "").replace("-", "").replace(" ", "").upper()
        alt_norm = _normalize_pair_name(alt)
        norm_map[alt_norm] = page_id

    logger.info(f"pair_map: loaded={len(raw_map)}; normalized_keys={len(norm_map)}")
    return ptype, norm_map


# ----------------------------- Setup mapping for journal -----------------------------

def _load_setup_code_maps_from_journal(
        notion: Client, db_props: Dict[str, Any]
) -> Tuple[Dict[str, str], Dict[str, str], Optional[str]]:
    if "Setup" not in db_props:
        return {}, {}, None

    info = db_props["Setup"]
    prop_type = info.get("type")
    code_to_id: Dict[str, str] = {}
    code_to_name: Dict[str, str] = {}

    if prop_type == "relation":
        setup_db_id = info["relation"]["database_id"]
        cursor = None
        while True:
            resp = notion.databases.query(database_id=setup_db_id, start_cursor=cursor)
            for p in resp.get("results", []):
                title = ""
                for k, v in p.get("properties", {}).items():
                    if v.get("type") == "title" and v["title"]:
                        title = v["title"][0]["plain_text"].strip()
                        break
                if title:
                    code = _derive_setup_code(title)
                    code_to_id[code] = p["id"]
                    code_to_name[code] = title
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")

        logger.info(f"Setup map (journal): codes={list(code_to_name.keys())}")
        return code_to_id, code_to_name, "relation"

    if prop_type == "select":
        opts = info.get("select", {}).get("options", []) or []
        for o in opts:
            name = o.get("name") or ""
            if not name:
                continue
            code = _derive_setup_code(name)
            code_to_name[code] = name
        logger.info(f"Setup select options (journal): codes={list(code_to_name.keys())}")
        return {}, code_to_name, "select"

    return {}, {}, None


# ----------------------------- import -----------------------------

@dataclass
class ImportResult:
    ok: bool
    imported: int = 0
    skipped: int = 0
    error: Optional[str] = None


async def import_trades_from_csv(user_id: int, file_path: str) -> Dict[str, Any]:
    try:
        row = await get_data(user_id)
        if not row:
            return {"ok": False, "error": "credentials_not_found: пользователь не найден"}

        notion_token, page_link = row[0], row[1]

        notion = Client(auth=notion_token)
        journal_db_id = resolve_journal_db_id(notion, page_link)

        db_meta = notion.databases.retrieve(journal_db_id)
        db_props = db_meta["properties"]
        logger.info(f"journal props: {list(db_props.keys())}")

        title_prop = _title_prop_name(db_props)
        logger.info(f"title prop: {title_prop}")

        # Карты для свойства Setup самой журнальной БД
        setup_code_to_id, setup_code_to_name, setup_prop_type = _load_setup_code_maps_from_journal(notion, db_props)

        # -------- PAIR: автоопределение property и нормализованный matcher (NEW) --------
        pair_prop_name: Optional[str] = _find_pair_prop_name(notion, db_props)
        pair_prop_type: Optional[str] = None
        pair_map_norm: Dict[str, str] = {}
        if pair_prop_name:
            try:
                pair_prop_type, pair_map_norm = _build_normalized_pair_map(notion, db_props, pair_prop_name)
            except Exception as e:
                logger.exception(f"pair_map build error: {e}")

        # Day / Session
        day_prop_key, session_prop_key, rel_maps = _auto_detect_day_session_props(notion, db_props)

        # Entry / SL indexes (+ множества сетапов по каждой детали для сообщений о рассинхроне)
        (
            entry_prop_key,
            sl_prop_key,
            entry_index,
            sl_index,
            entry_detail_setups,
            sl_detail_setups,
        ) = _make_entry_sl_index(notion, db_props)

        # === NEW: автоякоря "🧮 Матожидание" и "Direction" ===
        expect_prop_key = _find_expectation_anchor_prop(db_props)
        expect_anchor_id: Optional[str] = None
        if expect_prop_key:
            try:
                expect_db_id = db_props[expect_prop_key]["relation"]["database_id"]
                expect_anchor_id = _pick_single_page_id(notion, expect_db_id)
                logger.info(
                    f"expectation anchor: prop='{expect_prop_key}', db={expect_db_id[:6]}…, "
                    f"anchor_page={expect_anchor_id[:6] + '…' if expect_anchor_id else None}"
                )
            except Exception as e:
                logger.warning(f"expectation anchor resolve failed: {e!r}")

        direction_prop_key = _find_direction_anchor_prop(db_props)
        direction_anchor_id: Optional[str] = None
        if direction_prop_key:
            try:
                direction_db_id = db_props[direction_prop_key]["relation"]["database_id"]
                direction_anchor_id = _pick_single_page_id(notion, direction_db_id)
                logger.info(
                    f"direction anchor: prop='{direction_prop_key}', db={direction_db_id[:6]}…, "
                    f"anchor_page={direction_anchor_id[:6] + '…' if direction_anchor_id else None}"
                )
            except Exception as e:
                logger.warning(f"direction anchor resolve failed: {e!r}")

        # detect CSV encoding once
        enc = _pick_csv_encoding(file_path)
        logger.info(f"CSV: opened with encoding={enc!r}")

        # detect tags field
        with open(file_path, "r", encoding=enc, newline="") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            logger.info(f"csv header: {header}")
            tags_field = _detect_tags_field(header)
            logger.info(f"tags field detected: {tags_field!r}")

        with open(file_path, "r", encoding=enc, newline="") as f:
            reader = csv.DictReader(f)

            start_num = _fetch_max_title_number(notion, journal_db_id, title_prop)
            next_num = start_num + 1

            imported = 0
            skipped = 0

            for i, trade in enumerate(reader, start=1):
                try:
                    trade_id_raw = trade.get("id")
                    if not trade_id_raw and trade_id_raw != 0:
                        skipped += 1
                        logger.debug(f"row#{i}: no trade id -> skip")
                        continue

                    id_filter = _build_id_filter(db_props, trade_id_raw)
                    if id_filter:
                        try:
                            res = notion.databases.query(
                                database_id=journal_db_id, filter=id_filter, page_size=1
                            )
                            if res.get("results"):
                                skipped += 1
                                logger.debug(f"row#{i}: duplicate by ID={trade_id_raw} -> skip")
                                continue
                        except Exception as e:
                            logger.warning(f"row#{i}: dup-check error: {e!r}")

                    date_str = (trade.get("dateStart") or "").strip()
                    try:
                        dt_utc = datetime.strptime(date_str, "%Y/%m/%d %H:%M:%S").replace(
                            tzinfo=ZoneInfo("UTC")
                        )
                    except Exception:
                        skipped += 1
                        logger.debug(f"row#{i}: bad dateStart={date_str!r} -> skip")
                        continue
                    local_dt = dt_utc.astimezone(ZoneInfo("Europe/Kyiv"))
                    day_name = RU_WEEKDAY[local_dt.weekday()]
                    session_name = _detect_session(local_dt.hour)

                    # ----- Pair: нормализация и матчинг (NEW) -----
                    pair_raw = _strip_pair_name(trade.get("pair") or "")
                    pair_norm = _normalize_pair_name(pair_raw)

                    side_raw = (trade.get("side") or "").strip().lower()
                    if side_raw == "buy":
                        type_name = "Long"
                        bias_name = "Long"
                    elif side_raw == "sell":
                        type_name = "Short"
                        bias_name = "Short"
                    else:
                        type_name = side_raw.title() if side_raw else "—"
                        bias_name = type_name

                    rr = _to_float(trade.get("avgRiskReward"))
                    r_pnl = _to_float(trade.get("rPnL"))

                    props: Dict[str, Any] = {}
                    props[title_prop] = {"title": [{"text": {"content": str(next_num)}}]}
                    next_num += 1

                    if "ID" in db_props:
                        t = db_props["ID"]["type"]
                        if t == "number":
                            try:
                                props["ID"] = {"number": float(trade_id_raw)}
                            except Exception:
                                pass
                        elif t == "rich_text":
                            props["ID"] = {"rich_text": [{"text": {"content": str(trade_id_raw)}}]}
                        elif t == "title":
                            props["ID"] = {"title": [{"text": {"content": str(trade_id_raw)}}]}

                    # ----- Pair relation/select with normalized matching (NEW) -----
                    if pair_prop_name:
                        if pair_prop_type == "relation":
                            pid = pair_map_norm.get(pair_norm)
                            if not pid:
                                # попробовать агрессивно очищенный вариант
                                fallback = _normalize_pair_name(
                                    pair_raw.upper().replace("/", "").replace("-", "").replace(" ", "")
                                )
                                pid = pair_map_norm.get(fallback)
                            if pid:
                                props[pair_prop_name] = {"relation": [{"id": pid}]}
                            else:
                                logger.warning(f"row#{i}: Pair not matched -> csv={pair_raw!r} norm={pair_norm!r}")
                        elif pair_prop_type == "select":
                            sel_name = pair_raw.strip() or pair_norm
                            props[pair_prop_name] = {"select": {"name": sel_name}}

                    if "Date" in db_props:
                        props["Date"] = {"date": {"start": local_dt.isoformat()}}

                    if day_prop_key:
                        info = db_props[day_prop_key]
                        if info["type"] == "select":
                            props[day_prop_key] = {"select": {"name": day_name}}
                        elif info["type"] == "relation":
                            did = rel_maps.get(day_prop_key, {}).get(day_name)
                            if did:
                                props[day_prop_key] = {"relation": [{"id": did}]}
                            else:
                                logger.debug(f"row#{i}: Day '{day_name}' not found in relation map")

                    if session_prop_key and session_name:
                        info = db_props[session_prop_key]
                        if info["type"] == "select":
                            props[session_prop_key] = {"select": {"name": session_name}}
                        elif info["type"] == "relation":
                            sid = rel_maps.get(session_prop_key, {}).get(session_name)
                            if sid:
                                props[session_prop_key] = {"relation": [{"id": sid}]}
                            else:
                                logger.debug(f"row#{i}: Session '{session_name}' not found in relation map")

                    if "Type" in db_props:
                        props["Type"] = {"select": {"name": type_name}}
                    if "BIAS" in db_props:
                        props["BIAS"] = {"select": {"name": bias_name}}

                    if "RR" in db_props and isinstance(rr, (int, float)):
                        props["RR"] = {"number": float(rr)}

                    if "Result" in db_props and r_pnl is not None:
                        res_name = "TP" if r_pnl > 0 else ("SL" if r_pnl < 0 else "BE")
                        t = db_props["Result"]["type"]
                        if t in ("select", "multi_select"):
                            props["Result"] = {"select": {"name": res_name}}

                    # Risk
                    risk_pct = _estimate_risk_pct(trade)
                    if risk_pct is not None and "Risk" in db_props:
                        bucket = _risk_bucket(risk_pct)
                        info = db_props["Risk"]
                        if info.get("type") == "select":
                            props["Risk"] = {"select": {"name": bucket}}
                        elif info.get("type") == "relation":
                            risk_map = _load_relation_map(notion, info["relation"]["database_id"], "risk_map")
                            rid = risk_map.get(bucket)
                            if rid:
                                props["Risk"] = {"relation": [{"id": rid}]}
                            else:
                                logger.debug(f"row#{i}: Risk bucket '{bucket}' not found in risk_map")

                    # === NEW: автоякоря на создаваемую страницу ===
                    if expect_prop_key and expect_anchor_id:
                        props[expect_prop_key] = {"relation": [{"id": expect_anchor_id}]}
                    if direction_prop_key and direction_anchor_id:
                        props[direction_prop_key] = {"relation": [{"id": direction_anchor_id}]}

                    # Tags → Entry / SL
                    tags_raw = (trade.get(tags_field) if tags_field else trade.get("tags")) or ""
                    parsed_tags = _parse_tags(tags_raw)

                    # Setup (из тегов)
                    setup_codes = sorted({sc for (sc, _kind, _detail) in parsed_tags})
                    primary_setup = setup_codes[0] if setup_codes else None
                    if len(setup_codes) > 1:
                        logger.warning(
                            f"row#{i}: multiple setup codes in tags -> {setup_codes}; using primary={primary_setup}")

                    if primary_setup and "Setup" in db_props:
                        if setup_prop_type == "relation":
                            sid = setup_code_to_id.get(primary_setup)
                            if sid:
                                props.setdefault("Setup", {"relation": []})
                                props["Setup"]["relation"] = [{"id": sid}]
                                logger.debug(f"row#{i}: Setup relation set -> code={primary_setup} id={sid[:6]}…")
                            else:
                                fallback_name = setup_code_to_name.get(primary_setup)
                                if fallback_name:
                                    logger.debug(
                                        f"row#{i}: Setup relation id not found for code={primary_setup}, title={fallback_name}")
                                else:
                                    logger.debug(f"row#{i}: Setup relation miss for code={primary_setup}")
                        elif setup_prop_type == "select":
                            sel_name = setup_code_to_name.get(primary_setup, primary_setup)
                            props["Setup"] = {"select": {"name": sel_name}}
                            logger.debug(f"row#{i}: Setup select set -> code={primary_setup} name={sel_name}")

                    # Build relations from indexes
                    entry_rel_ids: List[str] = []
                    sl_rel_ids: List[str] = []

                    for setup_code, kind, detail_norm in parsed_tags:
                        if kind == "E" and entry_prop_key:
                            pid = (
                                    entry_index.get((setup_code, detail_norm))
                                    or entry_index.get(("GLOBAL", detail_norm))
                            )
                            if pid:
                                entry_rel_ids.append(pid)
                                logger.debug(f"row#{i}: ENTRY hit ({setup_code}, {detail_norm}) -> {pid[:6]}…")
                            else:
                                setups = entry_detail_setups.get(detail_norm)
                                if setups:
                                    logger.warning(
                                        f"row#{i}: ENTRY setup mismatch for detail '{detail_norm}': "
                                        f"tag setup={setup_code}, db setups={sorted(setups)}"
                                    )
                                else:
                                    logger.debug(f"row#{i}: ENTRY miss ({setup_code}, {detail_norm})")
                        if kind == "SL" and sl_prop_key:
                            pid = sl_index.get((setup_code, detail_norm)) or sl_index.get(("GLOBAL", detail_norm))
                            if pid:
                                sl_rel_ids.append(pid)
                                logger.debug(f"row#{i}: SL hit ({setup_code}, {detail_norm}) -> {pid[:6]}…")
                            else:
                                setups = sl_detail_setups.get(detail_norm)
                                if setups:
                                    logger.warning(
                                        f"row#{i}: SL setup mismatch for detail '{detail_norm}': "
                                        f"tag setup={setup_code}, db setups={sorted(setups)}"
                                    )
                                else:
                                    logger.debug(f"row#{i}: SL miss ({setup_code}, {detail_norm})")

                    if entry_prop_key and entry_rel_ids and db_props.get(entry_prop_key, {}).get("type") == "relation":
                        props[entry_prop_key] = {"relation": [{"id": pid} for pid in sorted(set(entry_rel_ids))]}
                    if sl_prop_key and sl_rel_ids and db_props.get(sl_prop_key, {}).get("type") == "relation":
                        props[sl_prop_key] = {"relation": [{"id": pid} for pid in sorted(set(sl_rel_ids))]}

                    notion.pages.create(
                        parent={"database_id": journal_db_id},
                        properties=props,
                        icon={"type": "external", "external": {"url": DEFAULT_ICON_URL}},
                    )
                    imported += 1
                    logger.info(f"row#{i}: created page #{next_num - 1}")

                except Exception as e:
                    skipped += 1
                    logger.exception(f"row#{i}: ERROR {e!r}")

        logger.info(f"entry_index size={len(entry_index)}; sl_index size={len(sl_index)}")

        return {"ok": True, "imported": imported, "skipped": skipped}

    except Exception as e:
        logger.exception("fatal import error")
        return {"ok": False, "error": str(e)}
