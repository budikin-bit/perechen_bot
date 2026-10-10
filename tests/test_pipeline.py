# -*- coding: utf-8 -*-
import docx
import pytest
import build_catalog as b
from common import acronyms, make_id, norm_name, org_list, short_org, stem
import parse_minpros


def test_stem_unifies_forms():
    assert stem("химии") == stem("химия")
    assert stem("права") == stem("право")
    assert stem("языки") == stem("язык")


@pytest.mark.parametrize("profile, must, must_not", [
    ("русский язык", "Русский язык", "Иностранный язык"),
    ("искусственный интеллект", "Информатика", "Искусство"),
    ("Вокальное направление", "Искусство", "Право"),
    ("информационная безопасность", "Информатика", "ОБЖ"),
    ("Управление в технических системах", "Технология", "Право"),
    ("спортивное программирование", "Информатика", "Физическая культура"),
    ("правила дорожного движения", None, "Право"),
    ("Право и политология", "Право", None),
    ("IT-решения для города", "Информатика", None),
    ("Виолончель", "Искусство", None),
])
def test_subject_rules(profile, must, must_not):
    got = b.guess_subjects([profile])
    if must:
        assert must in got
    if must_not:
        assert must_not not in got


def test_stable_id_ignores_official_number_and_quotes():
    s1, s2 = {}, {}
    a = make_id("mp", "Олимпиада «Ломоносов»", s1)
    c = make_id("mp", "Олимпиада  Ломоносов", s2)
    assert a == c
    assert make_id("mp", "Олимпиада «Ломоносов»", s1).endswith("-2")   # без secondary — счётчик


def test_duplicate_name_ids_do_not_depend_on_row_order():
    rows = [{"name": "Конкурс А", "organizer_raw": "Орг 2", "official_number": "6.10"},
            {"name": "Конкурс Б", "organizer_raw": "Орг", "official_number": "6.2"},
            {"name": "Конкурс А", "organizer_raw": "Орг 1", "official_number": "6.9"}]
    ids1 = dict(zip((r["official_number"] for r in rows), b.assign_ids("mp", rows, {})))
    rev = list(reversed(rows))
    ids2 = dict(zip((r["official_number"] for r in rev), b.assign_ids("mp", rev, {})))
    assert ids1 == ids2
    base = make_id("mp", "Конкурс А", {})
    assert ids1["6.9"] == base                                   # меньший номер — базовый id
    assert ids1["6.10"].startswith(base + "-") and len(ids1["6.10"]) == len(base) + 7
    assert ids1["6.2"] == make_id("mp", "Конкурс Б", {})        # не дубль — id без суффикса


def test_glued_organizers_are_split():
    raw = ("ФГАОУ ВО «Крымский федеральный университет имени В.И. Вернадского» "
           "ФГАОУ ВО «Северный (Арктический) федеральный университет имени М.В. Ломоносова»")
    assert len(org_list(raw)) == 2


def test_nested_quotes_and_acronyms():
    raw = "ФГАОУ ВО «Национальный исследовательский университет «Высшая школа экономики»»"
    assert len(org_list(raw)) == 1
    assert short_org(raw) == "Национальный исследовательский университет Высшая школа экономики"
    assert "ВШЭ" in acronyms(raw)
    assert "МФТИ" in acronyms("ФГАОУ ВО «Московский физико-технический институт (национальный исследовательский университет)»")


def test_alias_rules_applied():
    rules = b.load_alias_rules()
    al = b.build_aliases("ФГБОУ ВО «Санкт-Петербургский государственный университет»", rules)
    assert "СПбГУ" in al
    al = b.build_aliases("ФГАОУ ВО «Санкт-Петербургский государственный университет аэрокосмического приборостроения»", rules)
    assert "СПбГУ" not in al and "ГУАП" in al


def test_type_by_earliest_keyword():
    assert b.classify_type("Всероссийский конкурс олимпиадного движения") == "Конкурс"
    assert b.classify_type("Что-то необычное") == "Другое"


