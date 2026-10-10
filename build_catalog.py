# -*- coding: utf-8 -*-
"""
Сборка каталога для бота.

    python build_catalog.py                    # по утверждённым приказам → catalog.official.json
    python build_catalog.py --variant projects # по проектам приказов     → catalog.projects.json
    python build_catalog.py --report           # + подробный отчёт качества

Входные файлы — результат parse_minobr.py / parse_minpros.py:
    minobr_official.json, minpros_official.json   (или *_projects.json для проектов)
Необязательные, лежат рядом:
    aliases.json     — псевдонимы организаторов (ВШЭ, МФТИ, Физтех…)
    overrides.json   — ручные правки предметов/интересов/типа и подтверждённые
                       пары «одно и то же мероприятие в двух перечнях»

Схема каталога совместима с bot.py (schema_version 3): у мероприятий есть id
(стабильный, зависит от названия), old_ids (id из прошлой версии каталога — для
переноса избранного), section/section_title, official_number.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

from common import (STOP_STEMS, acronyms, fold_name, id_hash, legacy_key, make_id,
                    norm_name, org_list, short_org, stem)
from orders import format_source

HERE = Path(__file__).resolve().parent
ALIASES = HERE / "aliases.json"
OVERRIDES = HERE / "overrides.json"
REPORT = HERE / "catalog.report.txt"
CANDIDATES = HERE / "in_both_candidates.txt"
VARIANTS = {"official": "catalog.official.json", "projects": "catalog.projects.json"}

# Начало слова: перед совпадением не должно быть буквы/цифры.
W = r"(?<![a-zа-я0-9])"

# Правила применяются к norm_name(текста): строчные, ё→е, без знаков препинания.
SUBJECT_RULES: List[tuple] = [
    ("Математика", W + r"матем"),
    ("Физика", W + r"(?:физик|биофиз|геофиз|астрофиз)"),
    ("Информатика",
     W + r"(?:информатик|программир|кибер|компьютер|алгоритм|нейросет|data|"
     r"искусственн\w* интеллект|машинн\w* обучен|больш\w* данн|анализ данн|"
     r"цифров\w* технолог|информационн\w* (?:технолог|безопасност|систем)|"
     r"икт(?![а-я]))|" + W + r"it(?![a-zа-я0-9])"),
    ("Химия", W + r"(?:хим|биохим)"),
    ("Биология", W + r"(?:биолог|биохим|генетик|ботаник|зоолог|медицин|анатом|"
                 r"биотехнолог|биоинформ|физиолог|микробиолог|ветеринар)"),
    ("География", W + r"(?:географ|геолог|геоинформ|геодез|картограф|геофиз)"),
    ("Экология", W + r"эколог"),
    ("История", W + r"(?:истори|краевед|археолог|этнограф|музееведен)"),
    ("Обществознание", W + r"(?:обществозн|социолог|политолог|философ|культуролог|"
                       r"обществ\w* наук|гуманитарн\w* и социальн)"),
    ("Экономика", W + r"(?:эконом|финанс|бизнес|предпринимат|менеджмент|маркетинг|"
                  r"бухгалтер)"),
    ("Право", W + r"(?:право|права|праве|правом|правов\w*|правоведен\w*|юрид\w*|"
                  r"юриспруд\w*|конституц\w*)(?![а-я])"),
    ("Литература", W + r"(?:литератур|словесн|поэз|писател)"),
    ("Русский язык", W + r"(?:русск\w* язык|риторик|орфограф)"),
    ("Иностранный язык",
     W + r"(?:английск|немецк|французск|испанск|китайск|японск|итальянск|латинск|"
     r"иностран|арабск|корейск|турецк|восточн\w* язык|лингвострановед)"),
    ("Астрономия", W + r"(?:астроном|астрофиз)"),
    ("Искусство",
     W + r"(?:искусств(?!енн)|дизайн|живопис|рисун|график|композиц|скульптур|музык|"
     r"хореограф|вокал|театр|танц|художеств|архитектур|декоративн|кинематограф|"
     r"фотограф|оркестр|хоров|эстрад|изобразительн|виолончел|скрипк|фортепиан|"
     r"гитар|аккордеон|баян|домр|балалайк|флейт|саксофон|ударн\w* инструмент|"
     r"дирижир|концертмейстер|пени[ея]|мультимедийн\w* творчеств|"
     r"art(?![a-z]))"),
    ("Физическая культура",
     W + r"(?:физическ\w* культур|физкультур|гимнастик|"
     r"спорт(?!ивн\w* (?:программир|робот)))"),
    ("Технология", W + r"(?:технолог|инженер|робот|техническ|конструир|электроник|"
                   r"электротехник|кораблестро|судостро|авиац|космическ)"),
    ("Черчение", W + r"черчен"),
    ("ОБЖ", W + r"(?:обж|обзр|безопасност\w* жизнедеятельн|основы безопасност|"
                r"гражданск\w* оборон|пожарн|спасател)"),
]
SUBJECT_RX = [(s, re.compile(p)) for s, p in SUBJECT_RULES]

INTEREST_MAP: Dict[str, List[str]] = {
    "Математика": ["Математика и физика"],
    "Физика": ["Математика и физика"],
    "Информатика": ["IT и программирование"],
    "Химия": ["Химия и материалы"],
    "Биология": ["Медицина и биология"],
    "География": ["Экология и география"],
    "Экология": ["Экология и география"],
    "История": ["История и краеведение"],
    "Обществознание": ["Общество, право и политика"],
    "Экономика": ["Экономика и предпринимательство"],
    "Право": ["Общество, право и политика"],
    "Литература": ["Языки и филология"],
    "Русский язык": ["Языки и филология"],
    "Иностранный язык": ["Языки и филология"],
    "Астрономия": ["Математика и физика"],
    "Искусство": ["Искусство и дизайн"],
    "Физическая культура": ["Спорт"],
    "Технология": ["Инженерия и техника"],
    "Черчение": ["Инженерия и техника"],
    "ОБЖ": ["Безопасность и ОБЖ"],
}

INTEREST_HINTS = [
    (W + r"(?:журналист|медиа|телевид|блогер)", "Медиа и журналистика"),
    (W + r"(?:робот|беспилот|дрон|бпла)", "Робототехника и беспилотники"),
    (W + r"(?:искусственн\w* интеллект|машинн\w* обучен|нейросет|данн)",
     "Искусственный интеллект и данные"),
    (W + r"(?:международн\w* отношен|дипломат|востоковед|страноведен)",
     "Международные отношения и дипломатия"),
    (W + r"(?:педагог|психолог)", "Педагогика и психология"),
    (W + r"(?:проект|исследоват|стартап)",
     "Проектная и исследовательская деятельность"),
    (W + r"(?:предпринимат|бизнес|финанс|эконом)", "Экономика и предпринимательство"),
    (W + r"(?:эколог|географ)", "Экология и география"),
    (W + r"(?:истори|краевед)", "История и краеведение"),
    (W + r"(?:лингв|филолог|языкознан|иностран|язык[а-я]*(?![а-я])(?! программир))",
     "Языки и филология"),
    (W + r"(?:искусств(?!енн)|дизайн|музык|худож|театр|танц|хореограф|вокал)",
     "Искусство и дизайн"),
]
INTEREST_RX = [(re.compile(p), i) for p, i in INTEREST_HINTS]

TYPE_RULES = [
    ("олимпиад", "Олимпиада"), ("чемпионат", "Чемпионат"), ("конкурс", "Конкурс"),
    ("фестивал", "Фестиваль"), ("конференц", "Конференция"), ("хакатон", "Хакатон"),
    ("турнир", "Турнир"), ("соревнован", "Соревнование"), ("форум", "Форум"),
    ("чтени", "Чтения"), ("школа", "Школа"),
    ("первенств", "Первенство"), ("спартакиад", "Спартакиада"), ("выставк", "Выставка"),
    ("ярмарк", "Ярмарка"), ("смен", "Смена"),
]
TYPE_RX = [(re.compile(W + kw), t) for kw, t in TYPE_RULES]


# ───────────────────────── классификация ─────────────────────────

def classify_type(name: str) -> str:
    """Тип по самому раннему ключевому слову в названии."""
    low = norm_name(name)
    best: Optional[tuple] = None
    for rx, t in TYPE_RX:
        m = rx.search(low)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), t)
    return best[1] if best else "Другое"


def guess_subjects(texts: List[str], directions: Optional[List[str]] = None) -> List[str]:
    subs = set()
    for t in texts or []:
        n = norm_name(t)
        for sub, rx in SUBJECT_RX:
            if rx.search(n):
                subs.add(sub)
    for d in directions or []:
        dl = norm_name(d)
        if "физическ" in dl and "культур" in dl or "спорт" in dl:
            subs.add("Физическая культура")
        elif "искусств" in dl and "культур" in dl:
            subs.add("Искусство")
    return sorted(subs)


def guess_interests(texts: List[str], subjects: List[str]) -> List[str]:
    ints = set()
    for sub in subjects:
        ints.update(INTEREST_MAP.get(sub, []))
    for t in texts or []:
        n = norm_name(t)
        for rx, i in INTEREST_RX:
            if rx.search(n):
                ints.add(i)
    return sorted(ints)


def classify(name: str, profiles: List[str], extra: List[str], directions: List[str]):
    """Предметы/интересы по профилям; если они ничего не дали — по названию."""
    texts = list(profiles) + list(extra)
    subs = guess_subjects(texts, directions)
    ints = guess_interests(texts, subs)
    if not subs:
        subs = guess_subjects([name], None)
    if not ints:
        ints = guess_interests([name], subs)
    return subs, ints


# ───────────────────────── организаторы ─────────────────────────

def load_alias_rules() -> List[dict]:
    if not ALIASES.exists():
        return []
    return json.loads(ALIASES.read_text(encoding="utf-8")).get("rules", [])


def build_aliases(raw: str, rules: List[dict]) -> List[str]:
    out: List[str] = []

    def add(x):
        if x and x not in out:
            out.append(x)

    for line in org_list(raw):
        n_line = norm_name(line)
        n_inner = norm_name(short_org(line))
        for r in rules:
            if n_inner in r.get("equals", []) or any(c in n_line for c in r.get("contains", [])):
                for a in r["aliases"]:
                    add(a)
        for a in acronyms(line):
            add(a)
    return out


# ───────────────────────── конвертация ─────────────────────────

# Значения колонки «предметы или УГСН» (Минобрнауки), которые являются школьными
# предметами. Группы УГСН для определения предмета не используются: у олимпиады с
# 15 группами УГСН набралось бы пол-списка предметов.
SCHOOL_TOKENS = {"математика", "физика", "химия", "биология", "география", "история",
                 "обществознание", "право", "литература", "русский язык",
                 "иностранный язык", "информатика", "астрономия", "экономика", "искусство"}
MINOBR_SECTION = "Олимпиады школьников (перечень Минобрнауки)"


def load_parsed(path: Path):
    """Результат парсера: {"meta", "events", …} или (старый формат) просто список."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {"meta": {}, "events": data, "warnings": []}
    data.setdefault("meta", {})
    data.setdefault("warnings", [])
    return data


