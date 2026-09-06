"""Шрифты-доноры: извлечение программ шрифтов из PDF и их хранилище.

Зачем это нужно. Документы почти всегда несут не весь шрифт, а его
**подмножество** — только те глифы, что реально встретились в тексте. Набран
документ латиницей — кириллицы в нём нет, и вставить её нечем. Программа умеет
брать недостающие глифы из системных шрифтов, но системный Times и Times из
типографии заказчика — разные файлы, и подмена бывает заметна.

Здесь появляется третий источник, самый точный:

* **шрифты самого документа.** Один и тот же шрифт нередко внедрён в файл
  несколько раз разными подмножествами: в заголовках есть буквы, которых нет
  в основном тексте. Тогда донор искать вообще не нужно — он уже внутри.
* **шрифты других PDF.** Пользователь показывает программе документы, где
  нужные символы есть (скажем, другой документ того же издателя), и их шрифты
  пополняют личную библиотеку доноров. Дальше они используются наравне с
  системными, но раньше них.

Извлечённые программы складываются обычными файлами шрифтов, поэтому весь
существующий подбор — по семейству, начертанию и роду — работает с ними без
изменений.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pikepdf

from .fonts import SystemFont, _index_font_file, strip_subset_prefix

#: Ключи дескриптора, в которых лежит программа шрифта, и подходящее расширение
_PROGRAM_KEYS = ("/FontFile2", "/FontFile3", "/FontFile")

#: Начала файлов, по которым узнаётся формат программы шрифта
_TRUETYPE_SIGNATURES = (b"\x00\x01\x00\x00", b"true", b"ttcf")
_OPENTYPE_SIGNATURE = b"OTTO"


@dataclass
class ExtractedFont:
    """Извлечённая из PDF программа шрифта."""

    path: Path
    base_font: str
    kind: str          # truetype | opentype
    usable: bool       # можно ли использовать как донора
    note: str = ""

    @property
    def name(self) -> str:
        return strip_subset_prefix(self.base_font) or self.path.stem


def library_dir() -> Path:
    """Каталог личной библиотеки доноров.

    Лежит рядом с прочими пользовательскими данными, а не во временных файлах:
    библиотека собирается один раз и служит долго.
    """
    override = os.environ.get("PDFEDIT_FONT_LIBRARY")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "pdfedit" / "fonts"


def _safe_name(base_font: str) -> str:
    """Делает из имени шрифта пригодное имя файла."""
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", strip_subset_prefix(base_font)) or "font"
    return clean[:60].strip("_.") or "font"


def _program_kind(data: bytes) -> str | None:
    """Определяет формат программы шрифта по её первым байтам."""
    head = data[:4]
    if head in _TRUETYPE_SIGNATURES:
        return "truetype"
    if head == _OPENTYPE_SIGNATURE:
        return "opentype"
    return None


def _font_dicts(pdf: pikepdf.Pdf) -> Iterable[pikepdf.Object]:
    """Перебирает словари шрифтов документа, не повторяясь.

    У составного шрифта пометку ``/Type /Font`` носят два объекта: внешняя
    обёртка Type0 и вложенный в неё CIDFont. Разбирать нужно только обёртку —
    она знает и о кодировке, и о вложенном шрифте, — иначе каждый такой шрифт
    попадёт в список дважды.
    """
    fonts: list[pikepdf.Object] = []
    descendants: set[tuple[int, int]] = set()
    seen: set[tuple[int, int]] = set()

    for index in range(1, len(pdf.objects)):
        try:
            obj = pdf.objects[index]
            if not isinstance(obj, pikepdf.Dictionary):
                continue
            if str(obj.get("/Type", "")) != "/Font":
                continue
            key = obj.objgen
            if key in seen:
                continue
            seen.add(key)
            fonts.append(obj)
            for child in obj.get("/DescendantFonts", []) or []:
                try:
                    descendants.add(child.objgen)
                except Exception:
                    continue
        except Exception:
            continue

    for font in fonts:
        try:
            if font.objgen in descendants:
                continue
        except Exception:
            pass
        yield font


def _restore_cmap(path: Path, char_to_gid: dict[str, int]) -> tuple[bool, str]:
    """Вписывает в файл шрифта таблицу соответствия символов глифам.

    Извлечённое из PDF подмножество почти всегда идёт без ``cmap``: для показа
    страницы она не нужна, коды глифов записаны прямо в потоке содержимого.
    Но донору такая таблица необходима — иначе на вопрос «есть ли у тебя буква
    Ж» шрифт ответить не может. Соответствие берётся из документа и
    записывается в файл, после чего шрифт становится обычным, самодостаточным.
    """
    from fontTools.ttLib import TTFont, newTable

    font = None
    try:
        font = TTFont(str(path), recalcTimestamp=False, recalcBBoxes=False)
        order = font.getGlyphOrder()
        table: dict[int, str] = {}
        for char, gid in char_to_gid.items():
            if 0 <= gid < len(order):
                table[ord(char)] = order[gid]
        if not table:
            return False, "документ не сообщает, какие символы рисует этот шрифт"

        # Формат 4 покрывает основную многоязычную плоскость — этого хватает
        # для кириллицы, латиницы, греческого и почти всего, что встречается
        # в документах
        from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

        subtable = CmapSubtable.newSubtable(4)
        subtable.platformID, subtable.platEncID, subtable.language = 3, 1, 0
        subtable.cmap = {cp: name for cp, name in table.items() if cp <= 0xFFFF}

        cmap = newTable("cmap")
        cmap.tableVersion = 0
        cmap.tables = [subtable]
        font["cmap"] = cmap
        font.save(str(path))
        return True, f"таблица символов восстановлена по документу: {len(table)}"
    except Exception as exc:
        return False, f"не удалось восстановить таблицу символов: {exc}"
    finally:
        if font is not None:
            try:
                font.close()
            except Exception:
                pass


def extract_embedded_fonts(
    source: str | bytes | pikepdf.Pdf, target_dir: Path, password: str = ""
) -> list[ExtractedFont]:
    """Достаёт из PDF все внедрённые программы шрифтов.

    Файлы кладутся в ``target_dir`` под именем вида ``TimesNewRoman-1a2b3c4d.ttf``:
    хвост — отпечаток содержимого, поэтому один и тот же шрифт из разных
    документов не задваивается.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    if isinstance(source, pikepdf.Pdf):
        pdf, close_after = source, False
    else:
        import io

        stream = io.BytesIO(source) if isinstance(source, bytes) else source
        pdf, close_after = pikepdf.open(stream, password=password), True

    from .fonts import FontInfo

    results: list[ExtractedFont] = []
    try:
        for font_dict in _font_dicts(pdf):
            try:
                info = FontInfo("/donor", font_dict)
            except Exception as exc:
                results.append(ExtractedFont(
                    target_dir / "font", "?", "?", False, f"шрифт не разобран: {exc}"
                ))
                continue

            base_font = info.base_font_raw or "font"
            if not info.is_embedded:
                results.append(ExtractedFont(
                    target_dir / _safe_name(base_font), base_font, "нет", False,
                    "шрифт не внедрён в документ — брать нечего",
                ))
                continue

            try:
                data = bytes(info.font_program)
            except Exception as exc:
                results.append(ExtractedFont(
                    target_dir / _safe_name(base_font), base_font, "?", False,
                    f"поток шрифта не прочитан: {exc}",
                ))
                continue

            kind = _program_kind(data)
            if kind is None:
                # Type1 и «голый» CFF — не самостоятельные файлы шрифтов: чтобы
                # отдать их fontTools, программу пришлось бы пересобирать в
                # OpenType. Такие шрифты пропускаем осознанно.
                results.append(ExtractedFont(
                    target_dir / _safe_name(base_font), base_font, "cff/type1", False,
                    "формат Type1/CFF — как донор пока не поддерживается",
                ))
                continue

            digest = hashlib.sha256(data).hexdigest()[:8]
            suffix = ".ttf" if kind == "truetype" else ".otf"
            path = target_dir / f"{_safe_name(base_font)}-{digest}{suffix}"
            if not path.exists():
                path.write_bytes(data)

            # Имя шрифта восстанавливается всегда: без него донор не попадёт
            # в указатель и подобрать «тот же шрифт, что в документе» будет
            # нечем
            named = _restore_names(path, base_font)

            usable, note = _check_usable(path)
            if not usable:
                # Таблицы символов нет — восстанавливаем её по данным документа
                restored, restore_note = _restore_cmap(path, info.char_to_gid_map())
                if restored:
                    usable, note = _check_usable(path)
                    note = f"{restore_note}; {note}" if usable else restore_note
                else:
                    note = restore_note
            if named and usable:
                note = f"имя восстановлено; {note}"

            results.append(ExtractedFont(path, base_font, kind, usable, note))
            if not usable:
                # Файл, по которому нельзя найти символы, бесполезен как донор
                # и только замусорит каталог
                path.unlink(missing_ok=True)
    finally:
        if close_after:
            pdf.close()
    return results