def _make_docx(path, rows):
    d = docx.Document()
    t = d.add_table(rows=0, cols=6)
    for r in rows:
        cells = t.add_row().cells
        for c, v in zip(cells, r):
            c.text = v
    d.save(path)


def test_minpros_parser_merges_repeated_rows_and_keeps_pairs(tmp_path):
    p = tmp_path / "x.docx"
    _make_docx(p, [
        ["№ п/п", "Наименование мероприятия", "Организатор", "Направление", "Профиль", "Уровень"],
        ["", "Фестиваль А", "Орг 1", "Искусство", "Вокал", "IV"],
        ["", "Фестиваль А", "Орг 1", "Искусство", "Хор", "III"],
        ["", "", "", "Спорт", "Шахматы", "II"],
        ["", "Конкурс Б", "Орг 2", "Наука", "Физика", "I"],
        ["", "Фестиваль А", "Орг 3", "Искусство", "Вокал", "IV"],      # другой организатор → отдельное
    ])
    res = parse_minpros.parse(str(p))
    ev = res["events"]
    assert res["numbered"] is False
    assert [e["official_number"] for e in ev] == ["6.1", "6.2", "6.3"]   # нумерация по мероприятиям
    a = ev[0]
    assert a["profiles"] == ["Вокал", "Хор", "Шахматы"]
    assert a["directions"] == ["Искусство", "Спорт"]                      # не теряем направления
    assert {(x["profile"], x["level"]) for x in a["entries"]} == {("Вокал", "IV"), ("Хор", "III"), ("Шахматы", "II")}


# ───────────── утверждённые приказы (RTF/нумерация/уровни) ─────────────

import json
import orders
import parse_minobr


def test_norm_level_variants():
    n = orders.norm_level
    assert n("IV уровень") == "IV" and n("Высший уровень") == "Высший"
    assert n("Не установлен.") == "Не установлен"
    assert n("Не") == "Не установлен"          # обрезанное слово в приказе № 598, 6.345


def test_split_fields_glues_ugsn_with_commas():
    r = parse_minobr.split_fields("физика, электроника, радиотехника и системы связи, философия, этика и религиоведение")
    assert r == ["Физика", "Электроника, радиотехника и системы связи", "Философия, этика и религиоведение"]
    # усечённый вариант из приказа (опечатка) тоже склеивается
    assert parse_minobr.split_fields("прикладная геология, горное дело, нефтегазовое дело") == [
        "Прикладная геология, горное дело, нефтегазовое дело и геодезия"]


def test_minpros_numbered_rows_duplicate_number_stays_two_events():
    H = ["№ п/п", "Наименование", "Организатор", "Направление", "Профиль", "Уровень"]
    rows = [H,
            ["1.1", "ВсОШ", "Минпрос", "Наука", "Математика", "Высший уровень"],
            ["", "", "", "Наука", "Физика", "Высший уровень"],
            ["6.95", "Конкурс А", "Орг", "Искусство", "Вокал", "IV уровень"],
            ["6.95", "Конкурс Б", "Орг", "Искусство", "Хор", "III уровень"]]
    ev, _, num = parse_minpros.parse_rows(rows)
    assert num and [e["official_number"] for e in ev] == ["1.1", "6.95", "6.95"]
    assert ev[0]["section"] == 1 and ev[0]["profiles"] == ["Математика", "Физика"]
    assert ev[1]["levels"] == ["IV"] and ev[2]["levels"] == ["III"]
    assert any("6.95" in w for w in parse_minpros.check(ev, num))


