"""Нормализация названий для поиска: одинаковый ключ для «Плёс» и «плес», «São Paulo» и «sao paulo»."""
import re
import unicodedata

_MARKS_AFTER_LATIN = re.compile(r"(?<=[a-zα-ω])[̀-ͯ]+")  # латиница и греческий
_PUNCT = re.compile(r"[’'`ʼ\".,()«»]")
_SEPARATORS = re.compile(r"[\s\-‐-―_/]+")
_CITY_PREFIX = re.compile(r"^(г\.|город)\s*")


def norm_key(text: str) -> str:
    """«г. Ростов-на-Дону» → «ростов на дону», «Kazan’» → «kazan», «Йошкар-Ола» → «йошкар ола»."""
    s = unicodedata.normalize("NFKC", text).casefold()
    if not s.isascii():
        s = _CITY_PREFIX.sub("", s.replace("ё", "е"))
        # Диакритику снимаем только у латиницы и греческого: «й» остаётся «й»
        s = unicodedata.normalize("NFC", _MARKS_AFTER_LATIN.sub("", unicodedata.normalize("NFD", s)))
    s = _PUNCT.sub("", s)
    return _SEPARATORS.sub(" ", s).strip()


def glob_escape(key: str) -> str:
    """Экранирует спецсимволы GLOB, чтобы ключ искался буквально."""
    return re.sub(r"([*?\[])", r"[\1]", key)
