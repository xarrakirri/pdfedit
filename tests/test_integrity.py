"""Проверки того, что правка не оставляет следов.

Каждый разбор здесь отвечает одному из требований к движку:

1. структурная целостность — ``/ID``, ``/Producer``, даты, нумерация, xref;
2. шрифты — даты, порядок таблиц, согласие ширин, отсутствие сирот;
3. поток содержимого — манера записи строк, чисел, выбор ``Tj``/``TJ``;
4. избыточные представления — ``/ActualText``, поля форм, разметка в потоке;
5. сжатие и даты — уровень zlib, ``/CreationDate``, ``/ModDate``;
6. хвосты — неиспользуемые байты за концом сжатых данных.

Все проверки идут на документах, написанных разными почерками
(:mod:`tests.generators`): reportlab и fpdf2 вызываются по-настоящему, почерк
Word и LibreOffice воспроизводится вручную. Разнообразие здесь не украшение:
почти каждая ошибка в сохранении стиля видна только на одном из почерков.
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

import pikepdf

sys.path.insert(0, str(Path(__file__).resolve().parent))

import generators as gen  # noqa: E402

from pdfedit import style as style_mod  # noqa: E402
from pdfedit.editor import PdfEditor  # noqa: E402
from pdfedit.traces import (  # noqa: E402
    compare_traces,
    flate_tail,
    self_traces,
    stream_spans,
    zlib_header,
)


def edit(source: Path, target: Path, old: str, new: str, mode: str = "inplace"):
    """Правит текст и сохраняет выбранным способом. Возвращает отчёт о правке."""
    editor = PdfEditor(str(source))
    report = editor.replace(old, new)
    if mode == "inplace":
        editor.save(str(target), in_place=True)
    elif mode == "incremental":
        editor.save(str(target), incremental=True)
    else:
        editor.save(str(target))
    editor.close()
    return report


def content_bytes(path: Path, page: int = 0) -> bytes:
    """Распакованное содержимое страницы."""
    with pikepdf.open(path) as pdf:
        contents = pdf.pages[page].obj["/Contents"]
        if isinstance(contents, pikepdf.Array):
            return b"".join(item.read_bytes() for item in contents)
        return contents.read_bytes()


class StyleSniffTest(unittest.TestCase):
    """Снятие и воспроизведение манеры записи (модуль style)."""

    def test_шестнадцатеричные_строки_опознаются(self):
        style = style_mod.sniff(b"<48656C6C6F> Tj")
        self.assertTrue(style.hex_strings)
        self.assertTrue(style.hex_upper)

    def test_строки_в_скобках_опознаются(self):
        style = style_mod.sniff(b"(Hello) Tj")
        self.assertFalse(style.hex_strings)

    def test_разрядность_чисел_сохраняется(self):
        style = style_mod.sniff(b"72.00 700.00 Td")
        self.assertEqual(style.decimals, 2)
        self.assertTrue(style.force_decimal)
        self.assertEqual(style_mod.write_number(700, style), b"700.00")

    def test_целые_остаются_целыми(self):
        style = style_mod.sniff(b"72 700 Td")
        self.assertFalse(style.force_decimal)
        self.assertEqual(style_mod.write_number(700, style), b"700")

    def test_отсутствие_пробела_перед_оператором(self):
        style = style_mod.sniff(b"(x)Tj")
        self.assertFalse(style.space_before_operator)
        instruction = pikepdf.ContentStreamInstruction(
            [pikepdf.String(b"y")], pikepdf.Operator("Tj")
        )
        self.assertEqual(style_mod.render(instruction, style), b"(y)Tj")

    def test_запись_строки_в_снятом_стиле(self):
        style = style_mod.sniff(b"<0048> Tj")
        instruction = pikepdf.ContentStreamInstruction(
            [pikepdf.String(b"\x00\x49")], pikepdf.Operator("Tj")
        )
        self.assertEqual(style_mod.render(instruction, style), b"<0049> Tj")

    def test_Tj_не_превращается_в_TJ(self):
        # Редактор собирает показ текста массивом; для потока, где стоял Tj,
        # это смена почерка, которую надо отменить
        as_array = pikepdf.ContentStreamInstruction(
            [pikepdf.Array([pikepdf.String(b"x")])], pikepdf.Operator("TJ")
        )
        fixed = style_mod.harmonise_operator(as_array, b"(y) Tj")
        self.assertEqual(str(fixed.operator), "Tj")

    def test_TJ_с_кернингом_не_переводится_в_Tj(self):
        # Числа в массиве несут кернинг — перевод потерял бы его
        kerned = pikepdf.ContentStreamInstruction(
            [pikepdf.Array([pikepdf.String(b"x"), -20, pikepdf.String(b"y")])],
            pikepdf.Operator("TJ"),
        )
        fixed = style_mod.harmonise_operator(kerned, b"(y) Tj")
        self.assertEqual(str(fixed.operator), "TJ")

    def test_стиль_документа_считается_по_большинству(self):
        data = b"<0041> Tj\n<0042> Tj\n<0043> Tj\n(x) Tj\n"
        self.assertTrue(style_mod.document_style(data).hex_strings)


class SerializationStyleTest(unittest.TestCase):
    """Требование 3: манера записи потока переживает правку."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_шестнадцатеричные_строки_остаются_шестнадцатеричными(self):
        source = gen.word_style(self.dir / "word.pdf")
        target = self.dir / "out.pdf"
        edit(source, target, "Иванов Иван Иванович", "Иванов Иван Петрович")

        data = content_bytes(target)
        kinds = [kind for kind, _s, _e in style_mod.tokens(data)
                 if kind in ("hex", "string")]
        self.assertIn("hex", kinds, "шестнадцатеричная запись строк потеряна")
        self.assertNotIn(
            "string", kinds,
            "строка записана в скобках там, где весь поток шестнадцатеричный",
        )

    def test_скобки_остаются_скобками(self):
        source = gen.libreoffice_style(self.dir / "lo.pdf")
        target = self.dir / "out.pdf"
        edit(source, target, "Petrov Petr", "Petrov Ivan")

        data = content_bytes(target)
        kinds = [kind for kind, _s, _e in style_mod.tokens(data)
                 if kind in ("hex", "string")]
        self.assertNotIn("hex", kinds, "строка записана шестнадцатерично вместо скобок")

    @unittest.skipUnless(gen.have_fpdf(), "fpdf2 не установлен в этом окружении")
    def test_оператор_показа_не_меняется(self):
        source = gen.by_fpdf(self.dir / "fp.pdf")
        target = self.dir / "out.pdf"
        before = content_bytes(source)
        edit(source, target, "Contract No 17", "Contract No 18")
        after = content_bytes(target)
        self.assertEqual(
            before.count(b"Tj"), after.count(b"Tj"),
            "число операторов Tj изменилось — показ текста переписан иначе",
        )
        self.assertEqual(before.count(b"TJ"), after.count(b"TJ"))

    @unittest.skipUnless(gen.have_fpdf(), "fpdf2 не установлен в этом окружении")
    def test_разрядность_чисел_не_меняется(self):
        source = gen.by_fpdf(self.dir / "fp.pdf")
        target = self.dir / "out.pdf"
        before = content_bytes(source)
        edit(source, target, "Contract No 17", "Contract No 18")
        after = content_bytes(target)

        def numbers(data: bytes):
            return [data[s:e] for kind, s, e in style_mod.tokens(data) if kind == "number"]

        self.assertEqual(numbers(before), numbers(after),
                         "числа в потоке записаны иначе, чем были")

    def test_различие_потока_только_в_тексте(self):
        # Самая сильная формулировка требования: кроме самого текста, в
        # распакованном потоке не изменилось ничего. Замена равной ширины
        # (цифра на цифру) не требует подгонки, поэтому лишним инструкциям
        # взяться неоткуда
        source = gen.libreoffice_style(self.dir / "lo.pdf")
        target = self.dir / "out.pdf"
        edit(source, target, "100000", "900000")
        before = content_bytes(source).replace(b"100000", b"")
        after = content_bytes(target).replace(b"900000", b"")
        self.assertEqual(before, after, "в потоке изменилось что-то помимо текста")

    def test_подгонка_ширины_добавляет_инструкции_осознанно(self):
        # Обратный случай: текст другой ширины. Тогда редактор подгоняет
        # строку горизонтальным сжатием — и это не след неряшливости, а
        # сознательная плата за то, чтобы соседний текст не сдвинулся
        source = gen.libreoffice_style(self.dir / "lo.pdf")
        target = self.dir / "out.pdf"
        edit(source, target, "Petrov Petr", "Petrov Ivan")
        after = content_bytes(target)
        self.assertIn(b"Petrov Ivan", after)
        if b"Tz" in after:
            self.assertEqual(after.count(b"Tz"), 2,
                             "сжатие обязано и включаться, и выключаться")