def test_minobr_empty_org_cell_inherits_previous_row():
    H = ["№", "Название", "Организатор", "Профиль", "Предметы", "Уровень"]
    rows = [H, ["1", "Олимпиада X", "Вуз А", "Физика", "физика", "I уровень"],
            ["", "", "", "Химия", "химия", "II уровень"],
            ["", "", "Вуз Б", "Биология", "биология", "II уровень"]]
    ev = parse_minobr.parse_rows(rows)
    assert len(ev) == 1 and ev[0]["organizer_raw"].count("Вуз") == 2
    assert "organizer" not in ev[0]["entries"][1] and ev[0]["entries"][2]["organizer"] == "Вуз Б"


def test_rtf_reader_skips_binary_pictures_and_decodes_cp1251(tmp_path):
    rtf = (r"{\rtf1\ansi\ansicpg1251\deff0{\fonttbl{\f0 Arial;}}"
           r"\trowd\cellx1000\cellx2000\cellx3000\cellx4000\cellx5000\cellx6000"
           r"\intbl 1\cell \'cd\'e0\'e7\'e2\'e0\'ed\'e8\'e5\cell A\cell B\cell C\cell D\cell\row}")
    rows = orders.rtf_tables(rtf)
    assert rows and rows[0][0] == "1" and rows[0][1] == "Название"


def test_attach_old_ids_maps_renamed_event(tmp_path):
    import build_catalog as b
    old = {"events": [{"id": "mp-old", "name": "Олимпиада Фоксфорда 2026", "source_code": "minpros"},
                      {"id": "mp-gone", "name": "Совсем другое", "source_code": "minpros"}]}
    f = tmp_path / "old.json"; f.write_text(json.dumps(old), encoding="utf-8")
    new = [{"id": "mp-new", "name": "Олимпиада «Фоксфорд 2026»", "source_code": "minpros"}]
    matched, dropped = b.attach_old_ids(new, f)
    assert new[0]["old_ids"] == ["mp-old"] and len(matched) == 1 and dropped == ["Совсем другое"]


# ───────────── перенос избранного между версиями каталога ─────────────

from pathlib import Path
from common import fold_name, id_hash

ROOT = Path(__file__).resolve().parent.parent


def _write(tmp_path, events, name="prev.json"):
    f = tmp_path / name
    f.write_text(json.dumps({"events": events}, ensure_ascii=False), encoding="utf-8")
    return f


def _ev(eid, name, src="minpros", org="Орг", **kw):
    return dict(id=eid, name=name, source_code=src, organizer_raw=org, **kw)


def test_fold_name_unifies_scripts_and_org_forms():
    assert fold_name("ХIХ конкурс") == fold_name("XIX конкурс")             # кириллическая Х
    assert fold_name("Хакатон по IТ") == fold_name("Хакатон по IT")
    assert fold_name("фестиваль c участием") == fold_name("фестиваль с участием")
    assert fold_name("молодѐжная") == fold_name("молодежная")
    assert fold_name("ВСО среди вузов") == fold_name("Всероссийская студенческая олимпиада среди вузов")
    long_ = ("Олимпиада ФЕДЕРАЛЬНОЕ ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ ОБРАЗОВАТЕЛЬНОЕ УЧРЕЖДЕНИЕ "
             "ВЫСШЕГО ОБРАЗОВАНИЯ «Университет»")
    assert fold_name(long_) == fold_name("Олимпиада ФГБОУ ВО «Университет»") == "олимпиада университет"
    assert id_hash("mp-2d495f17ac-2") == id_hash("mo-2d495f17ac") == "2d495f17ac"


