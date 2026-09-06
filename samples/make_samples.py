"""Генератор тестовых PDF, покрывающих сложные для редактирования случаи.

Запуск::

    python samples/make_samples.py

Создаются документы с разными видами шрифтов и структуры — именно на них
проверяется, что замена текста работает не только на «удобных» файлах.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from pdfedit.mupdf import fitz  # PyMuPDF
import pikepdf

HERE = Path(__file__).resolve().parent

# Шрифты macOS; на других системах подставьте свои пути
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
    raise SystemExit("не найден TrueType-шрифт для генерации примеров")


FONT_PATH = pick_font()

#: Жирное начертание для второго составного шрифта; если его нет, берётся то же
BOLD_FONT_PATH = next(
    (path for path in (
        "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ) if os.path.isfile(path)),
    FONT_PATH,
)


def sample_contract(path: Path) -> None:
    """Многостраничный документ с кириллицей, полным внедрением и XMP."""
    doc = fitz.open()
    for page_number in (1, 2):
        page = doc.new_page()
        page.insert_font(fontname="F0", fontfile=FONT_PATH)
        y = 100
        lines = [
            ("ДОГОВОР № 17-А от 5 марта 2021 года", 16),
            ("", 0),
            ("Заказчик: ООО «Ромашка», ИНН 7701234567", 12),
            ("Исполнитель: ИП Иванов Иван Иванович", 12),
            ("Сумма договора: 150 000 (сто пятьдесят тысяч) рублей", 12),
            ("", 0),
            ("Срок выполнения работ — до 30 июня 2021 года.", 12),
            ("Оплата производится в течение 10 банковских дней.", 12),
            ("", 0),
            ("Latin text for mixed-script testing: Invoice No. 17-A", 12),
        ]
        if page_number == 2:
            lines = [
                ("Приложение № 1 к договору № 17-А", 14),
                ("", 0),
                ("Перечень работ: разработка, тестирование, внедрение.", 12),
                ("Ответственный исполнитель: Иванов И. И.", 12),
            ]
        for text, size in lines:
            if text:
                page.insert_text((72, y), text, fontname="F0", fontsize=size)
            y += 24 if size else 12

    doc.set_metadata({
        "title": "Договор № 17-А",
        "author": "Иванов Иван Иванович",
        "subject": "Договор оказания услуг",
        "keywords": "договор, услуги, 2021",
        "creator": "Отдел документооборота",
        "producer": "DocSystem 4.2",
        "creationDate": "D:20210305093000+03'00'",
        "modDate": "D:20210305094500+03'00'",
    })
    doc.save(str(path), garbage=0, deflate=True)
    doc.close()
    _add_xmp(path)


def _add_xmp(path: Path) -> None:
    """Добавляет XMP-поток, согласованный со словарём /Info."""
    with pikepdf.open(str(path), allow_overwriting_input=True) as pdf:
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
            meta["dc:title"] = "Договор № 17-А"
            meta["dc:creator"] = ["Иванов Иван Иванович"]
            meta["dc:description"] = "Договор оказания услуг"
            meta["pdf:Keywords"] = "договор, услуги, 2021"
            meta["pdf:Producer"] = "DocSystem 4.2"
            meta["xmp:CreatorTool"] = "Отдел документооборота"
            meta["xmp:CreateDate"] = "2021-03-05T09:30:00+03:00"
            meta["xmp:ModifyDate"] = "2021-03-05T09:45:00+03:00"
        pdf.save(str(path), fix_metadata_version=False, preserve_pdfa=False)


def sample_subset(path: Path) -> None:
    """Латиница с урезанным подмножеством шрифта.

    Замена латиницы на кириллицу здесь потребует расширения набора глифов —
    основной сложный сценарий для программы.
    """
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="F0", fontfile=FONT_PATH)
    page.insert_text((72, 100), "Invoice No. 17-A dated March 5, 2021", fontname="F0", fontsize=14)
    page.insert_text((72, 130), "Customer: Romashka LLC", fontname="F0", fontsize=14)
    page.insert_text((72, 160), "Total: 150000 RUB", fontname="F0", fontsize=14)
    doc.subset_fonts()  # оставляем в шрифте только использованные глифы
    doc.set_metadata({
        "title": "Invoice 17-A", "author": "Accounting Dept",
        "producer": "InvoiceGen 2.1", "creator": "InvoiceGen",
        "creationDate": "D:20210305120000+03'00'",
        "modDate": "D:20210305120000+03'00'",
    })
    doc.save(str(path), garbage=0, deflate=True)
    doc.close()


def sample_base14(path: Path) -> None:
    """Невнедрённый стандартный шрифт (простой шрифт с WinAnsiEncoding)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Standard Helvetica, not embedded", fontname="helv", fontsize=14)
    page.insert_text((72, 130), "Reference number: AB-1234", fontname="helv", fontsize=14)
    page.insert_text((72, 160), "Times Roman line here", fontname="tiro", fontsize=14)
    doc.set_metadata({"title": "Base-14 sample", "producer": "PlainPDF 1.0",
                      "creationDate": "D:20200101000000Z"})
    doc.save(str(path), garbage=0)
    doc.close()