def _num_key(num: str):
    """«6.95» → (6, 95); нечисловые части — после числовых."""
    return tuple((0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.split(r"[.\s]+", num or ""))


def assign_ids(prefix: str, raw: List[dict], seen: Dict[str, int]) -> List[str]:
    """
    id для записей парсера в порядке raw. Дубли названий внутри источника не зависят
    от порядка строк: базовый id получает запись с наименьшим (номер, организатор),
    остальные — суффикс от (название, организатор + номер) — см. common.make_id.
    """
    order = sorted(range(len(raw)), key=lambda i: (
        norm_name(raw[i]["name"]), _num_key(raw[i].get("official_number", "")),
        norm_name(raw[i].get("organizer_raw", ""))))
    ids = [""] * len(raw)
    for i in order:
        r = raw[i]
        ids[i] = make_id(prefix, r["name"], seen,
                         secondary=f'{r.get("organizer_raw", "")} {r.get("official_number", "")}')
    return ids


def convert_minobr(raw, rules, seen):
    out = []
    ids = assign_ids("mo", raw, seen)
    for r, eid in zip(raw, ids):
        directions = ["Наука и образование"]
        prof = r.get("profiles", [])
        fields = r.get("study_fields", [])
        school = [f for f in fields if f.lower() in SCHOOL_TOKENS]
        subs, ints = classify(r["name"], prof, school, directions)
        out.append({
            "id": eid,
            "legacy_key": legacy_key(r["name"]),
            "source_code": "minobr",
            "official_number": r["official_number"],
            "section_title": MINOBR_SECTION,
            "name": r["name"],
            "type": classify_type(r["name"]),
            "organizer_raw": r["organizer_raw"],
            "entries": r.get("entries", []),
            "directions": directions,
            "profiles": prof,
            "study_fields": fields,
            "levels": r.get("levels", []),
            "subjects": subs,
            "interests": ints,
            "organizer_aliases": build_aliases(r["organizer_raw"], rules),
        })
    return out


def convert_minpros(raw, rules, seen):
    out = []
    ids = assign_ids("mp", raw, seen)
    for r, eid in zip(raw, ids):
        directions = r.get("directions") or ([r["direction"]] if r.get("direction") else [])
        prof = r.get("profiles", [])
        entries = r.get("entries") or [
            {"direction": directions[0] if directions else "", "profile": p, "level": ""}
            for p in prof
        ]
        subs, ints = classify(r["name"], prof, [], directions)
        ev = {
            "id": eid,
            "legacy_key": legacy_key(r["name"]),
            "source_code": "minpros",
            "official_number": r["official_number"],
            "name": r["name"],
            "type": classify_type(r["name"]),
            "organizer_raw": r["organizer_raw"],
            "entries": entries,
            "directions": directions,
            "profiles": prof,
            "levels": r.get("levels", []),
            "subjects": subs,
            "interests": ints,
            "organizer_aliases": build_aliases(r["organizer_raw"], rules),
        }
        if r.get("section"):
            ev["section"] = r["section"]
            ev["section_title"] = r.get("section_title", "")
        out.append(ev)
    return out


def source_info(meta: dict, ministry: str, variant: str, projects_text: str) -> dict:
    if variant == "official" and meta.get("number"):
        return {"ministry": ministry, "source": format_source(meta), "order": meta}
    return {"ministry": ministry, "source": projects_text}


# ───────────────────────── связи и правки ─────────────────────────

def mark_in_both(events, manual_pairs, unused: Optional[List[str]] = None) -> int:
    """Точное совпадение нормализованного названия + пары, подтверждённые вручную."""
    mo = {}
    for e in events:
        if e["source_code"] == "minobr":
            mo.setdefault(norm_name(e["name"]), e)
    mp_by_name: Dict[str, List[dict]] = {}
    for e in events:
        if e["source_code"] == "minpros":
            mp_by_name.setdefault(norm_name(e["name"]), []).append(e)

    pairs = set()
    for k, a in mo.items():
        for b in mp_by_name.get(k, []):
            pairs.add((a["id"], b["id"]))
    for n_obr, n_pros in manual_pairs:
        a = mo.get(norm_name(n_obr))
        bs = mp_by_name.get(norm_name(n_pros), [])
        if not a or not bs:
            if unused is not None:
                miss = [x for x, ok in (("Минобр", a), ("Минпрос", bs)) if not ok]
                unused.append(f"same_event: [«{n_obr}», «{n_pros}»] — не найдено ({', '.join(miss)})")
            continue
        for b in bs:
            pairs.add((a["id"], b["id"]))
    by_id = {e["id"]: e for e in events}
    for a, b in pairs:
        by_id[a]["in_both"] = 1
        by_id[b]["in_both"] = 1
    return len(pairs)


def fuzzy_candidates(events, threshold=0.85, limit=80):
    mo = [e for e in events if e["source_code"] == "minobr" and not e.get("in_both")]
    mp = [e for e in events if e["source_code"] == "minpros" and not e.get("in_both")]
    mpn = [(norm_name(e["name"]), e) for e in mp]
    res = []
    for a in mo:
        k = norm_name(a["name"])
        best, br = None, 0.0
        for n, b in mpn:
            if abs(len(n) - len(k)) > 25:
                continue
            r = difflib.SequenceMatcher(None, k, n).ratio()
            if r > br:
                best, br = b, r
        if best is not None and br >= threshold:
            res.append((br, a["name"], best["name"]))
    return sorted(res, reverse=True)[:limit]


def load_overrides(path: Optional[Path] = None) -> dict:
    """overrides.json; ключи by_name и названия в same_event приводятся к norm_name."""
    path = path or OVERRIDES
    if not path.exists():
        return {"by_name": {}, "same_event": []}
    ov = json.loads(path.read_text(encoding="utf-8"))
    by_name = {}
    for k, v in (ov.get("by_name") or {}).items():
        if k.startswith("_"):
            continue
        by_name[norm_name(k)] = v
    ov["by_name"] = by_name
    ov["same_event"] = [[norm_name(a), norm_name(b)] for a, b in ov.get("same_event") or []]
    return ov


def apply_overrides(events, ov, unused: Optional[List[str]] = None) -> int:
    """Ручные правки по названию. Ключи, не совпавшие ни с одним мероприятием, — в unused."""
    by_name = {norm_name(k): v for k, v in (ov.get("by_name") or {}).items()}
    n = 0
    hit = set()
    for e in events:
        key = norm_name(e["name"])
        o = by_name.get(key)
        if not o:
            continue
        n += 1
        hit.add(key)
        for k in ("subjects", "interests", "type", "levels"):
            if k in o:
                e[k] = o[k]
        for a in o.get("add_aliases", []):
            if a not in e["organizer_aliases"]:
                e["organizer_aliases"].append(a)
    if unused is not None:
        unused.extend(f"by_name: «{k}» — нет мероприятия с таким названием"
                      for k in by_name if k not in hit)
    return n


# ───────────────────────── проверка и отчёт ─────────────────────────

def validate(events) -> List[str]:
    """Дубли id и пустые обязательные поля — ошибка; мероприятия без entries — предупреждение."""
    ids = Counter(e["id"] for e in events)
    dup = [k for k, v in ids.items() if v > 1]
    if dup:
        sys.exit(f"Дубли id: {dup[:5]}")
    warn = []
    for e in events:
        for k in ("id", "name", "source_code"):
            if not e.get(k):
                sys.exit(f"Пустое поле {k}: {e}")
        if not e.get("entries"):
            warn.append(f"{e.get('official_number') or '?'}: нет строк профиль/уровень — {e['name'][:60]!r}")
    return warn


# Слова-«предметы» для сопоставления версий: если названия отличаются только ими
# («… по физике» / «… по химии», «Ломоносов по математике» / «… по механике»),
# это соседние мероприятия, а не переименование.
_MATCH_SUBJECT_EXTRA = re.compile(
    W + r"(?:механик|лингвист|филолог|журналист|психолог|педагог|естествен|"
    r"гуманитар|граф|рисун|живопис|композиц|скульпт|дизайн|вокал|хор|музык|"
    r"робот|программир|электр|энергет|строит|агро|медиц|фармац|стомат|"
    r"ветерин|почв|лес|сельскохоз|архитект|литер|словес|язык|грамот)")
_TOKEN_RE = re.compile(r"[0-9a-zа-яі]+")


def _match_tokens(folded: str) -> set:
    return {stem(t) for t in _TOKEN_RE.findall(folded)} - STOP_STEMS


def _subject_of(token: str) -> Optional[str]:
    for sub, rx in SUBJECT_RX:
        if rx.search(token):
            return sub
    return token if _MATCH_SUBJECT_EXTRA.search(token) else None


def is_sibling(a_tokens: set, b_tokens: set) -> bool:
    """
    Названия отличаются предметом → разные мероприятия одного цикла:
      • все отличающиеся слова (с обеих сторон) — предметы;
      • или с обеих сторон среди отличий есть предметы, и это разные предметы.
    """
    da, db = a_tokens - b_tokens, b_tokens - a_tokens
    sa = {_subject_of(t) for t in da} - {None}
    sb = {_subject_of(t) for t in db} - {None}
    if not (sa or sb):
        return False
    only_subj_a = all(_subject_of(t) for t in da)
    only_subj_b = all(_subject_of(t) for t in db)
    if only_subj_a and only_subj_b:
        return True
    return bool(sa and sb and sa != sb)


def _trigrams(folded: str) -> set:
    t = folded.replace(" ", "")
    return {t[i:i + 3] for i in range(max(1, len(t) - 2))}


def align_acronyms(fa: str, fb: str):
    """
    Аббревиатура с одной стороны и полное название с другой: «рк кипу» ↔ «рк крымский
    инженерно педагогический университет». Цепочка слов, первые буквы которых дают
    аббревиатуру (2–6 букв), заменяется самой аббревиатурой.
    """
    def fold_side(x: str, y: str) -> str:
        xs, ys = x.split(), y.split()
        for t in set(xs) - set(ys):
            if not (2 <= len(t) <= 6 and t.isalpha()):
                continue
            n = len(t)
            for i in range(len(ys) - n + 1):
                run = ys[i:i + n]
                if "".join(w[0] for w in run) == t and t not in run:
                    ys = ys[:i] + [t] + ys[i + n:]
                    break
        return " ".join(ys)
    return fold_side(fb, fa), fold_side(fa, fb)


def name_score(a: str, b: str) -> float:
    """
    Похожесть названий 0…1 по fold_name (+ align_acronyms): среднее
    SequenceMatcher и Жаккара (максимум из Жаккара по буквенным триграммам без
    пробелов — «видео экскурсий» = «видеоэкскурсий» — и по основам слов —
    «Фоксфорда» = «Фоксфорд»).
    """
    fa, fb = align_acronyms(fold_name(a), fold_name(b))
    if fa == fb:
        return 1.0
    ga, gb = _trigrams(fa), _trigrams(fb)
    jac = len(ga & gb) / len(ga | gb) if ga | gb else 0.0
    ta, tb = _match_tokens(fa), _match_tokens(fb)
    if ta | tb:
        jac = max(jac, len(ta & tb) / len(ta | tb))
    return (difflib.SequenceMatcher(None, fa, fb).ratio() + jac) / 2


def org_score(a: str, b: str) -> float:
    """Похожесть организаторов (fold_name + align_acronyms, SequenceMatcher)."""
    fa, fb = align_acronyms(fold_name(a), fold_name(b))
    if not fa or not fb:
        return 0.0
    return difflib.SequenceMatcher(None, fa, fb).ratio()


def compare_names(a: str, b: str, fa: Optional[str] = None, fb: Optional[str] = None,
                  org_a: str = "", org_b: str = "", threshold: float = 0.9):
    """
    Одно ли это мероприятие (для переноса избранного)? None — нет, иначе
    (оценка названий, похожесть организаторов, вид: «fuzzy» | «contain»).
      • fuzzy   — name_score ≥ threshold;
      • contain — все основы короткого названия (≥ 4 значимых) входят в длинное,
                  длинное добавляет не больше слов, чем есть в коротком, и
                  организаторы совпадают (org_score ≥ 0.85): «Путь к Олимпу:
                  Проекты будущего» → «Всероссийский … конкурс «Путь к Олимпу: …»».
    Пары, отличающиеся предметом (is_sibling), отвергаются всегда.
    """
    fa = fold_name(a) if fa is None else fa
    fb = fold_name(b) if fb is None else fb
    if abs(len(fa) - len(fb)) > 60:
        return None
    xa, xb = align_acronyms(fa, fb)
    ta, tb = _match_tokens(xa), _match_tokens(xb)
    if xa != xb and is_sibling(ta, tb):
        return None
    sc = name_score(a, b)
    if sc >= threshold:
        return sc, org_score(org_a, org_b), "fuzzy"
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) >= 4 and short <= long_ and len(long_ - short) <= len(short):
        osc = org_score(org_a, org_b)
        if osc >= 0.85:
            return sc, osc, "contain"
    return None