def test_attach_old_ids_never_maps_subject_siblings(tmp_path):
    prev = [_ev("mp-p1", "Аэрокосмическая олимпиада ГУАП по физике"),
            _ev("mp-p2", "Аэрокосмическая олимпиада ГУАП по химии"),
            _ev("mp-p3", "Олимпиада «Ломоносов» по математике"),
            _ev("mp-p4", "Олимпиада Звезда")]
    new = [_ev("mp-n2", "Аэрокосмическая олимпиада ГУ АП по химии"),
           _ev("mp-n5", "Аэрокосмическая олимпиада ГУ АП по информатике"),
           _ev("mp-n6", "Олимпиада «Ломоносов» по механике"),
           _ev("mp-n7", "Олимпиада Звезда по физике")]
    matched, dropped = b.attach_old_ids(new, _write(tmp_path, prev))
    got = {m[3]: m[4] for m in matched}
    assert got == {"mp-p2": "mp-n2"}
    assert sorted(dropped) == sorted(["Аэрокосмическая олимпиада ГУАП по физике",
                                      "Олимпиада «Ломоносов» по математике", "Олимпиада Звезда"])
    assert b.is_sibling({"аэрокосмическ", "гуап", "физик"}, {"аэрокосмическ", "гу", "ап", "хим"})


def test_attach_old_ids_global_best_pairs_and_carry_forward(tmp_path):
    prev = [_ev("mp-a", "Конкурс «Юный вокалист» 2026 года"),
            _ev("mp-b", "Конкурс «Юный вокалист» 2026", old_ids=["mp-older"]),
            _ev("mp-keep", "Фестиваль Тот же", old_ids=["mp-ancient"])]
    new = [_ev("mp-new", "Конкурс «Юный вокалист» 2026"),
           _ev("mp-keep", "Фестиваль Тот же")]
    matched, dropped = b.attach_old_ids(new, _write(tmp_path, prev))
    # лучшая пара (mp-b, 1.0) выбирается раньше mp-a, хотя mp-a идёт первым
    assert new[0]["old_ids"] == ["mp-b", "mp-older"]
    assert new[1]["old_ids"] == ["mp-ancient"]                   # уцелевшее мероприятие сохраняет old_ids
    assert dropped == ["Конкурс «Юный вокалист» 2026 года"]


def test_attach_old_ids_exact_hash_across_sources_and_dup_suffix(tmp_path):
    from common import make_id
    n1 = "Оборонно-техническая олимпиада"
    n2 = "Олимпиада «Вечный двигатель»"
    prev = [_ev(make_id("mo", n1, {}), n1, "minobr"),
            _ev(make_id("mp", n2, {}), n2), _ev(make_id("mp", n2, {}) + "-2", n2)]
    new = [_ev(make_id("mp", n1, {}), n1), _ev(make_id("mp", n2, {}), n2)]
    matched, dropped = b.attach_old_ids(new, _write(tmp_path, prev))
    assert new[0]["old_ids"] == [prev[0]["id"]]
    assert new[1]["old_ids"] == [prev[2]["id"]]                  # -2 → существующий базовый id
    assert not dropped and {m[5] for m in matched} == {"id"}


def test_attach_old_ids_real_catalogs_regression(tmp_path):
    """catalog.projects.json → каталог из minobr/minpros_official.json."""
    if not (ROOT / "minobr_official.json").exists():
        pytest.skip("нет разобранных приказов")
    rules = b.load_alias_rules()
    seen = {}
    events = (b.convert_minobr(b.load_parsed(ROOT / "minobr_official.json")["events"], rules, seen)
              + b.convert_minpros(b.load_parsed(ROOT / "minpros_official.json")["events"], rules, seen))
    matched, dropped = b.attach_old_ids(events, ROOT / "catalog.projects.json")
    by_old = {o: e for e in events for o in e.get("old_ids", [])}
    for old, new in [("mo-117a9fe69f", "mp-117a9fe69f"), ("mp-e98c396f0a", "mo-e98c396f0a"),
                     ("mp-59325eacfd", "mo-59325eacfd"), ("mp-7dccb24131", "mo-7dccb24131"),
                     ("mp-7e90fdce3c", "mo-7e90fdce3c"), ("mp-2d495f17ac-2", "mp-2d495f17ac")]:
        assert by_old[old]["id"] == new, old
    # ВСО ↔ полное название, «ФЕДЕРАЛЬНОЕ … УЧРЕЖДЕНИЕ …» ↔ «ФГБОУ ВО»
    names = {m[0]: m[1] for m in matched}
    assert names["ВСО среди вузов страны «Национальная экономика» - III этап"].startswith(
        "Всероссийская студенческая олимпиада среди вузов страны «Национальная экономика»")
    assert "ФГБОУ ВО" in names[next(k for k in names if k.startswith("Открытая олимпиада ФЕДЕРАЛЬНОЕ"))]
    # предметы не путаются: у каждого сопоставления одинаковые предметы в названии
    for x, y, r, o, n, k in matched:
        sx, sy = b.guess_subjects([fold_name(x)]), b.guess_subjects([fold_name(y)])
        assert not (sx and sy and sx != sy), (x, y)
    assert len(dropped) <= 25
    # каждый старый id — не более чем у одного мероприятия, и это не живой id
    olds = [o for e in events for o in e.get("old_ids", [])]
    assert len(olds) == len(set(olds)) and not set(olds) & {e["id"] for e in events}


