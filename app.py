"""Демо-стенд: выбор города отеля, его координаты и названия на языках из конфига.

    uvicorn app:app        # http://127.0.0.1:8000, описание API — /docs

Справочник data/geonames.sqlite (только чтение) собирает build_db.py.
Сохранённые города — data/app.sqlite, языки — languages.json.
Данные о городах: GeoNames (geonames.org), CC BY 4.0.
"""
import json
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from textnorm import glob_escape, norm_key

ROOT = Path(__file__).resolve().parent
REF_DB = ROOT / "data" / "geonames.sqlite"
APP_DB = ROOT / "data" / "app.sqlite"
LANG_FILE = ROOT / "languages.json"
UI_CHAIN = ["ru", "en"]  # на каком языке подписывать города, регионы и страны в админке
LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")
TERMINAL = "en"  # конечный язык: запасного у него нет, им заканчивается любая цепочка
# Другие коды того же языка и письма в GeoNames: ищутся вместе с основным, в цепочку запасных не входят
SAME_LANGUAGE = {"zh-hans": ["zh-cn", "zh-sg", "zh-my"], "zh-hant": ["zh-tw", "zh-hk", "zh-mo"]}
# Общий код без указания письма: берётся после точных кодов и уходит на проверку
GENERIC = {"zh-hans": "zh", "zh-hant": "zh"}

APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS city (
  geonameid INTEGER PRIMARY KEY,
  latitude  REAL NOT NULL,
  longitude REAL NOT NULL,
  label     TEXT NOT NULL,  -- подпись в админке: «Сочи, Краснодарский край, Россия»
  saved_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS city_name (
  geonameid  INTEGER NOT NULL REFERENCES city,
  lang       TEXT NOT NULL,
  value      TEXT NOT NULL,
  source     TEXT NOT NULL,  -- geonames | geonames:zh | fallback:<код> | latin | manual
  review     INTEGER NOT NULL,  -- 1: подставлено автоматически, человек ещё не проверял
  updated_at TEXT NOT NULL,
  PRIMARY KEY (geonameid, lang)
);
"""
SUGGEST_SQL = """
SELECT p.geonameid, p.name, p.latitude, p.longitude, p.country_code, p.admin1_code, p.population,
       MAX(k.key = :key) AS exact,                 -- название введено целиком
       MAX(k.weight) AS weight,                    -- 0: совпало только историческое или разговорное
       MIN(length(k.key)) - length(:key) AS rest   -- сколько букв осталось дописать
FROM search_key k JOIN place p ON p.geonameid = k.geonameid
WHERE k.key GLOB :pattern
GROUP BY p.geonameid
-- Население с поправками: точное совпадение ×10, иначе делим на число недописанных букв;
-- только историческое имя ×0,1. «Соч» → Сочи выше Сочимилько, «Анта» → Анталья выше деревни Анта
ORDER BY (p.population + 1.0) * (CASE WHEN exact THEN 10 ELSE 1.0 / (1 + rest) END)
         * (CASE WHEN weight > 0 THEN 1 ELSE 0.1 END) DESC
LIMIT :limit
"""

app = FastAPI(title="Город отеля", description="Демо-стенд на данных GeoNames (CC BY 4.0)")
lang_lock = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ref():
    if not REF_DB.exists():
        raise HTTPException(503, "Справочник не собран: запустите python download.py и python build_db.py")
    con = sqlite3.connect(REF_DB.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def app_db():
    con = sqlite3.connect(APP_DB)
    con.row_factory = sqlite3.Row
    return con


APP_DB.parent.mkdir(parents=True, exist_ok=True)
with closing(app_db()) as _db, _db:
    _db.executescript(APP_SCHEMA)
    # Записи прежнего конфига: zh-cn, zh-tw … были запасными языками, теперь это коды того же письма
    _db.execute("UPDATE city_name SET source = 'geonames' WHERE source IN "
                "('fallback:zh-cn', 'fallback:zh-sg', 'fallback:zh-my', 'fallback:zh-tw', 'fallback:zh-hk', 'fallback:zh-mo')")
    _db.execute("UPDATE city_name SET source = 'geonames:zh' WHERE source = 'fallback:zh'")


# ---- конфиг языков

def chain_of(code, langs):
    """Цепочка запасных языков от code до английского: kk → ru → en."""
    chain = [code]
    while chain[-1] != TERMINAL:
        nxt = langs[chain[-1]].get("fallback")
        if nxt in chain:
            raise ValueError(f"цикл в цепочке {' → '.join(chain + [nxt])}")
        chain.append(nxt)
    return chain


def check_config(cfg):
    """Правила: английский есть и никуда не ведёт; у остальных запасной язык есть и он из списка; без циклов."""
    langs = {lang["code"]: lang for lang in cfg["languages"]}
    if TERMINAL not in langs or langs[TERMINAL].get("fallback"):
        raise ValueError(f"в списке должен быть {TERMINAL} без запасного языка")
    for code, lang in langs.items():
        if code != TERMINAL and lang.get("fallback") not in langs:
            raise ValueError(f"у {code} запасной язык «{lang.get('fallback')}» не из списка языков")
    for code in langs:
        chain_of(code, langs)
    return langs


def load_config():
    cfg = json.loads(LANG_FILE.read_text(encoding="utf-8"))
    try:
        return cfg, check_config(cfg)
    except ValueError as e:
        raise HTTPException(500, f"Ошибка в languages.json: {e}")


def save_config(cfg):
    """Один язык — одна строка, чтобы конфиг было удобно читать и править руками."""
    langs = ",\n".join("    " + json.dumps(lang, ensure_ascii=False) for lang in cfg["languages"])
    LANG_FILE.write_text(f'{{\n  "languages": [\n{langs}\n  ]\n}}\n', encoding="utf-8", newline="\n")


def ordered(cfg):
    """Сначала основные языки приложения, затем остальные — в порядке конфига."""
    return ([lang for lang in cfg["languages"] if lang.get("main")]
            + [lang for lang in cfg["languages"] if not lang.get("main")])


def chain_groups(cfg, langs):
    """Самые длинные цепочки без повторов: от языков, которые ни для кого не запасные.
    Языки с одинаковым продолжением цепочки собраны в одну строку: kk, be → ru → en."""
    used = {lang.get("fallback") for lang in cfg["languages"]}
    groups = {}
    for lang in ordered(cfg):
        code = lang["code"]
        if code != TERMINAL and code not in used:
            groups.setdefault(tuple(chain_of(code, langs)[1:]), []).append(code)
    return sorted(({"languages": codes, "tail": list(tail)} for tail, codes in groups.items()),
                  key=lambda g: -len(g["tail"]))


# ---- выбор названия

def placeholders(values):
    return ",".join("?" * len(values))


def current_names(db, ids):
    """{geonameid: [названия с языком]} без исторических и разговорных."""
    names = {}
    if ids:
        ids = list(ids)
        for r in db.execute("SELECT geonameid, lang, name, is_preferred, is_short, alternatenameid FROM alt_name "
                            f"WHERE geonameid IN ({placeholders(ids)}) AND lang IS NOT NULL AND is_current", ids):
            names.setdefault(r["geonameid"], []).append(r)
    return names


def pick(rows, codes):
    """Название GeoNames по первому коду из codes, у которого оно есть: официальное, затем краткое,
    затем самое раннее."""
    for code in codes:
        candidates = [r for r in rows if r["lang"] == code]
        if candidates:
            return min(candidates, key=lambda r: (-r["is_preferred"], -r["is_short"], r["alternatenameid"]))["name"]
    return None


def genuine(stored):
    """Сохранённые названия, которые действительно на своём языке, а не взяты с запасного."""
    return {lang: r["value"] for lang, r in stored.items()
            if r["source"] in ("geonames", "manual") or r["source"].startswith("geonames:")}


def resolve(rows, code, langs, latin, known=None):
    """Название на языке code: {value, source, review}.
    1) GeoNames на этом языке, включая другие коды того же письма (zh-cn для zh-hans);
    2) общий код без письма (zh) — с пометкой на проверку;
    3) запасные языки по цепочке: сохранённое название города (known), иначе GeoNames;
    4) основное имя GeoNames латиницей."""
    value = pick(rows, [code, *SAME_LANGUAGE.get(code, [])])
    if value:
        return {"value": value, "source": "geonames", "review": False}
    generic = GENERIC.get(code)
    if generic and (value := pick(rows, [generic])):
        return {"value": value, "source": f"geonames:{generic}", "review": True}
    for fallback in chain_of(code, langs)[1:]:
        value = (known or {}).get(fallback) or pick(rows, [fallback, *SAME_LANGUAGE.get(fallback, [])])
        if value:
            return {"value": value, "source": f"fallback:{fallback}", "review": True}
    return {"value": latin, "source": "latin", "review": True}


def describe(db, places):
    """Подписи для админки: название, регион и страна на языке UI_CHAIN. → (список, названия по id)."""
    codes = {p["country_code"] for p in places if p["country_code"]}
    regions = {f'{p["country_code"]}.{p["admin1_code"]}' for p in places if p["admin1_code"]}
    countries = {r["iso"]: r for r in db.execute(
        f"SELECT iso, name, geonameid FROM country WHERE iso IN ({placeholders(codes)})", list(codes))} if codes else {}
    admin1 = {r["code"]: r for r in db.execute(
        f"SELECT code, name, geonameid FROM admin1 WHERE code IN ({placeholders(regions)})", list(regions))} if regions else {}
    names = current_names(db, {p["geonameid"] for p in places}
                          | {r["geonameid"] for r in [*countries.values(), *admin1.values()] if r["geonameid"]})

    def ui_name(geonameid, default):
        return pick(names.get(geonameid, []), UI_CHAIN) or default

    result = []
    for p in places:
        c = countries.get(p["country_code"])
        a = admin1.get(f'{p["country_code"]}.{p["admin1_code"]}')
        result.append({
            "geonameid": p["geonameid"],
            "name": ui_name(p["geonameid"], p["name"]),
            "region": a and ui_name(a["geonameid"], a["name"]),
            "country": ui_name(c["geonameid"], c["name"]) if c else p["country_code"],
            "population": p["population"],
            "lat": p["latitude"],
            "lon": p["longitude"],
        })
    return result, names


def load_place(geonameid):
    with closing(ref()) as db:
        p = db.execute("SELECT * FROM place WHERE geonameid = ?", (geonameid,)).fetchone()
        if not p:
            raise HTTPException(404, f"geonameid {geonameid} не найден")
        (info,), names = describe(db, [p])
    return p, info, names.get(geonameid, [])


def stored_names(adb, geonameid):
    return {r["lang"]: dict(r) for r in adb.execute(
        "SELECT lang, value, source, review FROM city_name WHERE geonameid = ?", (geonameid,))}


# ---- страница и города

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/suggest")
def suggest(q: str = Query(max_length=100), limit: int = Query(10, ge=1, le=30)):
    """Подсказки при вводе: поиск по началу названия на любом языке."""
    key = norm_key(q)
    if len(key) < 2:
        return []
    with closing(ref()) as db:
        rows = db.execute(SUGGEST_SQL, {"key": key, "pattern": glob_escape(key) + "*", "limit": limit}).fetchall()
        items, _ = describe(db, rows) if rows else ([], {})
    with closing(app_db()) as adb:
        saved = {r[0] for r in adb.execute("SELECT geonameid FROM city")}
    return [{**item, "saved": item["geonameid"] in saved} for item in items]


@app.get("/api/place/{geonameid}")
def place(geonameid: int):
    """Карточка города: координаты и названия на языках из конфига, основные языки первыми.
    Если город сохранён — сохранённые названия, иначе предложенные из GeoNames."""
    cfg, langs = load_config()
    p, info, rows = load_place(geonameid)
    with closing(app_db()) as adb:
        saved_at = adb.execute("SELECT saved_at FROM city WHERE geonameid = ?", (geonameid,)).fetchone()
        stored = stored_names(adb, geonameid)
    known = genuine(stored)
    languages = []
    for lang in ordered(cfg):
        code = lang["code"]
        proposed = resolve(rows, code, langs, p["name"], known)
        row = stored.get(code)
        current = ({"value": row["value"], "source": row["source"], "review": bool(row["review"])}
                   if row else proposed)
        languages.append({"code": code, "main": bool(lang.get("main")), "chain": chain_of(code, langs),
                          **current, "suggested": proposed})
    return {**info, "saved_at": saved_at and saved_at[0], "languages": languages}


class CityIn(BaseModel):
    geonameid: int
    names: dict[str, str]


@app.post("/api/cities")
def save_city(body: CityIn):
    """Сохранить город: названия, которые человек изменил, помечаются как manual."""
    cfg, langs = load_config()
    p, info, rows = load_place(body.geonameid)
    with closing(app_db()) as adb:
        known = genuine(stored_names(adb, body.geonameid))
    stamp = now()
    records = []
    for lang in ordered(cfg):
        code = lang["code"]
        value = (body.names.get(code) or "").strip()
        if not value:
            raise HTTPException(422, f"Пустое название для языка {code}")
        proposed = resolve(rows, code, langs, p["name"], known)
        source = proposed["source"] if value == proposed["value"] else "manual"
        records.append((body.geonameid, code, value, source, 0, stamp))
    label = ", ".join(x for x in (info["name"], info["region"], info["country"]) if x)
    with closing(app_db()) as adb, adb:
        adb.execute("INSERT INTO city VALUES (?,?,?,?,?) ON CONFLICT (geonameid) DO UPDATE "
                    "SET label = excluded.label, saved_at = excluded.saved_at",
                    (body.geonameid, p["latitude"], p["longitude"], label, stamp))
        adb.executemany("INSERT INTO city_name VALUES (?,?,?,?,?,?) ON CONFLICT (geonameid, lang) DO UPDATE "
                        "SET value = excluded.value, source = excluded.source, review = 0, "
                        "updated_at = excluded.updated_at", records)
    return client(body.geonameid)


@app.delete("/api/cities/{geonameid}")
def delete_city(geonameid: int):
    """Удалить сохранённый город вместе с его названиями."""
    with closing(app_db()) as adb, adb:
        names = adb.execute("DELETE FROM city_name WHERE geonameid = ?", (geonameid,)).rowcount
        if not adb.execute("DELETE FROM city WHERE geonameid = ?", (geonameid,)).rowcount:
            raise HTTPException(404, "Город не сохранён")
    return {"geonameid": geonameid, "names_removed": names}


@app.get("/api/cities")
def cities():
    """Сохранённые города; review — сколько названий ждут проверки."""
    with closing(app_db()) as adb:
        rows = adb.execute("""SELECT c.geonameid, c.label, c.saved_at, coalesce(sum(n.review), 0) AS review
                              FROM city c LEFT JOIN city_name n USING (geonameid)
                              GROUP BY c.geonameid ORDER BY c.saved_at DESC""").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/client/{geonameid}")
def client(geonameid: int):
    """То, что получает клиент: только сохранённые данные и только языки из конфига, основные первыми."""
    cfg, _ = load_config()
    with closing(app_db()) as adb:
        city = adb.execute("SELECT latitude, longitude FROM city WHERE geonameid = ?", (geonameid,)).fetchone()
        if not city:
            raise HTTPException(404, "Город не сохранён")
        names = {r["lang"]: r["value"] for r in adb.execute(
            "SELECT lang, value FROM city_name WHERE geonameid = ?", (geonameid,))}
    return {"geonameid": geonameid, "lat": city["latitude"], "lon": city["longitude"],
            "names": {lang["code"]: names[lang["code"]] for lang in ordered(cfg) if lang["code"] in names}}


# ---- языки

def refill(codes, cfg, langs, overwrite_unreviewed):
    """Заполнить языки codes у сохранённых городов: там, где названия нет, а при overwrite_unreviewed —
    и там, где оно подставлено автоматически и ещё не проверено. Ручные и проверенные названия не трогаются.
    → (сколько названий записано, сколько из них на проверку)."""
    with closing(app_db()) as adb:
        ids = [r[0] for r in adb.execute("SELECT geonameid FROM city")]
        stored = {gid: stored_names(adb, gid) for gid in ids}
    if not ids:
        return 0, 0
    with closing(ref()) as db:
        latin = {r[0]: r[1] for r in db.execute(
            f"SELECT geonameid, name FROM place WHERE geonameid IN ({placeholders(ids)})", ids)}
        names = current_names(db, ids)
    order = sorted(codes, key=lambda c: len(chain_of(c, langs)))  # сначала языки, через которые идут остальные
    stamp = now()
    records = []
    for gid in ids:
        city = stored[gid]
        for code in order:
            row = city.get(code)
            if row and not (overwrite_unreviewed and row["review"]):
                continue
            s = resolve(names.get(gid, []), code, langs, latin.get(gid, ""), genuine(city))
            city[code] = {"value": s["value"], "source": s["source"], "review": int(s["review"])}
            records.append((gid, code, s["value"], s["source"], int(s["review"]), stamp))
    with closing(app_db()) as adb, adb:
        adb.executemany("INSERT INTO city_name VALUES (?,?,?,?,?,?) ON CONFLICT (geonameid, lang) DO UPDATE "
                        "SET value = excluded.value, source = excluded.source, review = excluded.review, "
                        "updated_at = excluded.updated_at", records)
    return len(records), sum(r[4] for r in records)


def coverage(code):
    """У скольких населённых пунктов в GeoNames есть название на этом языке (с другими кодами того же письма)."""
    codes = [code, *SAME_LANGUAGE.get(code, [])]
    with closing(ref()) as db:
        return db.execute("SELECT count(DISTINCT a.geonameid) FROM alt_name a JOIN place p USING (geonameid) "
                          f"WHERE a.lang IN ({placeholders(codes)}) AND a.is_current", codes).fetchone()[0]


@app.get("/api/languages")
def languages():
    """Языки конфига (основные первыми), их цепочки и самые длинные цепочки для показа."""
    cfg, langs = load_config()
    used_by = {}
    for lang in cfg["languages"]:
        used_by.setdefault(lang.get("fallback"), []).append(lang["code"])
    return {
        "terminal": TERMINAL,
        "chains": chain_groups(cfg, langs),
        "languages": [{"code": lang["code"], "fallback": lang.get("fallback"), "main": bool(lang.get("main")),
                       "chain": chain_of(lang["code"], langs), "used_by": used_by.get(lang["code"], []),
                       "geonames_codes": [lang["code"], *SAME_LANGUAGE.get(lang["code"], []),
                                          *([GENERIC[lang["code"]]] if lang["code"] in GENERIC else [])]}
                      for lang in ordered(cfg)],
    }


class LanguageIn(BaseModel):
    code: str
    fallback: str = TERMINAL


class FallbackIn(BaseModel):
    fallback: str


@app.post("/api/languages")
def add_language(body: LanguageIn):
    """Добавить язык с запасным языком из списка и сразу заполнить его у сохранённых городов."""
    code, fallback = body.code.strip().lower(), body.fallback.strip().lower()
    if not LANG_RE.match(code):
        raise HTTPException(422, f"Код языка «{code}» не похож на код ISO 639: ru, hy, zh-hans …")
    with lang_lock:
        cfg, langs = load_config()
        if code in langs:
            raise HTTPException(409, f"Язык {code} уже есть")
        if fallback not in langs:
            raise HTTPException(422, f"Запасной язык «{fallback}» должен быть из списка языков")
        cfg["languages"].append({"code": code, "fallback": fallback})
        langs = check_config(cfg)
        save_config(cfg)
    filled, review = refill([code], cfg, langs, overwrite_unreviewed=False)
    return {"code": code, "chain": chain_of(code, langs), "cities_filled": filled, "need_review": review,
            "places_with_language": coverage(code)}


@app.patch("/api/languages/{code}")
def change_fallback(code: str, body: FallbackIn):
    """Сменить запасной язык. Автоматически подставленные и ещё не проверенные названия этого языка
    и языков, чья цепочка через него проходит, пересчитываются."""
    fallback = body.fallback.strip().lower()
    with lang_lock:
        cfg, langs = load_config()
        if code not in langs:
            raise HTTPException(404, f"Языка {code} нет в конфиге")
        if code == TERMINAL:
            raise HTTPException(422, f"{TERMINAL} — конечный язык, запасного у него нет")
        if fallback not in langs or fallback == code:
            raise HTTPException(422, f"Запасной язык «{fallback}» должен быть другим языком из списка")
        if code in chain_of(fallback, langs):
            raise HTTPException(422, f"Получится цикл: {' → '.join(chain_of(fallback, langs))} → {fallback}")
        langs[code]["fallback"] = fallback
        langs = check_config(cfg)
        save_config(cfg)
    affected = [c for c in langs if code in chain_of(c, langs)]
    updated, review = refill(affected, cfg, langs, overwrite_unreviewed=True)
    return {"code": code, "chain": chain_of(code, langs), "names_updated": updated, "need_review": review}


@app.delete("/api/languages/{code}")
def remove_language(code: str):
    """Убрать язык вместе с его названиями. Нельзя убрать английский, основные языки приложения
    и язык, который служит запасным для других."""
    with lang_lock:
        cfg, langs = load_config()
        if code not in langs:
            raise HTTPException(404, f"Языка {code} нет в конфиге")
        if code == TERMINAL or langs[code].get("main"):
            raise HTTPException(422, f"{code} — основной язык приложения, его нельзя убрать")
        dependents = [c for c, lang in langs.items() if lang.get("fallback") == code]
        if dependents:
            raise HTTPException(422, f"{code} — запасной язык для {', '.join(dependents)}; сначала смените им запасной")
        cfg["languages"] = [lang for lang in cfg["languages"] if lang["code"] != code]
        save_config(cfg)
    with closing(app_db()) as adb, adb:
        removed = adb.execute("DELETE FROM city_name WHERE lang = ?", (code,)).rowcount
    return {"code": code, "names_removed": removed}


@app.get("/api/meta")
def meta():
    with closing(ref()) as db:
        info = json.loads(db.execute("SELECT value FROM meta WHERE key = 'info'").fetchone()[0])
    return {"places": info["counts"]["place"], "built_at": info["built_at"],
            "geonames_date": info["files"].get("cities500.zip", {}).get("last_modified")}
