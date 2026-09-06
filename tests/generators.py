"""Тестовые PDF, написанные разными «почерками».

Проверки целостности имеют смысл только на разнообразии: программа, которая
бережно правит файл от reportlab, легко может испортить файл из Word, потому
что тот пишет строки шестнадцатерично, прячет объекты в ``/ObjStm``, ведёт
таблицу ссылок потоком и дублирует текст в ``/ActualText``. Здесь собраны
четыре почерка, различающихся ровно тем, что важно для сохранения стиля.

Два из них настоящие: **reportlab** и **fpdf2** установлены и вызываются
по-настоящему. Word и LibreOffice на этой машине поставить нельзя, поэтому их
почерк воспроизводится вручную — по признакам, которые эти пакеты оставляют в
файле. Это честнее, чем притворяться: функции так и называются
(``word_style``, ``libreoffice_style``), и в них перечислено, какие именно
черты воспроизводятся. Для проверок сохранения стиля важен сам стиль, а не то,
какая программа его породила.

Ни один из документов не выдаёт себя за настоящий: тексты в них условные.
"""

from __future__ import annotations

import io
import os
import zlib
from pathlib import Path

import pikepdf

#: Наборы символов для подмножеств шрифта. Настоящий документ несёт весь
#: алфавит, которым набран, — фикстура должна быть такой же
RUSSIAN = "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
LATIN = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
DIGITS = "0123456789"

CANDIDATE_FONTS = [
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "C:/Windows/Fonts/times.ttf",
]


def pick_font() -> str:
    for path in CANDIDATE_FONTS:
        if os.path.isfile(path):
            return path
    raise RuntimeError("не найден TrueType-шрифт для генерации примеров")


def have_reportlab() -> bool:
    try:
        import reportlab  # noqa: F401
    except Exception:
        return False
    return True


def have_fpdf() -> bool:
    try:
        import fpdf  # noqa: F401
    except Exception:
        return False
    return True


# ----------------------------------------------------------------------
# Настоящие генераторы
# ----------------------------------------------------------------------

def by_reportlab(path: Path, lines=("Contract of supply N 17", "Amount: 100 000")) -> Path:
    """Файл, собранный reportlab: строки в скобках, таблица xref, сжатие потоков."""
    from reportlab.pdfgen import canvas

    doc = canvas.Canvas(str(path), pagesize=(612, 792))
    doc.setTitle("Проба")
    doc.setFont("Helvetica", 12)
    height = 700
    for line in lines:
        doc.drawString(72, height, line)
        height -= 20
    doc.save()
    return path


def by_fpdf(path: Path, lines=("Contract No 17", "Total: 100000")) -> Path:
    """Файл, собранный fpdf2: числа с двумя знаками, ``Tj``, сжатие уровнем 6."""
    from fpdf import FPDF

    doc = FPDF()
    doc.add_page()
    doc.set_font("helvetica", size=12)
    for line in lines:
        doc.cell(0, 10, line)
        doc.ln()
    doc.output(str(path))
    return path


# ----------------------------------------------------------------------
# Воспроизведение почерка офисных пакетов
# ----------------------------------------------------------------------

def _subset_program(font_path: str, characters: str) -> bytes:
    """Программа шрифта, урезанная до нужных символов — как в настоящих файлах."""
    from fontTools import subset
    from fontTools.ttLib import TTFont

    font = TTFont(font_path, recalcTimestamp=False, recalcBBoxes=False)
    options = subset.Options(notdef_outline=True, recalc_bounds=False,
                             recalc_timestamp=False)
    subsetter = subset.Subsetter(options)
    subsetter.populate(text=characters)
    subsetter.subset(font)
    buffer = io.BytesIO()
    font.save(buffer, reorderTables=False)
    font.close()
    return buffer.getvalue()


