# ruff: noqa
import csv
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from notion_client import Client
from zoneinfo import ZoneInfo

from src.app.database.requests import get_data  # твоя async-функция

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

# ---------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------

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


def collect_child_databases(notion: Client, root_block_id: str, max_depth: int = 4) -> List[str]:
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
    return score


def resolve_journal_db_id(notion: Client, root_url: str) -> str:
    page_or_db_id = _page_id_from_url(root_url)
    try:
        notion.databases.retrieve(page_or_db_id)
        return page_or_db_id
    except Exception:
        pass
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
    return best_id

# ---------------------------------------------------------------------
# misc helpers
# ---------------------------------------------------------------------

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
                return max_num
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
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


def _load_relation_map(notion: Client, related_db_id: str) -> Dict[str, str]:
    name_to_id: Dict[str, str] = {}
    cursor = None
    while True:
        resp = notion.databases.query(database_id=related_db_id, start_cursor=cursor)
        for page in resp.get("results", []):
            props = page.get("properties", {})
            for key, val in props.items():
                if val.get("type") == "title" and val["title"]:
                    name = val["title"][0]["plain_text"].strip()
                    if name:
                        name_to_id[name] = page["id"]
                    break
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return name_to_id

# ---------------------------------------------------------------------
# Day / Session auto-detect
# ---------------------------------------------------------------------

def _auto_detect_day_session_props(
    notion: Client, db_props: Dict[str, Any]
) -> Tuple[Optional[str], Optional[str], Dict[str, Dict[str, str]]]:
    rel_maps: Dict[str, Dict[str, str]] = {}
    day_key: Optional[str] = None
    session_key: Optional[str] = None

    # relation
    for prop_name, info in db_props.items():
        if info.get("type") == "relation":
            related_db = info["relation"]["database_id"]
            try:
                m = _load_relation_map(notion, related_db)
            except Exception:
                m = {}
            rel_maps[prop_name] = m
            names = set(m.keys())

            if not day_key and len(names & set(RU_WEEKDAY.values())) >= 4:
                day_key = prop_name
            if not session_key and len(names & SESSION_SET) >= 3:
                session_key = prop_name

    # select
    for prop_name, info in db_props.items():
        if info.get("type") == "select":
            options = {o.get("name") for o in info.get("select", {}).get("options", []) if o.get("name")}
            if not day_key and len(options & set(RU_WEEKDAY.values())) >= 4:
                day_key = prop_name
            if not session_key and len(options & SESSION_SET) >= 3:
                session_key = prop_name

    return day_key, session_key, rel_maps

# ---------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------

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

# ---------------------------------------------------------------------
# Tags → Entry / SL
# ---------------------------------------------------------------------

_TAG_RE = re.compile(r"^\s*([A-Za-z0-9]+)_(E|SL)_(.+?)\s*$")

# спец-мап сетапов в коды — чтобы “30m OF” → “30F”
_SETUP_CODE_OVERRIDES = {
    "30m OF": "30F",
}

def _normalize_detail(s: str) -> str:
    x = s.replace("_", " ").strip()
    x = re.sub(r"([A-Za-z])(\d)", r"\1 \2", x)
    x = re.sub(r"(\d)([A-Za-z])", r"\1 \2", x)
    x = re.sub(r"([a-z])([A-Z])", r"\1 \2", x)
    repl = {
        "FVG 0 5": "FVG 0.5",
        "FVGFulfill": "FVG Fulfill",
        "FVGOpen": "FVG Open",
        "SLFractal": "SL Fractal",
        "SLFulfill": "SL Fulfill",
        "TPFractal": "TP Fractal",
        "TPFix2 RR": "TP Fix2RR",
        "TPFix2RR": "TP Fix2RR",
        "TPFVG": "TP FVG",
        "TF30 m": "TF 30m",
        "TF15 m": "TF 15m",
        "RulesRR": "Rules RR",
    }
    for a, b in repl.items():
        x = x.replace(a, b)
    x = re.sub(r"\s{2,}", " ", x).strip()
    return x