def sample_simple_truetype(path: Path) -> None:
    """Внедрённый простой шрифт /TrueType с однобайтовой кодировкой.

    Такие шрифты собираются вручную: PyMuPDF всегда создаёт составные Type0,
    а простой вариант надо проверить отдельно.
    """
    from fontTools.ttLib import TTFont

    text = "Simple TrueType font sample - order 42"
    ttf = TTFont(FONT_PATH)
    upem = ttf["head"].unitsPerEm
    cmap = ttf.getBestCmap()
    hmtx = ttf["hmtx"]

    first_char, last_char = 32, 126
    widths = []
    for code in range(first_char, last_char + 1):
        glyph_name = cmap.get(code)
        advance = hmtx[glyph_name][0] if glyph_name else 0
        widths.append(round(advance * 1000 / upem))
    ttf.close()

    with open(FONT_PATH, "rb") as handle:
        program = handle.read()

    pdf = pikepdf.new()
    font_file = pdf.make_stream(program)
    font_file["/Length1"] = len(program)
    descriptor = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/FontDescriptor"),
        FontName=pikepdf.Name("/TimesNewRomanPSMT"),
        Flags=32, FontBBox=pikepdf.Array([-568, -307, 2000, 1007]),
        ItalicAngle=0, Ascent=891, Descent=-216, CapHeight=662, StemV=80,
        FontFile2=font_file,
    ))
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/Font"), Subtype=pikepdf.Name("/TrueType"),
        BaseFont=pikepdf.Name("/TimesNewRomanPSMT"),
        FirstChar=first_char, LastChar=last_char,
        Widths=pikepdf.Array(widths),
        Encoding=pikepdf.Name("/WinAnsiEncoding"),
        FontDescriptor=descriptor,
    ))
    content = (
        b"BT\n/F1 14 Tf\n72 700 Td\n(" + text.encode("cp1252") + b") Tj\nET\n"
        b"BT\n/F1 14 Tf\n72 670 Td\n[(Kerned) -300 (text) -300 (sample)] TJ\nET\n"
    )
    page = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/Page"),
        MediaBox=pikepdf.Array([0, 0, 595, 842]),
        Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
        Contents=pdf.make_stream(content),
    ))
    pdf.Root["/Pages"] = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/Pages"), Kids=pikepdf.Array([page]), Count=1,
    ))
    page["/Parent"] = pdf.Root["/Pages"]
    pdf.trailer["/Info"] = pdf.make_indirect(pikepdf.Dictionary(
        Title=pikepdf.String("Simple TrueType sample"),
        Producer=pikepdf.String("HandBuilt 0.1"),
        CreationDate=pikepdf.String("D:20190515101500+02'00'"),
    ))
    pdf.save(str(path))
    pdf.close()


def sample_xobject(path: Path) -> None:
    """Текст, спрятанный внутри Form XObject (типичный штамп или бланк)."""
    pdf = pikepdf.new()
    helvetica = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/Font"), Subtype=pikepdf.Name("/Type1"),
        BaseFont=pikepdf.Name("/Helvetica"), Encoding=pikepdf.Name("/WinAnsiEncoding"),
    ))
    form_content = b"BT\n/F1 12 Tf\n0 0 Td\n(Stamp: APPROVED by Smith) Tj\nET\n"
    form = pdf.make_stream(form_content)
    form["/Type"] = pikepdf.Name("/XObject")
    form["/Subtype"] = pikepdf.Name("/Form")
    form["/BBox"] = pikepdf.Array([0, 0, 300, 20])
    form["/Resources"] = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica))
    form = pdf.make_indirect(form)

    content = (
        b"BT\n/F1 14 Tf\n72 750 Td\n(Main page text: contract 2021) Tj\nET\n"
        b"q\n1 0 0 1 72 700 cm\n/Fx1 Do\nQ\n"
    )
    page = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/Page"),
        MediaBox=pikepdf.Array([0, 0, 595, 842]),
        Resources=pikepdf.Dictionary(
            Font=pikepdf.Dictionary(F1=helvetica),
            XObject=pikepdf.Dictionary(Fx1=form),
        ),
        Contents=pdf.make_stream(content),
    ))
    pdf.Root["/Pages"] = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/Pages"), Kids=pikepdf.Array([page]), Count=1,
    ))
    page["/Parent"] = pdf.Root["/Pages"]
    pdf.trailer["/Info"] = pdf.make_indirect(pikepdf.Dictionary(
        Title=pikepdf.String("XObject sample"), Producer=pikepdf.String("FormGen 3.0"),
    ))
    pdf.save(str(path))
    pdf.close()