def _cid_widths(program: bytes, _gids=None) -> str:
    """``/W`` для ВСЕХ глифов подмножества.

    Именно для всех, а не только для встречающихся в тексте: настоящие
    генераторы перечисляют весь набор, и глиф, не упомянутый ни в ``/W``, ни в
    ``/ToUnicode``, справедливо считается сиротой.
    """
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(program), lazy=True)
    upem = font["head"].unitsPerEm
    order = font.getGlyphOrder()
    parts = []
    for gid in range(font["maxp"].numGlyphs):
        advance = font["hmtx"][order[gid]][0]
        parts.append(f"{gid} [{round(advance * 1000.0 / upem, 2)}]")
    font.close()
    return " ".join(parts)


def _simple_widths(program: bytes, first: int, last: int) -> tuple[str, dict[int, int]]:
    """``/Widths`` простого шрифта, посчитанные по самому шрифту.

    Придуманные ширины разошлись бы с таблицей ``hmtx`` — а это ровно тот
    признак, который ищут проверки. Фикстура обязана быть согласованной.
    """
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(program), lazy=True)
    upem = font["head"].unitsPerEm
    cmap = font.getBestCmap()
    order = font.getGlyphOrder()
    index = {name: gid for gid, name in enumerate(order)}
    values = []
    gids: dict[int, int] = {}
    for code in range(first, last + 1):
        name = cmap.get(code)
        if name is None:
            values.append("0")
            continue
        gid = index[name]
        gids[code] = gid
        values.append(f"{round(font['hmtx'][name][0] * 1000.0 / upem, 2)}")
    font.close()
    return " ".join(values), gids


def _gids_for(program: bytes, text: str) -> list[int]:
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(program), lazy=True)
    cmap = font.getBestCmap()
    order = font.getGlyphOrder()
    index = {name: gid for gid, name in enumerate(order)}
    gids = []
    for char in text:
        name = cmap.get(ord(char))
        gids.append(index.get(name, 0) if name else 0)
    font.close()
    return gids


