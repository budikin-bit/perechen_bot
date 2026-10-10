# -*- coding: utf-8 -*-
"""
Парсер приказа Минобрнауки об утверждении перечня олимпиад школьников.

Вход:  приказ в формате .rtf или .docx (официальный текст с pravo.gov.ru /
       из справочной системы). Выход: minobr_official.json
       {"meta": {реквизиты приказа}, "events": [...]}.

Запуск:
    python parse_minobr.py                       # ищет приказ Минобрнауки в текущей папке
    python parse_minobr.py приказ.rtf [-o minobr_official.json]

Как устроена таблица:
  • новая олимпиада начинается там, где появилось название (в RTF название и номер
    стоят только в первой строке олимпиады, в docx — повторяются);
  • у одной олимпиады несколько строк «профиль — УГСН — уровень»;
  • у разных профилей одной олимпиады могут быть разные организаторы — в событие
    попадает объединение организаторов, а различия сохраняются в entries[].organizer;
  • колонка «предметы или УГСН» — перечисление через запятую; у пяти групп УГСН
    запятые входят в само название (см. MULTIPART) — они склеиваются обратно.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List

from common import norm_name, org_list
from orders import (find_order, image_warnings, is_header_row, is_section_row, norm_level,
                    pad_row, read_order, uniq)

DEFAULT_OUT = "minobr_official.json"
MINISTRY = "Минобрнауки России"

# Названия групп УГСН, в которых есть запятые. Второй элемент — допустимые
# последовательности частей (в приказе встречаются усечённые варианты — опечатки).
MULTIPART = [
    ("Электроника, радиотехника и системы связи",
     [["электроника", "радиотехника и системы связи"]]),
    ("Фотоника, приборостроение, оптические и биотехнические системы и технологии",
     [["фотоника", "приборостроение", "оптические и биотехнические системы и технологии"],
      ["фотоника", "приборостроение"]]),
    ("Философия, этика и религиоведение",
     [["философия", "этика и религиоведение"]]),
    ("Сельское, лесное и рыбное хозяйство",
     [["сельское", "лесное и рыбное хозяйство"]]),
    ("Прикладная геология, горное дело, нефтегазовое дело и геодезия",
     [["прикладная геология", "горное дело", "нефтегазовое дело и геодезия"],
      ["прикладная геология", "горное дело", "нефтегазовое дело"]]),
]
_SEQS = sorted(((seq, canon) for canon, seqs in MULTIPART for seq in seqs),
               key=lambda x: -len(x[0]))


def cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def split_fields(text: str) -> List[str]:
    """«физика, электроника, радиотехника и системы связи» → ['Физика', 'Электроника, радиотехника и системы связи']."""
    text = re.sub(r"\.\s+(?=[а-яё])", ", ", text or "")        # опечатка «техника. машиностроение»
    parts = [p.strip() for p in text.split(",") if p.strip()]
    out: List[str] = []
    i = 0
    while i < len(parts):
        for seq, canon in _SEQS:
            if [p.lower() for p in parts[i:i + len(seq)]] == seq:
                out.append(canon)
                i += len(seq)
                break
        else:
            out.append(cap(parts[i]))
            i += 1
    return uniq(out)


def parse_rows(rows: List[List[str]]) -> List[dict]:
    events: List[dict] = []
    cur = None
    cur_key = ""
    first_orgs: set = set()
    for r in rows:
        r = pad_row(r)
        if is_header_row(r) or is_section_row(r) is not None or not any(r[:6]):
            continue
        num, name, org, profile, field, level = r[:6]
        key = norm_name(name)
        new = bool(name) and (cur is None or key != cur_key
                              or (num and num != cur["official_number"]))
        if new:
            if cur:
                events.append(cur)
            cur = {"official_number": num or (cur["official_number"] if cur else ""),
                   "name": name, "organizer_raw": "", "entries": [], "_orgs": []}
            cur_key = key
            first_orgs = set()
        elif cur is None:
            continue

        # В RTF продолжение вертикально объединённой ячейки пусто: организатор такой же,
        # как в предыдущей строке (в docx текст повторяется сам).
        if org:
            cur["_last_org"] = org
        row_orgs = org_list(org or cur.get("_last_org", ""))
        for o in row_orgs:
            if norm_name(o) not in {norm_name(x) for x in cur["_orgs"]}:
                cur["_orgs"].append(o)
        entry = {"profile": profile, "study_field": field,
                 "study_fields": split_fields(field), "level": norm_level(level)}
        if new:
            first_orgs = {norm_name(o) for o in row_orgs}
        elif row_orgs and {norm_name(o) for o in row_orgs} != first_orgs:
            entry["organizer"] = "\n".join(row_orgs)
        if profile or field or level:
            cur["entries"].append(entry)
    if cur:
        events.append(cur)

    for e in events:
        e["organizer_raw"] = "\n".join(e.pop("_orgs"))
        e.pop("_last_org", None)
        ents = e["entries"]
        e["profiles"] = uniq(x["profile"] for x in ents)
        e["study_fields"] = uniq(f for x in ents for f in x["study_fields"])
        e["levels"] = uniq(x["level"] for x in ents)
    return events


def check(events: List[dict]) -> List[str]:
    """Предупреждения о подозрительных данных."""
    if not events:
        return ["нет ни одной олимпиады — проверьте файл"]
    w = []
    nums = [e["official_number"] for e in events]
    if len(set(nums)) != len(nums):
        w.append(f"повторяющиеся номера: {sorted({n for n in nums if nums.count(n) > 1})[:8]}")
    try:
        ints = [int(n) for n in nums]
        if ints != list(range(ints[0], ints[0] + len(ints))):
            w.append("номера идут не подряд")
    except ValueError:
        w.append("нечисловые номера")
    for e in events:
        if not e["profiles"] or not e["levels"]:
            w.append(f"№{e['official_number']}: нет профиля/уровня")
        if not e["organizer_raw"]:
            w.append(f"№{e['official_number']}: нет организатора")
        bad = [l for l in e["levels"] if l not in ("I", "II", "III")]
        if bad:
            w.append(f"№{e['official_number']}: необычный уровень {bad}")
    return w


def parse(path: str) -> dict:
    order = read_order(path)
    if order.meta.get("ministry") not in (None, MINISTRY):
        sys.exit(f"{path}: это приказ {order.meta.get('ministry')}, а не {MINISTRY}")
    events = parse_rows(order.rows)
    return {"meta": order.meta, "warnings": check(events) + image_warnings(order), "events": events}


def main():
    ap = argparse.ArgumentParser(description="Парсер приказа Минобрнауки (олимпиады школьников)")
    ap.add_argument("order", nargs="?", help="файл приказа .rtf/.docx (по умолчанию — поиск в текущей папке)")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT)
    a = ap.parse_args()
    path = a.order or find_order(MINISTRY)
    if not path or not Path(path).exists():
        sys.exit("Не найден приказ Минобрнауки. Укажите файл: python parse_minobr.py приказ.rtf")
    data = parse(path)
    ev = data["events"]
    m = data["meta"]
    print(f"{Path(path).name}: приказ {m.get('ministry', '?')} от {m.get('date', '?')} № {m.get('number', '?')}")
    print(f"Олимпиад: {len(ev)}; строк «профиль–УГСН–уровень»: {sum(len(e['entries']) for e in ev)}")
    for w in data["warnings"]:
        print("⚠", w)
    Path(a.out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Сохранено → {a.out}")


if __name__ == "__main__":
    main()