# ───────────── сборка: правки, вариант, проверки ─────────────

def test_overrides_keys_normalized_and_unused_reported(tmp_path):
    f = tmp_path / "ov.json"
    f.write_text(json.dumps({"by_name": {"Олимпиада «Звезда»": {"type": "Конкурс"},
                                         "Нет такого": {"type": "X"}},
                             "same_event": [["Олимпиада «Звезда»", "Звезда"], ["А", "Б"]]},
                            ensure_ascii=False), encoding="utf-8")
    ov = b.load_overrides(f)
    assert "олимпиада звезда" in ov["by_name"]
    ev = [dict(_ev("mo-1", "Олимпиада Звезда", "minobr"), organizer_aliases=[], type="Олимпиада"),
          dict(_ev("mp-1", "ЗВЕЗДА"), organizer_aliases=[], type="Другое")]
    unused = []
    assert b.apply_overrides(ev, ov, unused) == 1 and ev[0]["type"] == "Конкурс"
    assert b.mark_in_both(ev, ov["same_event"], unused) == 1 and ev[1].get("in_both")
    assert len(unused) == 2 and any("нет такого" in u for u in unused) and any("same_event" in u for u in unused)


def test_pick_variant(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="minpros_official.json"):
        (tmp_path / "minobr_official.json").write_text("{}")
        b.pick_variant(tmp_path)
    (tmp_path / "minpros_official.json").write_text("{}")
    assert b.pick_variant(tmp_path) == "official"
    empty = tmp_path / "e"
    empty.mkdir()
    assert b.pick_variant(empty) == "projects"


def test_empty_inputs_do_not_crash():
    assert parse_minobr.check([]) == ["нет ни одной олимпиады — проверьте файл"]
    assert "Событий: 0" in b.make_report([], 0, 0, [])
    warn = b.validate([{"id": "mp-1", "name": "X", "source_code": "minpros", "entries": []}])
    assert warn and "нет строк" in warn[0]
    with pytest.raises(SystemExit):
        b.validate([{"id": "mp-1", "name": "", "source_code": "minpros", "entries": [1]}])


# ───────────── RTF: Unicode, кодировки, границы таблиц, картинки ─────────────

HDR = r"{\rtf1\ansi\ansicpg1251\deff0{\fonttbl{\f0 Arial;}}"
ROW6 = r"\trowd\cellx1\cellx2\cellx3\cellx4\cellx5\cellx6 "


def _row(*cells):
    return ROW6 + r"\pard\intbl " + "".join(c + r"\cell " for c in cells) + r"\row "


U = "\\u"          # управляющее слово RTF «\\uN» (собираем строкой, чтобы не путать с escape Python)


