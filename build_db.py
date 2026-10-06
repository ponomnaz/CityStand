"""Собирает справочник data/geonames.sqlite из файлов GeoNames в data/raw.

    python build_db.py

Справочник только читается сервером; сохранённые города лежат отдельно, в data/app.sqlite.
Перед пересборкой остановите сервер: готовый файл заменяет старый целиком.
Данные: GeoNames (geonames.org), CC BY 4.0.
"""
import io
import json
import re
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from textnorm import norm_key

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "geonames.sqlite"
LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")  # отсекает link, wkdt, post, iata …
STRESS = re.compile(r"(?<=[Ѐ-ӿ])́")  # знак ударения после кириллической буквы
LANG_ALIASES = {  # устаревшие коды языков из дампа → современные
    "arm": "hy", "geo": "ka", "gre": "el", "chi": "zh", "ger": "de", "fre": "fr", "rus": "ru", "per": "fa",
    "cze": "cs", "dut": "nl", "ice": "is", "mac": "mk", "may": "ms", "rum": "ro", "slo": "sk", "tib": "bo",
    "wel": "cy", "baq": "eu", "alb": "sq", "bur": "my", "iw": "he", "in": "id", "ji": "yi",
}

SCHEMA = """
CREATE TABLE place (               -- cities500.txt: населённые пункты от 500 жителей и центры районов
  geonameid    INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,      -- основное имя GeoNames, латиницей
  latitude     REAL NOT NULL,
  longitude    REAL NOT NULL,
  country_code TEXT,
  admin1_code  TEXT,
  population   INTEGER NOT NULL DEFAULT 0,
  feature_code TEXT                -- PPLC — столица, PPLA — центр региона …
);
CREATE TABLE country (iso TEXT PRIMARY KEY, name TEXT NOT NULL, geonameid INTEGER);
CREATE TABLE admin1 (code TEXT PRIMARY KEY, name TEXT NOT NULL, geonameid INTEGER);  -- code: «RU.38»
CREATE TABLE alt_name (            -- alternateNamesV2.txt: названия городов, регионов и стран
  alternatenameid INTEGER PRIMARY KEY,
  geonameid       INTEGER NOT NULL,
  lang            TEXT,            -- ru, en, zh-hans …; NULL — название без языка (только для поиска)
  name            TEXT NOT NULL,
  is_preferred    INTEGER NOT NULL,
  is_short        INTEGER NOT NULL,
  is_current      INTEGER NOT NULL -- 0: историческое, разговорное или с датой окончания
);
CREATE TABLE search_key (          -- нормализованные названия для подсказок
  key       TEXT NOT NULL,
  geonameid INTEGER NOT NULL,
  weight    INTEGER NOT NULL,      -- 3 основное имя, 1 другое актуальное, 0 историческое
  PRIMARY KEY (key, geonameid)
) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def norm_lang(code):
    """'zh-Hans' → 'zh-hans', 'arm' → 'hy'; псевдокоды (link, wkdt, post …) → None."""
    if not code:
        return None
    code = LANG_ALIASES.get(code.lower(), code.lower())
    return code if LANG_RE.match(code) else None


def read_lines(path):
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z, z.open(path.stem + ".txt") as f:
            yield from (line.rstrip("\r\n") for line in io.TextIOWrapper(f, encoding="utf-8"))
    else:
        with open(path, encoding="utf-8") as f:
            yield from (line.rstrip("\r\n") for line in f)


def load_countries(db):
    rows = [(c[0], c[4], int(c[16]) if c[16] else None)
            for c in (line.split("\t") for line in read_lines(RAW / "countryInfo.txt") if line and line[0] != "#")]
    db.executemany("INSERT INTO country VALUES (?,?,?)", rows)
    return len(rows)


def load_admin1(db):
    rows = [(c[0], c[1], int(c[3]) if c[3] else None)
            for c in (line.split("\t") for line in read_lines(RAW / "admin1CodesASCII.txt") if line)]
    db.executemany("INSERT OR REPLACE INTO admin1 VALUES (?,?,?)", rows)
    return len(rows)


def load_places(db):
    """Заодно возвращает ключи из asciiname и колонки alternatenames: их нет в alt_name."""
    rows, extra_keys = [], []
    for line in read_lines(RAW / "cities500.zip"):
        c = line.split("\t")
        gid = int(c[0])
        rows.append((gid, c[1], float(c[4]), float(c[5]), c[8] or None, c[10] or None, int(c[14] or 0), c[7]))
        extra_keys.append((gid, c[2], 3))
        extra_keys.extend((gid, name, 1) for name in c[3].split(",") if name)
    db.executemany("INSERT INTO place VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows), extra_keys


def load_alt_names(db):
    need = {str(r[0]) for r in db.execute(
        "SELECT geonameid FROM place UNION SELECT geonameid FROM admin1 UNION SELECT geonameid FROM country")
        if r[0] is not None}
    batch, total = [], 0
    for line in read_lines(RAW / "alternateNamesV2.zip"):
        c = line.split("\t")
        if len(c) < 4 or c[1] not in need:
            continue
        c += [""] * (10 - len(c))
        lang = norm_lang(c[2])
        if c[2] and not lang:  # ссылка, почтовый индекс, код аэропорта — не название
            continue
        current = not (c[6] == "1" or c[7] == "1" or c[9])
        name = STRESS.sub("", c[3])  # «Кузба́сс» → «Кузбасс»
        batch.append((int(c[0]), int(c[1]), lang, name, c[4] == "1", c[5] == "1", current))
        if len(batch) == 100_000:
            db.executemany("INSERT INTO alt_name VALUES (?,?,?,?,?,?,?)", batch)
            total += len(batch)
            batch.clear()
    db.executemany("INSERT INTO alt_name VALUES (?,?,?,?,?,?,?)", batch)
    return total + len(batch)


def build_search_keys(db, extra_keys):
    """При совпадении ключа остаётся первая запись, поэтому вставляем от самых весомых имён."""
    def insert(rows):
        db.executemany("INSERT OR IGNORE INTO search_key VALUES (?,?,?)",
                       ((key, gid, weight) for gid, name, weight in rows for key in [norm_key(name)] if key))

    insert(db.execute("SELECT geonameid, name, 3 FROM place").fetchall())
    insert(db.execute("""SELECT a.geonameid, a.name, a.is_current FROM alt_name a JOIN place p USING (geonameid)
                         ORDER BY a.is_current DESC""").fetchall())
    insert(extra_keys)
    return db.execute("SELECT count(*) FROM search_key").fetchone()[0]


def main():
    started = time.monotonic()
    tmp = DB_PATH.with_name(DB_PATH.name + ".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; PRAGMA cache_size = -200000;")
    db.executescript(SCHEMA)
    counts = {"country": load_countries(db), "admin1": load_admin1(db)}
    counts["place"], extra_keys = load_places(db)
    counts["alt_name"] = load_alt_names(db)
    db.execute("CREATE INDEX alt_name_geonameid ON alt_name (geonameid, lang)")
    counts["search_key"] = build_search_keys(db, extra_keys)
    manifest = RAW / "manifest.json"
    meta = {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "files": json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {},
            "counts": counts}
    db.execute("INSERT INTO meta VALUES ('info', ?)", (json.dumps(meta, ensure_ascii=False),))
    db.execute("ANALYZE")
    db.commit()
    db.close()
    tmp.replace(DB_PATH)
    print(", ".join(f"{k}: {v:,}".replace(",", " ") for k, v in counts.items()))
    print(f"готово: {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.0f} МБ, {time.monotonic() - started:.0f} с)")


if __name__ == "__main__":
    main()
