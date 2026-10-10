# -*- coding: utf-8 -*-
"""
Парсер приказа Минпросвещения об утверждении перечня олимпиад и иных
интеллектуальных и (или) творческих конкурсов, мероприятий.

Вход:  приказ в формате .rtf или .docx. Выход: minpros_official.json
       {"meta": {реквизиты приказа}, "events": [...]}.

Запуск:
    python parse_minpros.py                      # ищет приказ Минпросвещения в текущей папке
    python parse_minpros.py приказ.rtf [-o minpros_official.json]

Структура приказа: шесть разделов (по подпунктам пункта 5 и пункту 4 Правил):
ВсОШ, олимпиады из перечня Минобрнауки, международные олимпиады, спорт, творчество
(Минкультуры) и — самый большой — раздел 6 «по предложениям организаторов».
Номера мероприятий в приказе настоящие (1.1, 3.4, 6.291 …).

Особенности таблицы:
  • в RTF название и номер стоят только в первой строке мероприятия, в docx — повторяются;
    строки с пустым названием — продолжение предыдущего мероприятия;
  • у одного номера иногда два разных мероприятия (так в приказе: № 6.95) — они
    остаются двумя записями с одинаковым official_number;
  • у мероприятия может быть несколько направлений;
  • в поле «профильное направление» разделы 1, 3, 5 перечисляют профили через «;».

Если в документе нет колонки с номерами (проект перечня), номера 6.N присваиваются
по порядку мероприятий, а мероприятие определяется парой «название + организатор».
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List

from common import norm_name
from orders import (find_order, image_warnings, is_header_row, is_section_row, norm_level,
                    pad_row, read_order, split_top_level, uniq)

DEFAULT_OUT = "minpros_official.json"
MINISTRY = "Минпросвещения России"

SECTION_TITLES = {
    1: "Всероссийская олимпиада школьников",
    2: "Олимпиады школьников из перечня Минобрнауки",
    3: "Международные олимпиады по общеобразовательным предметам",
    4: "Спортивные мероприятия (по представлению Минспорта)",
    5: "Творческие мероприятия (по представлению Минкультуры)",
    6: "Мероприятия по предложениям организаторов",
}
NUM_RE = re.compile(r"^(\d+)\.(\d+)$")


def parse_rows(rows: List[List[str]]):
    rows = [pad_row(r) for r in rows]
    data = [r for r in rows if not is_header_row(r) and is_section_row(r) is None and any(r[1:6])]
    num_mode = any(NUM_RE.match(r[0]) for r in data)

    events: List[dict] = []
    sections_text = {}
    cur = None
    cur_key = None
    section = 0 if num_mode else 6
    seq = 0
    for r in rows:
        if is_header_row(r) or not any(r[:6]):
            continue
        sec = is_section_row(r)
        if sec is not None:
            section = sec
            sections_text[str(sec)] = r[0]
            continue
        num, name, org, direction, profile, level = r[:6]

        if num_mode:
            new = bool(name) and (cur is None or norm_name(name) != cur_key[0]
                                  or (num and num != cur["official_number"]))
        else:
            key = (norm_name(name), norm_name(org))
            new = bool(name) and (cur is None or key != cur_key
                                  and not (not org and key[0] == cur_key[0]))
        if new:
            if cur:
                events.append(cur)
            seq += 1
            if num_mode:
                number = num or (cur["official_number"] if cur else "")
                m = NUM_RE.match(number)
                if m:
                    section = int(m.group(1))
            else:
                number = f"6.{seq}"
            cur = {"official_number": number, "section": section, "name": name,
                   "organizer_raw": org or "", "entries": []}
            cur_key = (norm_name(name),) if num_mode else (norm_name(name), norm_name(org))
        elif cur is None:
            continue
        if direction or profile or level:
            entry = {"direction": direction, "profile": profile, "level": norm_level(level)}
            if level.strip().rstrip(".").lower() == "не":
                entry["level_raw"] = level
            if entry not in cur["entries"]:
                cur["entries"].append(entry)
    if cur:
        events.append(cur)

    for e in events:
        ents = e["entries"]
        e["directions"] = uniq(x["direction"] for x in ents)
        e["profiles"] = uniq(p for x in ents for p in (split_top_level(x["profile"]) or [x["profile"]]))
        e["levels"] = uniq(x["level"] for x in ents)
        if num_mode:
            e["section_title"] = SECTION_TITLES.get(e["section"], "")
        else:
            e.pop("section", None)
    return events, sections_text, num_mode


def check(events: List[dict], num_mode: bool) -> List[str]:
    w = []
    if num_mode:
        by_sec = {}
        for e in events:
            m = NUM_RE.match(e["official_number"])
            if not m:
                w.append(f"нестандартный номер: {e['official_number']!r}")
                continue
            by_sec.setdefault(int(m.group(1)), []).append(int(m.group(2)))
        for sec, nums in sorted(by_sec.items()):
            uniq_nums = sorted(set(nums))
            gaps = sorted(set(range(1, max(uniq_nums) + 1)) - set(uniq_nums))
            if gaps:
                w.append(f"раздел {sec}: пропущены номера {gaps[:8]}")
            dup = sorted({n for n in nums if nums.count(n) > 1})
            if dup:
                w.append(f"раздел {sec}: номер повторяется у разных мероприятий: "
                         + ", ".join(f"{sec}.{n}" for n in dup))
    for e in events:
        if any(x.get("level_raw") for x in e["entries"]):
            w.append(f"{e['official_number']}: в приказе уровень обрезан до «Не» — принято «Не установлен»")
        if not e["profiles"]:
            w.append(f"{e['official_number']}: нет профиля — {e['name'][:60]!r}")
        if not e["levels"]:
            w.append(f"{e['official_number']}: нет уровня — {e['name'][:60]!r}")
    return w


def parse(path: str) -> dict:
    order = read_order(path)
    if order.meta.get("ministry") not in (None, MINISTRY):
        sys.exit(f"{path}: это приказ {order.meta.get('ministry')}, а не {MINISTRY}")
    events, sections, num_mode = parse_rows(order.rows)
    return {"meta": order.meta, "sections": sections, "numbered": num_mode,
            "warnings": check(events, num_mode) + image_warnings(order), "events": events}


def main():
    ap = argparse.ArgumentParser(description="Парсер приказа Минпросвещения (перечень мероприятий)")
    ap.add_argument("order", nargs="?", help="файл приказа .rtf/.docx (по умолчанию — поиск в текущей папке)")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT)
    a = ap.parse_args()
    path = a.order or find_order(MINISTRY)
    if not path or not Path(path).exists():
        sys.exit("Не найден приказ Минпросвещения. Укажите файл: python parse_minpros.py приказ.rtf")
    data = parse(path)
    ev, m = data["events"], data["meta"]
    print(f"{Path(path).name}: приказ {m.get('ministry', '?')} от {m.get('date', '?')} № {m.get('number', '?')}")
    print(f"Мероприятий: {len(ev)}; строк «направление–профиль–уровень»: {sum(len(e['entries']) for e in ev)}")
    if data["numbered"]:
        from collections import Counter
        c = Counter(e["section"] for e in ev)
        print("По разделам:", ", ".join(f"{k}: {v}" for k, v in sorted(c.items())))
    else:
        print("ℹ В документе нет номеров — присвоены 6.N по порядку (проект перечня).")
    if data["sections"]:
        print("Заголовки разделов:", ", ".join(sorted(data["sections"])))
    for w in data["warnings"]:
        print("⚠", w)
    Path(a.out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Сохранено → {a.out}")


if __name__ == "__main__":
    main()
