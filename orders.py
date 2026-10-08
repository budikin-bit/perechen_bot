# -*- coding: utf-8 -*-
"""
Чтение приказов в формате RTF или DOCX → таблица строк + реквизиты приказа.

    from orders import read_order
    order = read_order("приказ.rtf")      # или .docx
    order.meta   # {'ministry': 'Минобрнауки России', 'number': '521', 'date': '31.08.2026', …}
    order.rows   # [[ячейка1, …, ячейка6], …] — все строки таблиц с ≥ 6 колонок

RTF читается собственным читалкой (без LibreOffice и без сторонних пакетов),
поэтому сборка каталога работает и на Windows. DOCX читается через python-docx.

Особенность вертикально объединённых ячеек:
  • в RTF продолжение объединённой ячейки — пустая строка;
  • python-docx повторяет в них текст первой ячейки.
Парсеры (parse_minobr.py, parse_minpros.py) рассчитаны на оба варианта.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# ───────────────────────── кавычки ─────────────────────────

_OPEN_AFTER = set(" \t\n(«[—–-/;:")


def fix_quotes(s: str) -> str:
    """
    Прямые кавычки " → «ёлочки» (с учётом вложенности: «…«…»…»).
    В официальных текстах кавычки прямые; без этого не работают разбор
    организаторов и аббревиатуры.
    """
    if '"' not in s:
        return s
    out: List[str] = []
    for i, ch in enumerate(s):
        if ch != '"':
            out.append(ch)
            continue
        prev = out[-1] if out else ""
        nxt = s[i + 1] if i + 1 < len(s) else ""
        opening = (not prev or prev in _OPEN_AFTER) and nxt and not nxt.isspace()
        out.append("«" if opening else "»")
    res = "".join(out)
    return res.replace("“", "«").replace("”", "»").replace("„", "«")


def clean_cell(s: str) -> str:
    """Пробелы нормализуются построчно: переводы строк внутри ячейки сохраняются."""
    s = re.sub(r"[\udc80-\udcff]", "", s).replace("\xa0", " ").replace("\r", "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    return fix_quotes("\n".join(ln for ln in lines if ln))


# ───────────────────────── RTF ─────────────────────────

_SKIP_DEST = {
    "fonttbl", "colortbl", "stylesheet", "info", "pict", "shp", "shpinst", "shptxt",
    "shprslt", "nonshppict", "header", "footer", "headerl", "headerr", "headerf",
    "footerl", "footerr", "footerf", "listtable", "listoverridetable", "rsidtbl",
    "generator", "themedata", "colorschememapping", "latentstyles", "datastore",
    "xmlnstbl", "fldinst", "bkmkstart", "bkmkend", "pgptbl", "revtbl", "filetbl", "private",
}
_SYMBOLS = {"emdash": "—", "endash": "–", "lquote": "‘", "rquote": "’",
            "ldblquote": "«", "rdblquote": "»", "bullet": "•", "enspace": " ", "emspace": " "}
_TOK = re.compile(
    r"\\'([0-9a-fA-F]{2})|\\([a-zA-Z]+)(-?\d+)? ?|\\(.)|([{}])|([^\\{}]+)", re.S)


_ENC = "cp1251"


def _decode_rtf(raw: bytes) -> str:
    """Текст RTF; байты без символа в кодировке сохраняются (surrogateescape) —
    это нужно, чтобы посчитать хэш встроенных картинок по исходным байтам."""
    global _ENC
    m = re.search(rb"\\ansicpg(\d+)", raw[:2000])
    _ENC = f"cp{m.group(1).decode()}" if m else "cp1251"
    try:
        return raw.decode(_ENC, "surrogateescape")
    except LookupError:
        _ENC = "cp1251"
        return raw.decode(_ENC, "surrogateescape")


def _iter_tokens(s: str):
    """Токены RTF. Двоичные данные после \\binN пропускаются и отдаются целиком."""
    pos, n = 0, len(s)
    while pos < n:
        m = _TOK.match(s, pos)
        if not m:
            pos += 1
            continue
        pos = m.end()
        hexv, word, num, sym, brace, text = m.groups()
        if word == "bin" and num:
            k = int(num)
            data = s[pos:pos + k]
            pos += k
            yield (None, "bin", data, None, None, None)
        else:
            yield hexv, word, num, sym, brace, text


# ───────────────────────── картинки вместо текста ─────────────────────────
# Справочные системы выносят в картинки (объекты MathType) иероглифы, якутские и
# латинские слова с диакритикой. Тексты картинок хранятся в image_text.json
# (ключ — первые 12 знаков SHA-1 содержимого картинки).

IMG_RE = re.compile(r"⟦img:([0-9a-f]{12})⟧")
UNRESOLVED: Dict[str, str] = {}      # хэш → где встретилась (для предупреждений)


def _load_image_text() -> Dict[str, str]:
    import json
    p = Path(__file__).resolve().parent / "image_text.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


IMAGE_TEXT = _load_image_text()


def resolve_images(s: str) -> str:
    def sub(m):
        t = IMAGE_TEXT.get(m.group(1))
        if t is None:
            UNRESOLVED[m.group(1)] = s[:80]
            return "⟦рисунок⟧"
        return t
    out = IMG_RE.sub(sub, s)
    return re.sub(r"\s*\x08", "", out)       # \b в тексте картинки: «приклеить к предыдущему»


def rtf_tables(s: str) -> List[List[str]]:
    """Все строки всех таблиц RTF как списки текстов ячеек (абзацы ячейки — через \\n)."""
    rows: List[List[str]] = []
    cells: List[str] = []
    buf: List[str] = []
    stack: List[bool] = []
    skip = False
    first = False
    started = False
    uc_skip = 0
    pict_ok = False
    for hexv, word, num, sym, brace, text in _iter_tokens(s):
        if brace == "{":
            stack.append(skip)
            first = True
            continue
        if brace == "}":
            skip = stack.pop() if stack else False
            first = False
            continue
        if sym is not None:
            if sym == "*" and first:
                skip = True
            elif not skip and started:
                if sym in "{}\\":
                    buf.append(sym)
                elif sym == "~":
                    buf.append(" ")
            first = False
            continue
        if word is not None:
            if first and word in _SKIP_DEST:
                skip = True
                if word == "pict":
                    pict_ok = started and not (stack[-1] if stack else False)
            first = False
            if word == "bin":
                if pict_ok:
                    digest = hashlib.sha1(num.encode(_ENC, "surrogateescape")).hexdigest()[:12]
                    buf.append(f"⟦img:{digest}⟧")
                pict_ok = False
                continue
            if word == "trowd":
                started = True
            if skip or not started:
                continue
            if word == "cell":
                cells.append("".join(buf))
                buf = []
            elif word == "row":
                rows.append(cells)
                cells = []
            elif word in ("par", "line"):
                buf.append("\n")
            elif word == "tab":
                buf.append(" ")
            elif word == "u" and num is not None:
                buf.append(chr(int(num) % 65536))
                uc_skip = 1
            elif word in _SYMBOLS:
                buf.append(_SYMBOLS[word])
            continue
        first = False
        if skip or not started:
            continue
        if hexv is not None:
            buf.append(bytes([int(hexv, 16)]).decode("cp1251", "replace"))
        elif text is not None:
            if uc_skip:
                text, uc_skip = text[uc_skip:], 0
            buf.append(text.replace("\r", "").replace("\n", ""))
    return [[clean_cell(resolve_images(c)) for c in r] for r in rows]


def rtf_plain(s: str, limit: int = 60000) -> str:
    """Весь видимый текст RTF (вне зависимости от таблиц) — для реквизитов приказа."""
    out: List[str] = []
    stack: List[bool] = []
    skip = first = False
    uc_skip = size = 0
    for hexv, word, num, sym, brace, text in _iter_tokens(s):
        if brace == "{":
            stack.append(skip)
            first = True
            continue
        if brace == "}":
            skip = stack.pop() if stack else False
            first = False
            continue
        if sym is not None:
            if sym == "*" and first:
                skip = True
            elif not skip and sym == "~":
                out.append(" ")
            first = False
            continue
        if word is not None:
            if first and word in _SKIP_DEST:
                skip = True
            first = False
            if skip:
                continue
            if word in ("par", "line", "row"):
                out.append("\n")
            elif word in ("cell", "tab"):
                out.append(" ")
            elif word == "u" and num is not None:
                out.append(chr(int(num) % 65536))
                uc_skip = 1
            continue
        first = False
        if skip:
            continue
        piece = (bytes([int(hexv, 16)]).decode("cp1251", "replace") if hexv is not None
                 else text.replace("\r", "").replace("\n", ""))
        if uc_skip and hexv is None:
            piece, uc_skip = piece[uc_skip:], 0
        out.append(piece)
        size += len(piece)
        if size > limit:
            break
    return "".join(out)


# ───────────────────────── DOCX ─────────────────────────

def docx_tables(path: Path):
    from docx import Document
    d = Document(str(path))
    rows = []
    for t in d.tables:
        for r in t.rows:
            rows.append([clean_cell(c.text) for c in r.cells])
    head = "\n".join(p.text for p in d.paragraphs if p.text.strip())
    return rows, head


# ───────────────────────── реквизиты ─────────────────────────

MINISTRIES = {
    "Минобрнауки": "Минобрнауки России",
    "Минпросвещения": "Минпросвещения России",
}


def parse_meta(text: str) -> Dict[str, str]:
    """Реквизиты приказа из текста заголовка («Приказ Минобрнауки России от … N …»)."""
    t = text.replace("\xa0", " ")
    meta: Dict[str, str] = {}
    m = re.search(r"Приказ\s+(Мин[а-я]+)\s+России\s+от\s+(\d\d\.\d\d\.\d{4})\s+N\s*([\w/-]+)", t)
    if m:
        meta["ministry"] = MINISTRIES.get(m.group(1), m.group(1) + " России")
        meta["date"], meta["number"] = m.group(2), m.group(3)
    m = re.search(r'"\s*(Об утверждении.*?)\s*"\s*\(Зарегистрировано', t, re.S)
    if m:
        meta["title"] = re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"Зарегистрировано в Минюсте России\s+(\d\d\.\d\d\.\d{4})\s+N\s*(\d+)", t)
    if m:
        meta["reg_date"], meta["reg_number"] = m.group(1), m.group(2)
    m = re.search(r"Начало действия документа\s*-\s*(\d\d\.\d\d\.\d{4})", t)
    if m:
        meta["effective_date"] = m.group(1)
    return meta


def format_source(meta: Dict[str, str]) -> str:
    """Строка-ссылка на приказ для карточки и справки."""
    if not meta.get("number"):
        return ""
    s = f"Приказ {meta.get('ministry', '')} от {meta['date']} № {meta['number']}"
    if meta.get("title"):
        s += f" «{meta['title']}»"
    extra = []
    if meta.get("reg_number"):
        extra.append(f"зарегистрирован в Минюсте России {meta['reg_date']} № {meta['reg_number']}")
    if meta.get("effective_date"):
        extra.append(f"действует с {meta['effective_date']}")
    if extra:
        s += " (" + "; ".join(extra) + ")"
    return s


# ───────────────────────── точка входа ─────────────────────────

@dataclass
class Order:
    path: str
    meta: Dict[str, str] = field(default_factory=dict)
    rows: List[List[str]] = field(default_factory=list)


def read_order(path) -> Order:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    suffix = p.suffix.lower()
    if suffix == ".rtf":
        s = _decode_rtf(p.read_bytes())
        rows = rtf_tables(s)
        meta_text = rtf_plain(s)
    elif suffix == ".docx":
        rows, head = docx_tables(p)
        meta_text = head
    else:
        raise ValueError(f"Неподдерживаемый формат: {p.suffix} (нужен .rtf или .docx)")
    rows = [r for r in rows if len(r) >= 6]
    return Order(path=str(p), meta=parse_meta(meta_text), rows=rows)


def find_order(ministry: str, folder: str = ".") -> Optional[str]:
    """Находит в папке приказ нужного министерства (.rtf предпочтительнее .docx)."""
    found = []
    for p in sorted(Path(folder).iterdir()):
        if p.suffix.lower() not in (".rtf", ".docx") or p.name.startswith("~$"):
            continue
        try:
            o = read_order(p)
        except Exception:  # noqa: BLE001
            continue
        if o.meta.get("ministry") == ministry and o.meta.get("number"):
            found.append((p.suffix.lower() == ".rtf", p.stat().st_mtime, str(p)))
    return max(found)[2] if found else None


# ───────────────────────── общие приёмы разбора строк ─────────────────────────

def is_header_row(r: List[str]) -> bool:
    n, name = r[0], r[1]
    if ("п/п" in n) or n.lower().startswith(("n п", "№")):
        return True
    if name.startswith(("Полное наименование", "Название мероприятия", "Наименование")):
        return True
    if r[3].startswith(("Профиль олимпиады", "Профильное направление", "Направление мероприятия")):
        return True
    if r[4].startswith(("Общеобразовательные предметы", "Профильное направление")):
        return True
    return False


SECTION_RE = re.compile(r"^(\d+)\.\s+Мероприятия\b")


def is_section_row(r: List[str]) -> Optional[int]:
    """Номер раздела, если это строка-заголовок раздела (в RTF — слитая ячейка)."""
    m = SECTION_RE.match(r[0])
    if m and (not r[1] or r[1] == r[0]):
        return int(m.group(1))
    return None


LEVEL_RX = [
    (re.compile(r"^\s*высш", re.I), "Высший"),
    (re.compile(r"^\s*(IV|III|II|I)\b", re.I), None),      # римская цифра как есть
    (re.compile(r"^\s*не\s+установлен", re.I), "Не установлен"),
    (re.compile(r"^\s*не\s*\.?\s*$", re.I), "Не установлен"),   # в приказе № 598 (6.345) слово обрезано до «Не»
]


def norm_level(s: str) -> str:
    """«IV уровень» → «IV», «Высший уровень» → «Высший», «Не установлен.» → «Не установлен»."""
    s = (s or "").strip()
    for rx, val in LEVEL_RX:
        m = rx.match(s)
        if m:
            return val or m.group(1).upper()
    return s


def split_top_level(s: str, sep: str = ";") -> List[str]:
    """Делит по разделителю вне скобок и кавычек; пустые части отбрасывает."""
    parts, buf, depth, quote = [], [], 0, 0
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch == "«":
            quote += 1
        elif ch == "»":
            quote = max(0, quote - 1)
        if ch == sep and depth == 0 and quote == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf).strip())
    return [p for p in parts if p]


def uniq(seq):
    return list(dict.fromkeys(x for x in seq if x))
