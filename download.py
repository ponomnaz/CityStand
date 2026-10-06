"""Скачивает дампы GeoNames в data/raw с докачкой.

Сначала пробует обычную загрузку. Если соединение «замирает» (на части сетей после ~16 КБ
данные перестают идти), переключается на загрузку кусками по 16 000 байт: каждый кусок —
отдельный HTTP Range-запрос в новом соединении. Архивы после загрузки проверяются по CRC.

    python download.py                       # все файлы
    python download.py cities500.zip         # только указанные
    python download.py --chunked             # сразу кусками
"""
import argparse
import http.client
import json
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://download.geonames.org/export/dump/"
FILES = ["countryInfo.txt", "admin1CodesASCII.txt", "cities500.zip", "alternateNamesV2.zip"]
RAW_DIR = Path(__file__).resolve().parent / "data" / "raw"
MANIFEST = RAW_DIR / "manifest.json"
USER_AGENT = "geoSearch-CityStand/0.1 (python-urllib)"
CHUNK = 16_000        # меньше порога, после которого соединение замирает
WORKERS = 3           # параллельных соединений в режиме кусков
STALL_SECONDS = 15    # окно, за которое меряем скорость обычной загрузки
MIN_SPEED = 20_000    # байт/с; медленнее — переключаемся на куски
NET_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException)


class Stalled(Exception):
    pass


def open_url(url, start=None, end=None, method="GET", timeout=STALL_SECONDS):
    headers = {"User-Agent": USER_AGENT}
    if start is not None:
        headers["Range"] = f"bytes={start}-{'' if end is None else end}"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers, method=method), timeout=timeout)


def remote_info(url):
    with open_url(url, method="HEAD", timeout=60) as r:
        return int(r.headers["Content-Length"]), r.headers.get("Last-Modified")


class Progress:
    def __init__(self, name, total, have):
        self.name, self.total, self.start_bytes = name, total, have
        self.started = self.last_print = time.monotonic()

    def update(self, have, force=False):
        now = time.monotonic()
        if force or now - self.last_print >= 5:
            speed = (have - self.start_bytes) / max(now - self.started, 1e-6)
            print(f"  {self.name}: {have / 1e6:.1f} / {self.total / 1e6:.1f} МБ "
                  f"({100 * have / self.total:.0f} %), {speed / 1e3:.0f} КБ/с", flush=True)
            self.last_print = now


def stream(url, part, total, progress):
    """Обычная загрузка с докачкой; бросает Stalled, если данные почти не идут."""
    have = part.stat().st_size if part.exists() else 0
    if have >= total:
        return
    with open_url(url, start=have or None) as r:
        mode = "ab"
        if have and r.status != 206:  # сервер проигнорировал Range — начинаем заново
            have, mode = 0, "wb"
        with open(part, mode) as f:
            window_started, window_bytes = time.monotonic(), 0
            while True:
                try:
                    chunk = r.read1(64 * 1024)
                except NET_ERRORS as e:
                    raise Stalled(f"нет данных {STALL_SECONDS} с ({e.__class__.__name__})") from e
                if not chunk:
                    break
                f.write(chunk)
                have += len(chunk)
                window_bytes += len(chunk)
                elapsed = time.monotonic() - window_started
                if elapsed >= STALL_SECONDS:
                    if window_bytes / elapsed < MIN_SPEED:
                        raise Stalled(f"{window_bytes / elapsed:.0f} Б/с")
                    window_started, window_bytes = time.monotonic(), 0
                progress.update(have)


def fetch_range(url, start, end):
    for attempt in range(6):
        try:
            with open_url(url, start, end, timeout=20) as r:
                data = r.read()
            if r.status == 206 and len(data) == end - start + 1:
                return data
        except NET_ERRORS:
            pass
        time.sleep(1 + 2 * attempt)
    raise RuntimeError(f"не удалось получить байты {start}–{end}")


def chunked(url, part, total, progress):
    """Загрузка кусками: куски скачиваются параллельно, а пишутся строго по порядку."""
    have = part.stat().st_size if part.exists() else 0
    ranges = [(s, min(s + CHUNK, total) - 1) for s in range(have, total, CHUNK)]
    pool = ThreadPoolExecutor(WORKERS)
    try:
        with open(part, "ab") as f:
            for data in pool.map(lambda r: fetch_range(url, *r), ranges):
                f.write(data)
                have += len(data)
                progress.update(have)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def download(name, base_url, force_chunked):
    url = base_url + name
    dest, part = RAW_DIR / name, RAW_DIR / (name + ".part")
    total, last_modified = remote_info(url)
    if dest.exists() and dest.stat().st_size == total:
        print(f"{name}: уже скачан ({total / 1e6:.1f} МБ)")
        return {"size": total, "last_modified": last_modified}
    progress = Progress(name, total, part.stat().st_size if part.exists() else 0)
    print(f"{name}: {total / 1e6:.1f} МБ, обновлён {last_modified}", flush=True)
    if not force_chunked:
        try:
            stream(url, part, total, progress)
        except (Stalled, *NET_ERRORS) as e:
            print(f"  {name}: соединение замирает ({e}) — качаю кусками по {CHUNK} байт", flush=True)
    if not part.exists() or part.stat().st_size < total:
        chunked(url, part, total, progress)
    progress.update(part.stat().st_size, force=True)
    if part.stat().st_size != total:
        raise SystemExit(f"{name}: размер {part.stat().st_size} вместо {total}")
    if name.endswith(".zip"):
        with zipfile.ZipFile(part) as z:
            broken = z.testzip()
        if broken:
            raise SystemExit(f"{name}: в архиве повреждён {broken}; удалите {part.name} и скачайте заново")
    part.replace(dest)
    print(f"{name}: готово", flush=True)
    return {"size": total, "last_modified": last_modified,
            "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def main():
    ap = argparse.ArgumentParser(description="Скачивание дампов GeoNames")
    ap.add_argument("files", nargs="*", default=FILES, help="имена файлов (по умолчанию все нужные)")
    ap.add_argument("--base-url", default=BASE_URL, help="зеркало дампов")
    ap.add_argument("--chunked", action="store_true", help="сразу качать кусками")
    args = ap.parse_args()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    for name in args.files:
        manifest[name] = {**manifest.get(name, {}), **download(name, args.base_url, args.chunked)}
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