def word_style(path: Path, text: str = "Иванов Иван Иванович", actual_text: bool = True) -> Path:
    """Почерк Microsoft Word.

    Воспроизводятся черты, из-за которых такие файлы ломаются при небрежной
    правке:

    * составной шрифт ``/Type0`` с ``/Identity-H`` и подмножеством глифов —
      текст в потоке записан **шестнадцатерично**, кодами глифов;
    * тегированная структура: ``/StructTreeRoot`` и ``/ActualText`` — копия
      текста словами, которую правка обязана обновить вместе с содержимым;
    * та же копия ещё и внутри потока, операндом ``BDC``;
    * ``/Producer`` офисного пакета, ``/CreationDate`` и ``/ModDate``;
    * сжатие уровнем 6 — как у zlib по умолчанию.

    Таблица ссылок сделана обычной (не потоком): при xref-потоке объекты
    попадают в ``/ObjStm``, а правка на месте туда не добирается — этот случай
    проверяется отдельно в :func:`word_style_objstm`.
    """
    font_path = pick_font()
    # Подмножество берётся с запасом — по всему алфавиту, а не по одной этой
    # строке. Так делают настоящие генераторы: в документе не одно слово, и
    # шрифт несёт все встреченные буквы. Узкое подмножество превратило бы
    # любую правку в расширение шрифта, а это уже другой случай
    program = _subset_program(font_path, RUSSIAN + LATIN + DIGITS + " .,-N")
    gids = _gids_for(program, text)
    hexed = "".join(f"{gid:04X}" for gid in gids)

    # Кириллица в строке PDF записывается UTF-16BE с меткой порядка байтов —
    # именно так её пишет Word, и именно в таком виде её надо уметь находить
    utf16 = ("\ufeff" + text).encode("utf-16-be").hex().upper()
    marked_open = (
        f"/Span <</ActualText <{utf16}>>> BDC\n" if actual_text else ""
    )
    marked_close = "EMC\n" if actual_text else ""
    # Вторая строка — с числом. Цифры в шрифте одной ширины, поэтому правка
    # числа не меняет ширину строки: это и самый частый случай в жизни
    # (опечатка в сумме), и единственный, где правка заведомо ложится на место
    amount = "Сумма 100000"
    amount_hex = "".join(f"{gid:04X}" for gid in _gids_for(program, amount))
    content = (
        "BT\n/F1 12 Tf\n72 700 Td\n"
        f"{marked_open}<{hexed}> Tj\n{marked_close}"
        f"0 -20 Td\n<{amount_hex}> Tj\n"
        "ET\n"
    ).encode("latin-1")
    packed = zlib.compress(content, 6)

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 9 0 R /MarkInfo << /Marked true >> >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> /StructParents 0 >>"),
        4: b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed)
           + packed + b"\nendstream",
        5: (b"<< /Type /Font /Subtype /Type0 /BaseFont /ABCDEF+TimesNewRomanPSMT "
            b"/Encoding /Identity-H /DescendantFonts [6 0 R] /ToUnicode 8 0 R >>"),
        6: (b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /ABCDEF+TimesNewRomanPSMT "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            b"/FontDescriptor 7 0 R /CIDToGIDMap /Identity /DW 1000 /W ["
            + _cid_widths(program, sorted(set(gids))).encode("ascii") + b"] >>"),
        7: (b"<< /Type /FontDescriptor /FontName /ABCDEF+TimesNewRomanPSMT /Flags 34 "
            b"/FontBBox [-568 -307 2000 1007] /ItalicAngle 0 /Ascent 891 /Descent -216 "
            b"/CapHeight 662 /StemV 80 /FontFile2 10 0 R >>"),
    }

    tounicode = _tounicode(gids + _gids_for(program, amount), text + amount)
    objects[8] = b"<< /Length %d >>\nstream\n" % len(tounicode) + tounicode + b"\nendstream"
    objects[9] = (b"<< /Type /StructTreeRoot /K [11 0 R] >>")
    objects[10] = (b"<< /Length %d /Length1 %d >>\nstream\n" % (len(program), len(program))
                   + program + b"\nendstream")
    objects[11] = (b"<< /Type /StructElem /S /P /P 9 0 R /Pg 3 0 R /K [0] /ActualText <"
                   + utf16.encode("ascii") + b"> >>") if actual_text else \
                  b"<< /Type /StructElem /S /P /P 9 0 R /Pg 3 0 R /K [0] >>"

    info = (b"<< /Producer (Microsoft\\256 Word for Microsoft 365) "
            b"/Creator (Microsoft\\256 Word for Microsoft 365) "
            b"/CreationDate (D:20240115103000+03'00') /ModDate (D:20240115103000+03'00') >>")
    return _assemble(path, objects, info=info, version=b"1.7")


def libreoffice_style(path: Path, text: str = "Petrov Petr") -> Path:
    """Почерк LibreOffice.

    Отличается от предыдущего ровно тем, что важно для сохранения стиля:

    * простой шрифт ``/TrueType`` с ``/Differences`` — строки записаны
      **в скобках**, обычными кодами;
    * числа без дробной части там, где они целые;
    * ``/Producer`` LibreOffice, таблица ссылок обычная;
    * сжатие уровнем 9 — LibreOffice жмёт сильнее Word, и это видно по
      заголовку zlib.
    """
    font_path = pick_font()
    program = _subset_program(font_path, LATIN + DIGITS + " .,-")

    content = (
        "BT\n/F1 12 Tf\n72 700 Td\n"
        f"({text}) Tj\n"
        "0 -20 Td\n(Sum 100000) Tj\n"
        "ET\n"
    ).encode("latin-1")
    packed = zlib.compress(content, 9)

    widths, _gids = _simple_widths(program, 32, 126)
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"),
        4: b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed)
           + packed + b"\nendstream",
        5: (b"<< /Type /Font /Subtype /TrueType /BaseFont /BAAAAA+TimesNewRoman "
            b"/FirstChar 32 /LastChar 126 /Widths [" + widths.encode() + b"] "
            b"/FontDescriptor 6 0 R /Encoding << /Type /Encoding "
            b"/BaseEncoding /WinAnsiEncoding >> >>"),
        6: (b"<< /Type /FontDescriptor /FontName /BAAAAA+TimesNewRoman /Flags 4 "
            b"/FontBBox [-568 -307 2000 1007] /ItalicAngle 0 /Ascent 891 /Descent -216 "
            b"/CapHeight 662 /StemV 80 /FontFile2 7 0 R >>"),
        7: (b"<< /Length %d /Length1 %d >>\nstream\n" % (len(program), len(program))
            + program + b"\nendstream"),
    }
    info = (b"<< /Producer (LibreOffice 7.6) /Creator (Writer) "
            b"/CreationDate (D:20240301120000+01'00') >>")
    return _assemble(path, objects, info=info, version=b"1.6")


