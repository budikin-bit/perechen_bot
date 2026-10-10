# -*- coding: utf-8 -*-
import json
from pathlib import Path
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


def msg(text, uid=1, chat_type="dialog", chat=10):
    return {"update_type": "message_created", "message": {
        "sender": {"user_id": uid}, "recipient": {"chat_id": chat, "chat_type": chat_type},
        "body": {"text": text}}}


def click(payload, uid=1, cid="cb1", chat=10):
    return {"update_type": "message_callback", "callback": {
        "callback_id": cid, "payload": payload, "user": {"user_id": uid},
        "message": {"body": {"mid": "m1"}, "recipient": {"chat_id": chat}}}}


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

def test_stats_is_admin_only_and_anonymous(monkeypatch):
    monkeypatch.setattr(bot, "ADMIN_IDS", {7})
    api = FakeAPI()
    bot.handle_update(api, msg("химия", uid=1))
    bot.handle_update(api, msg("химия", uid=2))
    bot.handle_update(api, msg("кулинария", uid=2))
    bot.STORE.toggle(1, bot.CAT.events[0]["id"])
    bot.handle_update(api, click("fv:level:" + bot.vh("II") + ":0", uid=1))
    api.sent.clear()
    bot.handle_update(api, msg("/stats", uid=1))                  # не админ → «неизвестная команда»
    assert "Статистика" not in api.sent[-1]["text"]
    bot.handle_update(api, msg("/stats 7", uid=7))
    t = api.sent[-1]["text"]
    assert "Статистика за 7 дн." in t and "химия — 2" in t
    assert "кулинария — 1" in t and "Уровень: II" in t
    assert "user" not in " ".join(r[0] for r in bot.STORE.db.execute("SELECT q FROM qlog").fetchall()).lower()


def test_myid_command_replies_with_user_id():
    api = FakeAPI()
    bot.handle_update(api, msg("/myid", uid=424242))
    assert "424242" in api.sent[-1]["text"] and "ADMIN_IDS=424242" in api.sent[-1]["text"]


def test_db_always_inside_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "DATA_DIR", tmp_path / "data")
    for val in ("navigator.db", "sub/x.db", "/etc/other/bot.db"):
        monkeypatch.setenv("DB_PATH", val)
        p = bot._db_path()
        assert p.parent == tmp_path / "data" and p.name == Path(val).name


def test_old_db_in_project_root_is_migrated(monkeypatch, tmp_path):
    old = tmp_path / "root"; old.mkdir()
    (old / "navigator.db").write_bytes(b"data")
    monkeypatch.setattr(bot, "BASE", old)
    target = tmp_path / "data" / "navigator.db"
    bot.migrate_old_db(target)
    assert target.read_bytes() == b"data"


# ───────── регрессии по ревью ─────────

def qlog_rows():
    return bot.STORE.db.execute("SELECT kind, q, n FROM qlog").fetchall()


@pytest.mark.parametrize("q", ["!!!", "?", "😀", " … ", "«»"])
def test_queries_without_words_do_not_return_catalog(q):
    c = bot.CAT
    assert c.search(q=q) == [] and c.search(org=q) == []
    assert c.smart_search(q) == ([], False)
    api = FakeAPI()
    bot.handle_update(api, msg(q))
    assert "Напишите слово" in api.sent[-1]["text"] and "Найдено" not in api.sent[-1]["text"]
    bot.handle_update(api, msg("/org " + q))
    assert "Напишите слово" in api.sent[-1]["text"]
    bot.handle_update(api, click("sq:" + q))
    assert "Напишите слово" in api.answers[-1]["text"]
    assert qlog_rows() == []                               # в статистику не попадают


def test_await_is_bound_to_chat_where_it_was_requested():
    api = FakeAPI()
    bot.handle_update(api, click("m:search", chat=10))     # подсказка открыта в личке
    bot.handle_update(api, msg("физика", chat_type="chat", chat=20))
    assert api.sent == []                                  # в группе свободный текст не трогаем
    bot.handle_update(api, msg("физика", chat=10))
    assert "Найдено" in api.sent[-1]["text"]
    # ожидание, открытое в группе, срабатывает только в этой группе
    bot.handle_update(api, msg("/search", chat_type="chat", chat=20))
    n = len(api.sent)
    bot.handle_update(api, msg("химия", chat_type="chat", chat=30))
    assert len(api.sent) == n
    bot.handle_update(api, msg("химия", chat=10))          # в личке — обычный поиск
    assert api.sent[-1]["chat_id"] == 10 and "Найдено" in api.sent[-1]["text"]
    assert bot.sess(1).await_ == "search"                  # ожидание в группе не съедено
    bot.handle_update(api, msg("химия", chat_type="chat", chat=20))
    assert api.sent[-1]["chat_id"] == 20 and "Найдено" in api.sent[-1]["text"]
    assert bot.sess(1).await_ is None


