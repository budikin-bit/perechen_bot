# Навигатор перечневых мероприятий 2026/27 (бот для МАХ)

## Состав
| Файл | Назначение |
|---|---|
| `bot.py` | бот (long polling), SQLite для избранного |
| `common.py` | общие функции: нормализация, стемминг, организаторы, id |
| `orders.py` | чтение приказов: .rtf (предпочтительно) и .docx, реквизиты приказа |
| `parse_minobr.py`, `parse_minpros.py` | приказ → JSON (`minobr_official.json`, `minpros_official.json`) |
| `image_text.json` | текст картинок в названиях (иероглифы, «Space-π» и т.п.), ключ — sha1 картинки |
| `build_catalog.py` | сборка `catalog.official.json` (или `--variant projects`) + `catalog.report.txt` |
| `aliases.json` | псевдонимы организаторов (ВШЭ, Физтех, СПбГУ…) |
| `overrides.json` | ручные правки предметов/интересов/типа и пары «одно мероприятие в двух перечнях» |
| `diff_catalogs.py` | сравнение каталогов (проект ↔ утверждённый), влияние на избранное |
| `tests/` | pytest-тесты |

## Обновление каталога
Берите приказы в **RTF** (из справочной системы): при конвертации в docx теряются
картинки в названиях. Бот читает RTF сам, LibreOffice не нужен.
```bash
pip install -r requirements-tools.txt
python parse_minobr.py  orders_src/prikaz_521_minobr.rtf      # → minobr_official.json
python parse_minpros.py orders_src/prikaz_598_minpros.rtf      # → minpros_official.json
python build_catalog.py --report            # → catalog.official.json (+ сверка с catalog.projects.json)
python diff_catalogs.py catalog.projects.json catalog.official.json --favs favorites.json
```
* Откройте `catalog.report.txt`: замечания к данным приказов, доля записей без предмета/псевдонимов,
  типы, предметы, переименованные мероприятия.
* Откройте `in_both_candidates.txt`: если пара — одно мероприятие, добавьте её в
  `overrides.json → same_event`.
* Неверный предмет — правьте в `overrides.json → by_name` (ключ — `common.norm_name(название)`).
* Идентификаторы зависят только от названия. Если название в приказе изменилось, сборка
  сохраняет прежний id в `old_ids`, и бот переносит избранное пользователей.
  Пропавшие мероприятия перечислены в отчёте.

## Запуск бота
```bash
cp .env.example .env     # вписать MAX_BOT_TOKEN
pip install -r requirements.txt
python bot.py
```
При первом запуске `favorites.json` (если был) переносится в `navigator.db`,
старые id избранного автоматически сопоставляются с новыми.

## Тесты
```bash
pytest -q tests
```

## Что полезно знать
* Номера мероприятий — настоящие, из приказа (Минобр 1…60; Минпрос 1.1…6.979). Номер 6.95 в приказе
  занят двумя разными мероприятиями — они остаются двумя записями.
* В приказе № 598 у 6.345 уровень обрезан до «Не» — принят «Не установлен» (есть предупреждение).
* Уровни: Высший, I–IV, Не установлен. Фильтр «Раздел приказа» — по шести разделам Минпросвещения.
* Если в документе нет колонки номеров (проект перечня), `6.N` присваиваются по порядку.
* Таблица `qlog` в `navigator.db` — анонимный лог запросов (без user_id);
  `SELECT q, COUNT(*) FROM qlog WHERE n=0 GROUP BY q ORDER BY 2 DESC` — чего ищут и не находят.