def attach_old_ids(events, prev_path: Optional[Path], threshold: float = 0.9):
    """
    Переносит избранное между версиями каталога: старые id, которых нет в новом
    каталоге, записываются в old_ids нового мероприятия (bot.py по ним переносит
    избранное пользователей).

    1. Точное совпадение: одинаковая хэш-часть id (то же norm_name) в любом
       источнике — мероприятие переехало между перечнями («mo-…» → «mp-…») или
       было дублем названия («mp-…-2»). Несколько старых id могут указывать на
       одно новое мероприятие.
    2. Нечёткое: среди оставшихся старых и новых (которых не было в прошлом
       каталоге) — name_score ≥ threshold по fold_name (латиница/римские цифры,
       формы собственности, «ВСО» …), без пар, отличающихся предметом
       (is_sibling). Пары выбираются глобально: все кандидаты по убыванию
       (оценка, похожесть организатора, тот же источник), жадно один к одному.
    old_ids прошлого каталога переносятся дальше (цепочки переименований).

    Возвращает (сопоставлено [(старое название, новое название, оценка, старый id,
    новый id, вид)], пропавшие названия). Вид: «id» — тот же хэш, «fuzzy» — нечёткое.
    """
    if not prev_path or not prev_path.exists():
        return [], []
    prev = json.loads(prev_path.read_text(encoding="utf-8")).get("events", [])
    by_id = {e["id"]: e for e in events}
    prev_ids = {e["id"] for e in prev}

    def add_old(e, old_id):
        if old_id and old_id != e["id"] and old_id not in by_id:
            lst = e.setdefault("old_ids", [])
            if old_id not in lst:
                lst.append(old_id)

    # старые id, уже сохранённые в прошлом каталоге, для живущих дальше мероприятий
    for pe in prev:
        if pe["id"] in by_id:
            for o in pe.get("old_ids") or []:
                add_old(by_id[pe["id"]], o)

    gone = [pe for pe in prev if pe["id"] not in by_id]
    by_hash: Dict[str, List[dict]] = {}
    for e in events:
        by_hash.setdefault(id_hash(e["id"]), []).append(e)

    matched, rest = [], []
    exact_targets = set()
    for pe in gone:
        cands = by_hash.get(id_hash(pe["id"]), [])
        if not cands:
            rest.append(pe)
            continue
        # тот же источник и id без суффикса — предпочтительнее
        tgt = min(cands, key=lambda e: (e["source_code"] != pe["source_code"],
                                        e["id"].count("-"), e["id"]))
        add_old(tgt, pe["id"])
        for o in pe.get("old_ids") or []:
            add_old(tgt, o)
        exact_targets.add(tgt["id"])
        matched.append((pe["name"], tgt["name"], 1.0, pe["id"], tgt["id"], "id"))

    fresh = [e for e in events if e["id"] not in prev_ids and e["id"] not in exact_targets]
    pairs = []
    for pe in rest:
        fp = fold_name(pe["name"])
        for e in fresh:
            kind = compare_names(pe["name"], e["name"], fp, None,
                                 pe.get("organizer_raw", ""), e.get("organizer_raw", ""),
                                 threshold)
            if kind is None:
                continue
            sc, osc, how = kind
            pairs.append((sc, osc, pe["source_code"] == e["source_code"], pe["id"], e["id"],
                          pe, e, how))
    pairs.sort(key=lambda x: (-x[0], -x[1], not x[2], x[3], x[4]))
    used_old, used_new = set(), set()
    for sc, osc, _same, oid, nid, pe, e, how in pairs:
        if oid in used_old or nid in used_new:
            continue
        used_old.add(oid)
        used_new.add(nid)
        add_old(e, oid)
        for o in pe.get("old_ids") or []:
            add_old(e, o)
        matched.append((pe["name"], e["name"], sc, oid, nid, how))
    dropped = [pe["name"] for pe in rest if pe["id"] not in used_old]
    return matched, dropped