def _tounicode(gids, text: str) -> bytes:
    """Простая таблица ``/ToUnicode`` для составного шрифта."""
    pairs = []
    for gid, char in zip(gids, text):
        pairs.append(f"<{gid:04X}> <{ord(char):04X}>")
    body = "\n".join(pairs)
    return (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CMapName /A def\n/CMapType 2 def\n1 begincodespacerange\n<0000> <FFFF>\n"
        f"endcodespacerange\n{len(pairs)} beginbfchar\n{body}\nendbfchar\n"
        "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    ).encode("latin-1")


def _assemble(path: Path, objects: dict[int, bytes], info: bytes, version: bytes) -> Path:
    """Собирает файл с обычной таблицей ссылок и постоянным ``/ID``."""
    numbers = sorted(objects)
    info_number = max(numbers) + 1
    out = bytearray(b"%PDF-" + version + b"\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in numbers:
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    offsets[info_number] = len(out)
    out += b"%d 0 obj\n" % info_number + info + b"\nendobj\n"

    xref_at = len(out)
    last = info_number
    out += b"xref\n0 %d\n" % (last + 1)
    out += b"0000000000 65535 f \n"
    for number in range(1, last + 1):
        if number in offsets:
            out += b"%010d 00000 n \n" % offsets[number]
        else:
            out += b"0000000000 65535 f \n"
    out += (
        b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R "
        b"/ID [<0123456789ABCDEF0123456789ABCDEF><FEDCBA9876543210FEDCBA9876543210>] >>\n"
        b"startxref\n%d\n%%%%EOF\n" % (last + 1, info_number, xref_at)
    )
    Path(path).write_bytes(bytes(out))
    return path


def with_form_field(path: Path, value: str = "Ivanov") -> Path:
    """Документ с заполненным полем формы.

    Значение поля лежит в ``/V``, а нарисовано оно в потоке внешнего вида
    ``/AP /N``. Это две независимые копии одного текста, и правка обязана
    привести в согласие обе — иначе на экране остаётся старое значение при
    новом ``/V`` (или наоборот).
    """
    appearance = f"/Tx BMC\nq\nBT\n/Helv 10 Tf\n2 3 Td\n({value}) Tj\nET\nQ\nEMC\n".encode()
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [6 0 R] /DA (/Helv 0 Tf 0 g) "
           b"/DR << /Font << /Helv 5 0 R >> >> >> >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /Helv 5 0 R >> >> /Annots [6 0 R] >>"),
        4: b"<< /Length 44 >>\nstream\nBT /Helv 10 Tf 72 720 Td (Anketa) Tj ET\nendstream",
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        6: (b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (name) /V ("
            + value.encode("latin-1") + b") /DV (" + value.encode("latin-1") + b") "
            b"/TU (Familia) /Rect [72 690 300 710] /F 4 /P 3 0 R /DA (/Helv 10 Tf 0 g) "
            b"/AP << /N 7 0 R >> >>"),
        7: (b"<< /Type /XObject /Subtype /Form /BBox [0 0 228 20] "
            b"/Resources << /Font << /Helv 5 0 R >> >> /Length %d >>\nstream\n" % len(appearance)
            + appearance + b"\nendstream"),
    }
    info = b"<< /Producer (proba) /CreationDate (D:20240101000000Z) >>"
    return _assemble(path, objects, info=info, version=b"1.6")


def split_digits(path: Path, number: str = "1234567", actual_text: bool = True) -> Path:
    """Число, записанное по цифре отдельными операторами показа.

    Так выводят числа генераторы таблиц и бланков: каждая цифра ставится
    своим ``Td``, чтобы колонка выровнялась по разрядам. Читатель видит одно
    число, а в потоке это семь независимых ``Tj`` — и поиск по одному
    фрагменту не находит его вовсе.

    Рядом кладётся ``/ActualText`` со всей строкой: он показывает, что правка
    обязана обновить и скрытую копию — причём целиком, а не по цифре.
    """
    font_path = pick_font()
    program = _subset_program(font_path, DIGITS + LATIN + " N")
    widths, _gids = _simple_widths(program, 32, 126)

    from fontTools.ttLib import TTFont
    probe = TTFont(io.BytesIO(program), lazy=True)
    upem = probe["head"].unitsPerEm
    cmap = probe.getBestCmap()
    advance = {ch: probe["hmtx"][cmap[ord(ch)]][0] * 12.0 / upem for ch in number}
    probe.close()

    # Разметка охватывает всю последовательность целиком — так её и ставят
    # office-пакеты: одному логическому куску текста один /ActualText
    body = [f"/Span <</ActualText (N {number})>> BDC\n"] if actual_text else []
    body.append("BT\n/F1 12 Tf\n72.00 700.00 Td\n(N ) Tj\nET\n")
    x = 72.0 + 12.0 * 0.5
    for char in number:
        body.append(f"BT\n/F1 12 Tf\n{x:.2f} 700.00 Td\n({char}) Tj\nET\n")
        x += advance[char]
    if actual_text:
        body.append("EMC\n")
    content = "".join(body).encode("latin-1")
    packed = zlib.compress(content, 6)

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"),
        4: b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed)
           + packed + b"\nendstream",
        5: (b"<< /Type /Font /Subtype /TrueType /BaseFont /CAAAAA+SplitDigits "
            b"/FirstChar 32 /LastChar 126 /Widths [" + widths.encode() + b"] "
            b"/FontDescriptor 6 0 R /Encoding << /Type /Encoding "
            b"/BaseEncoding /WinAnsiEncoding >> >>"),
        6: (b"<< /Type /FontDescriptor /FontName /CAAAAA+SplitDigits /Flags 4 "
            b"/FontBBox [-568 -307 2000 1007] /ItalicAngle 0 /Ascent 891 /Descent -216 "
            b"/CapHeight 662 /StemV 80 /FontFile2 7 0 R >>"),
        7: (b"<< /Length %d /Length1 %d >>\nstream\n" % (len(program), len(program))
            + program + b"\nendstream"),
    }
    info = b"<< /Producer (proba) /CreationDate (D:20240101000000Z) >>"
    return _assemble(path, objects, info=info, version=b"1.6")


