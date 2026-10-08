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
    assert make_id("mp", "Олимпиада «Ломоносов»", s1).endswith("-2")   # дубль получает суффикс


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
