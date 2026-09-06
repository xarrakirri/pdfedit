"""Стандартные однобайтовые кодировки простых шрифтов PDF.

Для простых шрифтов (Type1, TrueType, Type3) байт строки — это код символа,
а смысл кода задаётся кодировкой: именем (``/WinAnsiEncoding``) и/или таблицей
отличий ``/Differences``, где кодам сопоставлены **имена глифов**.

``WinAnsiEncoding`` совпадает с CP1252, ``MacRomanEncoding`` — с mac_roman,
поэтому таблицы строятся кодеками Python, а не выписываются вручную:
это короче и не даёт опечаток. ``StandardEncoding`` берётся из fontTools.
"""

from __future__ import annotations

from functools import lru_cache

from fontTools.agl import toUnicode


def _from_codec(codec: str, overrides: dict[int, str]) -> dict[int, str]:
    """Строит таблицу код → Unicode на основе однобайтового кодека Python."""
    table: dict[int, str] = {}
    for code in range(32, 256):
        try:
            char = bytes([code]).decode(codec)
        except UnicodeDecodeError:
            continue
        table[code] = char
    table.update(overrides)
    return table


# В WinAnsiEncoding код 160 — обычный пробел, 173 — дефис (в CP1252 это
# неразрывный пробел и мягкий перенос); приводим к тому, что видит читатель.
WIN_ANSI = _from_codec("cp1252", {0xA0: " ", 0xAD: "-", 0x7F: "•"})
MAC_ROMAN = _from_codec("mac_roman", {0xCA: " "})


@lru_cache(maxsize=1)
def _standard() -> dict[int, str]:
    from fontTools.encodings.StandardEncoding import StandardEncoding

    table: dict[int, str] = {}
    for code, glyph_name in enumerate(StandardEncoding):
        if not glyph_name or glyph_name == ".notdef":
            continue
        text = toUnicode(glyph_name)
        if text:
            table[code] = text
    return table


#: Кодировки, доступные по имени из словаря ``/Encoding``.
BASE_ENCODINGS: dict[str, dict[str, object]] = {
    "/WinAnsiEncoding": WIN_ANSI,
    "/MacRomanEncoding": MAC_ROMAN,
}


def base_encoding(name: str | None) -> dict[int, str]:
    """Возвращает таблицу код → Unicode для именованной базовой кодировки."""
    if name == "/WinAnsiEncoding":
        return dict(WIN_ANSI)
    if name == "/MacRomanEncoding":
        return dict(MAC_ROMAN)
    if name in ("/StandardEncoding", "/MacExpertEncoding", None):
        # MacExpertEncoding содержит типографские варианты глифов и почти не
        # встречается; StandardEncoding — разумное приближение для него.
        return dict(_standard())
    return dict(_standard())


#: Имена глифов для символов, которых нет в AGL, — используются при генерации
#: ``/Differences`` для расширенных подмножеств простых шрифтов.
def glyph_name_for(char: str) -> str:
    """Подбирает имя глифа для символа (обратное преобразование к AGL)."""
    from fontTools.agl import UV2AGL

    code = ord(char)
    name = UV2AGL.get(code)
    if name:
        return name
    return f"uni{code:04X}" if code <= 0xFFFF else f"u{code:06X}"