def form_split_digits(path: Path, number: str = "1234567") -> Path:
    """Поле формы, во внешнем виде которого число разбито по цифрам.

    Самый неудобный случай сразу целиком: значение поля лежит в ``/V``,
    видимое изображение — в отдельном потоке ``/AP /N``, да ещё и по цифре
    отдельными операторами. Правка обязана привести в согласие всё: и
    значение, и внешний вид.
    """
    font_path = pick_font()
    program = _subset_program(font_path, DIGITS + LATIN + " ")
    widths, _gids = _simple_widths(program, 32, 126)

    from fontTools.ttLib import TTFont
    probe = TTFont(io.BytesIO(program), lazy=True)
    upem = probe["head"].unitsPerEm
    cmap = probe.getBestCmap()
    advance = {ch: probe["hmtx"][cmap[ord(ch)]][0] * 10.0 / upem for ch in number}
    probe.close()

    parts = ["/Tx BMC\nq\n"]
    x = 2.0
    for char in number:
        parts.append(f"BT\n/F1 10 Tf\n{x:.2f} 3.00 Td\n({char}) Tj\nET\n")
        x += advance[char]
    parts.append("Q\nEMC\n")
    appearance = "".join(parts).encode("latin-1")

    objects: dict[int, bytes] = {
        1: (b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [6 0 R] "
            b"/DA (/F1 0 Tf 0 g) /DR << /Font << /F1 5 0 R >> >> >> >>"),
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> /Annots [6 0 R] >>"),
        4: b"<< /Length 40 >>\nstream\nBT /F1 10 Tf 72 720 Td (Blank) Tj ET\nendstream",
        5: (b"<< /Type /Font /Subtype /TrueType /BaseFont /DAAAAA+FormDigits "
            b"/FirstChar 32 /LastChar 126 /Widths [" + widths.encode() + b"] "
            b"/FontDescriptor 8 0 R /Encoding << /Type /Encoding "
            b"/BaseEncoding /WinAnsiEncoding >> >>"),
        6: (b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (amount) /V ("
            + number.encode("latin-1") + b") /DV (" + number.encode("latin-1") + b") "
            b"/Rect [72 690 300 710] /F 4 /P 3 0 R /DA (/F1 10 Tf 0 g) "
            b"/AP << /N 7 0 R >> >>"),
        7: (b"<< /Type /XObject /Subtype /Form /BBox [0 0 228 20] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Length %d >>\nstream\n"
            % len(appearance) + appearance + b"\nendstream"),
        8: (b"<< /Type /FontDescriptor /FontName /DAAAAA+FormDigits /Flags 4 "
            b"/FontBBox [-568 -307 2000 1007] /ItalicAngle 0 /Ascent 891 /Descent -216 "
            b"/CapHeight 662 /StemV 80 /FontFile2 9 0 R >>"),
        9: (b"<< /Length %d /Length1 %d >>\nstream\n" % (len(program), len(program))
            + program + b"\nendstream"),
    }
    info = b"<< /Producer (proba) /CreationDate (D:20240101000000Z) >>"
    return _assemble(path, objects, info=info, version=b"1.6")


def donor_document(path: Path, text: str = "Донорский текст ЖЩЭ") -> Path:
    """Документ-донор: несёт шрифт с кириллицей, которой нет в правимом файле.

    Нужен точному режиму: глифы разрешено брать только отсюда, и ниоткуда
    больше. Шрифт внедрён подмножеством — как в настоящих документах.
    """
    font_path = pick_font()
    program = _subset_program(font_path, RUSSIAN + LATIN + DIGITS + " .,-")
    gids = _gids_for(program, text)
    hexed = "".join(f"{gid:04X}" for gid in gids)
    content = (f"BT\n/F1 12 Tf\n72 700 Td\n<{hexed}> Tj\nET\n").encode("latin-1")
    packed = zlib.compress(content, 6)

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"),
        4: b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed)
           + packed + b"\nendstream",
        5: (b"<< /Type /Font /Subtype /Type0 /BaseFont /EAAAAA+DonorSerif "
            b"/Encoding /Identity-H /DescendantFonts [6 0 R] /ToUnicode 8 0 R >>"),
        6: (b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /EAAAAA+DonorSerif "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            b"/FontDescriptor 7 0 R /CIDToGIDMap /Identity /DW 1000 /W ["
            + _cid_widths(program).encode("ascii") + b"] >>"),
        7: (b"<< /Type /FontDescriptor /FontName /EAAAAA+DonorSerif /Flags 34 "
            b"/FontBBox [-568 -307 2000 1007] /ItalicAngle 0 /Ascent 891 /Descent -216 "
            b"/CapHeight 662 /StemV 80 /FontFile2 9 0 R >>"),
    }
    tounicode = _tounicode(gids, text)
    objects[8] = b"<< /Length %d >>\nstream\n" % len(tounicode) + tounicode + b"\nendstream"
    objects[9] = (b"<< /Length %d /Length1 %d >>\nstream\n" % (len(program), len(program))
                  + program + b"\nendstream")
    info = b"<< /Producer (donor) /CreationDate (D:20230101000000Z) >>"
    return _assemble(path, objects, info=info, version=b"1.7")