def make_report(events, n_pairs, n_over, cands, extra=None, limit: int = 100) -> str:
    n = len(events)
    L = [f"Событий: {n}"]
    for src, title in (("minobr", "Минобрнауки"), ("minpros", "Минпросвещения")):
        L.append(f"  {title}: {sum(1 for e in events if e['source_code'] == src)}")
    secs = Counter(e.get("section_title") for e in events if e["source_code"] == "minpros")
    if secs:
        L.append("  по разделам Минпросвещения: " + "; ".join(
            f"{t or 'без раздела'} — {c}" for t, c in sorted(secs.items(), key=lambda x: str(x[0]))))
    L.append(f"Пар «в обоих перечнях»: {n_pairs}")
    L.append(f"Применено ручных правок: {n_over}")
    L.append("")
    for key, label in (("subjects", "без предмета"), ("interests", "без интересов"),
                       ("levels", "без уровня"), ("profiles", "без профиля"),
                       ("organizer_aliases", "без псевдонимов организатора")):
        k = sum(1 for e in events if not e.get(key))
        L.append(f"{label}: {k} ({k * 100 // n if n else 0}%)")
    L.append("")
    L.append("Типы: " + ", ".join(f"{t} {c}" for t, c in Counter(e["type"] for e in events).most_common()))
    L.append("Уровни: " + ", ".join(f"{t} {c}" for t, c in Counter(l for e in events for l in e["levels"]).most_common()))
    L.append("Предметы: " + ", ".join(f"{t} {c}" for t, c in
                                      Counter(s for e in events for s in e["subjects"]).most_common()))
    L.append("Интересы: " + ", ".join(f"{t} {c}" for t, c in
                                       Counter(s for e in events for s in e["interests"]).most_common()))
    L.append("")
    L.append(f"Нечётких пар для ручной проверки: {len(cands)} (см. {CANDIDATES.name})")
    for title, items in (extra or {}).items():
        L.append("")
        L.append(f"{title}: {len(items)}")
        L.extend("  • " + x for x in items[:limit])
        if len(items) > limit:
            L.append(f"  … и ещё {len(items) - limit}")
    return "\n".join(L)