def test_filter_text_search_await_bound_to_chat():
    api = FakeAPI()
    bot.handle_update(api, click("fs:subject", chat=10))
    bot.handle_update(api, msg("мат", chat_type="chat", chat=20))
    assert api.sent == [] and bot.sess(1).fquery == ""


class SyncPool:
    """Пул, выполняющий задачи позже по команде run_all() — для детерминированных тестов."""
    def __init__(self):
        self.tasks, self.closed = [], False

    def submit(self, fn, *a):
        if self.closed:
            raise RuntimeError("cannot schedule new futures after shutdown")
        self.tasks.append((fn, a))

    def run_all(self):
        while self.tasks:
            fn, a = self.tasks.pop(0)
            fn(*a)


def test_dispatcher_keeps_per_user_order_and_frees_memory():
    seen = []
    pool = SyncPool()
    d = bot.Dispatcher(None, pool, handler=lambda api, u: seen.append(u["n"]))
    for i in range(5):
        d.submit({**msg("x", uid=1), "n": f"a{i}"})
        d.submit({**msg("x", uid=2), "n": f"b{i}"})
    assert len(pool.tasks) == 2                            # одна задача на пользователя
    pool.run_all()
    assert [x for x in seen if x[0] == "a"] == [f"a{i}" for i in range(5)]
    assert [x for x in seen if x[0] == "b"] == [f"b{i}" for i in range(5)]
    assert d.queues == {} and d.pending == 0
    d.submit({**msg("x", uid=1), "n": "c"})               # после опустошения — новая задача
    assert len(pool.tasks) == 1
    pool.run_all()
    assert seen[-1] == "c" and d.wait_idle(0.1)


def test_dispatcher_survives_errors_and_null_user():
    seen = []

    def handler(api, u):
        seen.append(u.get("n"))
        if u.get("n") == 1:
            raise RuntimeError("boom")

    pool = SyncPool()
    d = bot.Dispatcher(None, pool, handler=handler)
    for n in (1, 2, 3):
        d.submit({**msg("x", uid=7), "n": n})
    bad = {"update_type": "message_callback", "n": 9, "callback": {"user": None, "payload": "m:menu"}}
    d.submit(bad)
    d.submit({"update_type": "x", "user": None, "message": None, "n": 10})
    pool.run_all()
    assert seen == [1, 2, 3, 9, 10] and d.pending == 0 and d.queues == {}
    assert bot.update_user(bad) is None
    bot.safe_handle(FakeAPI(), bad)                        # не падает


def test_dispatcher_with_real_threads_preserves_order():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time as _t
    seen, lock = {}, threading.Lock()

    def handler(api, u):
        _t.sleep(0.001)
        with lock:
            seen.setdefault(u["uid"], []).append(u["n"])

    pool = ThreadPoolExecutor(max_workers=4)
    d = bot.Dispatcher(None, pool, handler=handler)
    for n in range(30):
        for uid in (1, 2, 3, 4, 5, 6):
            d.submit({**msg("x", uid=uid), "uid": uid, "n": n})
    assert d.wait_idle(10)
    pool.shutdown(wait=True)
    assert all(v == list(range(30)) for v in seen.values()) and len(seen) == 6
    assert d.queues == {} and d.pending == 0


def test_dispatcher_after_pool_shutdown_does_not_strand():
    pool = SyncPool()
    pool.closed = True
    d = bot.Dispatcher(None, pool, handler=lambda a, u: None)
    d.submit(msg("x", uid=1))
    assert d.queues == {} and d.pending == 0


