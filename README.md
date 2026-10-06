# CityStand

Демо-стенд: находит город по первым буквам, показывает его координаты и названия на нужных языках, даёт их поправить и сохранить. Данные — [GeoNames](https://www.geonames.org/) (CC BY 4.0). После установки стенд работает без интернета.

## Установка (один раз)

Нужны git, Python 3.10+, 1 ГБ оперативной памяти и 500 МБ на диске.

```bash
git clone https://github.com/ponomnaz/CityStand.git
cd CityStand
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python download.py   # скачать данные GeoNames, 219 МБ; оборвалось — запустить снова, докачает
.venv/bin/python build_db.py   # собрать из них справочник, около минуты
```

Если `python3 -m venv` ругается на `ensurepip` (так бывает на чистой Ubuntu), выполните `sudo apt install python3-venv` и повторите команду.

## Запуск

```bash
cd CityStand
.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

Остановить — `Ctrl+C`. Чтобы стенд работал и после закрытия терминала:

```bash
nohup .venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000 > stand.log 2>&1 &
pkill -f "uvicorn app:app"   # остановить
```

## Открыть

В браузере: <http://localhost:8000> на той же машине или `http://<IP сервера>:8000` с другой. Описание API — на `/docs`.

Пароля у стенда нет, поэтому не открывайте порт 8000 в интернет.

## Данные

Скачанные файлы и базы лежат в `data/` и в git не попадают. Сохранённые города — в `data/app.sqlite`, языки — в `languages.json`. Чтобы обновить данные GeoNames, остановите стенд и снова запустите `download.py` и `build_db.py`: сохранённые города останутся.