# ───────────────────────── main ─────────────────────────

def pick_variant(folder: Path) -> str:
    """
    Вариант по умолчанию: official, если есть оба minobr_official.json и
    minpros_official.json; если есть только один — ошибка (иначе молча собрался бы
    каталог по проектам); если нет ни одного — projects с явным сообщением.
    """
    have = [f for f in ("minobr_official.json", "minpros_official.json") if (folder / f).exists()]
    if len(have) == 2:
        return "official"
    if len(have) == 1:
        missing = ({"minobr_official.json", "minpros_official.json"} - set(have)).pop()
        sys.exit(f"Есть {have[0]}, но нет {missing}. Запустите второй парсер "
                 f"(parse_minobr.py / parse_minpros.py) или укажите --variant явно.")
    print("ℹ Нет minobr_official.json и minpros_official.json — собираю каталог по ПРОЕКТАМ "
          "приказов (--variant projects).")
    return "projects"


def main():
    ap = argparse.ArgumentParser(description="Сборка каталога для бота")
    ap.add_argument("--variant", choices=sorted(VARIANTS), default=None,
                    help="official — по утверждённым приказам (по умолчанию, если есть файлы), projects — по проектам")
    ap.add_argument("--prev", help="прошлый каталог для переноса избранного (по умолчанию — каталог другого варианта)")
    ap.add_argument("--report", action="store_true", help="напечатать подробный отчёт")
    a = ap.parse_args()

    variant = a.variant or pick_variant(HERE)
    f_mo, f_mp = HERE / f"minobr_{variant}.json", HERE / f"minpros_{variant}.json"
    for f in (f_mo, f_mp):
        if not f.exists():
            sys.exit(f"Нет файла {f.name}. Сначала запустите parse_minobr.py и parse_minpros.py.")
    out_path = HERE / VARIANTS[variant]
    prev_path = Path(a.prev) if a.prev else HERE / VARIANTS["projects" if variant == "official" else "official"]

    mo, mp = load_parsed(f_mo), load_parsed(f_mp)
    rules = load_alias_rules()
    ov = load_overrides()

    seen: Dict[str, int] = {}
    events = convert_minobr(mo["events"], rules, seen) + convert_minpros(mp["events"], rules, seen)
    unused: List[str] = []
    n_over = apply_overrides(events, ov, unused)
    n_pairs = mark_in_both(events, ov.get("same_event", []), unused)
    for u in unused:
        print("⚠ overrides.json:", u)
    data_warn = validate(events)
    matched, dropped = attach_old_ids(events, prev_path if prev_path.resolve() != out_path.resolve() else None)

    cands = fuzzy_candidates(events)
    CANDIDATES.write_text(
        "Похожие названия в разных перечнях. Если это одно и то же мероприятие —\n"
        "добавьте пару в overrides.json → same_event: [[название Минобр, название Минпрос]].\n\n"
        + "\n".join(f"{r:.2f}\n  Минобр:  {x}\n  Минпрос: {y}\n" for r, x, y in cands),
        encoding="utf-8",
    )

    catalog = {
        "schema_version": 3,
        "academic_year": "2026/27",
        "variant": variant,
        "sources": {
            "minobr": source_info(
                mo["meta"], "Минобрнауки России", variant,
                "Проект приказа Минобрнауки России «Об утверждении перечня олимпиад школьников "
                "и их уровней на 2026/27 учебный год»"),
            "minpros": source_info(
                mp["meta"], "Минпросвещения России", variant,
                "Проект приказа Минпросвещения России «Об утверждении перечня олимпиад и иных "
                "интеллектуальных и (или) творческих конкурсов, мероприятий на 2026/27 учебный год» (Раздел 6)"),
        },
        "events": events,
    }
    out_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")

    extra = {}
    warnings = ([f"Минобрнауки: {w}" for w in mo["warnings"]] + [f"Минпросвещения: {w}" for w in mp["warnings"]]
                + [f"каталог: {w}" for w in data_warn])
    if warnings:
        extra["Замечания к данным приказов"] = warnings
    if unused:
        extra["overrides.json: правки, которые ни к чему не применились"] = unused
    same = [m for m in matched if m[5] == "id"]
    fuzzy = sorted((m for m in matched if m[5] != "id"), key=lambda t: t[2])
    if same:
        extra[f"Перенесены по тому же названию (сменился перечень или был дубль; сверка с {prev_path.name})"] = [
            f"{o} → {n}  {x[:80]}" for x, y, r, o, n, k in same]
    if fuzzy:
        extra[f"Переименованные мероприятия (старый id сохранён в old_ids, сверка с {prev_path.name})"] = [
            f"{r:.2f}{' (вхождение)' if k == 'contain' else ''}  {x[:70]}  →  {y[:70]}"
            for x, y, r, o, n, k in fuzzy]
    if dropped:
        extra[f"Были в {prev_path.name}, нет в новом каталоге (их избранное не перенесётся)"] = dropped
    rep = make_report(events, n_pairs, n_over, cands, extra)
    REPORT.write_text(rep, encoding="utf-8")
    print(rep if a.report else rep.split("\n\n")[0])
    if matched or dropped:
        print(f"Сверка с {prev_path.name}: тот же id-хэш {len(same)}, переименовано {len(fuzzy)}, "
              f"пропало {len(dropped)}")
        low = [m for m in fuzzy if m[2] < 0.95]
        if low:
            print("Нечёткие сопоставления с оценкой < 0.95 — проверьте:")
            for x, y, r, o, n, k in low:
                print(f"  {r:.3f} {k:7} {x[:60]}  →  {y[:60]}")
    print(f"Сохранено → {out_path.name}")


if __name__ == "__main__":
    main()
