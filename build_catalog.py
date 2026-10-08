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

from common import acronyms, legacy_key, make_id, norm_name, org_list, short_org
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


def convert_minobr(raw, rules, seen):
    out = []
    for r in raw:
        directions = ["Наука и образование"]
        prof = r.get("profiles", [])
        fields = r.get("study_fields", [])
        school = [f for f in fields if f.lower() in SCHOOL_TOKENS]
        subs, ints = classify(r["name"], prof, school, directions)
        out.append({
            "id": make_id("mo", r["name"], seen),
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
    for r in raw:
        directions = r.get("directions") or ([r["direction"]] if r.get("direction") else [])
        prof = r.get("profiles", [])
        entries = r.get("entries") or [
            {"direction": directions[0] if directions else "", "profile": p, "level": ""}
            for p in prof
        ]
        subs, ints = classify(r["name"], prof, [], directions)
        ev = {
            "id": make_id("mp", r["name"], seen),
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

def mark_in_both(events, manual_pairs) -> int:
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
        for b in mp_by_name.get(norm_name(n_pros), []):
            if a:
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


def apply_overrides(events, ov) -> int:
    by_name = ov.get("by_name", {})
    n = 0
    for e in events:
        o = by_name.get(norm_name(e["name"]))
        if not o:
            continue
        n += 1
        for k in ("subjects", "interests", "type", "levels"):
            if k in o:
                e[k] = o[k]
        for a in o.get("add_aliases", []):
            if a not in e["organizer_aliases"]:
                e["organizer_aliases"].append(a)
    return n


# ───────────────────────── проверка и отчёт ─────────────────────────

def validate(events):
    ids = Counter(e["id"] for e in events)
    dup = [k for k, v in ids.items() if v > 1]
    if dup:
        sys.exit(f"Дубли id: {dup[:5]}")
    for e in events:
        for k in ("id", "name", "source_code", "entries"):
            if not e.get(k) and k != "entries":
                sys.exit(f"Пустое поле {k}: {e}")


def attach_old_ids(events, prev_path: Optional[Path], threshold: float = 0.9):
    """
    Переносит избранное между версиями каталога: если мероприятие переименовано
    (id изменился), старый id записывается в old_ids нового. Возвращает
    (сопоставлено [(старое название, новое название, коэффициент)], пропавшие названия).
    """
    if not prev_path or not prev_path.exists():
        return [], []
    prev = json.loads(prev_path.read_text(encoding="utf-8")).get("events", [])
    new_ids = {e["id"] for e in events}
    prev_ids = {e["id"] for e in prev}
    fresh = [e for e in events if e["id"] not in prev_ids]
    fresh_by_src: Dict[str, List[tuple]] = {}
    for e in fresh:
        fresh_by_src.setdefault(e["source_code"], []).append((norm_name(e["name"]), e))
    matched, dropped = [], []
    taken = set()
    for pe in prev:
        if pe["id"] in new_ids:
            continue
        k = norm_name(pe["name"])
        best, br = None, 0.0
        for n, e in fresh_by_src.get(pe["source_code"], []):
            if e["id"] in taken or abs(len(n) - len(k)) > 30:
                continue
            sm = difflib.SequenceMatcher(None, k, n)
            if sm.real_quick_ratio() < br or sm.quick_ratio() < br:
                continue
            r = sm.ratio()
            if r > br:
                best, br = e, r
        if best is not None and br >= threshold:
            best.setdefault("old_ids", []).append(pe["id"])
            taken.add(best["id"])
            matched.append((pe["name"], best["name"], br))
        else:
            dropped.append(pe["name"])
    return matched, dropped


def make_report(events, n_pairs, n_over, cands, extra=None) -> str:
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
        L.append(f"{label}: {k} ({k * 100 // n}%)")
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
        L.extend("  • " + x for x in items[:40])
        if len(items) > 40:
            L.append(f"  … и ещё {len(items) - 40}")
    return "\n".join(L)


# ───────────────────────── main ─────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Сборка каталога для бота")
    ap.add_argument("--variant", choices=sorted(VARIANTS), default=None,
                    help="official — по утверждённым приказам (по умолчанию, если есть файлы), projects — по проектам")
    ap.add_argument("--prev", help="прошлый каталог для переноса избранного (по умолчанию — каталог другого варианта)")
    ap.add_argument("--report", action="store_true", help="напечатать подробный отчёт")
    a = ap.parse_args()

    variant = a.variant or ("official" if (HERE / "minobr_official.json").exists()
                            and (HERE / "minpros_official.json").exists() else "projects")
    f_mo, f_mp = HERE / f"minobr_{variant}.json", HERE / f"minpros_{variant}.json"
    for f in (f_mo, f_mp):
        if not f.exists():
            sys.exit(f"Нет файла {f.name}. Сначала запустите parse_minobr.py и parse_minpros.py.")
    out_path = HERE / VARIANTS[variant]
    prev_path = Path(a.prev) if a.prev else HERE / VARIANTS["projects" if variant == "official" else "official"]

    mo, mp = load_parsed(f_mo), load_parsed(f_mp)
    rules = load_alias_rules()
    ov = json.loads(OVERRIDES.read_text(encoding="utf-8")) if OVERRIDES.exists() else {}

    seen: Dict[str, int] = {}
    events = convert_minobr(mo["events"], rules, seen) + convert_minpros(mp["events"], rules, seen)
    n_over = apply_overrides(events, ov)
    n_pairs = mark_in_both(events, ov.get("same_event", []))
    validate(events)
    matched, dropped = attach_old_ids(events, prev_path if prev_path != out_path else None)

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
    warnings = [f"Минобрнауки: {w}" for w in mo["warnings"]] + [f"Минпросвещения: {w}" for w in mp["warnings"]]
    if warnings:
        extra["Замечания к данным приказов"] = warnings
    if matched:
        extra[f"Переименованные мероприятия (старый id сохранён в old_ids, сверка с {prev_path.name})"] = [
            f"{r:.2f}  {x[:70]}  →  {y[:70]}" for x, y, r in sorted(matched, key=lambda t: t[2])]
    if dropped:
        extra[f"Были в {prev_path.name}, нет в новом каталоге (их избранное не перенесётся)"] = dropped
    rep = make_report(events, n_pairs, n_over, cands, extra)
    REPORT.write_text(rep, encoding="utf-8")
    print(rep if a.report else rep.split("\n\n")[0])
    if matched or dropped:
        print(f"Сверка с {prev_path.name}: переименовано {len(matched)}, пропало {len(dropped)}")
    print(f"Сохранено → {out_path.name}")


if __name__ == "__main__":
    main()