class InPlaceTest(unittest.TestCase):
    """Требования 1, 5, 6: длина, стиль сжатия и отсутствие хвостов."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _edited(self, name, builder):
        """Правит документ выбранного почерка и возвращает ``(оригинал, результат)``."""
        source = builder(self.dir / f"{name}.pdf")
        target = self.dir / f"{name}-out.pdf"
        edit(source, target, gen.text_of(name), gen.replacement_of(name))
        return source, target

    def test_длина_файла_не_меняется(self):
        for name, builder in gen.all_generators():
            with self.subTest(почерк=name):
                source, target = self._edited(name, builder)
                self.assertEqual(
                    source.stat().st_size, target.stat().st_size,
                    "длина файла изменилась — часть правки ушла дописанным слоем",
                )

    def test_уровень_сжатия_сохраняется(self):
        for name, builder in gen.all_generators():
            with self.subTest(почерк=name):
                source, target = self._edited(name, builder)
                before_data, after_data = source.read_bytes(), target.read_bytes()
                before = {objgen: zlib_header(before_data[s:s + n])
                          for objgen, (s, n) in stream_spans(before_data).items()}
                after = {objgen: zlib_header(after_data[s:s + n])
                         for objgen, (s, n) in stream_spans(after_data).items()}
                for objgen, header in after.items():
                    if header and before.get(objgen):
                        self.assertEqual(
                            before[objgen], header,
                            f"заголовок zlib потока {objgen} изменился",
                        )

    def test_за_данными_не_остаётся_хвоста(self):
        for name, builder in gen.all_generators():
            with self.subTest(почерк=name):
                _source, target = self._edited(name, builder)
                data = target.read_bytes()
                for objgen, (start, length) in stream_spans(data).items():
                    payload = data[start:start + length]
                    if not zlib_header(payload):
                        continue
                    self.assertEqual(
                        flate_tail(payload), 0,
                        f"за данными потока {objgen} остались неиспользуемые байты",
                    )

    def test_идентификатор_и_даты_не_трогаются(self):
        for name, builder in gen.all_generators():
            with self.subTest(почерк=name):
                source, target = self._edited(name, builder)
                report = compare_traces(str(source), str(target))
                keys = {trace.key for trace in report.traces}
                for forbidden in ("id-изменён", "дата-изменена", "производитель-изменён",
                                  "новые-объекты", "объекты-исчезли", "стиль-xref"):
                    self.assertNotIn(forbidden, keys, report.describe())

    def test_точное_сжатие_даёт_ровную_длину(self):
        from pdfedit.inplace import flate_exact

        payload = b"BT /F1 12 Tf (proba) Tj ET\n" * 20
        plain = zlib.compress(payload, 6)
        hits = 0
        for extra in range(10, 60):
            packed = flate_exact(payload, len(plain) + extra, 6)
            if packed is None:
                continue
            hits += 1
            self.assertEqual(len(packed), len(plain) + extra)
            self.assertEqual(zlib.decompress(packed), payload,
                             "точное сжатие изменило данные")
            self.assertEqual(flate_tail(packed), 0, "точное сжатие оставило хвост")
            self.assertEqual(packed[:2], plain[:2], "точное сжатие сменило заголовок")
        self.assertGreater(hits, 40, "точная длина набирается слишком редко")


class HiddenCopiesTest(unittest.TestCase):
    """Требование 4: скрытые копии текста не расходятся с видимым."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_ActualText_в_структурном_дереве_обновляется(self):
        source = gen.word_style(self.dir / "word.pdf")
        target = self.dir / "out.pdf"
        edit(source, target, "Иванов Иван Иванович", "Иванов Иван Петрович")

        with pikepdf.open(target) as pdf:
            element = pdf.Root["/StructTreeRoot"]["/K"][0]
            self.assertEqual(str(element["/ActualText"]), "Иванов Иван Петрович")

    def test_ActualText_внутри_потока_обновляется(self):
        source = gen.word_style(self.dir / "word.pdf")
        target = self.dir / "out.pdf"
        edit(source, target, "Иванов Иван Иванович", "Иванов Иван Петрович")

        found = []
        with pikepdf.open(target) as pdf:
            for instruction in pikepdf.parse_content_stream(pdf.pages[0]):
                if str(instruction.operator) != "BDC":
                    continue
                operands = list(instruction.operands)
                if len(operands) >= 2 and isinstance(operands[1], pikepdf.Dictionary):
                    value = operands[1].get("/ActualText")
                    if value is not None:
                        found.append(str(value))
        self.assertEqual(found, ["Иванов Иван Петрович"],
                         "копия текста в разметке потока осталась прежней")

    def test_копия_в_словаре_правится_на_месте(self):
        """Скрытая копия текста лежит в словаре, а не в потоке.

        Правка структурного дерева меняет объект-словарь, а не поток. Пока
        правка на месте умела только потоки, любой тегированный документ
        из-за одной такой копии уходил в дописанный слой целиком. Проверяем,
        что словарь переписывается на месте: длина файла не меняется, ничего
        не откладывается на потом.

        Речь именно про словарь: поток содержимого может и не поместиться —
        текст той же длины сжимается по-своему, — и это отдельный случай.
        """
        source = gen.word_style(self.dir / "word.pdf")
        target = self.dir / "out.pdf"
        with pikepdf.open(source) as pdf:
            element_objgen = pdf.Root["/StructTreeRoot"]["/K"][0].objgen

        editor = PdfEditor(str(source))
        try:
            editor.replace("Иванов Иван Иванович", "Иванов Иван Ивановчи")
            editor.save(str(target), in_place=True)
            report = editor.last_in_place_report
        finally:
            editor.close()

        self.assertIn(element_objgen, report.patched,
                      "узел структурного дерева не записан на месте")
        self.assertNotIn(
            element_objgen, [objgen for objgen, _why in report.deferred],
            "узел структурного дерева ушёл в дописанный слой",
        )
        with pikepdf.open(target) as pdf:
            element = pdf.Root["/StructTreeRoot"]["/K"][0]
            self.assertEqual(str(element["/ActualText"]), "Иванов Иван Ивановчи")
            self.assertEqual(element.objgen, element_objgen,
                             "номер объекта изменился")

    def test_манера_записи_строки_в_словаре_сохраняется(self):
        """Шестнадцатеричная копия остаётся шестнадцатеричной и в верхнем регистре.

        Пересобирая словарь, библиотека пишет строку по-своему — в частности,
        шестнадцатеричные цифры строчными. Во всём документе они прописные, и
        одна строчная запись выдаёт правленый объект не хуже самого текста.
        """
        source = gen.word_style(self.dir / "word.pdf")
        target = self.dir / "out.pdf"
        editor = PdfEditor(str(source))
        try:
            editor.replace("Иванов Иван Иванович", "Иванов Иван Ивановчи")
            editor.save(str(target), in_place=True)
        finally:
            editor.close()

        raw = target.read_bytes()
        marker = raw.find(b"/ActualText")
        self.assertNotEqual(marker, -1, "/ActualText потерялся")
        value = raw[marker + len("/ActualText"):marker + 120].lstrip()
        self.assertTrue(value.startswith(b"<"),
                        "строка записана в скобках вместо шестнадцатеричной записи")
        digits = value[1:value.index(b">")]
        self.assertEqual(digits, digits.upper(),
                         "шестнадцатеричные цифры записаны строчными")

    def test_старый_текст_не_остаётся_в_файле(self):
        source = gen.word_style(self.dir / "word.pdf")
        target = self.dir / "out.pdf"
        edit(source, target, "Иванов Иван Иванович", "Иванов Иван Петрович")

        # Прямой поиск исходной формулировки по всем байтам, включая
        # распакованные потоки: именно так её и находит проверяющий
        haystack = bytearray(target.read_bytes())
        with pikepdf.open(target) as pdf:
            for obj in pdf.objects:
                if isinstance(obj, pikepdf.Stream):
                    try:
                        haystack += obj.read_bytes()
                    except Exception:
                        continue
        for encoding in ("utf-16-be", "utf-8"):
            self.assertNotIn(
                "Иванович".encode(encoding), bytes(haystack),
                f"старая формулировка осталась в файле ({encoding})",
            )

    def test_значение_поля_формы_и_его_внешний_вид_согласованы(self):
        source = gen.with_form_field(self.dir / "form.pdf", value="Ivanov")
        target = self.dir / "out.pdf"
        edit(source, target, "Ivanov", "Petrov")

        with pikepdf.open(target) as pdf:
            field = pdf.Root["/AcroForm"]["/Fields"][0]
            self.assertEqual(str(field["/V"]), "Petrov", "значение поля не обновлено")
            appearance = field["/AP"]["/N"].read_bytes()
            self.assertIn(b"(Petrov)", appearance,
                          "внешний вид поля рисует прежнее значение")
            self.assertNotIn(b"(Ivanov)", appearance)

    def test_проверка_ловит_расхождение_копий(self):
        # Собираем файл, где ActualText намеренно расходится с содержимым:
        # проверка обязана это заметить
        source = gen.word_style(self.dir / "word.pdf", text="Иванов Иван Иванович")
        with pikepdf.open(source, allow_overwriting_input=True) as pdf:
            page = pdf.pages[0]
            data = page.obj["/Contents"].read_bytes()
            data = data.replace(b"/ActualText <", b"/ActualText <0041")
            page.obj["/Contents"].write(zlib.compress(data, 6),
                                        filter=pikepdf.Name("/FlateDecode"))
            pdf.save(self.dir / "broken.pdf")
        report = self_traces(str(self.dir / "broken.pdf"))
        self.assertIn("скрытая-копия", {trace.key for trace in report.traces},
                      "расхождение видимого текста и скрытой копии не замечено")