def _derive_setup_code(setup_name: str) -> str:
    name = setup_name.strip()
    # явные переопределения
    if name in _SETUP_CODE_OVERRIDES:
        return _SETUP_CODE_OVERRIDES[name]
    # “(\d+)m OF” -> “\1F”
    m = re.search(r"^\s*(\d+)\s*m\s+OF\s*$", name, re.I)
    if m:
        return f"{m.group(1)}F"
    # общий фолбэк
    digits = "".join(re.findall(r"\d+", name))
    words = [w for w in re.split(r"\s+", re.sub(r"[^0-9A-Za-z\s]", " ", name)) if w]
    if digits and words:
        last = words[-1]
        return f"{digits}{last[0].upper()}"
    return re.sub(r"[^0-9A-Za-z]", "", name).upper()


def _index_detail_db_by_setup(
    notion: Client, detail_db_id: str, setup_rel_prop: str
) -> Dict[Tuple[str, str], str]:
    # 1) Setup DB
    detail_props = notion.databases.retrieve(detail_db_id)["properties"]
    setup_prop_info = detail_props.get(setup_rel_prop)
    if not setup_prop_info or setup_prop_info.get("type") != "relation":
        return {}

    setup_db_id = setup_prop_info["relation"]["database_id"]

    # 2) setup_id -> setup_code
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
                setup_id_to_code[p["id"]] = _derive_setup_code(stitle)
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")

    # 3) индекс деталей
    index: Dict[Tuple[str, str], str] = {}
    cursor = None
    while True:
        resp = notion.databases.query(database_id=detail_db_id, start_cursor=cursor)
        for page in resp.get("results", []):
            props = page.get("properties", {})

            # title -> detail
            detail_text = ""
            for k, v in props.items():
                if v.get("type") == "title" and v["title"]:
                    detail_text = v["title"][0]["plain_text"].strip()
                    break
            if not detail_text:
                continue
            detail_norm = _normalize_detail(detail_text)

            # связанный Setup
            rel = props.get(setup_rel_prop, {})
            rel_items = rel.get("relation") or []
            if not rel_items:
                index[("GLOBAL", detail_norm)] = page["id"]
                continue

            for it in rel_items:
                scode = setup_id_to_code.get(it["id"]) or "GLOBAL"
                index[(scode, detail_norm)] = page["id"]

        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")

    return index


def _make_entry_sl_index(
    notion: Client, db_props: Dict[str, Any]
) -> Tuple[Optional[str], Optional[str], Dict[Tuple[str, str], str], Dict[Tuple[str, str], str], str]:
    lower_to_real = {k.lower(): k for k in db_props.keys()}
    entry_prop = lower_to_real.get("entry")
    sl_prop = lower_to_real.get("sl")

    def detect_setup_prop_name(detail_db_id: str) -> Optional[str]:
        props = notion.databases.retrieve(detail_db_id)["properties"]
        # ищем ровно "Setup"
        for k in props.keys():
            if k.lower() == "setup" and props[k].get("type") == "relation":
                return k
        for k, v in props.items():
            if v.get("type") == "relation" and "setup" in k.lower():
                return k
        return None

    entry_index: Dict[Tuple[str, str], str] = {}
    sl_index: Dict[Tuple[str, str], str] = {}
    diag = []

    if entry_prop and db_props.get(entry_prop, {}).get("type") == "relation":
        entry_db_id = db_props[entry_prop]["relation"]["database_id"]
        setup_rel = detect_setup_prop_name(entry_db_id)
        if setup_rel:
            entry_index = _index_detail_db_by_setup(notion, entry_db_id, setup_rel)
            diag.append(f"Entry prop='{entry_prop}', setup_rel='{setup_rel}', indexed={len(entry_index)}")
        else:
            diag.append(f"Entry prop='{entry_prop}': не найдена колонка Setup в дочерней БД")

    if sl_prop and db_props.get(sl_prop, {}).get("type") == "relation":
        sl_db_id = db_props[sl_prop]["relation"]["database_id"]
        setup_rel = detect_setup_prop_name(sl_db_id)
        if setup_rel:
            sl_index = _index_detail_db_by_setup(notion, sl_db_id, setup_rel)
            diag.append(f"SL prop='{sl_prop}', setup_rel='{setup_rel}', indexed={len(sl_index)}")
        else:
            diag.append(f"SL prop='{sl_prop}': не найдена колонка Setup в дочерней БД")

    return entry_prop, sl_prop, entry_index, sl_index, " | ".join(diag)