def _restore_names(path: Path, base_font: str) -> bool:
    """Вписывает в файл шрифта таблицу имён, если её нет.

    Подмножества, вырезанные в PDF, почти всегда идут без таблицы ``name``:
    для показа страницы имя шрифта не нужно, оно есть в самом документе.
    Но донор без имени бесполезен — по нему нельзя понять ни семейства, ни
    начертания, и подбор «того же шрифта, что в документе» не срабатывает.
    Имя берётся из ``/BaseFont`` документа: ``ABCDEF+TinkoffSans-Medium``
    даёт семейство ``TinkoffSans`` и начертание ``Medium``.
    """
    from fontTools.ttLib import TTFont, newTable

    clean = strip_subset_prefix(base_font).lstrip("/")
    parts = re.split(r"[-,]", clean, maxsplit=1)
    family = parts[0] or clean or "Unknown"
    subfamily = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "Regular"

    font = None
    try:
        font = TTFont(str(path), recalcTimestamp=False, recalcBBoxes=False)
        if "name" in font and font["name"].getDebugName(1):
            return False  # имя уже есть, трогать нечего

        table = newTable("name")
        table.names = []
        # Записываем в обеих принятых кодировках: часть программ читает только
        # macOS-вариант (1,0), часть — только Windows (3,1)
        for platform, encoding, language in ((1, 0, 0), (3, 1, 0x409)):
            table.setName(family, 1, platform, encoding, language)
            table.setName(subfamily, 2, platform, encoding, language)
            table.setName(f"{family}-{subfamily}", 4, platform, encoding, language)
            table.setName(clean or f"{family}-{subfamily}", 6, platform, encoding, language)
        font["name"] = table
        font.save(str(path))
        return True
    except Exception:
        return False
    finally:
        if font is not None:
            try:
                font.close()
            except Exception:
                pass


