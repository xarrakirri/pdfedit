"""Разорванный текст, поля форм и точный режим.

Три разных изъяна, у которых общий корень — то, что видит читатель, и то, что
записано в файле, устроено по-разному:

1. **Текст, разорванный на несколько операторов показа.** Число «1234567»,
   выведенное по цифре отдельными ``Tj`` (так делают генераторы таблиц, чтобы
   выровнять колонку по разрядам), для читателя одно, а в потоке — семь
   независимых кусков. Поиск внутри одного куска не находил его вовсе.
2. **Поле формы.** Значение лежит в ``/V``, а нарисовано в отдельном потоке
   внешнего вида ``/AP``. Правка обязана привести в согласие оба, иначе на
   экране остаётся прежнее значение при новом ``/V``.
3. **Точный режим.** На странице должен оказаться ровно заданный текст, набранный
   ровно донорскими глифами: без подгонки ширины, без подстановки похожего
   шрифта, без сокращений.
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import pikepdf

sys.path.insert(0, str(Path(__file__).resolve().parent))

import generators as gen  # noqa: E402

from pdfedit.editor import PdfEditor  # noqa: E402
from pdfedit.mupdf import fitz  # noqa: E402
from pdfedit.traces import self_traces  # noqa: E402


def page_text(path: Path) -> str:
    doc = fitz.open(path)
    try:
        return doc[0].get_text().strip()
    finally:
        doc.close()


def font_program(path: Path) -> bytes | None:
    """Первая внедрённая программа шрифта документа."""
    with pikepdf.open(path) as pdf:
        for obj in pdf.objects:
            if not isinstance(obj, pikepdf.Dictionary):
                continue
            if str(obj.get("/Type", "")) != "/FontDescriptor":
                continue
            for key in ("/FontFile2", "/FontFile3", "/FontFile"):
                if key in obj:
                    return obj[key].read_bytes()
    return None


class SplitTextTest(unittest.TestCase):
    """Требование 1: текст ищется и заменяется через границы фрагментов."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_разорванное_число_собирается_в_группу(self):
        source = gen.split_digits(self.dir / "split.pdf", number="1234567")
        editor = PdfEditor(str(source))
        try:
            editor.parse()
            # Разборщик честно видит куски: BT/ET и Td закрывают фрагмент
            self.assertGreater(len(editor.runs), 1, "фрагмент не разорван — нечего проверять")
            groups = editor.visual_groups()
            self.assertEqual(len(groups), 1, "куски одной строки не собрались в группу")
            text, _index = editor._group_text(groups[0])
            self.assertIn("1234567", text, "в тексте группы числа нет")
        finally:
            editor.close()

    def test_поиск_находит_разорванное_число(self):
        source = gen.split_digits(self.dir / "split.pdf", number="1234567")
        editor = PdfEditor(str(source))
        try:
            editor.parse()
            self.assertEqual(len(editor.find("1234567")), 0,
                             "внутри одного фрагмента число найтись не могло")
            found = editor.find_grouped("1234567")
            self.assertEqual(len(found), 1)
            self.assertEqual(len(found[0].pieces), 7,
                             "совпадение должно охватить все семь кусков")
        finally:
            editor.close()

    def test_поиск_находит_часть_разорванного_числа(self):
        source = gen.split_digits(self.dir / "split.pdf", number="1234567")
        editor = PdfEditor(str(source))
        try:
            editor.parse()
            found = editor.find_grouped("345")
            self.assertEqual(len(found), 1)
            self.assertEqual(len(found[0].pieces), 3)
        finally:
            editor.close()

    def test_заменяются_все_куски_группы(self):
        source = gen.split_digits(self.dir / "split.pdf", number="1234567")
        target = self.dir / "out.pdf"
        editor = PdfEditor(str(source))
        try:
            editor.replace("1234567", "7654321")
            editor.save(str(target), in_place=True)
        finally:
            editor.close()

        text = page_text(target)
        self.assertIn("7654321", text)
        self.assertNotIn("1234567", text, "старое число осталось на странице")
        # Ни одна цифра старого числа не должна уцелеть отдельным куском
        self.assertEqual(text.count("7654321"), 1)

    def test_скрытая_копия_обновляется_целиком(self):
        """Правка группы синхронизирует /ActualText одной заменой, а не по цифре.

        Куски-продолжения несут пары вида «2» → «»: приняв их за
        самостоятельные замены, синхронизация вычистила бы по этому правилу
        все двойки в структурном дереве, закладках и полях.
        """
        source = gen.split_digits(self.dir / "split.pdf", number="1234567")
        target = self.dir / "out.pdf"
        editor = PdfEditor(str(source))
        try:
            editor.replace("1234567", "7654321")
            editor.save(str(target), in_place=True)
        finally:
            editor.close()

        actual: list[str] = []
        with pikepdf.open(target) as pdf:
            for instruction in pikepdf.parse_content_stream(pdf.pages[0]):
                if str(instruction.operator) != "BDC":
                    continue
                operands = list(instruction.operands)
                if len(operands) >= 2 and isinstance(operands[1], pikepdf.Dictionary):
                    value = operands[1].get("/ActualText")
                    if value is not None:
                        actual.append(str(value))
        self.assertEqual(actual, ["N 7654321"],
                         "скрытая копия обновлена не целиком")

    def test_правка_ложится_на_место(self):
        source = gen.split_digits(self.dir / "split.pdf", number="1234567")
        target = self.dir / "out.pdf"
        editor = PdfEditor(str(source))
        try:
            editor.replace("1234567", "7654321")
            editor.save(str(target), in_place=True)
            report = editor.last_in_place_report
        finally:
            editor.close()
        self.assertTrue(report.length_kept, f"ушло слоем: {report.deferred}")
        self.assertEqual(source.stat().st_size, target.stat().st_size)

    def test_группа_применяется_целиком_или_никак(self):
        """Отказ на первом куске отменяет вычистку остальных.

        Худшее, что может случиться с разорванным текстом, — это половинчатая
        правка: новый текст поместить не удалось, а старый уже вычищен, и на
        странице не осталось ничего. Поэтому куски группы применяются только
        все вместе.

        Проверяем на замене, которую заведомо не набрать: в шрифте документа
        только латиница и цифры, а точный режим без донора запрещает искать
        глифы на стороне.
        """
        source = gen.split_digits(self.dir / "split.pdf", number="1234567")
        before = source.read_bytes()
        target = self.dir / "out.pdf"
        editor = PdfEditor(str(source), exact=True)
        try:
            report = editor.replace("1234567", "Ж654321")
            editor.save(str(target), in_place=True)
        finally:
            editor.close()

        self.assertEqual(len(report.applied), 0, "часть группы всё же применилась")
        self.assertEqual(len(report.skipped), 7, "отменены не все куски группы")
        self.assertIn("1234567", page_text(target), "старое число исчезло со страницы")
        self.assertEqual(before, target.read_bytes(),
                         "непринятая правка изменила файл")

    def test_фрагменты_разных_потоков_не_склеиваются(self):
        """Внешний вид поля и текст страницы — разные объекты.

        Оказаться на одной базовой линии они могут запросто, но читатель не
        видит их как одну строку, и склеивать их нельзя: поиск нашёл бы текст,
        которого нет, и правка пошла бы в чужой поток.
        """
        source = gen.form_split_digits(self.dir / "form.pdf", number="1234567")
        editor = PdfEditor(str(source))
        try:
            editor.parse()
            streams = {
                run.stream_id
                for group in editor.visual_groups() for run in group
            }
            for group in editor.visual_groups():
                self.assertEqual(
                    len({run.stream_id for run in group}), 1,
                    "в одну группу попали фрагменты разных потоков",
                )
            self.assertGreater(len(streams), 1, "в примере должен быть не один поток")
        finally:
            editor.close()


