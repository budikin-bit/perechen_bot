#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот «Навигатор перечневых мероприятий 2026/27» для мессенджера МАХ.

Запуск:
    pip install -r requirements.txt
    python bot.py

Настройки — в .env (см. .env.example). Токен: MAX_BOT_TOKEN.

Источники каталога (первый найденный; CATALOG_PATH, если задан, — первым):
    catalog.official.json → catalog.projects.json → catalog.json → navigator.html

Что важно знать при доработке:
  • Кнопки «stateless»: в payload лежит id мероприятия и id «выдачи» (view),
    а не индекс в сессии. Старые сообщения в чате не ломают новые.
  • Выдачи (VIEWS) живут в памяти; после перезапуска старые списки сообщают,
    что устарели, но карточки и избранное продолжают работать.
  • Избранное и анонимный лог запросов — в SQLite (navigator.db).
  • Сертификаты Минцифры подключаются только к сессии MAX (verify=…),
    глобальные переменные окружения не трогаются.
Остановка: Ctrl+C.
"""
from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
import urllib.request
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from common import (
    WORD_RE, norm_name, org_list, plural, query_stems, short_org, stem,
    stem_hit, stem_index,
)

BASE = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
    load_dotenv()
except ImportError:
    pass

# ────────────────────────────── настройки ──────────────────────────────

BOT_TOKEN = os.environ.get("MAX_BOT_TOKEN", "").strip()
API_BASE = os.environ.get("MAX_API_BASE", "https://platform-api2.max.ru").rstrip("/")


def _path(env: str, default: str) -> Path:
    p = Path(os.environ.get(env, default))
    return p if p.is_absolute() else BASE / p


CATALOG_PATH = _path("CATALOG_PATH", "catalog.json")
HTML_PATH = _path("HTML_PATH", "navigator.html")
# Данные, которые должны переживать обновление кода из Git: на bothost.ru это /app/data
# (папка не входит в синхронизацию с Git). Иначе — рядом с bot.py. Переопределяется DB_PATH / DATA_DIR.
DATA_DIR = Path(os.environ.get("DATA_DIR") or ("/app/data" if Path("/app/data").is_dir() else BASE))


def _db_path() -> Path:
    """Все базы — только в DATA_DIR. Относительный или внешний DB_PATH из окружения сводится к имени
    файла внутри DATA_DIR (раньше «DB_PATH=navigator.db» создавал базу в корне проекта)."""
    name = Path(os.environ.get("DB_PATH") or "navigator.db").name or "navigator.db"
    return DATA_DIR / name


DB_PATH = _db_path()
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ADMIN_IDS", "")) if x.strip().isdigit()}
LEGACY_FAV_PATH = _path("FAVORITES_PATH", "favorites.json")
CERT_DIR = _path("CERT_DIR", str(DATA_DIR / "certs" / "russian-trusted"))
ROOT_CA = "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt"
SUB_CA = "https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt"

PAGE_SIZE = 5
OPT_PAGE = 8
MAX_LEN = 3900
POLL_TIMEOUT = 30
WORKERS = int(os.environ.get("WORKERS", "4"))
UPDATE_TYPES = "message_created,message_callback,bot_started"
MAX_VIEWS = 3000
MAX_SESSIONS = 5000

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger("max-navigator")


# ─────────────────── сертификаты Минцифры (только для MAX) ───────────────────

def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "max-navigator-bot/2.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    if b"BEGIN CERTIFICATE" not in data:
        raise RuntimeError(f"Не сертификат: {url}")
    return data if data.endswith(b"\n") else data + b"\n"


def _system_bundle() -> bytes:
    for path in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem",
                 "/etc/pki/tls/certs/ca-bundle.crt"):
        p = Path(path)
        if p.is_file():
            d = p.read_bytes()
            return d if d.endswith(b"\n") else d + b"\n"
    try:
        import certifi
        d = Path(certifi.where()).read_bytes()
        return d if d.endswith(b"\n") else d + b"\n"
    except Exception:  # noqa: BLE001
        return b""


def prepare_ca_bundle(max_age_days: int = 30) -> Optional[str]:
    """
    Путь к bundle (системные CA + российские) или None.
    Файл кэшируется; при старте качаем заново только если он старше max_age_days.
    Необязательная защита: ROOT_CA_SHA256 / SUB_CA_SHA256 в .env — хэши
    файлов сертификатов; при несовпадении bundle не используется.
    """
    bundle = CERT_DIR / "ca-bundle.crt"
    fresh = bundle.exists() and time.time() - bundle.stat().st_mtime < max_age_days * 86400
    if fresh:
        return str(bundle)
    try:
        CERT_DIR.mkdir(parents=True, exist_ok=True)
        root, sub = _download(ROOT_CA), _download(SUB_CA)
        for data, env in ((root, "ROOT_CA_SHA256"), (sub, "SUB_CA_SHA256")):
            pin = os.environ.get(env, "").strip().lower()
            if pin and hashlib.sha256(data).hexdigest() != pin:
                raise RuntimeError(f"{env}: хэш сертификата не совпал")
        system = _system_bundle()
        if not system:
            LOG.warning("Системный CA-bundle не найден — только российские CA")
        bundle.write_bytes(system + root + sub)
        return str(bundle)
    except Exception as ex:  # noqa: BLE001
        LOG.warning("Не удалось обновить сертификаты Минцифры: %s", ex)
        return str(bundle) if bundle.exists() else None


# ────────────────────────── текстовые утилиты ──────────────────────────

def clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def chunk_text(text: str, limit: int = MAX_LEN) -> List[str]:
    """Режет по строкам; слишком длинную строку режет жёстко."""
    out: List[str] = []
    buf = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if buf:
                out.append(buf.rstrip())
                buf = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) + 1 > limit:
            out.append(buf.rstrip())
            buf = ""
        buf += line + "\n"
    if buf.strip():
        out.append(buf.rstrip())
    return out or [""]


def b36(n: int) -> str:
    d = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = ""
    while True:
        n, r = divmod(n, 36)
        s = d[r] + s
        if n == 0:
            return s


def vh(v: str) -> str:
    """Короткий хэш значения фильтра для payload."""
    return hashlib.md5(v.encode("utf-8")).hexdigest()[:6]


def to_int(x: str, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


# ─────────────────────────── извлечение каталога ───────────────────────────

def extract_from_html(src: str) -> dict:
    key = "const CATALOG="
    i = src.find(key)
    if i < 0:
        raise ValueError("в HTML не найден фрагмент 'const CATALOG='")
    i += len(key)
    depth, in_str, esc = 0, False, False
    for j in range(i, len(src)):
        ch = src[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(src[i: j + 1])
    raise ValueError("объект CATALOG не закрыт")


def ensure_catalog() -> dict:
    candidates: List[Path] = []
    if os.environ.get("CATALOG_PATH"):
        candidates.append(CATALOG_PATH)
    candidates += [BASE / "catalog.official.json", BASE / "catalog.projects.json", CATALOG_PATH]
    for path in candidates:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            LOG.info("Каталог %s: вариант %s, событий %d", path.name,
                     data.get("variant", "unknown"), len(data.get("events", [])))
            if data.get("variant") == "projects":
                LOG.warning("ВНИМАНИЕ: загружен ПРОЕКТНЫЙ каталог %s — утверждённый catalog.official.json "
                            "не найден или в .env задан CATALOG_PATH на проектный.", path.name)
            return data
    if not HTML_PATH.exists():
        raise SystemExit(
            "Не найден каталог: ни catalog.official.json, ни catalog.projects.json, "
            f"ни {CATALOG_PATH.name}, ни {HTML_PATH.name}.\n"
            "Соберите его: python build_catalog.py")
    LOG.info("Извлекаю каталог из %s …", HTML_PATH)
    data = extract_from_html(HTML_PATH.read_text(encoding="utf-8"))
    CATALOG_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


# ──────────────────────────────── каталог ────────────────────────────────

FILTER_KEYS = ["type", "level", "subject", "section", "direction", "profile", "study_field", "source"]
LEVEL_ORDER = ["Высший", "I", "II", "III", "IV", "Не установлен"]
FILTER_TITLES = {
    "type": "Тип мероприятия",
    "level": "Уровень",
    "subject": "Предмет",
    "section": "Раздел приказа",
    "direction": "Направление",
    "profile": "Профиль",
    "study_field": "УГСН / предмет олимпиады (Минобр)",
    "source": "Источник (министерство)",
}
# как получить значения фильтра из мероприятия
_GETTERS = {
    "type": lambda e: [e["type"]] if e.get("type") else [],
    "level": lambda e: e.get("levels") or [],
    "subject": lambda e: e.get("subjects") or [],
    "section": lambda e: [e["section_title"]] if e.get("section_title") else [],
    "direction": lambda e: e.get("directions") or [],
    "profile": lambda e: e.get("profiles") or [],
    "study_field": lambda e: e.get("study_fields") or [],
    "source": lambda e: [e["ministry"]] if e.get("ministry") else [],
}


class Catalog:
    def __init__(self, data: dict):
        self.data = data
        self.sources = data.get("sources", {})
        self.events: List[dict] = data.get("events", [])
        self.academic_year = data.get("academic_year", "2026/27")
        self.variant = data.get("variant", "unknown")
        self.by_id: Dict[str, dict] = {}
        self.by_legacy: Dict[str, str] = {}
        self.by_old: Dict[str, str] = {}

        for e in self.events:
            s = self.sources.get(e.get("source_code"), {})
            e["ministry"] = s.get("ministry", "")
            e["source"] = s.get("source", "")
            prof_txt = " ".join([*e.get("profiles", []), *e.get("study_fields", []),
                                 *e.get("subjects", []), *e.get("directions", []),
                                 *e.get("interests", [])])
            org_txt = " ".join([e.get("organizer_raw", ""), *e.get("organizer_aliases", [])])
            meta_txt = " ".join([e.get("type", ""), e.get("ministry", ""), *e.get("levels", [])])
            e["_n"] = stem_index(e.get("name", ""))
            e["_p"] = stem_index(prof_txt)
            e["_o"] = stem_index(org_txt)
            e["_all"] = e["_n"] + e["_p"] + e["_o"] + stem_index(meta_txt)
            self.by_id[e["id"]] = e
            if e.get("legacy_key"):
                self.by_legacy.setdefault(e["legacy_key"], e["id"])
            for old in e.get("old_ids") or []:
                self.by_old.setdefault(old, e["id"])

        self.interests = sorted({i for e in self.events for i in (e.get("interests") or [])},
                                key=lambda s: s.lower())
        cnt: Dict[str, int] = defaultdict(int)
        for e in self.events:
            for i in e.get("interests") or []:
                cnt[i] += 1
        self.interest_counts = dict(cnt)
        self._blob = " ".join(e["_all"] for e in self.events)
        self._vocab: Optional[List[str]] = None

    # ── фильтры ──
    @staticmethod
    def _match_filters(e: dict, f: Dict[str, str]) -> bool:
        for k, v in f.items():
            if v and k in _GETTERS and v not in _GETTERS[k](e):
                return False
        return True

    def options(self, key: str, filters: Dict[str, str], contains: str = "") -> List[str]:
        others = {k: v for k, v in filters.items() if k != key and v}
        vals = set()
        for e in self.events:
            if self._match_filters(e, others):
                vals.update(_GETTERS[key](e))
        if key == "level":
            out = sorted(vals, key=lambda s: (LEVEL_ORDER.index(s) if s in LEVEL_ORDER else 99, s))
        else:
            out = sorted(vals, key=lambda s: s.lower())
        if contains.strip():
            q = norm_name(contains)
            out = [v for v in out if q in norm_name(v)]
        return out

    # ── поиск ──
    def search(self, q: str = "", org: str = "", interest: str = "",
               filters: Optional[Dict[str, str]] = None) -> List[dict]:
        """Строгий поиск (все слова запроса), результаты ранжированы."""
        filters = filters or {}
        qs = query_stems(q) if q.strip() else []
        os_ = query_stems(org) if org.strip() else []
        scored = []
        for idx, e in enumerate(self.events):
            if interest and interest not in (e.get("interests") or []):
                continue
            if not self._match_filters(e, filters):
                continue
            if os_ and not all(stem_hit(e["_o"], t) for t in os_):
                continue
            score, ok = 0, True
            for t in qs:
                if stem_hit(e["_n"], t):
                    score += 3
                elif stem_hit(e["_p"], t):
                    score += 2
                elif stem_hit(e["_all"], t):
                    score += 1
                else:
                    ok = False
                    break
            if ok:
                scored.append((-score, idx, e))
        scored.sort(key=lambda x: (x[0], x[1]))
        return [e for _, _, e in scored]

    def smart_search(self, q: str, filters: Optional[Dict[str, str]] = None
                     ) -> Tuple[List[dict], bool]:
        """Строгий поиск; если пусто и слов ≥ 2 — близкие (без одного слова)."""
        res = self.search(q=q, filters=filters)
        if res:
            return res, False
        qs = query_stems(q)
        if len(qs) < 2:
            return [], False
        filters = filters or {}
        need = len(qs) - 1
        scored = []
        for idx, e in enumerate(self.events):
            if not self._match_filters(e, filters):
                continue
            hits = sum(1 for t in qs if stem_hit(e["_all"], t))
            if hits >= need:
                name_hits = sum(1 for t in qs if stem_hit(e["_n"], t))
                scored.append((-hits, -name_hits, idx, e))
        scored.sort(key=lambda x: x[:3])
        return [x[3] for x in scored], True

    def suggest(self, q: str) -> str:
        """Исправление опечаток: заменяет слова без совпадений ближайшими из каталога."""
        if self._vocab is None:
            self._vocab = sorted({w for e in self.events
                                  for w in WORD_RE.findall(norm_name(
                                      e.get("name", "") + " " + " ".join(e.get("profiles", []))))
                                  if len(w) >= 4})
        words = WORD_RE.findall(norm_name(q))
        changed, out = False, []
        for w in words:
            if len(w) >= 4 and not stem_hit(self._blob, stem(w)):
                m = difflib.get_close_matches(w, self._vocab, n=1, cutoff=0.78)
                if m:
                    out.append(m[0])
                    changed = True
                    continue
            out.append(w)
        return " ".join(out) if changed else ""


# ─────────────────────── представление мероприятия ───────────────────────

def org_line(e: dict) -> str:
    parts = org_list(e.get("organizer_raw"))
    if not parts:
        return "—"
    head = short_org(parts[0]) or "—"
    rest = len(parts) - 1
    if rest > 0:
        return f"{head} и ещё {rest} {plural(rest, 'организация', 'организации', 'организаций')}"
    return head


def level_label(x: str) -> str:
    return ("Уровень " + x) if x in ("I", "II", "III", "IV") else x


def fmt_levels(e: dict) -> str:
    return " · ".join(level_label(x) for x in e.get("levels") or [])


def levels_block(e: dict) -> List[str]:
    """Если у профилей разные уровни — показываем, какой профиль на каком уровне."""
    lv = e.get("levels") or []
    if len(lv) <= 1:
        s = fmt_levels(e)
        return ["🎖 " + s] if s else []
    groups: Dict[str, List[str]] = {}
    for en in e.get("entries") or []:
        if en.get("level") and en.get("profile"):
            groups.setdefault(en["level"], []).append(en["profile"])
    if not groups:
        return ["🎖 " + fmt_levels(e)]
    out = []
    for level in lv:
        names = groups.get(level, [])
        label = level_label(level)
        if names:
            head = "; ".join(names[:3]) + (f" и ещё {len(names) - 3}" if len(names) > 3 else "")
            out.append(f"🎖 {label}: {head}")
        else:
            out.append(f"🎖 {label}")
    return out


def class_range(cl: List[int]) -> str:
    if not cl:
        return ""
    a = sorted(cl)
    if len(a) > 1 and all(a[i] == a[i - 1] + 1 for i in range(1, len(a))):
        return f"{a[0]}–{a[-1]}"
    return ", ".join(str(x) for x in a)


def catalog_status() -> str:
    if CAT.variant == "projects":
        return ("📄 База собрана по проектам приказов Минобрнауки и Минпросвещения "
                "на 2026/27 учебный год.\n"
                "После официального утверждения приказов база будет сверена и обновлена.")
    if CAT.variant == "official":
        parts = [f"✅ База собрана по утверждённым приказам на 2026/27 учебный год:"]
        for src in CAT.sources.values():
            if src.get("source"):
                parts.append("• " + src["source"])
        return "\n".join(parts)
    return ""


def card_text(e: dict) -> str:
    L: List[str] = ["🏆 " + e["name"], ""]
    L.append("📚 " + e.get("ministry", "") + (" · " + e["type"] if e.get("type") else ""))
    if CAT.variant == "projects":
        L.append("📄 Проект приказа (2026/27)")
    elif CAT.variant == "official":
        num = e.get("official_number")
        L.append("✅ Утверждённый приказ" + (f" · № {num}" if num else ""))
        if e.get("section_title"):
            L.append("📑 " + e["section_title"])
    L.extend(levels_block(e))
    if e.get("in_both"):
        L.append("🔗 Входит в оба перечня")
    L.append("")
    if e.get("subjects"):
        L.append("📖 Предметы: " + ", ".join(e["subjects"]))
    if e.get("directions"):
        L.append("🧭 Направление: " + ", ".join(e["directions"]))
    pf = e.get("profiles") or []
    if pf:
        head = "; ".join(pf[:5]) + (f" и ещё {len(pf) - 5}" if len(pf) > 5 else "")
        L.append("🎯 Профили: " + head)
    sf = e.get("study_fields") or []
    if sf:
        head = "; ".join(sf[:4]) + (f" и ещё {len(sf) - 4}" if len(sf) > 4 else "")
        L.append("🎓 Направления вуза: " + head)
    if e.get("classes"):
        L.append("👥 Классы: " + class_range(e["classes"]))
    L.append("")
    L.append("🏛 Организатор: " + org_line(e))
    if e.get("interests"):
        L.append("")
        L.append("🏷 " + " · ".join(e["interests"]))
    return "\n".join(L)


def detail_pages(e: dict, limit: int = 3300) -> List[str]:
    """Официальные данные, разбитые на страницы по длине текста."""
    lines: List[str] = []
    for r in e.get("entries") or []:
        parts = []
        if r.get("direction"):
            parts.append("Направление: " + str(r["direction"]))
        if r.get("profile"):
            parts.append("Профиль: " + str(r["profile"]))
        if r.get("study_field"):
            parts.append("Направление вуза: " + str(r["study_field"]))
        if r.get("level"):
            parts.append("Уровень: " + str(r["level"]))
        if r.get("organizer"):
            parts.append("Организатор: " + short_org(org_list(r["organizer"])[0]) if org_list(r["organizer"]) else "")
        if parts:
            lines.append("• " + " | ".join(parts))
    footer = ("\n\nИсточник: " + e["source"]) if e.get("source") else ""
    chunks: List[List[str]] = [[]]
    size = 0
    for ln in lines:
        ln = clip(ln, 600)
        if size + len(ln) + 1 > limit and chunks[-1]:
            chunks.append([])
            size = 0
        chunks[-1].append(ln)
        size += len(ln) + 1
    n = len(chunks)
    out = []
    for i, ch in enumerate(chunks):
        num = f" № {e['official_number']}" if e.get("official_number") and CAT.variant == "official" else ""
        head = f"📋 Официальные данные{num} · стр. {i + 1}/{n}\n\n🏆 {e['name']}\n\n"
        out.append(head + "\n".join(ch) + footer)
    return out


# ──────────────────────────────── MAX API ────────────────────────────────

class MaxAPI:
    """Клиент API MAX. Авторизация — заголовок Authorization (токен в query нельзя)."""

    def __init__(self, token: str, base: str = API_BASE, verify: Any = True):
        self.token, self.base, self.verify = token, base, verify
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": "max-navigator-bot/2.0", "Authorization": token})
        retry = Retry(total=3, backoff_factor=1, status_forcelist=(500, 502, 503, 504),
                      allowed_methods=frozenset({"GET", "PUT", "PATCH", "DELETE"}),
                      respect_retry_after_header=True)
        self.http.mount("https://", HTTPAdapter(max_retries=retry))
        self.http.mount("http://", HTTPAdapter(max_retries=retry))

    def _request(self, method: str, path: str, params: Optional[dict] = None,
                 body: Optional[dict] = None, timeout: int = 60):
        url = f"{self.base}/{path}"
        p = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(4):
            r = self.http.request(method, url, params=p, timeout=timeout, verify=self.verify,
                                  json=(body if body is not None else {}) if method != "GET" else None)
            if r.status_code == 429 and attempt < 3:      # лимит: ждём и повторяем
                time.sleep(min(float(r.headers.get("Retry-After", 1 + attempt)), 10))
                continue
            r.raise_for_status()
            return r.json() if r.content else {}
        return {}

    def get(self, method: str, timeout: int = 60, **params):
        return self._request("GET", method, params, timeout=timeout)

    def post(self, method: str, body: Optional[dict] = None, **params):
        return self._request("POST", method, params, body)

    def put(self, method: str, body: Optional[dict] = None, **params):
        return self._request("PUT", method, params, body)

    def patch(self, method: str, body: Optional[dict] = None, **params):
        return self._request("PATCH", method, params, body)

    def get_updates(self, marker: Optional[int] = None, timeout: int = POLL_TIMEOUT,
                    limit: int = 100):
        return self._request("GET", "updates", {"timeout": timeout, "limit": limit,
                                                "marker": marker, "types": UPDATE_TYPES},
                             timeout=timeout + 30)

    def send(self, text: str = "", keyboard: Optional[dict] = None,
             user_id: Optional[int] = None, chat_id: Optional[int] = None,
             attachments: Optional[List[dict]] = None, notify: bool = True):
        body: Dict[str, Any] = {"text": text[:MAX_LEN], "notify": notify}
        atts: List[dict] = ([keyboard] if keyboard else []) + list(attachments or [])
        if atts:
            body["attachments"] = atts
        params = {"chat_id": chat_id} if chat_id is not None else (
            {"user_id": user_id} if user_id is not None else {})
        for attempt in range(5):                          # файл может быть ещё не обработан
            try:
                return self.post("messages", body, **params)
            except requests.HTTPError as ex:
                txt = (ex.response.text or "") if ex.response is not None else ""
                if attachments and "not.ready" in txt and attempt < 4:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
        return {}

    def edit(self, message_id: str, text: str, keyboard: Optional[dict] = None):
        return self.put("messages", {"text": text[:MAX_LEN],
                                     "attachments": [keyboard] if keyboard else []},
                        message_id=message_id)

    def answer_callback(self, callback_id: str, notification: Optional[str] = None,
                        text: Optional[str] = None, keyboard: Optional[dict] = None):
        body: Dict[str, Any] = {}
        if notification:
            body["notification"] = notification
        if text is not None:
            m: Dict[str, Any] = {"text": text[:MAX_LEN]}
            if keyboard:
                m["attachments"] = [keyboard]
            body["message"] = m
        return self.post("answers", body, callback_id=callback_id)

    def upload_file(self, filename: str, content: bytes) -> Optional[str]:
        try:
            r = self.post("uploads", {}, type="file")
            url = r.get("url")
            if not url:
                return None
            # адрес загрузки — presigned, без Authorization
            resp = requests.post(url, files={"data": (filename, content)}, timeout=120,
                                 verify=self.verify)
            resp.raise_for_status()
            j = resp.json()
            return j.get("token") or (j.get("file") or {}).get("token")
        except Exception as ex:  # noqa: BLE001
            LOG.warning("upload_file failed: %s", ex)
            return None

    def set_my_commands(self, commands: List[dict]) -> dict:
        return self.patch("me/commands", {"commands": commands})


# ────────────────────────────── клавиатуры ──────────────────────────────

def kb(rows: List[List[Tuple]]) -> dict:
    buttons: List[List[dict]] = []
    for row in rows:
        r = []
        for b in row:
            if not b:
                continue
            if len(b) >= 3 and b[2] == "link":
                r.append({"type": "link", "text": b[0], "url": b[1]})
            else:
                r.append({"type": "callback", "text": b[0], "payload": b[1]})
        if r:
            buttons.append(r)
    return {"type": "inline_keyboard", "payload": {"buttons": buttons}}


def ui_update(api: MaxAPI, cb: dict, text: str, keyboard: Optional[dict] = None,
              notification: Optional[str] = None) -> None:
    """Обновляет сообщение с кнопкой одним ответом на callback (+ всплывающее уведомление)."""
    cb_id = cb.get("callback_id")
    msg = cb.get("message") or {}
    mid = ((msg.get("body") or {}).get("mid")) or None
    if cb_id:
        try:
            api.answer_callback(cb_id, text=text, keyboard=keyboard, notification=notification)
            return
        except Exception as ex:  # noqa: BLE001
            LOG.warning("answer_callback failed: %s", ex)
    if mid:
        try:
            api.edit(mid, text, keyboard)
            return
        except Exception as ex:  # noqa: BLE001
            LOG.warning("edit failed: %s", ex)
    uid = (cb.get("user") or {}).get("user_id")
    chat_id = (msg.get("recipient") or {}).get("chat_id")
    if chat_id:
        api.send(chat_id=chat_id, text=text, keyboard=keyboard)
    elif uid:
        api.send(user_id=uid, text=text, keyboard=keyboard)


# ─────────────────────────────── состояние ───────────────────────────────

class Store:
    """SQLite: избранное, пользователи, анонимный лог запросов."""

    def __init__(self, path):
        self.lock = threading.RLock()
        if not isinstance(path, str) or path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        LOG.info("База данных: %s", path)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS favs(
                user_id INTEGER NOT NULL, event_id TEXT NOT NULL, added_at REAL NOT NULL,
                PRIMARY KEY(user_id, event_id));
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY, first_seen REAL, last_seen REAL);
            CREATE TABLE IF NOT EXISTS qlog(ts REAL, kind TEXT, q TEXT, n INTEGER);
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        """)

    def _q(self, sql: str, args: tuple = ()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def touch(self, uid: int) -> None:
        now = time.time()
        self._q("INSERT INTO users(user_id, first_seen, last_seen) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen", (uid, now, now))

    def fav_ids(self, uid: int, valid: Optional[Dict[str, Any]] = None) -> List[str]:
        with self.lock:
            rows = self.db.execute("SELECT event_id FROM favs WHERE user_id=? "
                                   "ORDER BY added_at, rowid", (uid,)).fetchall()
        ids = [r[0] for r in rows]
        return [i for i in ids if i in valid] if valid is not None else ids

    def is_fav(self, uid: int, eid: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM favs WHERE user_id=? AND event_id=?",
                                   (uid, eid)).fetchone() is not None

    def toggle(self, uid: int, eid: str) -> bool:
        with self.lock:
            if self.is_fav(uid, eid):
                self._q("DELETE FROM favs WHERE user_id=? AND event_id=?", (uid, eid))
                return False
            self._q("INSERT OR IGNORE INTO favs VALUES(?,?,?)", (uid, eid, time.time()))
            return True

    def remove(self, uid: int, eid: str) -> None:
        self._q("DELETE FROM favs WHERE user_id=? AND event_id=?", (uid, eid))

    def log_query(self, kind: str, q: str, n: int) -> None:
        """Анонимно: без user_id. Нужен, чтобы видеть запросы без результатов."""
        self._q("INSERT INTO qlog VALUES(?,?,?,?)", (time.time(), kind, q[:200], n))

    def stats_data(self, days: int = 30) -> dict:
        """Агрегаты для /stats. Все данные анонимные: в qlog нет user_id."""
        since = time.time() - days * 86400
        with self.lock:
            one = lambda sql, a=(): self.db.execute(sql, a).fetchone()[0]
            allq = lambda sql, a=(): self.db.execute(sql, a).fetchall()
            return {
                "days": days,
                "users_total": one("SELECT COUNT(*) FROM users"),
                "users_active": one("SELECT COUNT(*) FROM users WHERE last_seen>=?", (since,)),
                "users_new": one("SELECT COUNT(*) FROM users WHERE first_seen>=?", (since,)),
                "queries": one("SELECT COUNT(*) FROM qlog WHERE kind IN ('search','org') AND ts>=?", (since,)),
                "zero": one("SELECT COUNT(*) FROM qlog WHERE kind IN ('search','org') AND n=0 AND ts>=?", (since,)),
                "top": allq("SELECT lower(q), COUNT(*) c FROM qlog WHERE kind IN ('search','org') AND n>0 AND ts>=? "
                            "GROUP BY lower(q) ORDER BY c DESC, q LIMIT 10", (since,)),
                "top_zero": allq("SELECT lower(q), COUNT(*) c FROM qlog WHERE kind IN ('search','org') AND n=0 AND ts>=? "
                                 "GROUP BY lower(q) ORDER BY c DESC, q LIMIT 10", (since,)),
                "filters": allq("SELECT q, COUNT(*) c FROM qlog WHERE kind='filter' AND ts>=? "
                                "GROUP BY q ORDER BY c DESC, q LIMIT 10", (since,)),
                "filter_keys": allq("SELECT substr(q,1,instr(q,'=')-1), COUNT(*) c FROM qlog WHERE kind='filter' AND ts>=? "
                                    "GROUP BY 1 ORDER BY c DESC", (since,)),
                "fav_top": allq("SELECT event_id, COUNT(*) c FROM favs GROUP BY event_id ORDER BY c DESC LIMIT 5"),
                "fav_users": one("SELECT COUNT(DISTINCT user_id) FROM favs"),
            }

    def import_legacy_json(self, path: Path) -> int:
        """Разовый перенос favorites.json → SQLite."""
        with self.lock:
            done = self.db.execute("SELECT v FROM meta WHERE k='favs_json_imported'").fetchone()
            if done or not path.exists():
                return 0
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as ex:  # noqa: BLE001
                LOG.warning("favorites.json не прочитан: %s", ex)
                return 0
            n, t = 0, time.time()
            for uid, ids in data.items():
                for k, eid in enumerate(ids):
                    self.db.execute("INSERT OR IGNORE INTO favs VALUES(?,?,?)",
                                    (int(uid), eid, t + k * 1e-3))
                    n += 1
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('favs_json_imported', ?)", (str(t),))
            self.db.commit()
            LOG.info("Избранное перенесено из %s: %d записей", path.name, n)
            return n

    def remap_ids(self, catalog: Catalog) -> int:
        """Старые id → новые (old_ids из сборки или legacy_key); id, которых нет в каталоге, не трогаем."""
        moved = 0
        with self.lock:
            rows = self.db.execute("SELECT DISTINCT event_id FROM favs").fetchall()
            for (eid,) in rows:
                if eid in catalog.by_id:
                    continue
                new = catalog.by_old.get(eid) or catalog.by_legacy.get(eid.rsplit("-", 1)[-1])
                if new and new != eid:
                    self.db.execute("UPDATE OR IGNORE favs SET event_id=? WHERE event_id=?", (new, eid))
                    self.db.execute("DELETE FROM favs WHERE event_id=?", (eid,))
                    moved += 1
            self.db.commit()
        if moved:
            LOG.info("Избранное: обновлено id: %d", moved)
        return moved


@dataclass
class Session:
    await_: Optional[str] = None
    filters: Dict[str, str] = field(default_factory=dict)
    interest: str = ""
    fquery: str = ""


class LRU:
    def __init__(self, cap: int):
        self.cap, self.d, self.lock = cap, OrderedDict(), threading.Lock()

    def get(self, k):
        with self.lock:
            if k in self.d:
                self.d.move_to_end(k)
                return self.d[k]
        return None

    def put(self, k, v):
        with self.lock:
            self.d[k] = v
            self.d.move_to_end(k)
            while len(self.d) > self.cap:
                self.d.popitem(last=False)


class Views:
    """Результаты поиска: view_id → {title, ids, note}. id уникален между запусками."""

    def __init__(self, cap: int = MAX_VIEWS):
        self.lru = LRU(cap)
        self.boot = b36(int(time.time()) % 1296).rjust(2, "0")
        self.counter = itertools.count(1)

    def new(self, title: str, ids: List[str], note: str = "") -> str:
        vid = f"{self.boot}{b36(next(self.counter))}"
        self.lru.put(vid, {"title": title, "ids": ids, "note": note})
        return vid

    def get(self, vid: str) -> Optional[dict]:
        return self.lru.get(vid)


CAT: Catalog
STORE: Store
VIEWS = Views()
SESSIONS = LRU(MAX_SESSIONS)
_ULOCKS: Dict[int, threading.Lock] = {}
_ULOCKS_GUARD = threading.Lock()


def sess(uid: int) -> Session:
    s = SESSIONS.get(uid)
    if s is None:
        s = Session()
        SESSIONS.put(uid, s)
    return s


def user_lock(uid: int) -> threading.Lock:
    with _ULOCKS_GUARD:
        if len(_ULOCKS) > 20000:
            _ULOCKS.clear()
        return _ULOCKS.setdefault(uid, threading.Lock())


def migrate_old_db(target: Path) -> None:
    """Разово переносит базу, созданную старой версией в корне проекта, в DATA_DIR."""
    import shutil
    if target.exists():
        return
    for old in {BASE / "navigator.db", Path.cwd() / "navigator.db"}:
        if old.exists() and old.resolve() != target.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                if Path(str(old) + suffix).exists():
                    shutil.copy2(str(old) + suffix, str(target) + suffix)
            LOG.warning("База перенесена из %s в %s", old, target)
            return


def init(catalog_data: Optional[dict] = None, db_path: Any = None) -> None:
    """Загружает каталог и БД (вызывается из run(); в тестах — с подставными данными)."""
    global CAT, STORE
    CAT = Catalog(catalog_data if catalog_data is not None else ensure_catalog())
    if db_path is None:
        migrate_old_db(DB_PATH)
    STORE = Store(db_path if db_path is not None else DB_PATH)
    STORE.import_legacy_json(LEGACY_FAV_PATH)
    STORE.remap_ids(CAT)


# ─────────────────────────────── статистика ───────────────────────────────

def stats_text(days: int = 30) -> str:
    d = STORE.stats_data(days)
    L = [f"📊 Статистика за {days} дн. (анонимная)", "",
         f"👥 Пользователей: {d['users_total']} всего, {d['users_active']} активных, {d['users_new']} новых",
         f"🔍 Запросов: {d['queries']}, без результата: {d['zero']}"
         + (f" ({d['zero'] * 100 // d['queries']}%)" if d["queries"] else "")]
    def block(title, rows, fmt=lambda r: r[0]):
        if rows:
            L.extend(["", title] + [f"{i}. {clip(fmt(r), 60)} — {r[1]}" for i, r in enumerate(rows, 1)])
    block("🔥 Популярные запросы:", d["top"])
    block("🕳 Запросы без результата (что добавить в псевдонимы):", d["top_zero"])
    if d["filter_keys"]:
        L.extend(["", "⚙️ Фильтры по типам: " + ", ".join(
            f"{FILTER_TITLES.get(k, k)} — {c}" for k, c in d["filter_keys"])])
    def fname(r):
        k, _, v = r[0].partition("=")
        return f"{FILTER_TITLES.get(k, k)}: {v}"
    block("⚙️ Популярные значения фильтров:", d["filters"], fname)
    if d["fav_users"]:
        L.extend(["", f"⭐ Избранное ведут {d['fav_users']} польз. Чаще всего:"])
        for i, (eid, c) in enumerate(d["fav_top"], 1):
            e = CAT.by_id.get(eid)
            L.append(f"{i}. {clip(e['name'], 60) if e else eid} — {c}")
    return "\n".join(L)


# ─────────────────────────────── тексты ───────────────────────────────

def main_text() -> str:
    n = len(CAT.events)
    tail = ""
    if CAT.variant == "projects":
        tail = "\n📄 База по проектам приказов — приказы пока не утверждены."
    elif CAT.variant == "official":
        tail = "\n✅ База по утверждённым приказам."
    return ("🏆 Навигатор перечневых мероприятий\n"
            f"📅 {CAT.academic_year} учебный год\n\n"
            f"В базе {n} {plural(n, 'мероприятие', 'мероприятия', 'мероприятий')}."
            f"{tail}\n\nВыберите, как искать, или просто напишите запрос:")


def main_menu() -> List[List[Tuple]]:
    return [
        [("🔍 Найти самому", "m:search")],
        [("🎯 Подобрать по интересам", "m:interests")],
        [("🏛 От ВУЗа / организатора", "m:org")],
        [("⚙️ Фильтры", "m:filters"), ("⭐ Избранное", "m:fav")],
        [("❓ Справка", "m:help"), ("ℹ️ О проекте", "m:about")],
    ]


def menu_kb() -> dict:
    return kb(main_menu())


def about_text() -> str:
    status = catalog_status()
    return ("ℹ️ О проекте\n\n" + (status + "\n\n" if status else "")
            + f"Поиск идёт по локальной базе перечневых мероприятий на {CAT.academic_year} "
            "учебный год.\n\n⭐ Избранное хранится персонально и не пропадает между сессиями.")


def help_text() -> str:
    return (
        "❓ Как пользоваться\n\n"
        "• Просто напишите запрос: «математика», «право», «журналистика», «ИТМО».\n"
        "• Бот понимает формы слов («химии», «программированию») и аббревиатуры вузов "
        "(ВШЭ, МФТИ, СПбГУ).\n"
        "• Несколько слов сужают поиск: «физика МФТИ».\n"
        "• ⚙️ Фильтры — по уровню, предмету, направлению подготовки вуза и т. д.\n"
        "• ⭐ В карточке — добавить в избранное; список можно скачать файлом.\n\n"
        "Команды: /menu, /search, /org, /filters, /fav, /help")


EXPIRED_TEXT = "⌛ Этот список устарел. Повторите поиск — карточки и избранное продолжают работать."


# ──────────────────────────────── рендер ────────────────────────────────

def pager(prefix: str, page: int, pages: int) -> List[Tuple]:
    nav: List[Tuple] = []
    if page > 0:
        nav.append(("◀", f"{prefix}:{page - 1}"))
    nav.append((f"{page + 1} / {pages}", "noop"))
    if page < pages - 1:
        nav.append(("▶", f"{prefix}:{page + 1}"))
    return nav


def render_list(vid: str, page: int) -> Tuple[str, dict]:
    v = VIEWS.get(vid)
    if not v:
        return EXPIRED_TEXT, kb([[("🔍 Новый поиск", "m:search"), ("🏠 Меню", "m:menu")]])
    ids = v["ids"]
    total = len(ids)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = ids[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]

    L = [v["title"], f"Найдено: {total} {plural(total, 'мероприятие', 'мероприятия', 'мероприятий')}"]
    if v.get("note"):
        L.append(v["note"])
    L.append("")
    num_row: List[Tuple] = []
    for i, eid in enumerate(chunk):
        e = CAT.by_id.get(eid)
        if not e:
            continue
        n = page * PAGE_SIZE + i + 1
        L.append(f"{n}. {clip(e['name'], 120)}")
        sub = [x for x in (e.get("type"), fmt_levels(e)) if x]
        if sub:
            L.append("   " + " · ".join(sub))
        L.append("")
        num_row.append((str(n), f"c:{eid}:{vid}:{page}"))
    rows: List[List[Tuple]] = []
    if num_row:
        rows.append(num_row)
    if pages > 1:
        rows.append(pager(f"l:{vid}", page, pages))
    rows.append([("🏠 Меню", "m:menu"), ("⭐ Избранное", "m:fav")])
    return "\n".join(L).rstrip(), kb(rows)


def render_card(uid: int, eid: str, vid: str, lpage: int) -> Tuple[str, dict]:
    e = CAT.by_id.get(eid)
    if not e:
        return "⚠️ Мероприятие не найдено (каталог мог обновиться).", menu_kb()
    fav = STORE.is_fav(uid, eid)
    if vid == "f":
        back = ("⬅ К избранному", f"l:f:{lpage}")
    elif VIEWS.get(vid):
        back = ("⬅ К списку", f"l:{vid}:{lpage}")
    else:
        back = ("🔍 Новый поиск", "m:search")
    rows = [
        [(("★ Убрать из избранного" if fav else "☆ В избранное"), f"fav:{eid}:{vid}:{lpage}")],
        [("📋 Все официальные данные", f"d:{eid}:0:{vid}:{lpage}")],
        [back, ("🏠 Меню", "m:menu")],
    ]
    return card_text(e), kb(rows)


def render_details(eid: str, dpage: int, vid: str, lpage: int) -> Tuple[str, dict]:
    e = CAT.by_id.get(eid)
    if not e:
        return "⚠️ Мероприятие не найдено.", menu_kb()
    pages = detail_pages(e)
    dpage = max(0, min(dpage, len(pages) - 1))
    rows: List[List[Tuple]] = []
    if len(pages) > 1:
        nav: List[Tuple] = []
        if dpage > 0:
            nav.append(("◀", f"d:{eid}:{dpage - 1}:{vid}:{lpage}"))
        nav.append((f"{dpage + 1} / {len(pages)}", "noop"))
        if dpage < len(pages) - 1:
            nav.append(("▶", f"d:{eid}:{dpage + 1}:{vid}:{lpage}"))
        rows.append(nav)
    rows.append([("⬅ К карточке", f"c:{eid}:{vid}:{lpage}"), ("🏠 Меню", "m:menu")])
    return pages[dpage], kb(rows)


def render_favorites(uid: int, page: int) -> Tuple[str, dict]:
    ids = STORE.fav_ids(uid, CAT.by_id)
    if not ids:
        return ("⭐ Избранное пусто\n\nДобавляйте мероприятия кнопкой «☆ В избранное» в карточке — "
                "они появятся здесь.",
                kb([[("🔍 Найти мероприятия", "m:search"), ("🏠 Меню", "m:menu")]]))
    pages = max(1, (len(ids) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = ids[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]
    L = [f"⭐ Избранное · {len(ids)} {plural(len(ids), 'мероприятие', 'мероприятия', 'мероприятий')}", ""]
    rows: List[List[Tuple]] = []
    for i, eid in enumerate(chunk):
        e = CAT.by_id[eid]
        L.append(f"{page * PAGE_SIZE + i + 1}. {clip(e['name'], 120)}")
        L.append("")
        rows.append([("📄 Открыть", f"c:{eid}:f:{page}"), ("🗑 Убрать", f"fd:{eid}:{page}")])
    if pages > 1:
        rows.append(pager("l:f", page, pages))
    rows.append([("⤓ Скачать .txt", "fexp")])
    rows.append([("🏠 Меню", "m:menu")])
    return "\n".join(L).rstrip(), kb(rows)


def render_interests(s: Session) -> Tuple[str, dict]:
    rows: List[List[Tuple]] = []
    for i, name in enumerate(CAT.interests):
        mark = "✅ " if s.interest == name else ""
        rows.append([(f"{mark}{name} · {CAT.interest_counts.get(name, 0)}", f"i:{i}")])
    rows.append([("♻️ Сбросить интерес", "i:clear"), ("🏠 Меню", "m:menu")])
    return "🎯 Что тебе интересно?\n\nВыберите направление — покажу подходящие мероприятия.", kb(rows)


def render_filters_menu(s: Session) -> Tuple[str, dict]:
    L = ["⚙️ Фильтры", ""]
    active = [k for k in FILTER_KEYS if s.filters.get(k)]
    if active:
        L += [f"• {FILTER_TITLES[k]}: {s.filters[k]}" for k in active] + [""]
    else:
        L.append("Фильтры не заданы.\n")
    rows = [[(("✅ " if s.filters.get(k) else "") + FILTER_TITLES[k], f"fk:{k}")] for k in FILTER_KEYS]
    rows.append([("▶️ Показать результаты", "fshow")])
    rows.append([("♻️ Сбросить всё", "fclear"), ("🏠 Меню", "m:menu")])
    return "\n".join(L).rstrip(), kb(rows)


def render_filter_options(s: Session, key: str, page: int) -> Tuple[str, dict]:
    opts = CAT.options(key, s.filters, s.fquery)
    pages = max(1, (len(opts) + OPT_PAGE - 1) // OPT_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = opts[page * OPT_PAGE: (page + 1) * OPT_PAGE]
    cur = s.filters.get(key)
    L = [f"⚙️ {FILTER_TITLES[key]}", f"Вариантов: {len(opts)} · стр. {page + 1}/{pages}"]
    if s.fquery:
        L.append(f"🔎 Поиск по списку: «{clip(s.fquery, 40)}»")
    L.append("")
    rows: List[List[Tuple]] = []
    for v in chunk:
        rows.append([(clip(("✅ " if cur == v else "") + v, 60), f"fv:{key}:{vh(v)}:{page}")])
    if pages > 1:
        rows.append(pager(f"fp:{key}", page, pages))
    tools = [("🔎 Найти в списке", f"fs:{key}")]
    if s.fquery:
        tools.append(("✖️ Сбросить поиск", f"fq:{key}"))
    rows.append(tools)
    if cur:
        rows.append([("✖️ Убрать этот фильтр", f"fx:{key}")])
    rows.append([("⬅ К фильтрам", "m:filters"), ("🏠 Меню", "m:menu")])
    return "\n".join(L).rstrip(), kb(rows)


# ──────────────────────────── поисковые сценарии ────────────────────────────

NO_RESULT_KB = kb([[("⚙️ Фильтры", "m:filters"), ("♻️ Сбросить", "fclear")], [("🏠 Меню", "m:menu")]])


def build_search_view(s: Session, q: str) -> Tuple[Optional[str], str, str]:
    """→ (vid | None, текст при пустом результате, подсказка-исправление)."""
    res, relaxed = CAT.smart_search(q, s.filters)
    STORE.log_query("search", q, len(res))
    if not res:
        return None, "", CAT.suggest(q)
    note = "ℹ️ Точных совпадений нет — показаны близкие по смыслу." if relaxed else ""
    return VIEWS.new(f"🔍 «{clip(q, 80)}»", [e["id"] for e in res], note), "", ""


def empty_search_reply(q: str, hint: str) -> Tuple[str, dict]:
    text = f"🔍 «{clip(q, 80)}»\n\nНичего не найдено.\nПопробуйте другое слово или сбросьте фильтры."
    rows: List[List[Tuple]] = []
    if hint:
        text += f"\n\nВозможно, вы имели в виду: «{hint}»"
        rows.append([(clip(f"🔍 {hint}", 60), f"sq:{hint[:120]}")])
    rows += [[("⚙️ Фильтры", "m:filters"), ("♻️ Сбросить", "fclear")], [("🏠 Меню", "m:menu")]]
    return text, kb(rows)


def do_search(api: MaxAPI, target: dict, s: Session, uid: int, q: str) -> None:
    q = (q or "").strip()
    if not q:
        api.send(**target, text="Напишите запрос текстом: название, предмет или вуз.",
                 keyboard=menu_kb())
        return
    vid, _, hint = build_search_view(s, q)
    if vid is None:
        t, k = empty_search_reply(q, hint)
        api.send(**target, text=t, keyboard=k)
        return
    t, k = render_list(vid, 0)
    api.send(**target, text=t, keyboard=k)


def do_org(api: MaxAPI, target: dict, s: Session, uid: int, q: str) -> None:
    q = (q or "").strip()
    if not q:
        api.send(**target, text="Напишите название вуза или организатора.", keyboard=menu_kb())
        return
    res = CAT.search(org=q, filters=s.filters)
    STORE.log_query("org", q, len(res))
    if not res:
        api.send(**target,
                 text=f"🏛 «{clip(q, 80)}»\n\nОрганизатор не найден. Попробуйте «ИТМО», «МФТИ», «ВШЭ»…",
                 keyboard=kb([[("🏠 Меню", "m:menu")]]))
        return
    vid = VIEWS.new(f"🏛 «{clip(q, 80)}»", [e["id"] for e in res])
    t, k = render_list(vid, 0)
    api.send(**target, text=t, keyboard=k)


def export_favorites(api: MaxAPI, target: dict, uid: int) -> None:
    ids = STORE.fav_ids(uid, CAT.by_id)
    if not ids:
        api.send(**target, text="⭐ Избранное пусто — сохранять нечего.")
        return
    head = (f"Избранные мероприятия — перечни {CAT.academic_year} уч. года\n"
            f"Сохранено: {time.strftime('%d.%m.%Y')} · всего: {len(ids)}\n" + "=" * 48 + "\n\n")
    blocks = []
    for i, eid in enumerate(ids, 1):
        e = CAT.by_id[eid]
        lines = [f"{i}. {e['name']}",
                 f"   Источник: {e.get('ministry', '')}" + (f" · {e['type']}" if e.get("type") else "")]
        if e.get("levels"):
            lines.append("   Уровень: " + ", ".join(e["levels"]))
        if e.get("subjects"):
            lines.append("   Предмет: " + ", ".join(e["subjects"]))
        if e.get("profiles"):
            pf = e["profiles"]
            lines.append("   Профиль: " + "; ".join(pf[:6]) + (" и др." if len(pf) > 6 else ""))
        lines.append("   Организатор: " + org_line(e))
        blocks.append("\n".join(lines))
    content = (head + "\n\n".join(blocks) + "\n").encode("utf-8")
    token = api.upload_file("izbrannoe-perechni.txt", content)
    if token:
        api.send(**target,
                 text=f"⤓ Избранное: {len(ids)} {plural(len(ids), 'мероприятие', 'мероприятия', 'мероприятий')}",
                 attachments=[{"type": "file", "payload": {"token": token}}])
        return
    for part in chunk_text(content.decode("utf-8")):
        api.send(**target, text=part)


# ──────────────────────────── обработчики событий ────────────────────────────

def reply_target(msg: dict) -> dict:
    r = msg.get("recipient") or {}
    if r.get("chat_id"):
        return {"chat_id": r["chat_id"]}
    s = msg.get("sender") or {}
    return {"user_id": s["user_id"]} if s.get("user_id") else {}


def handle_start(api: MaxAPI, upd: dict) -> None:
    """Пользователь нажал «Начать» (update_type=bot_started)."""
    uid = (upd.get("user") or {}).get("user_id")
    if not uid:
        return
    STORE.touch(uid)
    sess(uid).await_ = None
    target = {"chat_id": upd["chat_id"]} if upd.get("chat_id") else {"user_id": uid}
    api.send(**target, text=main_text(), keyboard=menu_kb())


def handle_message(api: MaxAPI, upd: dict) -> None:
    msg = upd.get("message") or {}
    body = msg.get("body") or {}
    text = (body.get("text") or "").strip()
    uid = (msg.get("sender") or {}).get("user_id")
    target = reply_target(msg)
    if not uid or not target:
        return
    STORE.touch(uid)
    s = sess(uid)
    in_group = (msg.get("recipient") or {}).get("chat_type") == "chat"

    if text.startswith("/"):
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.lower().split("@")[0], arg.strip()
        s.await_ = None
        if cmd in ("/start", "/menu"):
            api.send(**target, text=main_text(), keyboard=menu_kb())
        elif cmd == "/help":
            api.send(**target, text=help_text(), keyboard=kb([[("🏠 Меню", "m:menu")]]))
        elif cmd == "/search":
            if arg:
                do_search(api, target, s, uid, arg)
            else:
                s.await_ = "search"
                api.send(**target, text="🔍 Введите поисковый запрос (название, предмет, ВУЗ…):")
        elif cmd == "/org":
            if arg:
                do_org(api, target, s, uid, arg)
            else:
                s.await_ = "org"
                api.send(**target, text="🏛 Введите название ВУЗа или организатора:")
        elif cmd == "/filters":
            t, k = render_filters_menu(s)
            api.send(**target, text=t, keyboard=k)
        elif cmd == "/myid":
            api.send(**target, text=f"Ваш user_id: {uid}\n\nЧтобы открыть статистику, добавьте его в .env: "
                                    f"ADMIN_IDS={uid} — и перезапустите бота.")
        elif cmd == "/stats" and uid in ADMIN_IDS:
            days = to_int(arg) if arg.isdigit() and 0 < int(arg) <= 365 else 30
            api.send(**target, text=stats_text(days))
        elif cmd in ("/fav", "/favorites"):
            t, k = render_favorites(uid, 0)
            api.send(**target, text=t, keyboard=k)
        else:
            api.send(**target, text="Неизвестная команда. /menu — главное меню.", keyboard=menu_kb())
        return

    if in_group and not s.await_:
        return                                  # в группах отвечаем только на команды
    if not text:
        if not in_group:
            api.send(**target, text="Я понимаю только текст. Напишите запрос: название, предмет или вуз.",
                     keyboard=menu_kb())
        return

    awaiting, s.await_ = s.await_, None
    if awaiting == "org":
        do_org(api, target, s, uid, text)
    elif awaiting and awaiting.startswith("fs:"):
        key = awaiting[3:]
        if key in FILTER_KEYS:
            s.fquery = text
            t, k = render_filter_options(s, key, 0)
            api.send(**target, text=t, keyboard=k)
    else:
        do_search(api, target, s, uid, text)


def _notify(api: MaxAPI, cb: dict, text: str) -> None:
    try:
        api.answer_callback(cb.get("callback_id", ""), notification=text)
    except Exception:  # noqa: BLE001
        pass


def handle_callback(api: MaxAPI, upd: dict) -> None:
    cb = upd.get("callback") or {}
    data = (cb.get("payload") or "").strip()
    uid = (cb.get("user") or {}).get("user_id")
    if not uid:
        return
    STORE.touch(uid)
    s = sess(uid)
    p = data.split(":")
    head = p[0]

    def show(t: str, k: Optional[dict] = None, note: Optional[str] = None) -> None:
        ui_update(api, cb, t, k, note)

    if data in ("m:menu", "m:start"):
        s.await_ = None
        return show(main_text(), menu_kb())
    if data == "m:search":
        s.await_ = "search"
        return show("🔍 Введите поисковый запрос.\n\nНапример: информатика, дипломатия, журналистика, ИТМО…",
                    kb([[("🏠 Меню", "m:menu")]]))
    if data == "m:org":
        s.await_ = "org"
        return show("🏛 Введите ВУЗ или организатора.\n\nНапример: ИТМО, МФТИ, ВШЭ, СВФУ, СПбГУ…",
                    kb([[("🏠 Меню", "m:menu")]]))
    if data == "m:about":
        return show(about_text(), kb([[("🏠 Меню", "m:menu")]]))
    if data == "m:help":
        return show(help_text(), kb([[("🏠 Меню", "m:menu")]]))
    if data == "m:interests":
        return show(*render_interests(s))
    if data == "m:filters":
        return show(*render_filters_menu(s))
    if data == "m:fav":
        return show(*render_favorites(uid, 0))
    if data == "noop":
        return _notify(api, cb, " ")

    # ── интересы ──
    if head == "i" and len(p) == 2:
        if p[1] == "clear":
            s.interest = ""
            return show(*render_interests(s))
        idx = to_int(p[1], -1)
        if not 0 <= idx < len(CAT.interests):
            return
        name = CAT.interests[idx]
        s.interest = "" if s.interest == name else name
        if not s.interest:
            return show(*render_interests(s))
        res = CAT.search(interest=name, filters=s.filters)
        if not res:
            return show(f"🎯 {name}\n\nПо этому интересу с текущими фильтрами ничего нет.", NO_RESULT_KB)
        vid = VIEWS.new(f"🎯 {name}", [e["id"] for e in res])
        return show(*render_list(vid, 0))

    # ── списки и карточки (stateless) ──
    if head == "l" and len(p) == 3:
        vid, page = p[1], to_int(p[2])
        return show(*(render_favorites(uid, page) if vid == "f" else render_list(vid, page)))
    if head == "c" and len(p) == 4:
        return show(*render_card(uid, p[1], p[2], to_int(p[3])))
    if head == "fav" and len(p) == 4:
        eid = p[1]
        if eid in CAT.by_id:
            added = STORE.toggle(uid, eid)
            t, k = render_card(uid, eid, p[2], to_int(p[3]))
            return show(t, k, "Добавлено в избранное ⭐" if added else "Убрано из избранного")
        return
    if head == "d" and len(p) == 5:
        return show(*render_details(p[1], to_int(p[2]), p[3], to_int(p[4])))
    if head == "fd" and len(p) == 3:
        STORE.remove(uid, p[1])
        return show(*render_favorites(uid, to_int(p[2])))
    if data == "fexp":
        _notify(api, cb, "Готовлю файл…")
        rec = (cb.get("message") or {}).get("recipient") or {}
        target = {"chat_id": rec["chat_id"]} if rec.get("chat_id") else {"user_id": uid}
        return export_favorites(api, target, uid)
    if head == "sq" and len(p) >= 2:
        q = data[3:]
        vid, _, hint = build_search_view(s, q)
        if vid is None:
            return show(*empty_search_reply(q, hint))
        return show(*render_list(vid, 0))

    # ── фильтры ──
    if head == "fk" and len(p) == 2 and p[1] in FILTER_KEYS:
        s.fquery = ""
        if not CAT.options(p[1], s.filters):
            return show(f"⚙️ {FILTER_TITLES[p[1]]}\n\nНет доступных значений при текущих фильтрах.",
                        kb([[("⬅ К фильтрам", "m:filters")]]))
        return show(*render_filter_options(s, p[1], 0))
    if head == "fp" and len(p) == 3 and p[1] in FILTER_KEYS:
        return show(*render_filter_options(s, p[1], to_int(p[2])))
    if head == "fv" and len(p) == 4 and p[1] in FILTER_KEYS:
        key = p[1]
        val = next((o for o in CAT.options(key, s.filters) if vh(o) == p[2]), None)
        if val is not None:
            if s.filters.get(key) == val:
                s.filters.pop(key, None)
            else:
                s.filters[key] = val
                STORE.log_query("filter", f"{key}={val}", 0)
        return show(*render_filter_options(s, key, to_int(p[3])))
    if head == "fs" and len(p) == 2 and p[1] in FILTER_KEYS:
        s.await_ = f"fs:{p[1]}"
        return show(f"🔎 {FILTER_TITLES[p[1]]}\n\nВведите часть названия, чтобы сузить список вариантов.",
                    kb([[("⬅ Назад", f"fk:{p[1]}")]]))
    if head == "fq" and len(p) == 2 and p[1] in FILTER_KEYS:
        s.fquery = ""
        return show(*render_filter_options(s, p[1], 0))
    if head == "fx" and len(p) == 2:
        s.filters.pop(p[1], None)
        return show(*render_filters_menu(s))
    if data == "fclear":
        s.filters, s.interest, s.fquery = {}, "", ""
        return show(*render_filters_menu(s))
    if data == "fshow":
        res = CAT.search(interest=s.interest, filters=s.filters)
        if not res:
            return show("По выбранным фильтрам ничего не найдено.\nПопробуйте убрать часть условий.",
                        NO_RESULT_KB)
        vid = VIEWS.new("⚙️ Подборка по фильтрам", [e["id"] for e in res])
        return show(*render_list(vid, 0))

    LOG.warning("неизвестный payload: %s", data)
    _notify(api, cb, "Не понимаю эту кнопку")


def handle_update(api: MaxAPI, upd: dict) -> None:
    t = upd.get("update_type")
    if t == "message_created":
        handle_message(api, upd)
    elif t in ("message_callback", "callback"):
        handle_callback(api, upd)
    elif t == "bot_started":
        handle_start(api, upd)
    else:
        LOG.debug("пропускаю update_type=%s", t)


def update_user(upd: dict) -> Optional[int]:
    """user_id автора обновления — по нему сериализуем обработку."""
    return ((upd.get("message") or {}).get("sender") or {}).get("user_id") \
        or (upd.get("callback") or {}).get("user", {}).get("user_id") \
        or (upd.get("user") or {}).get("user_id")


def safe_handle(api: MaxAPI, upd: dict) -> None:
    uid = update_user(upd)
    lock = user_lock(uid) if uid else threading.Lock()
    with lock:
        try:
            handle_update(api, upd)
        except Exception:  # noqa: BLE001
            LOG.exception("ошибка обработки обновления %s", upd.get("update_type"))
            try:
                if upd.get("update_type") == "message_created":
                    tgt = reply_target(upd.get("message") or {})
                    if tgt:
                        api.send(**tgt, text="⚠️ Что-то пошло не так. Попробуйте ещё раз или /menu.")
                elif upd.get("callback"):
                    _notify(api, upd["callback"], "Ошибка, попробуйте ещё раз")
            except Exception:  # noqa: BLE001
                pass


# ─────────────────── регистрация команд в MAX ───────────────────

BOT_COMMANDS: List[dict] = [
    {"name": "start", "description": "Запустить бота и показать главное меню"},
    {"name": "menu", "description": "Показать главное меню"},
    {"name": "help", "description": "Справка по работе бота"},
    {"name": "search", "description": "Найти мероприятие по названию, предмету или ВУЗу"},
    {"name": "org", "description": "Найти мероприятия по организатору"},
    {"name": "filters", "description": "Фильтры: уровень, предмет, направление вуза"},
    {"name": "fav", "description": "Показать избранные мероприятия"},
]


def register_commands(api: MaxAPI) -> None:
    try:
        api.set_my_commands(BOT_COMMANDS)
        LOG.info("Команды зарегистрированы (%d шт.)", len(BOT_COMMANDS))
    except requests.HTTPError as ex:
        resp = ex.response
        LOG.warning("Не удалось зарегистрировать команды (HTTP %s): %s",
                    resp.status_code if resp is not None else "?", (resp.text or "")[:200] if resp is not None else "")
    except Exception as ex:  # noqa: BLE001
        LOG.warning("Не удалось зарегистрировать команды: %s", ex)


# ──────────────────────────────── поллинг ────────────────────────────────

def run() -> None:
    if not BOT_TOKEN:
        env_file = BASE / ".env"
        hint = (f"\nФайл .env найден ({env_file}), но MAX_BOT_TOKEN в нём не задан."
                if env_file.exists() else
                "\nСоздайте файл .env рядом с bot.py:\n    MAX_BOT_TOKEN=ваш_токен_из_MasterBot")
        raise SystemExit("Не задан MAX_BOT_TOKEN." + hint)

    init()
    api = MaxAPI(BOT_TOKEN, verify=prepare_ca_bundle() or True)
    try:
        me = api.get("me")
        LOG.info("Бот запущен: %s", json.dumps(me, ensure_ascii=False)[:300])
    except requests.HTTPError as ex:
        resp = ex.response
        raise SystemExit(
            f"Не удалось авторизоваться в MAX API (HTTP {resp.status_code if resp is not None else '?'}).\n"
            f"Проверьте токен в .env, доступность {API_BASE} и сертификаты Минцифры.\n"
            f"Ответ сервера: {(resp.text or '')[:300] if resp is not None else ''}")
    except Exception as ex:  # noqa: BLE001
        raise SystemExit(f"Не удалось авторизоваться в MAX API: {ex}")

    register_commands(api)
    LOG.info("Нажмите Ctrl+C для остановки бота.")

    pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="upd")
    marker: Optional[int] = None
    backoff = 1
    try:
        while True:
            try:
                data = api.get_updates(marker=marker, timeout=POLL_TIMEOUT)
                marker = data.get("marker", marker)
                for upd in data.get("updates") or []:
                    pool.submit(safe_handle, api, upd)
                backoff = 1
            except requests.RequestException as ex:
                LOG.warning("сеть/API: %s — повтор через %s c", ex, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except Exception:  # noqa: BLE001
                LOG.exception("непредвиденная ошибка в цикле поллинга")
                time.sleep(3)
    except KeyboardInterrupt:
        LOG.info("Остановка бота по Ctrl+C. До связи!")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    run()