def _parse_tags(raw: str) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    if not raw:
        return out
    parts = [t for t in raw.split(",") if t.strip()]
    for t in parts:
        m = _TAG_RE.match(t)
        if not m:
            continue
        setup_code = m.group(1).upper()
        kind = m.group(2).upper()  # E / SL
        detail_norm = _normalize_detail(m.group(3))
        out.append((setup_code, kind, detail_norm))
    return out

# ---------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------

@dataclass
class ImportResult:
    ok: bool
    imported: int = 0
    skipped: int = 0
    error: Optional[str] = None
    extra_log: Optional[str] = None


async def import_trades_from_csv(user_id: int, file_path: str) -> Dict[str, Any]:
    row = await get_data(user_id)
    if not row:
        return {"ok": False, "error": "credentials_not_found: пользователь не найден"}

    notion_token, page_link = row[0], row[1]

    try:
        notion = Client(auth=notion_token)
        journal_db_id = resolve_journal_db_id(notion, page_link)

        db_props = notion.databases.retrieve(journal_db_id)["properties"]
        title_prop = _title_prop_name(db_props)

        # Pair map (если relation)
        pair_map: Dict[str, str] = {}
        pair_prop_type = db_props.get("Pair", {}).get("type")
        if pair_prop_type == "relation":
            try:
                pair_map = _load_relation_map(notion, db_props["Pair"]["relation"]["database_id"])
            except Exception:
                pair_map = {}

        # Day / Session
        day_prop_key, session_prop_key, rel_maps = _auto_detect_day_session_props(notion, db_props)

        # Entry / SL indexes + диагностика
        entry_prop_key, sl_prop_key, entry_index, sl_index, diag_info = _make_entry_sl_index(notion, db_props)

        start_num = _fetch_max_title_number(notion, journal_db_id, title_prop)
        next_num = start_num + 1

        unmatched: List[str] = []
        diag_lines: List[str] = []
        if diag_info:
            diag_lines.append(diag_info)

        with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            imported = 0
            skipped = 0

            for trade in reader:
                try:
                    trade_id_raw = trade.get("id")
                    if not trade_id_raw and trade_id_raw != 0:
                        skipped += 1
                        continue

                    id_filter = _build_id_filter(db_props, trade_id_raw)
                    if id_filter:
                        try:
                            res = notion.databases.query(
                                database_id=journal_db_id,
                                filter=id_filter,
                                page_size=1,
                            )
                            if res.get("results"):
                                skipped += 1
                                continue
                        except Exception:
                            pass

                    date_str = (trade.get("dateStart") or "").strip()
                    try:
                        dt_utc = datetime.strptime(date_str, "%Y/%m/%d %H:%M:%S").replace(
                            tzinfo=ZoneInfo("UTC")
                        )
                    except Exception:
                        skipped += 1
                        continue
                    local_dt = dt_utc.astimezone(ZoneInfo("Europe/Kyiv"))
                    day_name = RU_WEEKDAY[local_dt.weekday()]
                    session_name = _detect_session(local_dt.hour)

                    pair_name = _strip_pair_name(trade.get("pair") or "")

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

                    # Title автоинкремент
                    props[title_prop] = {"title": [{"text": {"content": str(next_num)}}]}
                    next_num += 1

                    # ID
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

                    # Pair
                    if "Pair" in db_props:
                        if pair_prop_type == "select" and pair_name:
                            props["Pair"] = {"select": {"name": pair_name}}
                        elif pair_prop_type == "relation" and pair_name:
                            pid = pair_map.get(pair_name)
                            if pid:
                                props["Pair"] = {"relation": [{"id": pid}]}

                    # Date
                    if "Date" in db_props:
                        props["Date"] = {"date": {"start": local_dt.isoformat()}}

                    # Day
                    if day_prop_key:
                        info = db_props[day_prop_key]
                        if info["type"] == "select":
                            props[day_prop_key] = {"select": {"name": day_name}}
                        elif info["type"] == "relation":
                            did = rel_maps.get(day_prop_key, {}).get(day_name)
                            if did:
                                props[day_prop_key] = {"relation": [{"id": did}]}

                    # Session
                    if session_prop_key and session_name:
                        info = db_props[session_prop_key]
                        if info["type"] == "select":
                            props[session_prop_key] = {"select": {"name": session_name}}
                        elif info["type"] == "relation":
                            sid = rel_maps.get(session_prop_key, {}).get(session_name)
                            if sid:
                                props[session_prop_key] = {"relation": [{"id": sid}]}

                    # Type / BIAS
                    if "Type" in db_props:
                        props["Type"] = {"select": {"name": type_name}}
                    if "BIAS" in db_props:
                        props["BIAS"] = {"select": {"name": bias_name}}

                    # RR
                    if "RR" in db_props and isinstance(rr, (int, float)):
                        props["RR"] = {"number": float(rr)}

                    # Result
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
                            risk_map = {}
                            try:
                                risk_map = _load_relation_map(notion, info["relation"]["database_id"])
                            except Exception:
                                risk_map = {}
                            rid = risk_map.get(bucket)
                            if rid:
                                props["Risk"] = {"relation": [{"id": rid}]}

                    # Tags → Entry / SL
                    tags_raw = trade.get("tags") or ""
                    parsed_tags = _parse_tags(tags_raw)

                    entry_rel_ids: List[str] = []
                    sl_rel_ids: List[str] = []
                    for setup_code, kind, detail_norm in parsed_tags:
                        if kind == "E" and entry_prop_key:
                            pid = entry_index.get((setup_code, detail_norm)) or entry_index.get(("GLOBAL", detail_norm))
                            if pid:
                                entry_rel_ids.append(pid)
                            else:
                                unmatched.append(f"E:{setup_code}:{detail_norm}")
                        if kind == "SL" and sl_prop_key:
                            pid = sl_index.get((setup_code, detail_norm)) or sl_index.get(("GLOBAL", detail_norm))
                            if pid:
                                sl_rel_ids.append(pid)
                            else:
                                unmatched.append(f"SL:{setup_code}:{detail_norm}")

                    if entry_prop_key and entry_rel_ids and db_props.get(entry_prop_key, {}).get("type") == "relation":
                        props[entry_prop_key] = {"relation": [{"id": pid} for pid in sorted(set(entry_rel_ids))]}
                    if sl_prop_key and sl_rel_ids and db_props.get(sl_prop_key, {}).get("type") == "relation":
                        props[sl_prop_key] = {"relation": [{"id": pid} for pid in sorted(set(sl_rel_ids))]}

                    # create
                    notion.pages.create(parent={"database_id": journal_db_id}, properties=props)
                    imported += 1

                except Exception:
                    skipped += 1
                    continue

        # диагноcтика и первые 20 несовпавших
        extra = []
        if diag_lines:
            extra.append(" | ".join(diag_lines))
        if unmatched:
            extra.append("Unmatched tags (first 20): " + ", ".join(unmatched[:20]))
        result = {"ok": True, "imported": imported, "skipped": skipped}
        if extra:
            result["extra_log"] = "\n".join(extra)
        return result

    except Exception as e:
        return {"ok": False, "error": str(e)}
