# -*- coding: utf-8 -*-
"""
Сравнение двух каталогов (например, проектного и утверждённого) — что ушло,
что появилось и что изменилось у совпавших по названию мероприятий.

Запуск:
    python diff_catalogs.py [старый.json] [новый.json] [--favs favorites.json|navigator.db] [--all]

По умолчанию старый — catalog.projects.json, новый — catalog.official.json (рядом со скриптом).
--favs  — что станет с избранным пользователей при переходе на новый каталог: id совпал,
          перенесётся по old_ids, перенесётся по legacy_key (как делает bot.py) или потеряется.
          Принимает favorites.json ({user_id: [id, …]}) или базу бота navigator.db
          (таблица favs(user_id, event_id, added_at)).
--all   — не обрезать списки
"""
import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List

from common import norm_name

HERE = Path(__file__).resolve().parent


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def index(cat):
    by = defaultdict(list)
    for e in cat["events"]:
        by[norm_name(e["name"])].append(e)
    return by


def load_favs(path) -> Dict[str, List[str]]:
    """Избранное {user_id: [event_id, …]} из favorites.json или SQLite-базы бота."""
    p = Path(path)
    with p.open("rb") as f:
        head = f.read(16)
    if head.startswith(b"SQLite format 3") or p.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT user_id, event_id FROM favs").fetchall()
        finally:
            con.close()
        out: Dict[str, List[str]] = defaultdict(list)
        for uid, eid in rows:
            out[str(uid)].append(eid)
        return dict(out)
    return json.loads(p.read_text(encoding="utf-8"))


def fav_fate(new_cat) -> "callable":
    """Функция id → «same» | «old_ids» | «legacy» | «lost» — тот же порядок, что в bot.py."""
    ids_new = {e["id"] for e in new_cat["events"]}
    by_old = {}
    for e in new_cat["events"]:
        for o in e.get("old_ids") or []:
            by_old.setdefault(o, e["id"])
    legacy = {e["legacy_key"] for e in new_cat["events"] if e.get("legacy_key")}

    def fate(eid: str) -> str:
        if eid in ids_new:
            return "same"
        if eid in by_old:
            return "old_ids"
        if eid.rsplit("-", 1)[-1] in legacy:
            return "legacy"
        return "lost"
    return fate


def as_set(e, key):
    return set(e.get(key) or [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("old", nargs="?", default=str(HERE / "catalog.projects.json"))
    ap.add_argument("new", nargs="?", default=str(HERE / "catalog.official.json"))
    ap.add_argument("--favs", help="favorites.json или navigator.db")
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
        favs = load_favs(a.favs)
        fate = fav_fate(new)
        c = Counter()
        lost_ids = Counter()
        for uid, lst in favs.items():
            for eid in lst:
                f = fate(eid)
                c[f] += 1
                if f == "lost":
                    lost_ids[eid] += 1
        total = sum(c.values())
        print(f"\nИзбранное: всего {total} · id совпал {c['same']} · "
              f"перенесётся по old_ids {c['old_ids']} · по legacy_key {c['legacy']} · "
              f"потеряется {c['lost']}")
        if lost_ids:
            old_names = {e["id"]: e["name"] for e in old["events"]}
            print("Потеряются (id — сколько раз в избранном):")
            for eid, k in lost_ids.most_common(lim):
                print(f"  − {eid} ×{k}  {old_names.get(eid, '?')[:80]}")


if __name__ == "__main__":
    main()