def latin_only(path: Path, text: str = "Total 100") -> Path:
    """Документ, в шрифте которого только латиница и цифры.

    Кириллицы в нём нет вовсе — значит, вставить её можно только глифами
    со стороны. Для точного режима это и проверяется: со стороны разрешён
    ровно один источник, донорский документ.
    """
    font_path = pick_font()
    program = _subset_program(font_path, LATIN + DIGITS + " .,-")
    widths, _gids = _simple_widths(program, 32, 126)
    content = (f"BT\n/F1 12 Tf\n72 700 Td\n({text}) Tj\nET\n").encode("latin-1")
    packed = zlib.compress(content, 6)

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"),
        4: b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed)
           + packed + b"\nendstream",
        5: (b"<< /Type /Font /Subtype /TrueType /BaseFont /FAAAAA+LatinOnly "
            b"/FirstChar 32 /LastChar 126 /Widths [" + widths.encode() + b"] "
            b"/FontDescriptor 6 0 R /Encoding << /Type /Encoding "
            b"/BaseEncoding /WinAnsiEncoding >> >>"),
        6: (b"<< /Type /FontDescriptor /FontName /FAAAAA+LatinOnly /Flags 4 "
            b"/FontBBox [-568 -307 2000 1007] /ItalicAngle 0 /Ascent 891 /Descent -216 "
            b"/CapHeight 662 /StemV 80 /FontFile2 7 0 R >>"),
        7: (b"<< /Length %d /Length1 %d >>\nstream\n" % (len(program), len(program))
            + program + b"\nendstream"),
    }
    info = b"<< /Producer (proba) /CreationDate (D:20240101000000Z) >>"
    return _assemble(path, objects, info=info, version=b"1.6")