def test_data_dir_relative_to_base(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "BASE", tmp_path)
    monkeypatch.setenv("DATA_DIR", "data")
    assert bot._data_dir() == tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "abs"))
    assert bot._data_dir() == tmp_path / "abs"
    monkeypatch.setenv("DB_PATH", "sub/x.db")
    assert bot._db_raw() == tmp_path / "sub" / "x.db"


def test_db_redirect_warning(monkeypatch, tmp_path):
    target = tmp_path / "data" / "bot.db"
    monkeypatch.setenv("DB_PATH", "/etc/other/bot.db")
    assert bot.warn_db_redirect(target) is True
    monkeypatch.setenv("DB_PATH", "bot.db")                # только имя — штатно
    assert bot.warn_db_redirect(target) is False
    monkeypatch.setenv("DB_PATH", str(target))
    assert bot.warn_db_redirect(target) is False


def test_migration_from_custom_db_path_with_wal(monkeypatch, tmp_path):
    import sqlite3
    monkeypatch.setattr(bot, "BASE", tmp_path / "base")
    old = tmp_path / "elsewhere" / "custom.db"
    old.parent.mkdir()
    con = sqlite3.connect(str(old))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE t(x)")
    con.execute("INSERT INTO t VALUES(42)")
    con.commit()
    assert Path(str(old) + "-wal").exists()               # данные пока только в WAL
    monkeypatch.setenv("DB_PATH", str(old))
    target = tmp_path / "data" / "custom.db"
    assert bot.migrate_old_db(target) == old
    con.close()
    con2 = sqlite3.connect(str(target))
    assert con2.execute("SELECT x FROM t").fetchall() == [(42,)]
    con2.close()


def test_migration_search_order(monkeypatch, tmp_path):
    base = tmp_path / "base"; base.mkdir()
    monkeypatch.setattr(bot, "BASE", base)
    monkeypatch.setenv("DB_PATH", "rel/my.db")
    (base / "navigator.db").write_bytes(b"legacy")
    target = tmp_path / "data" / "my.db"
    assert bot.migrate_old_db(target) == base / "navigator.db"
    target.unlink()
    (base / "my.db").write_bytes(b"byname")
    assert bot.migrate_old_db(target) == base / "my.db"
    target.unlink()
    (base / "rel").mkdir()
    (base / "rel" / "my.db").write_bytes(b"asgiven")
    assert bot.migrate_old_db(target) == base / "rel" / "my.db"
    assert target.read_bytes() == b"asgiven"
    assert bot.migrate_old_db(target) is None             # цель уже есть — ничего не трогаем


def test_stats_cyrillic_case_and_days(monkeypatch):
    monkeypatch.setattr(bot, "ADMIN_IDS", {7})
    api = FakeAPI()
    for q in ("Химия", "химия", "  ХИМИЯ  "):
        bot.handle_update(api, msg(q, uid=2))
    assert {r[1] for r in qlog_rows()} == {"химия"}
    for arg, days in (("²", 30), ("7", 7), ("0", 30), ("366", 30), ("abc", 30), ("365", 365), ("", 30)):
        assert bot.stats_days(arg) == days
    bot.handle_update(api, msg("/stats ²", uid=7))         # раньше — исключение int("²")
    assert "Статистика за 30 дн." in api.sent[-1]["text"] and "химия — 3" in api.sent[-1]["text"]


def test_stats_in_group_goes_to_private(monkeypatch):
    monkeypatch.setattr(bot, "ADMIN_IDS", {7})
    api = FakeAPI()
    bot.handle_update(api, msg("/stats", uid=7, chat_type="chat", chat=20))
    priv = [x for x in api.sent if x.get("user_id") == 7]
    grp = [x for x in api.sent if x.get("chat_id") == 20]
    assert priv and "Статистика за" in priv[0]["text"]
    assert grp and "Статистика за" not in grp[0]["text"] and "личн" in grp[0]["text"]


def test_qlog_retention():
    bot.STORE.db.execute("INSERT INTO qlog VALUES(?,?,?,?)", (1.0, "search", "старое", 1))
    bot.STORE.log_query("search", "новое", 1)
    assert bot.STORE.purge_qlog(180) == 1
    assert [r[1] for r in qlog_rows()] == ["новое"]


