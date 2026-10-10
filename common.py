# -*- coding: utf-8 -*-
"""
Общие функции для парсеров, сборщика каталога и бота.

Всё, что должно вести себя одинаково в трёх местах (нормализация названий,
стемминг, разбор организаторов, id мероприятий), живёт здесь.
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, List

# ───────────────────────── нормализация ─────────────────────────

CYR = re.compile(r"[а-я]")
WORD_RE = re.compile(r"[a-zа-я0-9]+")
ENDINGS = re.compile(
    r"(иями|ями|ами|ого|ому|ыми|ими|ему|ией|ия|ии|ие|ию|ей|ой|ый|ий|ая|ое|ые|ых|"
    r"ах|ям|ов|ев|ам|ом|ем|у|ы|и|а|о|е|я|ь|й)$"
)

# Служебные слова, которые не должны сужать поиск.
STOP_STEMS = {
    "по", "для", "и", "в", "во", "на", "с", "со", "к", "от", "из", "о", "об", "за",
    "олимпиад", "школьник", "мероприят",
}


def norm_name(s: str) -> str:
    """lower, ё→е, кавычки убраны, всё кроме букв/цифр → пробел. Без усечения."""
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'`“”„]", "", s)
    s = re.sub(r"[^a-zа-я0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def stem(t: str) -> str:
    """Грубый стеммер: отрезает падежное окончание, корень не короче 3 букв."""
    if len(t) < 4 or not CYR.search(t):
        return t
    m = ENDINGS.search(t)
    if not m:
        return t
    s = t[: m.start()]
    return s if len(s) >= 3 else t


def stems(s: str) -> List[str]:
    return [stem(w) for w in WORD_RE.findall(norm_name(s))]


def query_stems(s: str) -> List[str]:
    """Основы запроса без служебных слов (если остались только они — берём как есть)."""
    all_st = stems(s)
    kept = [t for t in all_st if t not in STOP_STEMS]
    return kept or all_st


def stem_index(s: str) -> str:
    """Строка ' основа1 основа2 … ' для быстрого поиска по началу слова."""
    return " " + " ".join(stems(s)) + " "


def stem_hit(idx: str, q: str) -> bool:
    """Основа запроса q входит в индекс: по началу слова (для коротких — целиком)."""
    if len(q) < 3:
        return f" {q} " in idx
    return f" {q}" in idx


# ───────────────────────── идентификаторы ─────────────────────────

def legacy_key(name: str) -> str:
    """Хвост старых id (md5 от сырого названия) — нужен для миграции избранного."""
    return hashlib.md5((name or "").encode("utf-8")).hexdigest()[:10]


def make_id(prefix: str, name: str, seen: Dict[str, int], secondary: str = None) -> str:
    """
    Стабильный id: зависит только от источника и нормализованного названия
    (``prefix-md5(norm_name)[:10]``). Не зависит от номера в приказе, поэтому
    переживает смену проект → утверждённый.

    Дубли названий внутри одного источника. Первое вхождение получает базовый id,
    следующие — суффикс:
      • ``secondary`` задан (так делает build_catalog: организатор + номер) —
        ``-`` + md5(norm_name(название) | norm_name(secondary))[:6]. Суффикс не
        зависит от порядка строк; чтобы и выбор «первого» не зависел от порядка,
        build_catalog.assign_ids сортирует дубли по (номеру, организатору);
      • ``secondary`` не задан — порядковый ``-2``, ``-3`` (старое поведение).
    Если и суффикс совпал (полные дубли), добавляется счётчик.
    В утверждённых приказах 2026/27 дублей названий нет; в проекте был один
    (mp-2d495f17ac-2) — build_catalog.attach_old_ids сопоставляет такие id по
    хэш-части, так что избранное не теряется.
    """
    base = f"{prefix}-{hashlib.md5(norm_name(name).encode('utf-8')).hexdigest()[:10]}"
    n = seen[base] = seen.get(base, 0) + 1
    if n == 1:
        return base
    if secondary is None:
        return f"{base}-{n}"
    key = norm_name(name) + "|" + norm_name(secondary)
    cand = f"{base}-{hashlib.md5(key.encode('utf-8')).hexdigest()[:6]}"
    k = seen[cand] = seen.get(cand, 0) + 1
    return cand if k == 1 else f"{cand}-{k}"


def id_hash(event_id: str) -> str:
    """Хэш-часть id: «mo-117a9fe69f» и «mp-117a9fe69f-2» → «117a9fe69f» (то же norm_name)."""
    parts = (event_id or "").split("-")
    return parts[1] if len(parts) >= 2 else event_id


# ───────────────────── сравнение названий (переименования) ─────────────────────
# Только для сопоставления версий каталога; в id и поиске не используется.

# Латиница и римские цифры, похожие на кириллицу, сводятся к кириллице: «XIX»,
# «ХIХ» (кириллическая Х), «IТ» и «IT» дают одно и то же.
_FOLD = str.maketrans({
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м", "o": "о",
    "p": "р", "t": "т", "x": "х", "y": "у", "i": "і", "ѐ": "е", "ё": "е", "ї": "і",
})
_ORG_FORM_LONG = re.compile(
    r"(?<![а-я])(?:(?:федеральн|государственн|муниципальн|бюджетн|автономн|казенн|"
    r"частн|негосударственн|некоммерческ|профессиональн|общеобразовательн|"
    r"образовательн|научн)[а-я]*\s+)+(?:учреждени[а-я]*|организаци[а-я]*)"
    r"(?:\s+(?:высшего|дополнительного|среднего профессионального|общего|"
    r"профессионального)\s+образования)?(?![а-я])")
_ORG_FORM_SHORT = re.compile(
    r"(?<![а-я])(?:ф?г[аб]?оу|[мч]?[аб]оу|гбпоу|гапоу|гпоу|(?:ф?г|м)[аб]у|[мг][аб]?удо|ано|"
    r"нко|фгбн?у|фгбун|ооо)(?:\s*(?:в|д|с)?п?о)?(?![а-я])")
_EXPAND = [
    (re.compile(r"(?<![а-я])всо(?![а-я])"), "всероссийская студенческая олимпиада"),
    (re.compile(r"(?<![а-я])им(?![а-я])"), "имени"),
    (re.compile(r"(?<![а-я])рф(?![а-я])"), "российской федерации"),
]


def fold_name(s: str) -> str:
    """
    Название для сравнения версий: norm_name + латиница/римские цифры → кириллица,
    без формы собственности организаций («ФГБОУ ВО», «федеральное государственное
    бюджетное образовательное учреждение высшего образования», «АНО» …),
    «ВСО» → «всероссийская студенческая олимпиада», «им.» → «имени».
    """
    s = (s or "").replace("ѐ", "е").replace("Ѐ", "Е").replace("і", "i").replace("І", "I")
    t = norm_name(s)
    t = t.translate(_FOLD)
    for rx, rep in _EXPAND:
        t = rx.sub(rep, t)
    t = _ORG_FORM_LONG.sub(" ", t)
    t = _ORG_FORM_SHORT.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


# ───────────────────────── организаторы ─────────────────────────

def _split_glued(s: str) -> List[str]:
    """
    В исходных документах две организации иногда склеены в одну строку:
    «…Вернадского» Федеральное государственное … «Северный…».
    Режем после закрывающей » (на нулевой глубине вложенности), если дальше
    начинается новая организация с заглавной буквы и своими кавычками.
    """
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(s):
        if ch == "«":
            depth += 1
        elif ch == "»":
            depth = max(0, depth - 1)
            if depth == 0:
                rest = s[i + 1:]
                m = re.match(r"\s+(?=[А-ЯЁA-Z])", rest)
                if m and "«" in rest[m.end():]:
                    parts.append(s[start: i + 1].strip())
                    start = i + 1 + m.end()
    parts.append(s[start:].strip())
    return [p for p in parts if p]


def org_list(raw: str) -> List[str]:
    """Делит поле «организатор» на отдельные организации с учётом кавычек «…»."""
    parts, buf = [], ""
    for line in (raw or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        buf = (buf + " " + line).strip() if buf else line
        if buf.count("«") <= buf.count("»") or buf.endswith("»"):
            parts.extend(_split_glued(buf))
            buf = ""
    if buf:
        parts.extend(_split_glued(buf))
    return parts


def short_org(one: str) -> str:
    """Название организации без формы собственности (то, что в кавычках)."""
    s = (one or "").strip()
    first = s.find("«")
    if first == -1:
        s = re.sub(
            r"^.*(?:высшего образования|дополнительного образования|"
            r"образовательное учреждение|некоммерческая организация|"
            r"общественная организация)\s+",
            "",
            s,
        )
        return s.strip(" ,;")
    last = s.rfind("»")
    if last <= first:
        last = len(s)
    return re.sub(
        r"\s+", " ", s[first + 1: last].replace("«", "").replace("»", "")
    ).strip()


_ACR_SKIP = {"и", "им", "имени", "в", "на", "по", "для", "при", "г", "им."}


def acronyms(one: str) -> List[str]:
    """
    Аббревиатуры из названия организации: «Высшая школа экономики» → ВШЭ.
    Берём весь текст до «имени» и самую внутреннюю часть в кавычках.
    Дефисные слова разбираем по частям (физико-технический → ФТ).
    """
    cands: List[str] = []
    inner = re.findall(r"«([^«»]+)»", one or "")
    if inner:
        cands.append(inner[-1])
    cands.append(short_org(one))
    out: List[str] = []
    for c in cands:
        c = re.split(r"\bим(?:ени|\.)\s", c, maxsplit=1)[0]
        c = re.sub(r"\([^)]*\)", " ", c)
        letters = []
        for w in re.split(r"[\s\-–—]+", c):
            w = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", w)
            if not w or w.lower() in _ACR_SKIP:
                continue
            letters.append(w[0].upper())
        if 3 <= len(letters) <= 8:
            a = "".join(letters)
            if a not in out:
                out.append(a)
    return out


def plural(n: int, a: str, b: str, c: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return a
    if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        return b
    return c
