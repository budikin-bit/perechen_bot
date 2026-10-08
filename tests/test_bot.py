# -*- coding: utf-8 -*-
import json
import pytest
import bot
from common import legacy_key


def ev(i, name, **kw):
    e = {"id": f"mp-{i:010d}", "legacy_key": legacy_key(name), "source_code": "minpros",
         "official_number": f"6.{i}", "name": name, "type": "Олимпиада",
         "organizer_raw": kw.pop("org", "ФГАОУ ВО «Университет ИТМО»"),
         "entries": kw.pop("entries", []), "directions": ["Наука и образование"],
         "profiles": kw.pop("profiles", []), "levels": kw.pop("levels", ["II"]),
         "subjects": kw.pop("subjects", []), "interests": kw.pop("interests", []),
         "organizer_aliases": kw.pop("aliases", [])}
    e.update(kw)
    return e


def make_catalog():
    return {"schema_version": 2, "academic_year": "2026/27", "variant": "projects",
            "sources": {"minpros": {"ministry": "Минпросвещения России", "source": "Проект"}},
            "events": [
                ev(1, "Олимпиада по химии", profiles=["Химия"], subjects=["Химия"],
                   interests=["Химия и материалы"]),
                ev(2, "Олимпиада по физике", profiles=["Физика"], subjects=["Физика"],
                   interests=["Математика и физика"],
                   org="ФГАОУ ВО «Национальный исследовательский университет «Высшая школа экономики»»",
                   aliases=["ВШЭ", "Вышка"]),
                ev(3, "Конкурс по праву", profiles=["Право"], subjects=["Право"],
                   interests=["Общество, право и политика"]),
                ev(4, "Олимпиада по математике", profiles=["Математика"], subjects=["Математика"]),
                ev(5, "Фестиваль искусств", profiles=["Музыка", "Живопись"], levels=["II", "III"],
                   entries=[{"direction": "Искусство", "profile": "Музыка", "level": "II"},
                            {"direction": "Искусство", "profile": "Живопись", "level": "III"}]),
                ev(6, "Длинная " + "x" * 50, entries=[
                    {"profile": "П" * 500 + str(i), "level": "I"} for i in range(40)]),
            ]}


class FakeAPI:
    def __init__(self):
        self.sent, self.answers, self.edits = [], [], []

    def send(self, **kw):
        self.sent.append(kw)
        return {}

    def answer_callback(self, callback_id, notification=None, text=None, keyboard=None):
        self.answers.append({"id": callback_id, "notification": notification,
                             "text": text, "keyboard": keyboard})
        return {}

    def edit(self, mid, text, keyboard=None):
        self.edits.append((mid, text, keyboard))

    def upload_file(self, filename, content):
        return None


@pytest.fixture(autouse=True)
def setup(tmp_path):
    bot.init(make_catalog(), tmp_path / "t.db")
    bot.SESSIONS = bot.LRU(100)
    bot.VIEWS = bot.Views()


def msg(text, uid=1, chat_type="dialog"):
    return {"update_type": "message_created", "message": {
        "sender": {"user_id": uid}, "recipient": {"chat_id": 10, "chat_type": chat_type},
        "body": {"text": text}}}


def click(payload, uid=1, cid="cb1"):
    return {"update_type": "message_callback", "callback": {
        "callback_id": cid, "payload": payload, "user": {"user_id": uid},
        "message": {"body": {"mid": "m1"}, "recipient": {"chat_id": 10}}}}


def payloads(kb):
    return [b["payload"] for row in kb["payload"]["buttons"] for b in row if b["type"] == "callback"]


def last_list_payloads(api):
    return payloads(api.sent[-1]["keyboard"])


# ───────── сценарии ─────────

def test_bot_started_shows_menu():
    api = FakeAPI()
    bot.handle_update(api, {"update_type": "bot_started", "chat_id": 10, "user": {"user_id": 1}})
    assert api.sent and "Навигатор" in api.sent[0]["text"]
    assert "m:search" in payloads(api.sent[0]["keyboard"])


def test_empty_and_sticker_messages_do_not_dump_catalog():
    api = FakeAPI()
    bot.handle_update(api, msg(""))
    assert "только текст" in api.sent[-1]["text"]
    bot.do_search(api, {"chat_id": 10}, bot.sess(1), 1, "   ")
    assert "Найдено" not in api.sent[-1]["text"]


def test_group_free_text_is_ignored_but_commands_work():
    api = FakeAPI()
    bot.handle_update(api, msg("физика", chat_type="chat"))
    assert api.sent == []
    bot.handle_update(api, msg("/menu", chat_type="chat"))
    assert len(api.sent) == 1


def test_search_forms_and_aliases():
    c = bot.CAT
    assert len(c.search(q="химии")) == len(c.search(q="химия")) == 1
    assert len(c.search(q="права")) == 1
    assert [e["name"] for e in c.search(org="ВШЭ")] == ["Олимпиада по физике"]
    assert len(c.search(q="ИТМО,")) >= 4        # пунктуация не ломает запрос
    assert c.search(q="ит") == []                # короткий токен — только целое слово


def test_ranking_prefers_name_matches():
    c = bot.CAT
    top = c.search(q="физика")[0]
    assert top["name"] == "Олимпиада по физике"


def test_relaxed_search_and_typo_hint():
    res, relaxed = bot.CAT.smart_search("химия бальные")
    assert relaxed and res
    assert bot.CAT.suggest("математкиа")