def test_chunk_text_no_empty_chunks():
    assert bot.chunk_text("x" * 3900) == ["x" * 3900]
    assert bot.chunk_text("x" * 100, 100) == ["x" * 100]
    for text in ("a\n" * 3000, "\n\n\n" + "y" * 250 + "\n\n", "ab\n" + "z" * 99 + "\n" + "q" * 100):
        parts = bot.chunk_text(text, 100)
        assert parts and all(p and len(p) <= 100 for p in parts)


def test_detail_pages_fit_with_long_header_and_footer():
    e = dict(bot.CAT.events[5])
    e["source"] = "И" * 700
    e["name"] = "Н" * 400
    pages = bot.detail_pages(e)
    assert len(pages) > 1 and all(len(p) <= bot.MAX_LEN for p in pages)


def test_detail_pages_organizer_only_when_present():
    e = dict(bot.CAT.events[0])
    e["entries"] = [{"profile": "Химия", "organizer": "  "},
                    {"profile": "Физика", "organizer": "ФГАОУ ВО «Университет ИТМО»"}]
    page = bot.detail_pages(e)[0]
    assert "• Профиль: Химия\n" in page and "| \n" not in page
    assert "Профиль: Физика | Организатор:" in page


def test_real_catalog_messages_fit_max_len(tmp_path):
    p = Path(bot.__file__).resolve().parent / "catalog.official.json"
    if not p.exists():
        return
    bot.init(json.loads(p.read_text(encoding="utf-8")), tmp_path / "real.db")
    assert len(bot.CAT.events) > 1000
    for e in bot.CAT.events:
        assert len(bot.card_text(e)) <= bot.MAX_LEN, e["id"]
        for page in bot.detail_pages(e):
            assert len(page) <= bot.MAX_LEN, e["id"]


def test_interest_is_visible_and_narrows_filter_options():
    api = FakeAPI()
    idx = bot.CAT.interests.index("Химия и материалы")
    bot.handle_update(api, click(f"i:{idx}"))
    bot.handle_update(api, click("m:filters"))
    t, kbd = api.answers[-1]["text"], api.answers[-1]["keyboard"]
    assert "🎯 Интерес: Химия и материалы" in t and "fx:interest" in payloads(kbd)
    bot.handle_update(api, click("fk:subject"))
    opts = [p for p in payloads(api.answers[-1]["keyboard"]) if p.startswith("fv:")]
    assert opts == [f"fv:subject:{bot.vh('Химия')}:0"]       # нет значений, дающих пустоту
    assert bot.CAT.options("subject", {}, interest="Химия и материалы") == ["Химия"]
    bot.handle_update(api, click("fx:interest"))
    assert bot.sess(1).interest == "" and "Интерес" not in api.answers[-1]["text"]
    bot.handle_update(api, click(f"i:{idx}"))
    bot.handle_update(api, click("fclear"))
    assert bot.sess(1).interest == "" and bot.sess(1).filters == {}


def test_view_ids_random_and_bound_to_owner():
    a, b = bot.Views(), bot.Views()
    assert a.boot != b.boot and len(a.boot) == 7
    api = FakeAPI()
    bot.handle_update(api, msg("олимпиада", uid=1))
    vid = next(p for p in last_list_payloads(api) if p.startswith("c:")).split(":")[2]
    assert bot.VIEWS.get(vid, 1) and bot.VIEWS.get(vid, 2) is None and bot.VIEWS.get(vid) is None
    assert "устарел" in bot.render_list(vid, 0, 2)[0]
    bot.handle_update(api, click(f"l:{vid}:0", uid=2, cid="x"))
    assert api.answers[-1]["text"] is None and "чужой" in api.answers[-1]["notification"]
    eid = bot.CAT.events[0]["id"]
    bot.handle_update(api, click(f"c:{eid}:{vid}:0", uid=2))  # карточка открывается, но без «К списку»
    assert not any(p.startswith("l:") for p in payloads(api.answers[-1]["keyboard"]))
    bot.handle_update(api, click(f"l:{vid}:0", uid=1))
    assert "Найдено" in api.answers[-1]["text"]


def test_qlog_masks_phones_and_emails():
    n = bot.Store.norm_query
    assert n("Иванов +7 (914) 123-45-67") == "иванов <номер>"
    assert n("пишите a.b@mail.ru") == "пишите <email>"
    assert n("олимпиада 2026/27") == "олимпиада 2026/27"      # годы и номера мероприятий не трогаем
    assert n("6.345") == "6.345"