def _check_usable(path: Path) -> tuple[bool, str]:
    """Проверяет, годится ли извлечённый файл в доноры.

    Донор обязан уметь отвечать на вопрос «есть ли у тебя такой символ», а для
    этого нужна таблица cmap. У подмножеств составных шрифтов её иногда нет:
    в документе соответствие символов задано отдельной таблицей ``/ToUnicode``,
    и внутри самой программы шрифта его не осталось.
    """
    from fontTools.ttLib import TTFont

    font = None
    try:
        font = TTFont(str(path), lazy=True, fontNumber=0)
        cmap = font.getBestCmap()
        if not cmap:
            return False, "в шрифте нет таблицы cmap — символы по нему не найти"
        return True, f"символов: {len(cmap)}"
    except Exception as exc:
        return False, f"не читается: {exc}"
    finally:
        if font is not None:
            try:
                font.close()
            except Exception:
                pass


def add_pdf_to_library(source: str, password: str = "") -> list[ExtractedFont]:
    """Пополняет библиотеку доноров шрифтами из указанного PDF."""
    return extract_embedded_fonts(source, library_dir(), password=password)


def library_fonts() -> list[SystemFont]:
    """Перечисляет шрифты, лежащие в библиотеке доноров."""
    directory = library_dir()
    if not directory.is_dir():
        return []
    fonts: list[SystemFont] = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() in (".ttf", ".otf", ".ttc", ".otc"):
            fonts.extend(_index_font_file(path))
    return fonts


def clear_library() -> int:
    """Опустошает библиотеку. Возвращает число удалённых файлов."""
    directory = library_dir()
    if not directory.is_dir():
        return 0
    count = 0
    for path in list(directory.iterdir()):
        if path.is_file():
            path.unlink()
            count += 1
        elif path.is_dir():
            shutil.rmtree(path)
    return count


def remove_from_library(pattern: str) -> list[str]:
    """Удаляет из библиотеки шрифты, чьё имя содержит указанную подстроку."""
    directory = library_dir()
    if not directory.is_dir():
        return []
    needle = pattern.lower()
    removed: list[str] = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and needle in path.name.lower():
            path.unlink()
            removed.append(path.name)
    return removed
