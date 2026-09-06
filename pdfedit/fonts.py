"""Модель шрифта PDF: кодировки, метрики, поиск системных аналогов.

Ключевая сложность редактирования текста в PDF в том, что в потоке содержимого
лежат не символы, а **коды глифов конкретного шрифта**. Чтобы заменить «Иванов»
на «Петров», нужно уметь:

1. по коду узнать символ (декодирование — для поиска текста);
2. по символу узнать код (кодирование — для записи нового текста);
3. по коду узнать ширину глифа (чтобы сохранить вёрстку строки).

Этот модуль решает все три задачи для простых (Type1/TrueType) и составных
(Type0/CID) шрифтов, а также определяет, внедрён ли шрифт в документ и есть ли
в системе подходящий донор для расширения набора глифов.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import pikepdf

from . import cmap as cmap_mod
from .encodings_tables import base_encoding
from .errors import FontError

#: Насыщенность начертания по его названию. Различать только «жирный /
#: не жирный» мало: Medium и Regular одного семейства при такой мерке
#: неотличимы, и донором для Medium мог стать заметно более лёгкий Regular.
WEIGHT_MARKS = (
    ("extrablack", 950), ("ultrablack", 950), ("extrabold", 800),
    ("ultrabold", 800), ("semibold", 600), ("demibold", 600),
    ("extralight", 200), ("ultralight", 200), ("hairline", 100),
    ("black", 900), ("heavy", 900), ("bold", 700), ("medium", 500),
    ("light", 300), ("thin", 100), ("book", 400), ("normal", 400),
    ("regular", 400),
)

#: Насыщенность, когда в названии о ней ничего не сказано
DEFAULT_WEIGHT = 400


def weight_from_name(name: str) -> int:
    """Определяет насыщенность по названию начертания."""
    low = name.lower()
    for mark, value in WEIGHT_MARKS:
        if mark in low:
            return value
    return DEFAULT_WEIGHT


#: Признаки шрифта с засечками в названии. Нужны потому, что флаг в
#: дескрипторе документа часто не заполнен.
SERIF_NAME_MARKS = (
    "times", "serif", "georgia", "garamond", "roman", "book", "minion",
    "cambria", "constantia", "palatino", "baskerville", "caslon", "charter",
    "didot", "utopia", "century", "schoolbook", "academy", "pt serif",
)

# Префикс подмножества шрифта: "ABCDEF+TimesNewRoman"
_SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")

#: Соответствие 14 стандартных шрифтов PDF внутренним именам PyMuPDF —
#: используется, чтобы получить настоящие метрики невнедрённых шрифтов.
_BASE14_TO_MUPDF = {
    "helvetica": "helv", "helvetica-bold": "hebo", "helvetica-oblique": "heit",
    "helvetica-boldoblique": "hebi", "courier": "cour", "courier-bold": "cobo",
    "courier-oblique": "coit", "courier-boldoblique": "cobi", "times-roman": "tiro",
    "times-bold": "tibo", "times-italic": "tiit", "times-bolditalic": "tibi",
    "symbol": "symb", "zapfdingbats": "zadb",
    # Часто встречающиеся синонимы
    "arial": "helv", "arial-bold": "hebo", "arialmt": "helv", "arial-boldmt": "hebo",
    "timesnewroman": "tiro", "timesnewromanpsmt": "tiro",
}


def strip_subset_prefix(name: str) -> str:
    """Убирает префикс подмножества (``ABCDEF+Arial`` → ``Arial``)."""
    return _SUBSET_PREFIX_RE.sub("", name.lstrip("/"))


def _normalize_font_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strip_subset_prefix(name).lower())


@dataclass
class FontStyle:
    """Начертание, вычленённое из имени шрифта."""

    bold: bool = False
    italic: bool = False
    weight: int = DEFAULT_WEIGHT

    @classmethod
    def from_name(cls, name: str, flags: int = 0) -> "FontStyle":
        low = strip_subset_prefix(name).lower()
        bold = "bold" in low or "black" in low or "heavy" in low
        italic = "italic" in low or "oblique" in low
        # Бит 18 (0x40000) флагов дескриптора — ForceBold, бит 7 (0x40) — Italic
        if flags & 0x40:
            italic = True
        return cls(bold=bold, italic=italic, weight=weight_from_name(low))


class FontInfo:
    """Разобранный шрифт страницы: кодировка, метрики, сведения о внедрении."""

    def __init__(self, resource_name: str, font_dict: pikepdf.Object):
        self.resource_name = resource_name
        self.dict = font_dict
        self.warnings: list[str] = []

        self.subtype = str(font_dict.get("/Subtype", "")) or ""
        raw_base = font_dict.get("/BaseFont")
        self.base_font_raw = str(raw_base) if raw_base is not None else ""
        self.base_font = strip_subset_prefix(self.base_font_raw)
        self.is_subset = bool(_SUBSET_PREFIX_RE.match(self.base_font_raw.lstrip("/")))
        self.is_composite = self.subtype == "/Type0"
        self.is_type3 = self.subtype == "/Type3"

        # Для Type0 реальные метрики и дескриптор лежат в потомке
        self.descendant: pikepdf.Object | None = None
        if self.is_composite:
            desc = font_dict.get("/DescendantFonts")
            if desc is not None and len(desc) > 0:
                self.descendant = desc[0]

        self.metrics_dict = self.descendant if self.descendant is not None else font_dict
        self.descriptor = self.metrics_dict.get("/FontDescriptor")
        self.flags = int(self.descriptor.get("/Flags", 0)) if self.descriptor is not None else 0
        self.style = FontStyle.from_name(self.base_font_raw, self.flags)

        self._encoding_cmap: cmap_mod.EncodingCMap | None = None
        self._to_unicode: dict[int, str] = {}
        # Один глиф нередко обслуживает несколько символов Unicode (обычный и
        # неразрывный пробел, разные виды дефисов и кавычек). Основное значение
        # хранится в _to_unicode, а все известные синонимы — здесь: они нужны,
        # чтобы новый текст закодировался даже при неточном совпадении символа.
        self._alt_texts: dict[int, list[str]] = {}
        self._from_unicode: dict[str, int] = {}
        self._max_key_len = 1
        self._widths: dict[int, float] = {}
        self._default_width: float | None = None
        self._mupdf_font = None
        self._usable_gids: frozenset[int] | None = None
        self._extra_usable: set[int] = set()
        self._cid_to_gid: dict[int, int] | None = None
        self._code_to_gid: dict[int, int] = {}

        # Порядок важен: наличие глифов нужно знать до построения обратного
        # отображения, которое выполняется в конце _load_encoding()
        self._load_glyph_availability()
        self._load_encoding()
        self._load_widths()

    # ------------------------------------------------------------------
    # Внедрение шрифта
    # ------------------------------------------------------------------
    @property
    def is_serif(self) -> bool:
        """Шрифт с засечками?

        Флаг дескриптора (бит 2) проставляют далеко не все программы: тот же
        Times New Roman из офисных пакетов нередко идёт с нулевым флагом.
        Поэтому в дополнение к флагу распознаём знакомые названия — иначе
        замена в документе, набранном Times, получит рубленый шрифт и будет
        бросаться в глаза сильнее, чем сама правка.
        """
        if self.flags & 2:
            return True
        name = _normalize_font_name(self.base_font)
        return any(mark in name for mark in SERIF_NAME_MARKS)

    @property
    def font_file_key(self) -> str | None:
        """Ключ дескриптора с программой шрифта: FontFile/FontFile2/FontFile3."""
        if self.descriptor is None:
            return None
        for key in ("/FontFile2", "/FontFile3", "/FontFile"):
            if key in self.descriptor:
                return key
        return None

    @property
    def is_embedded(self) -> bool:
        return self.font_file_key is not None

    @property
    def program_kind(self) -> str | None:
        """Формат внедрённой программы шрифта."""
        key = self.font_file_key
        if key is None:
            return None
        if key == "/FontFile":
            return "type1"
        if key == "/FontFile2":
            return "truetype"
        subtype = str(self.descriptor[key].get("/Subtype", ""))
        if subtype == "/OpenType":
            data = bytes(self.descriptor[key].read_bytes()[:4])
            return "truetype" if data in (b"\x00\x01\x00\x00", b"true") else "cff"
        return "cff"  # /Type1C, /CIDFontType0C

    @property
    def font_program(self) -> bytes | None:
        key = self.font_file_key
        if key is None:
            return None
        return self.descriptor[key].read_bytes()

    # ------------------------------------------------------------------
    # Кодировка
    # ------------------------------------------------------------------
    def _load_encoding(self) -> None:
        """Строит таблицы код → Unicode и Unicode → код."""
        # 1. Разбиение байтовой строки на коды
        if self.is_composite:
            enc = self.dict.get("/Encoding")
            if isinstance(enc, pikepdf.Name):
                name = str(enc)
                if name in ("/Identity-H", "/Identity-V"):
                    self._encoding_cmap = cmap_mod.identity_encoding_cmap()
                    if name == "/Identity-V":
                        self.warnings.append(
                            "вертикальное письмо (Identity-V): ширины считаются по горизонтали"
                        )
                else:
                    # Предопределённая CMap (например, UniGB-UCS2-H) — файла у нас
                    # нет, но почти все они двухбайтовые.
                    self._encoding_cmap = cmap_mod.identity_encoding_cmap()
                    self.warnings.append(
                        f"предопределённая CMap {name} не разобрана, коды считаются двухбайтовыми"
                    )
            elif isinstance(enc, pikepdf.Stream):
                self._encoding_cmap = cmap_mod.parse_encoding_cmap(enc.read_bytes())
            else:
                self._encoding_cmap = cmap_mod.identity_encoding_cmap()

        # 2. Код → Unicode: сначала /ToUnicode (самый надёжный источник)
        tu = self.dict.get("/ToUnicode")
        if isinstance(tu, pikepdf.Stream):
            try:
                parsed = cmap_mod.parse_tounicode(tu.read_bytes())
                self._to_unicode = dict(parsed.mapping)
            except Exception as exc:  # повреждённая CMap не должна ронять разбор
                self.warnings.append(f"не удалось разобрать /ToUnicode: {exc}")

        # 3. Для простых шрифтов достраиваем из /Encoding
        if not self.is_composite:
            table = self._simple_encoding_table()
            for code, text in table.items():
                self._to_unicode.setdefault(code, text)

        # 4. Пробелы в таблице закрываем по самой программе шрифта.
        #    Реальные /ToUnicode часто неполны (характерный пример — пропущенный
        #    код пробела), а внедрённый файл шрифта содержит собственную cmap
        #    и имена глифов, из которых Unicode восстанавливается точно.
        self._augment_from_font_program()

        if not self._to_unicode:
            self.warnings.append(
                "не удалось определить кодировку: нет /ToUnicode и распознаваемого /Encoding"
            )

        self._rebuild_reverse_map()

    def _augment_from_font_program(self) -> None:
        """Дополняет таблицу код → Unicode данными из внедрённого шрифта."""
        kind = self.program_kind
        if kind not in ("truetype", "cff"):
            return
        try:
            gid_to_text = _glyph_id_unicode_map(self.font_program, kind)
        except Exception as exc:
            self.warnings.append(f"не удалось прочитать внедрённый шрифт: {exc}")
            return
        if not gid_to_text:
            return

        if self.is_composite:
            cid2gid = self.metrics_dict.get("/CIDToGIDMap")
            if isinstance(cid2gid, pikepdf.Stream):
                # Явная таблица CID → GID
                table = cid2gid.read_bytes()
                code_map = {}
                for cid in range(len(table) // 2):
                    gid = int.from_bytes(table[cid * 2 : cid * 2 + 2], "big")
                    if gid and gid in gid_to_text:
                        code_map[cid] = gid_to_text[gid]
            else:
                # /CIDToGIDMap /Identity (или отсутствует): CID == GID
                code_map = gid_to_text
        else:
            # У простого шрифта код отображается в глиф через собственную cmap
            # шрифта: (3,0) для символьных, (1,0) для остальных.
            try:
                code_map = _simple_font_code_map(self.font_program, kind)
            except Exception:
                code_map = {}

        for code, texts in code_map.items():
            if not texts:
                continue
            self._to_unicode.setdefault(code, texts[0])
            primary = self._to_unicode.get(code)
            for text in texts:
                if text != primary:
                    self._alt_texts.setdefault(code, []).append(text)

    def _simple_encoding_table(self) -> dict[int, str]:
        """Таблица код → Unicode для простого шрифта по ``/Encoding``."""
        from fontTools.agl import toUnicode

        enc = self.dict.get("/Encoding")
        base_name: str | None = None
        differences = None

        if isinstance(enc, pikepdf.Name):
            base_name = str(enc)
        elif isinstance(enc, pikepdf.Dictionary):
            if "/BaseEncoding" in enc:
                base_name = str(enc.BaseEncoding)
            differences = enc.get("/Differences")

        symbolic = bool(self.flags & 0x4) and not (self.flags & 0x20)
        if base_name is None and symbolic and self.is_embedded:
            # Символьный шрифт со встроенной кодировкой: без /Differences
            # опираться на стандартную таблицу нельзя.
            table: dict[int, str] = {}
        else:
            table = base_encoding(base_name)

        if differences is not None:
            code = 0
            for item in differences:
                if isinstance(item, (int, float)):
                    code = int(item)
                elif isinstance(item, pikepdf.Name):
                    glyph_name = str(item).lstrip("/")
                    text = toUnicode(glyph_name)
                    if text:
                        table[code] = text
                    code += 1
        return table

    def _rebuild_reverse_map(self) -> None:
        """Инвертирует таблицу код → Unicode (для кодирования нового текста).

        В обратное отображение попадают только коды с настоящими глифами:
        иначе новый текст молча превратился бы в пустое место на странице.
        """
        reverse: dict[str, int] = {}
        # Основные значения имеют приоритет; при дубликатах оставляем меньший
        # код — он обычно «настоящий», а не декоративный дубль глифа.
        for code in sorted(self._to_unicode):
            text = self._to_unicode[code]
            if text and self.has_glyph(code):
                reverse.setdefault(text, code)
        # Синонимы добавляются только там, где точного совпадения не нашлось
        for code in sorted(self._alt_texts):
            if not self.has_glyph(code):
                continue
            for text in self._alt_texts[code]:
                if text:
                    reverse.setdefault(text, code)
        self._from_unicode = reverse
        self._max_key_len = max((len(k) for k in reverse), default=1)

    # ------------------------------------------------------------------
    # Наличие глифа
    # ------------------------------------------------------------------
    def _load_glyph_availability(self) -> None:
        """Определяет, для каких кодов в шрифте есть настоящее изображение."""
        if not self.is_embedded:
            return
        kind = self.program_kind
        if kind is None:
            return
        program = self.font_program
        self._usable_gids = _usable_glyph_ids(program, kind)
        # Дальше строится соответствие «код → номер глифа». Оно нужно и тогда,
        # когда выяснить непустоту глифов не удалось: это независимые сведения.
        # Раньше здесь стоял досрочный выход, и у шрифтов, чью таблицу глифов
        # прочитать не вышло, соответствие не строилось вовсе — такой шрифт
        # нельзя было использовать как донора.
        if self.is_composite:
            table = self.metrics_dict.get("/CIDToGIDMap")
            if isinstance(table, pikepdf.Stream):
                raw = table.read_bytes()
                self._cid_to_gid = {
                    cid: int.from_bytes(raw[cid * 2 : cid * 2 + 2], "big")
                    for cid in range(len(raw) // 2)
                }
        else:
            # Для простого шрифта код → глиф разрешается через /Differences
            # (по имени глифа) либо через собственную cmap шрифта
            self._code_to_gid = self._build_simple_code_to_gid(program, kind)

    def _build_simple_code_to_gid(self, program: bytes | None, kind: str) -> dict[int, int]:
        mapping: dict[int, int] = {}
        name_to_gid = _glyph_name_to_id(program, kind) if program else {}
        encoding = self.dict.get("/Encoding")
        if isinstance(encoding, pikepdf.Dictionary):
            differences = encoding.get("/Differences")
            if differences is not None:
                code = 0
                for item in differences:
                    if isinstance(item, (int, float)):
                        code = int(item)
                    elif isinstance(item, pikepdf.Name):
                        gid = name_to_gid.get(str(item).lstrip("/"))
                        if gid is not None:
                            mapping[code] = gid
                        code += 1
        font_cmap = _simple_font_code_gid_map(program, kind) if program else {}
        for code, gid in font_cmap.items():
            mapping.setdefault(code, gid)
        return mapping

    def char_to_gid_map(self) -> dict[str, int]:
        """Соответствие «символ → номер глифа» по данным документа.

        Нужно, чтобы использовать внедрённый шрифт как донора. Сам файл
        шрифта на этот вопрос часто ответить не может: создающие программы
        выбрасывают из подмножества таблицу ``cmap``, потому что для показа
        она не нужна — коды глифов уже записаны прямо в потоке содержимого.
        Зато документ хранит обратное соответствие в ``/ToUnicode``, и по нему
        карту можно восстановить.

        Возвращаются только односимвольные значения с настоящими глифами:
        лигатуры (один глиф на «ffi») в качестве донора бесполезны.
        """
        mapping: dict[str, int] = {}
        for code, text in self._to_unicode.items():
            if len(text) != 1 or not self.has_glyph(code):
                continue
            if self.is_composite:
                cid = self.to_cid(code)
                gid = self._cid_to_gid.get(cid, cid) if self._cid_to_gid else cid
            else:
                gid = self._code_to_gid.get(code)
            if gid:
                mapping.setdefault(text, gid)
        return mapping

    def has_glyph(self, code: int) -> bool:
        """Есть ли у кода настоящее изображение глифа."""
        if code in self._extra_usable:
            return True
        if self._usable_gids is None:
            return True  # определить не удалось — не мешаем работе
        if self.is_composite:
            cid = self.to_cid(code)
            gid = self._cid_to_gid.get(cid, 0) if self._cid_to_gid is not None else cid
            return gid in self._usable_gids
        gid = getattr(self, "_code_to_gid", {}).get(code)
        if gid is None:
            return True  # соответствие кода глифу неизвестно — не блокируем
        return gid in self._usable_gids

    # ------------------------------------------------------------------
    # Метрики
    # ------------------------------------------------------------------
    def _load_widths(self) -> None:
        if self.is_composite:
            self._default_width = float(self.metrics_dict.get("/DW", 1000))
            warr = self.metrics_dict.get("/W")
            if warr is not None:
                self._parse_w_array(warr)
        else:
            first = self.dict.get("/FirstChar")
            widths = self.dict.get("/Widths")
            if widths is not None and first is not None:
                start = int(first)
                for offset, value in enumerate(widths):
                    try:
                        self._widths[start + offset] = float(value)
                    except (TypeError, ValueError):
                        continue
            if self.descriptor is not None and "/MissingWidth" in self.descriptor:
                self._default_width = float(self.descriptor.MissingWidth)

    def _parse_w_array(self, warr: pikepdf.Object) -> None:
        """Разбирает массив ``/W``: ``[c [w...] | cfirst clast w]``."""
        items = list(warr)
        i = 0
        while i < len(items):
            try:
                first = int(items[i])
            except (TypeError, ValueError):
                i += 1
                continue
            if i + 1 >= len(items):
                break
            nxt = items[i + 1]
            if isinstance(nxt, pikepdf.Array):
                for offset, value in enumerate(nxt):
                    self._widths[first + offset] = float(value)
                i += 2
            else:
                if i + 2 >= len(items):
                    break
                last, value = int(nxt), float(items[i + 2])
                if last - first <= 0x10000:
                    for cid in range(first, last + 1):
                        self._widths[cid] = value
                i += 3

    @property
    def _mupdf(self):
        """Ленивая загрузка метрик стандартного шрифта средствами PyMuPDF."""
        if self._mupdf_font is False:
            return None
        if self._mupdf_font is None:
            self._mupdf_font = False
            key = _normalize_font_name(self.base_font)
            style = ""
            if self.style.bold:
                style += "-bold"
            if self.style.italic:
                style += "-oblique" if "helvetica" in key or "courier" in key else "-italic"
            mu_name = _BASE14_TO_MUPDF.get(key + style) or _BASE14_TO_MUPDF.get(key)
            if mu_name:
                try:
                    from .mupdf import fitz

                    self._mupdf_font = fitz.Font(mu_name)
                except Exception:
                    self._mupdf_font = False
        return self._mupdf_font or None

    def width(self, code: int) -> float:
        """Ширина глифа в тысячных долях размера шрифта (единицы глифа)."""
        cid = self.to_cid(code)
        if cid in self._widths:
            return self._widths[cid]
        if not self.is_composite and code in self._widths:
            return self._widths[code]
        # Невнедрённый стандартный шрифт без /Widths — берём настоящие метрики
        mu = self._mupdf
        if mu is not None:
            text = self._to_unicode.get(code)
            if text:
                try:
                    return mu.glyph_advance(ord(text[0])) * 1000.0
                except Exception:
                    pass
        if self._default_width is not None:
            return self._default_width
        return 1000.0 if self.is_composite else 500.0

    @property
    def ascent(self) -> float:
        if self.descriptor is not None and "/Ascent" in self.descriptor:
            return float(self.descriptor.Ascent)
        return 750.0

    @property
    def descent(self) -> float:
        if self.descriptor is not None and "/Descent" in self.descriptor:
            return float(self.descriptor.Descent)
        return -250.0

    # ------------------------------------------------------------------
    # Декодирование / кодирование
    # ------------------------------------------------------------------
    def split_codes(self, raw: bytes) -> list[tuple[int, int]]:
        """Разбивает строку показа текста на пары (код, длина кода в байтах)."""
        if self.is_composite and self._encoding_cmap is not None:
            cm = self._encoding_cmap
            if cm.identity or not cm.codespaces:
                return [
                    (int.from_bytes(raw[i : i + 2], "big"), 2)
                    for i in range(0, len(raw) - len(raw) % 2, 2)
                ]
            codes: list[tuple[int, int]] = []
            pos = 0
            lengths = sorted({cs.nbytes for cs in cm.codespaces})
            while pos < len(raw):
                for nb in lengths:
                    chunk = raw[pos : pos + nb]
                    if len(chunk) == nb and any(cs.contains(chunk) for cs in cm.codespaces):
                        codes.append((int.from_bytes(chunk, "big"), nb))
                        pos += nb
                        break
                else:
                    nb = lengths[0]
                    codes.append((int.from_bytes(raw[pos : pos + nb], "big"), nb))
                    pos += nb
            return codes
        return [(b, 1) for b in raw]

    def to_cid(self, code: int) -> int:
        if self.is_composite and self._encoding_cmap is not None:
            return self._encoding_cmap.to_cid(code)
        return code

    @property
    def code_size(self) -> int:
        """Число байт в одном коде (для записи нового текста)."""
        if not self.is_composite:
            return 1
        cm = self._encoding_cmap
        if cm is None or cm.identity or not cm.codespaces:
            return 2
        return min(cs.nbytes for cs in cm.codespaces)

    def code_to_text(self, code: int) -> str:
        return self._to_unicode.get(code, "")

    def decode(self, raw: bytes) -> str:
        return "".join(self.code_to_text(code) for code, _ in self.split_codes(raw))

    def missing_chars(self, text: str) -> str:
        """Возвращает символы текста, которых нет в шрифте (без повторов)."""
        missing: list[str] = []
        for _, chunk, ok in self._encode_parts(text):
            if not ok:
                for ch in chunk:
                    if ch not in missing:
                        missing.append(ch)
        return "".join(missing)

    def _encode_parts(self, text: str) -> list[tuple[list[int], str, bool]]:
        """Жадно разбирает текст на последовательности кодов.

        Сначала пробуются длинные ключи, чтобы лигатуры (например «ﬁ»,
        закодированные одним глифом) сопоставлялись одним кодом.
        """
        parts: list[tuple[list[int], str, bool]] = []
        i = 0
        while i < len(text):
            matched = False
            for length in range(min(self._max_key_len, len(text) - i), 0, -1):
                chunk = text[i : i + length]
                code = self._from_unicode.get(chunk)
                if code is not None:
                    parts.append(([code], chunk, True))
                    i += length
                    matched = True
                    break
            if not matched:
                parts.append(([], text[i], False))
                i += 1
        return parts

    def encode(self, text: str) -> tuple[bytes, list[int], str]:
        """Кодирует текст в байты строки показа.

        Возвращает ``(байты, список кодов, отсутствующие символы)``. Символы,
        для которых нет глифа, в результат не попадают — вызывающий код должен
        либо расширить шрифт, либо отказаться от замены.

        Попутно отмечается, какой код какой текст изображает: код мог быть
        найден по внутренней таблице внедрённого шрифта, а в ``/ToUnicode``
        документа записи для него не оказаться. Тогда страница выглядела бы
        правильно, но копирование и поиск выдавали бы не тот текст — заметное
        расхождение между тем, что видно, и тем, что извлекается.
        """
        size = self.code_size
        out = bytearray()
        codes: list[int] = []
        missing: list[str] = []
        written: list[tuple[int, str]] = []
        for part_codes, chunk, ok in self._encode_parts(text):
            if ok:
                for code in part_codes:
                    out += code.to_bytes(size, "big")
                    codes.append(code)
                if len(part_codes) == 1:
                    written.append((part_codes[0], chunk))
            else:
                for ch in chunk:
                    if ch not in missing:
                        missing.append(ch)
        if not missing:
            self.note_written_codes(written)
        return bytes(out), codes, "".join(missing)

    def note_written_codes(self, pairs: Iterable[tuple[int, str]]) -> bool:
        """Приводит таблицу код → Unicode в соответствие с записанным текстом.

        Возвращает ``True``, если что-то изменилось и ``/ToUnicode`` документа
        нужно переписать.
        """
        changed = False
        for code, chunk in pairs:
            if self._to_unicode.get(code) != chunk:
                self._to_unicode[code] = chunk
                changed = True
        if changed:
            self._tounicode_dirty = True
            self._rebuild_reverse_map()
        return changed

    @property
    def tounicode_dirty(self) -> bool:
        """Нужно ли переписать ``/ToUnicode`` этого шрифта в документе."""
        return getattr(self, "_tounicode_dirty", False)

    def clear_tounicode_dirty(self) -> None:
        self._tounicode_dirty = False

    def register_glyph(self, code: int, text: str, width: float) -> None:
        """Регистрирует добавленный глиф в таблицах шрифта (после расширения)."""
        self._to_unicode[code] = text
        self._widths[self.to_cid(code)] = width
        self._extra_usable.add(code)
        self._rebuild_reverse_map()

    def metrics_state(self) -> dict:
        """Снимок изменяемых таблиц шрифта — для отката расширения.

        Нужен :class:`pdfedit.fontops.FontSnapshot`: если добавленные глифы в
        итоге никем не используются, шрифт возвращается к прежнему виду
        целиком, вместе с этими таблицами.
        """
        return {
            "to_unicode": dict(self._to_unicode),
            "widths": dict(self._widths),
            "extra_usable": set(self._extra_usable),
            # Через свойство: сам атрибут появляется только при первой правке
            "tounicode_dirty": self.tounicode_dirty,
        }

    def restore_metrics_state(self, state: dict) -> None:
        """Возвращает таблицы шрифта к снимку, снятому :meth:`metrics_state`."""
        self._to_unicode = dict(state["to_unicode"])
        self._widths = dict(state["widths"])
        self._extra_usable = set(state["extra_usable"])
        self._tounicode_dirty = state["tounicode_dirty"]
        self._rebuild_reverse_map()

    def used_codes(self) -> set[int]:
        """Коды, уже занятые в шрифте (для подбора свободных в простых шрифтах)."""
        codes = {code for code in self._to_unicode if self.has_glyph(code)}
        codes |= set(self._widths)
        return codes

    @property
    def to_unicode_map(self) -> dict[int, str]:
        return dict(self._to_unicode)

    def describe(self) -> str:
        bits = [self.resource_name, self.base_font_raw or "(без имени)", self.subtype]
        bits.append("внедрён: " + (self.program_kind or "нет"))
        if self.is_subset:
            bits.append("подмножество")
        return " | ".join(bits)


# ----------------------------------------------------------------------
# Чтение внедрённых программ шрифтов
# ----------------------------------------------------------------------

def _open_font_program(data: bytes, kind: str):
    """Открывает внедрённую программу шрифта средствами fontTools."""
    from io import BytesIO

    from fontTools.ttLib import TTFont

    if kind == "truetype" or data[:4] in (b"\x00\x01\x00\x00", b"true", b"ttcf", b"OTTO"):
        # recalcTimestamp=False сохраняет исходную дату изменения шрифта.
        # По умолчанию fontTools записал бы текущее время, и внедрённый шрифт
        # выдал бы дату правки документа — при том что сам документ её не
        # раскрывает.
        return TTFont(BytesIO(data), lazy=True, fontNumber=0,
                      recalcTimestamp=False)
    return None  # «голый» CFF обрабатывается отдельно


@lru_cache(maxsize=64)
def _glyph_id_unicode_map_cached(data: bytes, kind: str) -> dict[int, tuple[str, ...]]:
    """Номер глифа → все символы Unicode, которые он изображает.

    Список упорядочен по предпочтительности: сначала «обычные» символы с
    меньшим кодом (например U+0020), затем их варианты (U+00A0).
    """
    from fontTools.agl import toUnicode

    collected: dict[int, set[str]] = {}
    font = _open_font_program(data, kind)
    if font is not None:
        try:
            glyph_order = font.getGlyphOrder()
            name_to_gid = {name: gid for gid, name in enumerate(glyph_order)}
            # 1. Прямой источник: cmap самого шрифта (все подтаблицы Unicode)
            cmap_table = font.get("cmap")
            if cmap_table is not None:
                for sub in cmap_table.tables:
                    if sub.platformID == 3 and sub.platEncID == 0:
                        continue  # символьная подтаблица разбирается отдельно
                    try:
                        pairs = sub.cmap.items()
                    except Exception:
                        continue
                    for uni, gname in pairs:
                        gid = name_to_gid.get(gname)
                        if gid is not None and 0 <= uni <= 0x10FFFF:
                            collected.setdefault(gid, set()).add(chr(uni))
            # 2. Оставшиеся глифы — по именам через таблицу AGL
            for gid, gname in enumerate(glyph_order):
                if gid in collected or not gname or gname == ".notdef":
                    continue
                text = toUnicode(gname)
                if text:
                    collected.setdefault(gid, set()).add(text)
        finally:
            try:
                font.close()
            except Exception:
                pass
        return {gid: tuple(sorted(texts)) for gid, texts in collected.items()}

    result: dict[int, tuple[str, ...]] = {}

    # «Голый» CFF (/FontFile3 /Type1C, /CIDFontType0C): имена глифов из charset
    try:
        from io import BytesIO

        from fontTools.cffLib import CFFFontSet

        cff = CFFFontSet()
        cff.decompile(BytesIO(data), None)
        top = cff[cff.fontNames[0]]
        charset = top.charset
        for gid, gname in enumerate(charset):
            if not gname or gname == ".notdef":
                continue
            text = toUnicode(gname)
            if text:
                result[gid] = (text,)
    except Exception:
        pass
    return result


def _glyph_id_unicode_map(data: bytes | None, kind: str) -> dict[int, tuple[str, ...]]:
    """Отображение «номер глифа → Unicode» для внедрённой программы шрифта."""
    if not data:
        return {}
    return _glyph_id_unicode_map_cached(data, kind)


@lru_cache(maxsize=64)
def _simple_font_code_map_cached(data: bytes, kind: str) -> dict[int, str]:
    """Отображение «код (0–255) → Unicode» по собственной cmap простого шрифта."""
    result: dict[int, str] = {}
    font = _open_font_program(data, kind)
    if font is None:
        return result
    try:
        cmap_table = font.get("cmap")
        if cmap_table is None:
            return result
        from fontTools.agl import toUnicode

        # (3,0) — символьная кодировка Windows: коды лежат в диапазоне F000–F0FF
        symbol = cmap_table.getcmap(3, 0)
        if symbol is not None:
            for code, gname in symbol.cmap.items():
                text = toUnicode(gname)
                if text:
                    result.setdefault(code & 0xFF, text)
        # (1,0) — Macintosh Roman: код и есть индекс в таблице
        mac = cmap_table.getcmap(1, 0)
        if mac is not None:
            for code, gname in mac.cmap.items():
                if code > 255:
                    continue
                text = toUnicode(gname)
                if text:
                    result.setdefault(code, text)
    except Exception:
        pass
    finally:
        try:
            font.close()
        except Exception:
            pass
    return result


def _simple_font_code_map(data: bytes | None, kind: str) -> dict[int, str]:
    if not data:
        return {}
    return _simple_font_code_map_cached(data, kind)


@lru_cache(maxsize=64)
def _usable_glyph_ids_cached(data: bytes, kind: str) -> frozenset[int] | None:
    """Номера глифов, у которых действительно есть изображение.

    Программы, создающие подмножества шрифтов, нередко оставляют исходную
    таблицу ``/ToUnicode`` нетронутой, а сами глифы «выпотрашивают»: контуров
    нет, ширина нулевая. Кодировать текст такими кодами нельзя — на странице
    получится пустое место, хотя текст будет копироваться. Поэтому наличие
    глифа проверяется по самой программе шрифта.

    Пустой глиф с ненулевой шириной (пробел) считается полноценным.
    """
    font = _open_font_program(data, kind)
    if font is None:
        return None  # CFF/Type1: подмножества там обычно не оставляют пустышек
    try:
        if "glyf" not in font:
            return None
        glyf = font["glyf"]
        hmtx = font["hmtx"]
        usable: set[int] = set()
        for gid, name in enumerate(font.getGlyphOrder()):
            try:
                glyph = glyf[name]
                if glyph.numberOfContours != 0 or hmtx[name][0] > 0:
                    usable.add(gid)
            except Exception:
                usable.add(gid)  # не смогли проверить — не мешаем
        return frozenset(usable)
    except Exception:
        return None
    finally:
        try:
            font.close()
        except Exception:
            pass


def _usable_glyph_ids(data: bytes | None, kind: str) -> frozenset[int] | None:
    if not data:
        return None
    return _usable_glyph_ids_cached(data, kind)


@lru_cache(maxsize=64)
def _simple_font_code_gid_map_cached(data: bytes, kind: str) -> dict[int, int]:
    """Код (0–255) → номер глифа по собственной cmap простого шрифта."""
    font = _open_font_program(data, kind)
    if font is None:
        return {}
    result: dict[int, int] = {}
    try:
        cmap_table = font.get("cmap")
        if cmap_table is None:
            return {}
        name_to_gid = {name: gid for gid, name in enumerate(font.getGlyphOrder())}
        for platform, encoding in ((3, 0), (1, 0), (3, 1)):
            sub = cmap_table.getcmap(platform, encoding)
            if sub is None:
                continue
            for code, gname in sub.cmap.items():
                gid = name_to_gid.get(gname)
                if gid is None:
                    continue
                key = code & 0xFF if (platform, encoding) == (3, 0) else code
                if key <= 255:
                    result.setdefault(key, gid)
    except Exception:
        pass
    finally:
        try:
            font.close()
        except Exception:
            pass
    return result


def _simple_font_code_gid_map(data: bytes | None, kind: str) -> dict[int, int]:
    if not data:
        return {}
    return _simple_font_code_gid_map_cached(data, kind)


@lru_cache(maxsize=64)
def _glyph_name_to_id(data: bytes, kind: str) -> dict[str, int]:
    font = _open_font_program(data, kind)
    if font is None:
        return {}
    try:
        return {name: gid for gid, name in enumerate(font.getGlyphOrder())}
    except Exception:
        return {}
    finally:
        try:
            font.close()
        except Exception:
            pass


# ----------------------------------------------------------------------
# Поиск системных шрифтов (доноров глифов)
# ----------------------------------------------------------------------

_FONT_DIRS_BY_PLATFORM = {
    "darwin": ["/System/Library/Fonts", "/Library/Fonts", "~/Library/Fonts"],
    "win32": ["C:/Windows/Fonts", "~/AppData/Local/Microsoft/Windows/Fonts"],
    "linux": ["/usr/share/fonts", "/usr/local/share/fonts", "~/.fonts", "~/.local/share/fonts"],
}


def system_font_dirs(extra: Iterable[str] = ()) -> list[Path]:
    key = "linux"
    if sys.platform.startswith("darwin"):
        key = "darwin"
    elif sys.platform.startswith("win"):
        key = "win32"
    dirs = [Path(p).expanduser() for p in _FONT_DIRS_BY_PLATFORM[key]]
    dirs += [Path(p).expanduser() for p in extra]
    return [d for d in dirs if d.is_dir()]


def _cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "pdfedit" / "fontindex.json"


@dataclass
class SystemFont:
    path: str
    index: int  # номер шрифта внутри .ttc
    ps_name: str
    family: str
    subfamily: str

    @property
    def style(self) -> FontStyle:
        low = (self.subfamily + " " + self.ps_name).lower()
        return FontStyle(
            bold="bold" in low or "black" in low,
            italic="italic" in low or "oblique" in low,
            weight=weight_from_name(low),
        )


def _index_font_file(path: Path) -> list[SystemFont]:
    """Читает таблицу имён шрифтового файла (включая коллекции .ttc)."""
    from fontTools.ttLib import TTFont, TTCollection

    entries: list[SystemFont] = []
    try:
        if path.suffix.lower() in (".ttc", ".otc"):
            coll = TTCollection(str(path), lazy=True)
            fonts = list(coll.fonts)
        else:
            fonts = [TTFont(str(path), lazy=True, fontNumber=0)]
    except Exception:
        return entries

    # Имена читаются у всех начертаний и только потом закрывается файл.
    # Начертания коллекции делят один открытый файл: если закрывать их по
    # одному прямо в цикле, то после первого же остальные перестают читаться
    # («seek of closed file») и молча теряются. Так из Times.ttc в указатель
    # попадало одно начертание из четырёх — жирный текст было нечем заменить,
    # и донором становился обычный Times.
    for idx, font in enumerate(fonts):
        try:
            name_table = font["name"]
            ps = name_table.getDebugName(6) or ""
            family = name_table.getDebugName(1) or ""
            subfamily = name_table.getDebugName(2) or ""
            entries.append(SystemFont(str(path), idx, ps, family, subfamily))
        except Exception:
            continue

    for font in fonts:
        try:
            font.close()
        except Exception:
            pass
    return entries


@lru_cache(maxsize=1)
def _load_index(extra_dirs: tuple[str, ...] = ()) -> list[SystemFont]:
    """Строит (и кэширует на диске) индекс системных шрифтов."""
    cache = _cache_path()
    dirs = system_font_dirs(extra_dirs)
    signature = sorted(
        (str(d), int(d.stat().st_mtime)) for d in dirs
    )
    if cache.is_file():
        try:
            blob = json.loads(cache.read_text("utf-8"))
            if blob.get("signature") == [list(x) for x in signature]:
                return [SystemFont(**item) for item in blob["fonts"]]
        except Exception:
            pass

    fonts: list[SystemFont] = []
    seen: set[str] = set()
    for directory in dirs:
        for path in sorted(directory.rglob("*")):
            if path.suffix.lower() not in (".ttf", ".otf", ".ttc", ".otc"):
                continue
            if str(path) in seen:
                continue
            seen.add(str(path))
            fonts.extend(_index_font_file(path))

    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {"signature": signature, "fonts": [f.__dict__ for f in fonts]},
                ensure_ascii=False,
            ),
            "utf-8",
        )
    except OSError:
        pass
    return fonts


@lru_cache(maxsize=16)
def fonts_in_dirs(dirs: tuple[str, ...]) -> list[SystemFont]:
    """Индексирует только указанные каталоги, без системных.

    Нужно для источников, которые должны просматриваться **раньше** системных
    шрифтов: программы шрифтов, извлечённые из самого документа, и личная
    библиотека доноров.
    """
    fonts: list[SystemFont] = []
    for name in dirs:
        directory = Path(name).expanduser()
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.suffix.lower() in (".ttf", ".otf", ".ttc", ".otc"):
                fonts.extend(_index_font_file(path))
    return fonts


def find_system_font(
    base_font: str,
    style: FontStyle | None = None,
    required_chars: str = "",
    extra_dirs: Iterable[str] = (),
    priority_dirs: Iterable[str] = (),
    only_priority: bool = False,
) -> SystemFont | None:
    """Ищет шрифт, пригодный как донор глифов.

    Отбор идёт по убыванию точности: точное PostScript-имя → семейство +
    начертание → нормализованное вхождение имени. Кандидат отбраковывается,
    если в нём нет нужных символов.

    Каталоги из ``priority_dirs`` просматриваются первыми и целиком: шрифт,
    взятый из самого документа, всегда лучше системного тёзки — это буквально
    та же программа, с теми же контурами и метриками.

    С ``only_priority`` поиск ими и ограничивается: системные шрифты не
    просматриваются вовсе. Это нужно точному режиму, где глифы разрешено
    брать только из заданного донора — системный тёзка с тем же именем имеет
    другие контуры, и подменять им донорский глиф значит менять вид документа.
    """
    priority = tuple(str(d) for d in priority_dirs)
    if priority:
        found = _pick_font(fonts_in_dirs(priority), base_font, style, required_chars)
        if found is not None:
            return found
    if only_priority:
        return None
    return _pick_font(_load_index(tuple(extra_dirs)), base_font, style, required_chars)


def _pick_font(
    fonts: list[SystemFont],
    base_font: str,
    style: FontStyle | None,
    required_chars: str,
) -> SystemFont | None:
    """Выбирает лучшего донора из готового списка."""
    if not fonts:
        return None
    target = _normalize_font_name(base_font)
    style = style or FontStyle.from_name(base_font)

    def style_ok(f: SystemFont) -> bool:
        s = f.style
        return s.bold == style.bold and s.italic == style.italic

    exact = [f for f in fonts if _normalize_font_name(f.ps_name) == target]
    family = [
        f for f in fonts
        if _normalize_font_name(f.family) == _normalize_font_name(
            re.split(r"[-,]", strip_subset_prefix(base_font))[0]
        )
    ]
    fuzzy = [
        f for f in fonts
        if target and (
            target in _normalize_font_name(f.ps_name)
            or _normalize_font_name(f.family) in target
        )
    ]

    def by_weight(bucket: list[SystemFont]) -> list[SystemFont]:
        """Внутри группы кандидаты идут от ближайшей насыщенности к дальней.

        Без этого Medium и Regular одного семейства равноправны, и донором
        для Medium мог оказаться заметно более лёгкий Regular — на странице
        это видно сразу.
        """
        return sorted(bucket, key=lambda f: abs(f.style.weight - style.weight))

    for bucket in (exact, [f for f in family if style_ok(f)], family,
                   [f for f in fuzzy if style_ok(f)], fuzzy):
        for candidate in by_weight(bucket):
            if not required_chars or font_has_chars(candidate, required_chars):
                return candidate
    return None


#: Запасные шрифты с засечками — для документов, набранных Times и подобными
FALLBACK_SERIF = [
    "Times New Roman", "Georgia", "PT Serif", "Charter", "Palatino",
    "Noto Serif", "Liberation Serif", "DejaVu Serif",
]

#: Запасные шрифты без засечек
FALLBACK_SANS = [
    "Arial", "Helvetica Neue", "Verdana", "Tahoma", "PT Sans",
    "Noto Sans", "Liberation Sans", "DejaVu Sans", "Segoe UI",
]

#: Последняя надежда: покрывает почти любую письменность, но начертание
#: только одно — обычное. Годится, лишь когда ничего другого не нашлось.
FALLBACK_LAST_RESORT = ["Arial Unicode MS"]


def find_fallback_font(
    required_chars: str,
    style: FontStyle | None = None,
    serif: bool | None = None,
    extra_dirs: Iterable[str] = (),
    priority_dirs: Iterable[str] = (),
    only_priority: bool = False,
) -> SystemFont | None:
    """Подбирает системный шрифт, когда донора того же семейства нет.

    Начертание здесь важнее широты охвата символов. Шрифт с огромным набором
    письменностей вроде Arial Unicode MS существует только в обычном
    начертании, и если подставить его вместо жирного, замена бросится в глаза:
    строка станет заметно тоньше соседних. Поэтому сначала ищется шрифт того
    же начертания (жирный к жирному, курсив к курсиву) и того же рода —
    с засечками или без, — и лишь потом всё остальное.

    Порядок отбора:

    1. предпочитаемое семейство нужного рода, начертание совпадает;
    2. любое семейство, начертание совпадает;
    3. предпочитаемое семейство, начертание любое;
    4. что угодно с нужными символами.

    С ``only_priority`` перебор ограничен приоритетными каталогами: подставлять
    системный шрифт в точном режиме нельзя ни при каких условиях.
    """
    priority = tuple(str(d) for d in priority_dirs)
    if priority:
        pool = [f for f in fonts_in_dirs(priority) if font_has_chars(f, required_chars)]
        chosen = _pick_fallback(pool, required_chars, style, serif)
        if chosen is not None:
            return chosen
    if only_priority:
        return None
    fonts = [f for f in _load_index(tuple(extra_dirs))
             if font_has_chars(f, required_chars)]
    return _pick_fallback(fonts, required_chars, style, serif)


def _pick_fallback(
    fonts: list[SystemFont],
    required_chars: str,
    style: FontStyle | None,
    serif: bool | None,
) -> SystemFont | None:
    """Выбирает запасной шрифт из готового списка."""
    if not fonts:
        return None
    style = style or FontStyle()

    def matches_style(font: SystemFont) -> bool:
        candidate = font.style
        return candidate.bold == style.bold and candidate.italic == style.italic

    # Род шрифта: если о нём ничего не известно, начинаем с рубленых —
    # они чаще встречаются в документах
    if serif:
        preferred = FALLBACK_SERIF + FALLBACK_SANS
    else:
        preferred = FALLBACK_SANS + FALLBACK_SERIF
    preferred = preferred + FALLBACK_LAST_RESORT

    by_family: dict[str, list[SystemFont]] = {}
    for font in fonts:
        by_family.setdefault(_normalize_font_name(font.family), []).append(font)

    def closest(bucket: list[SystemFont]) -> list[SystemFont]:
        return sorted(bucket, key=lambda f: abs(f.style.weight - style.weight))

    for name in preferred:
        for candidate in closest(by_family.get(_normalize_font_name(name), [])):
            if matches_style(candidate):
                return candidate

    for candidate in closest(fonts):
        if matches_style(candidate):
            return candidate

    for name in preferred:
        family = by_family.get(_normalize_font_name(name))
        if family:
            return family[0]

    return fonts[0]


@lru_cache(maxsize=256)
def _charset(path: str, index: int) -> frozenset[int]:
    from fontTools.ttLib import TTFont

    font = None
    try:
        font = TTFont(path, lazy=True, fontNumber=index)
        return frozenset(font.getBestCmap().keys())
    except Exception:
        return frozenset()
    finally:
        # Закрывать надо и после сбоя разбора: иначе на каждый нечитаемый
        # шрифт системы остаётся открытый файл
        if font is not None:
            try:
                font.close()
            except Exception:
                pass


def font_has_chars(font: SystemFont, chars: str) -> bool:
    if not chars:
        return True
    available = _charset(font.path, font.index)
    return all(ord(ch) in available for ch in chars)


def load_page_fonts(resources: pikepdf.Object) -> dict[str, FontInfo]:
    """Загружает все шрифты из словаря ресурсов страницы или XObject."""
    result: dict[str, FontInfo] = {}
    if resources is None or "/Font" not in resources:
        return result
    try:
        items = list(resources.Font.items())
    except Exception as exc:
        raise FontError(f"не удалось прочитать /Font в ресурсах: {exc}") from exc
    for name, font_dict in items:
        try:
            result[str(name)] = FontInfo(str(name), font_dict)
        except Exception:
            # Один битый шрифт не должен ломать разбор всей страницы: текст,
            # набранный им, просто окажется недоступен для правки
            continue
    return result