class FontTraceTest(unittest.TestCase):
    """Требование 2: копирование глифов не оставляет следов в шрифте."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.font = gen.pick_font()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _donor(self):
        from pdfedit.fonts import SystemFont

        return SystemFont(path=self.font, index=0, ps_name="Proba",
                          family="Proba", subfamily="Regular")

    def test_дата_шрифта_не_меняется(self):
        from fontTools.ttLib import TTFont

        from pdfedit.fontops import copy_glyphs_from_donor

        program = Path(self.font).read_bytes()
        before = TTFont(io.BytesIO(program), lazy=True)
        was = before["head"].modified
        before.close()

        extended, _added = copy_glyphs_from_donor(program, self._donor(), "жщ")
        after = TTFont(io.BytesIO(extended), lazy=True)
        self.assertEqual(after["head"].modified, was,
                         "в head.modified записана дата правки")
        after.close()

    def test_порядок_таблиц_не_меняется(self):
        from pdfedit.fontops import copy_glyphs_from_donor, physical_table_order

        program = Path(self.font).read_bytes()
        extended, _added = copy_glyphs_from_donor(program, self._donor(), "жщ")
        self.assertEqual(
            physical_table_order(program), physical_table_order(extended),
            "физический порядок таблиц в шрифте изменился",
        )

    def test_хинтинг_не_трогается(self):
        from fontTools.ttLib import TTFont

        from pdfedit.fontops import copy_glyphs_from_donor

        program = Path(self.font).read_bytes()
        extended, _added = copy_glyphs_from_donor(program, self._donor(), "жщ")
        before = TTFont(io.BytesIO(program), lazy=True)
        after = TTFont(io.BytesIO(extended), lazy=True)
        try:
            for tag in ("fpgm", "prep", "cvt "):
                if tag not in before.reader:
                    continue
                self.assertEqual(before.reader[tag], after.reader[tag],
                                 f"таблица хинтинга {tag!r} изменилась")
        finally:
            before.close()
            after.close()

    def test_ширины_согласованы_с_hmtx(self):
        from fontTools.ttLib import TTFont

        from pdfedit.fontops import copy_glyphs_from_donor, width_to_pdf

        program = Path(self.font).read_bytes()
        extended, added = copy_glyphs_from_donor(program, self._donor(), "жщ")
        font = TTFont(io.BytesIO(extended), lazy=True)
        try:
            upem = font["head"].unitsPerEm
            order = font.getGlyphOrder()
            for _char, (gid, width) in added.items():
                advance = font["hmtx"][order[gid]][0]
                self.assertEqual(
                    width_to_pdf(advance, upem), width,
                    "ширина для PDF разошлась с таблицей hmtx",
                )
        finally:
            font.close()

    def test_новые_глифы_имеют_ненулевую_рамку(self):
        # Без рамки глиф не отрисуется: расплата за recalcBBoxes=False,
        # если не считать рамку добавленным глифам отдельно
        from fontTools.ttLib import TTFont

        from pdfedit.fontops import copy_glyphs_from_donor

        program = Path(self.font).read_bytes()
        extended, added = copy_glyphs_from_donor(program, self._donor(), "жщ")
        font = TTFont(io.BytesIO(extended))
        try:
            order = font.getGlyphOrder()
            for _char, (gid, _width) in added.items():
                glyph = font["glyf"][order[gid]]
                self.assertGreater(glyph.xMax, glyph.xMin, "габаритная рамка пуста")
                self.assertGreater(glyph.yMax, glyph.yMin, "габаритная рамка пуста")
        finally:
            font.close()

    def test_снимок_возвращает_шрифт_байт_в_байт(self):
        # Защита от глифов-сирот держится на этом: если добавленные глифы не
        # понадобились, шрифт откатывается к снимку целиком — вместе с
        # программой, ширинами и таблицей соответствия кодов
        from pdfedit import fontops

        source = gen.word_style(self.dir / "word.pdf")
        editor = PdfEditor(str(source))
        try:
            editor.parse()
            font = next(
                info for context in editor.contexts.values()
                for info in context.fonts.values() if info.is_embedded
            )
            snapshot = fontops.FontSnapshot(font)
            self.assertTrue(snapshot.usable, "снимок шрифта не снялся")
            before = font.descriptor[font.font_file_key].read_bytes()
            before_widths = font.metrics_dict["/W"].unparse()

            result = fontops.extend_font(editor.pdf, font, "Ω")
            self.assertTrue(result.added, "глифы не добавились — нечего откатывать")
            after = font.descriptor[font.font_file_key].read_bytes()
            self.assertNotEqual(before, after, "расширение не изменило программу шрифта")

            self.assertTrue(snapshot.restore(), "откат не удался")
            self.assertEqual(
                before, font.descriptor[font.font_file_key].read_bytes(),
                "после отката программа шрифта отличается от исходной",
            )
            self.assertEqual(
                before_widths, font.metrics_dict["/W"].unparse(),
                "после отката ширины отличаются от исходных",
            )
        finally:
            editor.close()

    def test_ненайденный_текст_не_трогает_файл(self):
        source = gen.word_style(self.dir / "word.pdf")
        before = source.read_bytes()
        editor = PdfEditor(str(source))
        try:
            with self.assertRaises(Exception):
                editor.replace("такого текста здесь нет", "неважно")
        finally:
            editor.close()
        self.assertEqual(before, source.read_bytes(), "исходный файл изменился")

    def test_в_результате_нет_глифов_сирот(self):
        source = gen.word_style(self.dir / "word.pdf")
        target = self.dir / "out.pdf"
        edit(source, target, "Иванов Иван Иванович", "Иванов Иван Петрович")
        report = self_traces(str(target))
        self.assertNotIn("глиф-сирота", {trace.key for trace in report.traces},
                         report.describe())


class TraceCheckTest(unittest.TestCase):
    """Проверки обязаны срабатывать: испорченный файл должен быть замечен."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_хвост_в_потоке_замечается(self):
        source = gen.libreoffice_style(self.dir / "lo.pdf")
        data = bytearray(source.read_bytes())
        objgen, (start, length) = next(iter(stream_spans(bytes(data)).items()))
        # Укорачиваем сжатые данные и добиваем пробелами — ровно так выглядит
        # правка на месте, сделанная небрежно
        payload = bytes(data[start:start + length])
        packed = zlib.compress(zlib.decompress(payload), 9)
        if len(packed) < length:
            data[start:start + length] = packed + b" " * (length - len(packed))
            broken = self.dir / "broken.pdf"
            broken.write_bytes(bytes(data))
            report = self_traces(str(broken))
            self.assertIn("хвост-в-потоке", {t.key for t in report.traces},
                          "неиспользуемые байты за данными не замечены")

    def test_смена_уровня_сжатия_замечается(self):
        source = gen.word_style(self.dir / "word.pdf")
        data = bytearray(source.read_bytes())
        spans = stream_spans(bytes(data))
        changed = False
        for objgen, (start, length) in spans.items():
            payload = bytes(data[start:start + length])
            if not zlib_header(payload):
                continue
            plain = zlib.decompress(payload)
            packed = zlib.compress(plain, 9)
            if len(packed) <= length:
                data[start:start + length] = packed + b" " * (length - len(packed))
                changed = True
                break
        self.assertTrue(changed, "не нашлось потока для порчи")
        broken = self.dir / "broken.pdf"
        broken.write_bytes(bytes(data))
        report = compare_traces(str(source), str(broken))
        self.assertIn("уровень-сжатия", {t.key for t in report.traces},
                      "пересжатие другим уровнем не замечено")

    def test_смена_id_замечается(self):
        source = gen.libreoffice_style(self.dir / "lo.pdf")
        target = self.dir / "rebuilt.pdf"
        with pikepdf.open(source) as pdf:
            pdf.save(target, force_version="1.6")
        report = compare_traces(str(source), str(target))
        keys = {t.key for t in report.traces}
        self.assertTrue(
            keys & {"id-изменён", "длина-файла"},
            "полная пересборка не отмечена ни одним признаком",
        )

    def test_чистый_файл_не_даёт_ложных_срабатываний(self):
        for name, builder in gen.all_generators():
            with self.subTest(почерк=name):
                source = builder(self.dir / f"{name}.pdf")
                report = self_traces(str(source))
                self.assertEqual(
                    report.traces, [],
                    f"на нетронутом файле ({name}) найдены следы: {report.describe()}",
                )


