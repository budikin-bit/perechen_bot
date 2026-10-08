# -*- coding: utf-8 -*-
"""
Сравнение двух каталогов (например, проектного и утверждённого) — что ушло,
что появилось и что изменилось у совпавших по названию мероприятий.

Запуск:
    python diff_catalogs.py [старый.json] [новый.json] [--favs favorites.json] [--all]

--favs  — показать, у скольких избранных записей пользователей изменился/пропал id
--all   — не обрезать списки
"""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from common import norm_name


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def index(cat):
    by = defaultdict(list)
    for e in cat["events"]:
        by[norm_name(e["name"])].append(e)
    return by


def as_set(e, key):
    return set(e.get(key) or [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("old", nargs="?", default="catalog.projects.json")
    ap.add_argument("new", nargs="?", default="catalog.official.json")
    ap.add_argument("--favs")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    lim = None if a.all else 20

    old, new = load(a.old), load(a.new)
    oi, ni = index(old), index(new)
    print(f"Было:  {len(old['events'])} записей, {len(oi)} уникальных названий")
    print(f"Стало: {len(new['events'])} записей, {len(ni)} уникальных названий")

    only_old = sorted(oi.keys() - ni.keys())
    only_new = sorted(ni.keys() - oi.keys())
    both = sorted(oi.keys() & ni.keys())
    print(f"Совпало по названию: {len(both)} · только в старом: {len(only_old)} · только в новом: {len(only_new)}")

    if only_old:
        print("\nУшли из каталога:")
        for k in only_old[:lim]:
            print("  −", oi[k][0]["name"][:100])
    if only_new:
        print("\nПоявились в каталоге:")
        for k in only_new[:lim]:
            print("  +", ni[k][0]["name"][:100])

    changed = Counter()
    shown = 0
    print("\nИзменения у совпавших:")
    for k in both:
        o, n = oi[k][0], ni[k][0]
        diffs = []
        for key, label in (("levels", "уровни"), ("profiles", "профили"),
                           ("study_fields", "направления вуза")):
            so, sn = as_set(o, key), as_set(n, key)
            if so != sn:
                changed[key] += 1
                diffs.append(f"{label}: −{sorted(so - sn)[:3]} +{sorted(sn - so)[:3]}")
        if norm_name(o.get("organizer_raw", "")) != norm_name(n.get("organizer_raw", "")):
            changed["organizer"] += 1
            diffs.append("организатор изменён")
        if diffs and (lim is None or shown < lim):
            shown += 1
            print("  ~", n["name"][:70])
            for d in diffs:
                print("      ", d)
    print("  итого:", dict(changed) or "изменений нет")

    if a.favs:
        favs = load(a.favs)
        ids_new = {e["id"] for e in new["events"]}
        legacy = {e["legacy_key"] for e in new["events"] if e.get("legacy_key")}
        total = lost = remap = 0
        for uid, lst in favs.items():
            for eid in lst:
                total += 1
                if eid in ids_new:
                    continue
                if eid.rsplit("-", 1)[-1] in legacy:
                    remap += 1
                else:
                    lost += 1
        print(f"\nИзбранное: всего {total} · id совпал {total - remap - lost} · "
              f"перенесётся по legacy_key {remap} · потеряется {lost}")


if __name__ == "__main__":
    main()