def test_search_flow_payloads_are_stateless():
    api = FakeAPI()
    bot.handle_update(api, msg("олимпиада"))
    first = last_list_payloads(api)
    bot.handle_update(api, msg("право"))
    second = last_list_payloads(api)
    assert first != second
    # кнопка из СТАРОГО сообщения открывает именно свою карточку
    old_card = next(p for p in first if p.startswith("c:"))
    api2 = FakeAPI()
    bot.handle_update(api2, click(old_card))
    assert api2.answers[0]["text"].startswith("🏆")
    eid = old_card.split(":")[1]
    assert bot.CAT.by_id[eid]["name"] in api2.answers[0]["text"]
    # и «К списку» ведёт в тот же старый список
    back = [p for p in payloads(api2.answers[0]["keyboard"]) if p.startswith("l:")]
    assert back and back[0].split(":")[1] == old_card.split(":")[2]


def test_favorite_toggle_single_answer_with_notification():
    api = FakeAPI()
    eid = bot.CAT.events[0]["id"]
    bot.handle_update(api, click(f"fav:{eid}:x:0"))
    assert len(api.answers) == 1                       # раньше было 2 вызова
    assert api.answers[0]["notification"] and api.answers[0]["text"]
    assert bot.STORE.is_fav(1, eid)
    bot.handle_update(api, click(f"fav:{eid}:x:0", cid="cb2"))
    assert not bot.STORE.is_fav(1, eid)


def test_delete_from_favorites_uses_event_id_not_index():
    api = FakeAPI()
    a, b, c = [e["id"] for e in bot.CAT.events[:3]]
    for e in (a, b, c):
        bot.STORE.toggle(1, e)
    bot.handle_update(api, click(f"fd:{b}:0"))         # «устаревшая» кнопка по id
    assert bot.STORE.fav_ids(1) == [a, c]


def test_card_from_favorites_returns_to_favorites():
    api = FakeAPI()
    eid = bot.CAT.events[0]["id"]
    bot.STORE.toggle(1, eid)
    bot.handle_update(api, click(f"c:{eid}:f:0"))
    assert "l:f:0" in payloads(api.answers[-1]["keyboard"])


def test_expired_view_is_reported_but_cards_work():
    api = FakeAPI()
    bot.handle_update(api, click("l:zzzz:0"))
    assert "устарел" in api.answers[-1]["text"]
    eid = bot.CAT.events[0]["id"]
    bot.handle_update(api, click(f"c:{eid}:zzzz:0"))
    assert api.answers[-1]["text"].startswith("🏆")


def test_levels_shown_per_profile():
    api = FakeAPI()
    eid = bot.CAT.events[4]["id"]
    bot.handle_update(api, click(f"c:{eid}:x:0"))
    text = api.answers[-1]["text"]
    assert "Уровень II: Музыка" in text and "Уровень III: Живопись" in text


def test_details_are_paged_under_limit():
    e = bot.CAT.events[5]
    pages = bot.detail_pages(e)
    assert len(pages) > 1 and all(len(p) < bot.MAX_LEN for p in pages)


def test_filters_by_value_hash_and_text_search():
    api = FakeAPI()
    bot.handle_update(api, click("fk:subject"))
    opts = payloads(api.answers[-1]["keyboard"])
    fv = next(p for p in opts if p.startswith("fv:subject:") and bot.vh("Химия") in p)
    bot.handle_update(api, click(fv))
    assert bot.sess(1).filters == {"subject": "Химия"}
    bot.handle_update(api, click("fshow"))
    assert "Найдено: 1 " in api.answers[-1]["text"]
    # поиск по списку значений
    bot.handle_update(api, click("fclear"))
    bot.handle_update(api, click("fs:subject"))
    bot.handle_update(api, msg("мат"))
    assert "Математика" in api.sent[-1]["text"] or any(
        "Математика" in b["text"] for row in api.sent[-1]["keyboard"]["payload"]["buttons"] for b in row)


def test_unknown_payload_does_not_crash():
    api = FakeAPI()
    bot.safe_handle(api, click("garbage:1:2"))
    bot.safe_handle(api, click("c:no-such:x:0"))
    assert api.answers


def test_exception_in_handler_replies_to_user(monkeypatch):
    api = FakeAPI()
    monkeypatch.setattr(bot, "handle_message", lambda *a, **k: 1 / 0)
    bot.safe_handle(api, msg("привет"))
    assert "Что-то пошло не так" in api.sent[-1]["text"]


def test_legacy_favorites_migration(tmp_path):
    cat = bot.CAT
    target = cat.events[1]
    old_id = f"mp-6.77-{legacy_key(target['name'])}"      # старый формат id
    p = tmp_path / "favorites.json"
    p.write_text(json.dumps({"5": [old_id, "mp-6.1-deadbeef00"]}), encoding="utf-8")
    assert bot.STORE.import_legacy_json(p) == 2
    assert bot.STORE.remap_ids(cat) == 1
    assert bot.STORE.fav_ids(5, cat.by_id) == [target["id"]]
    assert bot.STORE.import_legacy_json(p) == 0           # повторно не импортируется


def test_chunk_text():
    parts = bot.chunk_text("a\n" * 3000, 100)
    assert all(len(x) <= 100 for x in parts)
    assert bot.chunk_text("x" * 250, 100) == ["x" * 100, "x" * 100, "x" * 50]