def test_rtf_unicode_uc_and_hex_fallback():
    # \\uc1 + \\'3f как символ замены; \\uc2 — пропуск двух символов; \\uc0 — без замены
    rtf = HDR + _row(U + "1046\\'3f" + U + "1046?", "{\\uc2" + U + "1046??}x",
                     "{\\uc0" + U + "1046" + U + "1047}", "a", "b", "c") + "}"
    r = orders.rtf_tables(rtf)[0]
    zh, ze = chr(1046), chr(1047)
    assert r[0] == zh + zh and r[1] == zh + "x" and r[2] == zh + ze
    # \\uc2 с заменой из двух \\'xx; группа восстанавливает \\uc1
    rtf = HDR + _row("{\\uc2" + U + "1046\\'3f\\'3f}" + U + "1047?", "x", "y", "z", "w", "v") + "}"
    assert orders.rtf_tables(rtf)[0][0] == zh + ze


def test_rtf_unicode_negative_and_surrogates():
    rtf = HDR + _row(U + "-3913?", U + "-10179?" + U + "-8704?", U + "-10179?x", U + "-8704?", "e", "f") + "}"
    r = orders.rtf_tables(rtf)[0]
    bad = chr(0xFFFD)
    assert r[0] == chr(65536 - 3913)
    assert r[1] == chr(0x1F600)                 # пара суррогатов → один символ
    assert r[2] == bad + "x" and r[3] == bad    # одиночные суррогаты → U+FFFD
    assert all(not (0xD800 <= ord(ch) <= 0xDFFF) for c in r for ch in c)


def test_rtf_hex_bytes_use_ansicpg():
    rtf = r"{\rtf1\ansi\ansicpg1252 " + _row(r"caf\'e9", "x", "y", "z", "w", "v") + "}"
    assert orders.rtf_tables(rtf)[0][0] == "café"
    rtf = r"{\rtf1\ansi\ansicpg9999 " + _row(r"\'cd\'e0", "x", "y", "z", "w", "v") + "}"
    assert orders.rtf_tables(rtf)[0][0] == "На"                  # неизвестная кодировка → cp1251


def test_rtf_text_between_tables_not_glued_and_tail_row_flushed():
    rtf = (HDR + _row("1", "A", "B", "C", "D", "E")
           + r"\pard Текст между таблицами\par "
           + _row("2", "F", "G", "H", "I", "J")
           + r"\pard Текст перед таблицей без конца абзаца "
           + ROW6 + r"\pard\intbl 3\cell K\cell L\cell M\cell N\cell O\cell }")
    rows = orders.rtf_tables(rtf)
    assert [r[0] for r in rows] == ["1", "2", "3"]
    assert rows[2][-1] == "O"


def test_rtf_nested_table_cells_are_separated():
    rtf = (HDR + ROW6 + r"\pard\intbl 1\cell "
           + r"\pard\intbl\itap2 Внутри1\nestcell Внутри2\nestcell{\*\nesttableprops\trowd\cellx1\nestrow}"
           + r"\pard\intbl\itap1\cell A\cell B\cell C\cell D\cell\row}")
    r = orders.rtf_tables(rtf)[0]
    assert r[1] == "Внутри1\nВнутри2"


def test_rtf_hex_picture_gets_placeholder_and_warning(tmp_path, monkeypatch):
    import hashlib
    data = bytes(range(40))
    sha = hashlib.sha1(data).hexdigest()[:12]
    pict = r"{\pict\pngblip\picw10 " + data.hex()[:30] + "\n" + data.hex()[30:] + "}"
    rtf = HDR + _row("6.1", "Конкурс " + pict, "Орг", "Н", "П", "IV уровень") + "}"
    raw = orders.rtf_rows_raw(rtf)
    assert raw[0][1] == f"Конкурс ⟦img:{sha}⟧"
    monkeypatch.setattr(orders, "IMAGE_TEXT", {sha: "Звезда"})
    assert orders.rtf_tables(rtf)[0][1] == "Конкурс Звезда"
    monkeypatch.setattr(orders, "IMAGE_TEXT", {})
    f = tmp_path / "o.rtf"
    f.write_bytes((HDR + r"\pard{\pict " + data.hex() + r"}\par " + _row("6.1", "Конкурс " + pict, "Орг", "Н", "П", "IV уровень") + "}").encode("cp1251"))
    o = orders.read_order(f)
    assert o.unresolved_images == [(1, "6.1", sha)]               # картинка вне таблицы — не в счёт
    w = orders.image_warnings(o)
    assert w == [f"картинка без расшифровки в строке 1 (№ 6.1) (sha1 {sha}) — добавьте в image_text.json"]