class UncompressedTest(unittest.TestCase):
    """Несжатый поток: заполнитель там виден напрямую."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_замена_той_же_ширины_ничего_не_добавляет(self):
        # Перестановка букв: набор глифов тот же, значит и ширина строки та же,
        # и подгонять ничего не надо — в потоке меняется только сам текст
        source = gen.uncompressed_style(self.dir / "plain.pdf", text="Plain text sample")
        target = self.dir / "out.pdf"
        edit(source, target, "Plain text sample", "Plain text sampel")
        self.assertEqual(source.stat().st_size, target.stat().st_size,
                         "длина файла изменилась при равноширокой замене")
        self.assertIn(b"(Plain text sampel) Tj", target.read_bytes())
        self.assertNotIn(b"Tz", target.read_bytes(),
                         "появилось горизонтальное сжатие там, где ширина не менялась")

    def test_равноширокая_замена_не_даёт_сдвига_выключки(self):
        # Одиночный `[число] TJ` перед строкой — сдвиг начала ради выключки.
        # Когда ширина не изменилась, ему взяться неоткуда
        source = gen.uncompressed_style(self.dir / "plain.pdf", text="Plain text sample")
        target = self.dir / "out.pdf"
        edit(source, target, "Plain text sample", "Plain text sampel")
        self.assertNotIn(b"TJ", target.read_bytes(),
                         "появился сдвиг выключки без надобности")

    def test_компенсированная_замена_не_получает_второго_сдвига(self):
        # Ширина подогнана горизонтальным сжатием — значит строка осталась
        # прежней ширины, и сдвиг начала строки был бы двойным учётом
        source = gen.uncompressed_style(self.dir / "plain.pdf", text="Plain text sample")
        target = self.dir / "out.pdf"
        edit(source, target, "Plain text sample", "Plain text simple")
        data = target.read_bytes()
        if b"Tz" in data:
            self.assertNotIn(
                b"TJ", data,
                "к сжатой по ширине строке добавлен ещё и сдвиг начала",
            )


if __name__ == "__main__":
    unittest.main()