def uncompressed_style(path: Path, text: str = "Plain text sample") -> Path:
    """Документ без сжатия вовсе — крайний случай для правки на месте."""
    content = f"BT\n/F1 12 Tf\n72 700 Td\n({text}) Tj\nET\n".encode("latin-1")
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"),
        4: b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    info = b"<< /Producer (proba) >>"
    return _assemble(path, objects, info=info, version=b"1.4")


def all_generators():
    """Все доступные почерки: ``(имя, функция)``."""
    found = [("word", word_style), ("libreoffice", libreoffice_style)]
    if have_reportlab():
        found.append(("reportlab", by_reportlab))
    if have_fpdf():
        found.append(("fpdf", by_fpdf))
    return found


def text_of(name: str) -> str:
    """Текст, который в документе этого почерка заведомо есть."""
    return {
        "word": "100000",
        "libreoffice": "100000",
        "reportlab": "N 17",
        "fpdf": "No 17",
    }[name]


def replacement_of(name: str) -> str:
    """Замена ТОЙ ЖЕ ШИРИНЫ — только цифры.

    Ширина здесь не мелочь. Если новый текст шире или уже старого, редактор
    подгоняет строку горизонтальным сжатием, а это две лишние инструкции
    ``Tz`` в потоке: правка перестаёт помещаться в исходную длину и уходит в
    дописанный слой. Цифры же почти во всех шрифтах одной ширины, и замена
    цифры на цифру ничего не двигает — это и самый частый случай правки
    в жизни (опечатка в номере или сумме).
    """
    return {
        "word": "900000",
        "libreoffice": "900000",
        "reportlab": "N 18",
        "fpdf": "No 18",
    }[name]