def test_rtf_bin_picture_hash_unchanged():
    import hashlib
    data = b"\x89PNG\x98\x00{}\\"          # 0x98 нет в cp1251 → surrogateescape
    payload = data.decode("cp1251", "surrogateescape")
    rtf = HDR + _row("1", r"A {\pict\pngblip\bin" + str(len(payload)) + " " + payload + "}", "x", "y", "z", "w") + "}"
    sha = hashlib.sha1(data).hexdigest()[:12]
    assert orders.rtf_rows_raw(rtf)[0][1] == f"A ⟦img:{sha}⟧"


def test_section_rows_reach_minpros_parser(tmp_path):
    rtf = (HDR + _row("N п/п", "Наименование", "Организатор", "Направление", "Профиль", "Уровень")
           + r"\trowd\cellx6\pard\intbl 1. Мероприятия в соответствии с пунктом 5\cell\row "
           + _row("1.1", "ВсОШ", "Минпрос", "Наука", "Математика", "Высший уровень")
           + r"\trowd\cellx6\pard\intbl 6. Мероприятия по предложениям организаторов\cell\row "
           + _row("6.1", "Конкурс", "Орг", "Искусство", "Вокал", "IV уровень")
           + r"\trowd\cellx6\pard\intbl Просто строка из одной ячейки\cell\row }")
    f = tmp_path / "p.rtf"
    f.write_bytes(rtf.encode("cp1251"))
    o = orders.read_order(f)
    assert len(o.rows) == 5                                       # одноячеечная не-раздел отброшена
    res = parse_minpros.parse(str(f))
    assert sorted(res["sections"]) == ["1", "6"]
    assert [(e["official_number"], e["section"]) for e in res["events"]] == [("1.1", 1), ("6.1", 6)]


def test_real_orders_sections_and_images():
    src = ROOT / "orders_src" / "prikaz_598_minpros.rtf"
    if not src.exists():
        pytest.skip("нет приказа")
    res = parse_minpros.parse(str(src))
    assert {"1", "3", "4", "5", "6"} <= set(res["sections"])
    assert not any("картинка" in w for w in res["warnings"])
    names = json.dumps(res["events"], ensure_ascii=False)
    assert "Space-π" in names and "Unité" in names and "Diversité" in names and "求同存" in names


# ───────────── diff_catalogs --favs ─────────────

def test_diff_catalogs_favs_old_ids_and_sqlite(tmp_path):
    import sqlite3
    import diff_catalogs as d
    new = {"events": [{"id": "mp-new", "name": "X", "old_ids": ["mp-old"], "legacy_key": "abcdef0123"},
                      {"id": "mp-same", "name": "Y"}]}
    fate = d.fav_fate(new)
    assert [fate(x) for x in ("mp-same", "mp-old", "zz-abcdef0123", "mp-gone")] == [
        "same", "old_ids", "legacy", "lost"]
    db = tmp_path / "navigator.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE favs(user_id INTEGER, event_id TEXT, added_at REAL)")
    con.executemany("INSERT INTO favs VALUES(?,?,?)", [(1, "mp-old", 0), (1, "mp-same", 0), (2, "mp-gone", 0)])
    con.commit()
    con.close()
    assert d.load_favs(db) == {"1": ["mp-old", "mp-same"], "2": ["mp-gone"]}
    j = tmp_path / "favorites.json"
    j.write_text(json.dumps({"1": ["mp-old"]}))
    assert d.load_favs(j) == {"1": ["mp-old"]}