class FormFieldTest(unittest.TestCase):
    """Требование 2: значение поля и его внешний вид меняются вместе."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _edited(self):
        source = gen.form_split_digits(self.dir / "form.pdf", number="1234567")
        target = self.dir / "out.pdf"
        editor = PdfEditor(str(source))
        try:
            editor.replace("1234567", "7654321")
            editor.save(str(target), in_place=True)
            report = editor.last_in_place_report
        finally:
            editor.close()
        return source, target, report

    def test_значение_поля_обновлено(self):
        _source, target, _report = self._edited()
        with pikepdf.open(target) as pdf:
            field = pdf.Root["/AcroForm"]["/Fields"][0]
            self.assertEqual(str(field["/V"]), "7654321")
            self.assertEqual(str(field["/DV"]), "7654321")

    def test_внешний_вид_обновлён(self):
        _source, target, _report = self._edited()
        with pikepdf.open(target) as pdf:
            field = pdf.Root["/AcroForm"]["/Fields"][0]
            appearance = field["/AP"]["/N"].read_bytes()
        self.assertIn(b"(7654321)", appearance,
                      "внешний вид поля рисует прежнее значение")
        self.assertNotIn(b"(1) Tj", appearance, "остался кусок старого числа")

    def test_имя_поля_не_тронуто(self):
        _source, target, _report = self._edited()
        with pikepdf.open(target) as pdf:
            field = pdf.Root["/AcroForm"]["/Fields"][0]
            self.assertEqual(str(field["/T"]), "amount",
                             "имя поля изменилось — по нему форму находят программы")

    def test_поле_правится_на_месте(self):
        source, target, report = self._edited()
        self.assertTrue(report.length_kept, f"ушло слоем: {report.deferred}")
        self.assertEqual(source.stat().st_size, target.stat().st_size)


class ExactModeTest(unittest.TestCase):
    """Требование 4: только заданный текст и только донорские глифы."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.donor = gen.donor_document(self.dir / "donor.pdf")
        self.source = gen.latin_only(self.dir / "latin.pdf", text="Total 100")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _edit(self, **kwargs):
        target = self.dir / "out.pdf"
        editor = PdfEditor(str(self.source), **kwargs)
        try:
            report = editor.replace("Total 100", "Итого 100")
            editor.save(str(target), in_place=True)
        finally:
            editor.close()
        return target, report

    def test_без_донора_правка_отклоняется(self):
        """Взять кириллицу неоткуда — и подменять её похожим нельзя."""
        _target, report = self._edit(exact=True)
        self.assertEqual(len(report.applied), 0, "правка прошла без источника глифов")
        self.assertTrue(report.skipped, "отказ не объяснён")
        self.assertIn("глиф", report.skipped[0][1].lower())

    def test_с_донором_текст_ровно_заданный(self):
        target, report = self._edit(exact=True, donor_pdf=str(self.donor))
        self.assertEqual(len(report.applied), 1)
        self.assertIn("Итого 100", page_text(target))

    def test_ширина_не_подгоняется(self):
        """Ни горизонтального сжатия, ни сдвига строки ради выключки."""
        target, _report = self._edit(exact=True, donor_pdf=str(self.donor))
        with pikepdf.open(target) as pdf:
            content = pdf.pages[0].obj["/Contents"].read_bytes()
        self.assertNotIn(b"Tz", content, "текст сжат по горизонтали под длину слота")
        self.assertNotIn(b"TJ", content, "появился сдвиг строки ради выключки")

    def test_системные_шрифты_не_используются(self):
        """Донор один, и второго источника глифов быть не должно.

        Проверяем от противного: без донора те же символы не находятся вовсе,
        значит найденные с донором пришли именно от него.
        """
        _target, without = self._edit(exact=True)
        self.assertEqual(len(without.applied), 0)
        target, with_donor = self._edit(exact=True, donor_pdf=str(self.donor))
        self.assertEqual(len(with_donor.applied), 1)
        self.assertTrue(
            any("добавлены глифы" in change for change in with_donor.font_changes),
            "глифы не добавлялись — проверка ничего не значит",
        )
        self.assertIn("Итого", page_text(target))

    def test_глифы_совпадают_с_донорскими_побайтово(self):
        """Главная проверка точного режима: в документе ровно донорский глиф.

        Простые глифы обязаны совпасть с донорскими байт в байт — вместе с
        координатами точек и инструкциями хинтинга. Перерисовка контуров пером
        дала бы тот же вид, но другие байты, и глиф перестал бы быть донорским.
        """
        from fontTools.ttLib import TTFont

        target, _report = self._edit(exact=True, donor_pdf=str(self.donor))
        result = TTFont(io.BytesIO(font_program(target)))
        donor = TTFont(io.BytesIO(font_program(self.donor)))
        try:
            donor_cmap, result_cmap = donor.getBestCmap(), result.getBestCmap()
            checked = 0
            for char in dict.fromkeys("Итого"):
                donor_name = donor_cmap.get(ord(char))
                result_name = result_cmap.get(ord(char))
                self.assertIsNotNone(donor_name, f"в доноре нет {char!r}")
                self.assertIsNotNone(result_name, f"в результате нет {char!r}")
                donor_glyph = donor["glyf"][donor_name]
                result_glyph = result["glyf"][result_name]

                self.assertEqual(
                    donor["hmtx"][donor_name][0], result["hmtx"][result_name][0],
                    f"ширина глифа {char!r} разошлась с донорской",
                )
                if donor_glyph.isComposite():
                    # Составной глиф ссылается на другие по имени, а имена в
                    # доноре и в правленом шрифте разные — совпасть байтам
                    # неоткуда. Совпасть обязаны сдвиги, флаги и сами
                    # составляющие
                    self.assertEqual(
                        [(c.x, c.y, c.flags) for c in donor_glyph.components],
                        [(c.x, c.y, c.flags) for c in result_glyph.components],
                        f"составной глиф {char!r} собран иначе",
                    )
                    for left, right in zip(donor_glyph.components,
                                           result_glyph.components):
                        self.assertEqual(
                            donor["glyf"][left.glyphName].compile(donor["glyf"]),
                            result["glyf"][right.glyphName].compile(result["glyf"]),
                            f"составляющая глифа {char!r} не донорская",
                        )
                else:
                    self.assertEqual(
                        donor_glyph.compile(donor["glyf"]),
                        result_glyph.compile(result["glyf"]),
                        f"глиф {char!r} не совпадает с донорским побайтово",
                    )
                checked += 1
            self.assertGreaterEqual(checked, 4, "проверено слишком мало глифов")
        finally:
            result.close()
            donor.close()

    def test_не_влезло_значит_слоем_но_всё_сохранено(self):
        """Точный текст важнее места: не поместился — дописывается слоем.

        Но и тогда обязано сохраниться всё остальное: идентификатор, даты,
        производитель, нумерация объектов.
        """
        from pdfedit.traces import compare_traces

        target, _report = self._edit(exact=True, donor_pdf=str(self.donor))
        report = compare_traces(str(self.source), str(target))
        keys = {trace.key for trace in report.traces}
        for forbidden in ("id-изменён", "дата-изменена", "производитель-изменён",
                          "xmp-изменён", "стиль-xref", "уровень-сжатия"):
            self.assertNotIn(forbidden, keys, report.describe())

    def test_скрытые_копии_согласованы(self):
        target, _report = self._edit(exact=True, donor_pdf=str(self.donor))
        traces = self_traces(str(target))
        keys = {trace.key for trace in traces.traces}
        for forbidden in ("скрытая-копия", "ширины-расходятся", "глиф-сирота"):
            self.assertNotIn(forbidden, keys, traces.describe())


if __name__ == "__main__":
    unittest.main()