def _subset_program(font_path: str, characters: str) -> bytes:
    """Программа шрифта, урезанная до нужных символов.

    Целый Times New Roman весит около 470 КБ и заслонил бы всё остальное в
    примере; настоящие документы несут именно подмножества.
    """
    from fontTools import subset
    from fontTools.ttLib import TTFont

    font = TTFont(font_path)
    subsetter = subset.Subsetter(subset.Options(notdef_outline=True, recalc_bounds=False))
    subsetter.populate(text=characters)
    subsetter.subset(font)
    buffer = io.BytesIO()
    font.save(buffer, reorderTables=False)
    font.close()
    return buffer.getvalue()


def _glyph_width(program: bytes, char: str) -> float:
    """Ширина глифа в тысячных долях кегля — из самой программы шрифта."""
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(program), lazy=True)
    try:
        name = font.getBestCmap()[ord(char)]
        return round(font["hmtx"][name][0] * 1000.0 / font["head"].unitsPerEm, 2)
    finally:
        font.close()


def sample_layered(path: Path) -> None:
    """Документ со «слоёной» структурой — самый сложный из примеров.

    Повторяет профиль, который дают отчётные генераторы вроде JasperReports:

    * страница нестандартного размера с группой прозрачности;
    * два составных шрифта ``/Type0`` с ``/Identity-H``, подмножествами глифов,
      ``/ToUnicode`` и ``/CIDToGIDMap /Identity``;
    * простой ``/TrueType`` с ``/Differences`` и **без** ``/ToUnicode`` — такой
      шрифт рисует значок, который из текста не извлекается;
    * четыре растровых изображения, два из них с полупрозрачной маской
      ``/SMask``;
    * ``/ExtGState`` с нулевой непрозрачностью;
    * аннотация-ссылка;
    * ``/Info`` со всеми полями, ``/ID``, без XMP, таблица ``xref``, PDF 1.5.

    Содержимое синтетическое: документ нужен как испытательный стенд для правки
    текста и проверки структуры, а не как образец какого-либо бланка.
    """
    import zlib

    # --- слой 1: текст двумя составными шрифтами (PyMuPDF внедряет Identity-H)
    doc = fitz.open()
    page = doc.new_page(width=270, height=451)
    page.insert_font(fontname="F1", fontfile=FONT_PATH)
    page.insert_font(fontname="F2", fontfile=BOLD_FONT_PATH)
    page.insert_text((20, 60), "ТЕСТОВЫЙ ДОКУМЕНТ", fontname="F2", fontsize=12)
    page.insert_text((20, 90), "Позиция первая", fontname="F1", fontsize=9)
    page.insert_text((20, 110), "Итого", fontname="F2", fontsize=14)
    page.insert_text((20, 130), "60", fontname="F1", fontsize=11)
    page.insert_text((20, 160), "Значение поля", fontname="F1", fontsize=9)
    page.insert_text((20, 180), "Иванов Иван Иванович", fontname="F1", fontsize=11)
    page.insert_text((20, 210), "Строка с переносом кернинга", fontname="F1", fontsize=9)
    doc.subset_fonts()
    data = doc.tobytes(garbage=0, deflate=True)
    doc.close()

    # --- слой 2: всё остальное дописывается на низком уровне
    pdf = pikepdf.open(io.BytesIO(data))
    page_obj = pdf.pages[0].obj
    resources = page_obj["/Resources"]

    # Простой шрифт со значком: /Differences подменяет один код, /ToUnicode нет,
    # поэтому такой символ в извлечённом тексте не появится
    program = _subset_program(FONT_PATH, "i")
    # Ширину берём из самого шрифта, а не выдумываем: разойдись она с hmtx —
    # и образец сам оказался бы носителем того признака, который на нём
    # проверяют (см. pdfedit.traces, «ширины-расходятся»)
    mark_width = _glyph_width(program, "i")
    font_file = pdf.make_stream(program)
    font_file["/Length1"] = len(program)
    descriptor = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/FontDescriptor"),
        FontName=pikepdf.Name("/AAAAAA+MarkGlyph"),
        Flags=4, FontBBox=pikepdf.Array([-568, -307, 2000, 1007]),
        ItalicAngle=0, Ascent=891, Descent=-216, CapHeight=662, StemV=80,
        FontFile2=font_file,
    ))
    mark_font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/Font"), Subtype=pikepdf.Name("/TrueType"),
        BaseFont=pikepdf.Name("/AAAAAA+MarkGlyph"),
        FirstChar=105, LastChar=105, Widths=pikepdf.Array([mark_width]),
        Encoding=pikepdf.Dictionary(
            Type=pikepdf.Name("/Encoding"),
            Differences=pikepdf.Array([105, pikepdf.Name("/i")]),
        ),
        FontDescriptor=descriptor,
    ))
    resources["/Font"]["/F3"] = mark_font

    # Изображения: два серых и два цветных, у цветных — полупрозрачная маска
    def image(width: int, height: int, colorspace: str, seed: int, mask=None):
        channels = 3 if colorspace == "/DeviceRGB" else 1
        raw = bytes(((x * seed + y) % 251 for y in range(height) for x in range(width * channels)))
        stream = pdf.make_stream(zlib.compress(raw, 6))
        stream["/Type"] = pikepdf.Name("/XObject")
        stream["/Subtype"] = pikepdf.Name("/Image")
        stream["/Width"] = width
        stream["/Height"] = height
        stream["/ColorSpace"] = pikepdf.Name(colorspace)
        stream["/BitsPerComponent"] = 8
        stream["/Filter"] = pikepdf.Name("/FlateDecode")
        if mask is not None:
            stream["/SMask"] = mask
        return pdf.make_indirect(stream)

    gray_small = image(64, 64, "/DeviceGray", 3)
    gray_wide = image(96, 32, "/DeviceGray", 5)
    xobjects = pikepdf.Dictionary(
        img0=gray_small,
        img1=image(64, 64, "/DeviceRGB", 7, mask=gray_small),
        img2=gray_wide,
        img3=image(96, 32, "/DeviceRGB", 11, mask=gray_wide),
    )
    resources["/XObject"] = xobjects
    resources["/ExtGState"] = pikepdf.Dictionary(
        GS1=pikepdf.Dictionary(ca=0), GS2=pikepdf.Dictionary(ca=1),
    )
    resources["/ColorSpace"] = pikepdf.Dictionary(CS=pikepdf.Name("/DeviceRGB"))

    # Рисование картинок и значка дописывается к содержимому страницы
    contents = page_obj["/Contents"]
    stream = contents[0] if isinstance(contents, pikepdf.Array) else contents
    extra = (
        b"q /GS2 gs 1 0 0 1 20 240 cm 40 0 0 40 0 0 cm /img0 Do Q\n"
        b"q /GS2 gs 1 0 0 1 70 240 cm 40 0 0 40 0 0 cm /img1 Do Q\n"
        b"q /GS1 gs 1 0 0 1 20 300 cm 60 0 0 20 0 0 cm /img2 Do Q\n"
        b"q /GS2 gs 1 0 0 1 20 340 cm 60 0 0 20 0 0 cm /img3 Do Q\n"
        b"BT /F3 11 Tf 100 130 Td (i) Tj ET\n"
    )
    stream.write(stream.read_bytes() + extra)

    page_obj["/Group"] = pikepdf.Dictionary(
        Type=pikepdf.Name("/Group"), S=pikepdf.Name("/Transparency"),
        CS=pikepdf.Name("/DeviceRGB"),
    )
    page_obj["/Annots"] = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
        Subtype=pikepdf.Name("/Link"),
        Rect=pikepdf.Array([20, 200, 250, 220]),
        Border=pikepdf.Array([0, 0, 0]),
        C=pikepdf.Array([0, 0, 1]),
        A=pikepdf.Dictionary(
            S=pikepdf.Name("/URI"), URI=pikepdf.String("https://example.invalid/test"),
        ),
    ))])

    pdf.trailer["/Info"] = pdf.make_indirect(pikepdf.Dictionary(
        Creator=pikepdf.String("SampleReportEngine 1.0"),
        Producer=pikepdf.String("SamplePDF 0.9"),
        Subject=pikepdf.String('"/samples/layered"'),
        Keywords=pikepdf.String("18.06.2026 18:22:26 | sample | 991"),
        CreationDate=pikepdf.String("D:20260618182226+03'00'"),
        ModDate=pikepdf.String("D:20260618182226+03'00'"),
    ))
    pdf.save(
        str(path),
        force_version="1.5",
        object_stream_mode=pikepdf.ObjectStreamMode.disable,
        compress_streams=True,
    )
    pdf.close()


def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    builders = [
        ("sample_contract.pdf", sample_contract),
        ("sample_subset.pdf", sample_subset),
        ("sample_base14.pdf", sample_base14),
        ("sample_simple_tt.pdf", sample_simple_truetype),
        ("sample_xobject.pdf", sample_xobject),
        ("sample_layered.pdf", sample_layered),
    ]
    for name, builder in builders:
        target = HERE / name
        builder(target)
        print(f"создан {target} ({target.stat().st_size} байт)")


if __name__ == "__main__":
    main()
