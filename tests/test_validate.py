"""Проверки модуля :mod:`pdfedit.validate`.

Здесь важно не только то, что целый файл признаётся целым, но и обратное:
намеренно испорченные документы должны опознаваться, иначе проверка не стоит
ничего. Поэтому в каждом тесте порча вносится точечно — битый поток, обрубленный
файл, сломанное содержимое страницы.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pikepdf  # noqa: E402

from pdfedit import PdfEditor  # noqa: E402
from pdfedit.validate import (  # noqa: E402
    check_file,
    compare_files,
    compare_object_hashes,
    object_roles,
)

SAMPLES = ROOT / "samples"


def ensure_samples() -> None:
    if not (SAMPLES / "sample_contract.pdf").is_file():
        subprocess.run(
            [sys.executable, str(SAMPLES / "make_samples.py")], check=True, cwd=str(ROOT)
        )


class CheckFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_samples()
        cls.source = SAMPLES / "sample_contract.pdf"

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.tmp = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_healthy_file_passes(self):
        report = check_file(str(self.source))
        self.assertTrue(report.valid, report.describe())
        self.assertEqual(report.page_count, 2)
        self.assertGreater(report.object_count, 0)
        self.assertGreater(report.font_count, 0)
        self.assertEqual(report.errors, [])

    def test_truncated_file_fails(self):
        broken = self.tmp / "truncated.pdf"
        broken.write_bytes(self.source.read_bytes()[: 1024])
        report = check_file(str(broken))
        self.assertFalse(report.valid)

    def test_broken_stream_detected(self):
        """Поток, объявленный сжатым, но содержащий мусор, обязан всплыть."""
        broken = self.tmp / "badstream.pdf"
        with pikepdf.open(str(self.source)) as pdf:
            contents = pdf.pages[0].obj["/Contents"]
            stream = contents[0] if isinstance(contents, pikepdf.Array) else contents
            stream.write(b"\x00\x01\x02 not deflate at all",
                         filter=pikepdf.Name("/FlateDecode"))
            pdf.save(str(broken))
        report = check_file(str(broken))
        self.assertFalse(report.valid)
        self.assertTrue(
            any("не распаковывается" in problem for problem in report.errors),
            report.errors,
        )

    def test_broken_content_syntax_detected(self):
        """Незакрытая строка в потоке содержимого — поломка, а не мелочь.

        Сам разбор qpdf при этом не падает: он дочитывает поток до конца и
        восстанавливается, лишь записав жалобу. Поэтому проверка и смотрит на
        жалобы, а не только на исключения — иначе такой файл прошёл бы как целый.
        """
        broken = self.tmp / "badcontent.pdf"
        with pikepdf.open(str(self.source)) as pdf:
            contents = pdf.pages[0].obj["/Contents"]
            stream = contents[0] if isinstance(contents, pikepdf.Array) else contents
            stream.write(b"BT /F1 12 Tf ((( Tj ET")
            pdf.save(str(broken))
        report = check_file(str(broken))
        self.assertFalse(report.valid, report.describe())
        self.assertTrue(
            any("EOF while reading" in problem for problem in report.errors),
            report.errors,
        )

    def test_broken_content_array_detected(self):
        """Незакрытый массив в потоке содержимого тоже опознаётся."""
        broken = self.tmp / "badarray.pdf"
        with pikepdf.open(str(self.source)) as pdf:
            contents = pdf.pages[0].obj["/Contents"]
            stream = contents[0] if isinstance(contents, pikepdf.Array) else contents
            stream.write(b"BT [ (a) 3 (b) TJ ET")
            pdf.save(str(broken))
        report = check_file(str(broken))
        self.assertFalse(report.valid, report.describe())

    def test_signature_field_reported(self):
        """Поле подписи распознаётся, и покрытие /ByteRange считается честно."""
        signed = self.tmp / "signed.pdf"
        with pikepdf.open(str(self.source)) as pdf:
            value = pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name("/Sig"),
                    Filter=pikepdf.Name("/Adobe.PPKLite"),
                    SubFilter=pikepdf.Name("/adbe.pkcs7.detached"),
                    M=pikepdf.String("D:20260101120000+03'00'"),
                    # Диапазон заведомо короче файла: подпись покрывает не всё
                    ByteRange=pikepdf.Array([0, 100, 200, 100]),
                )
            )
            field = pdf.make_indirect(
                pikepdf.Dictionary(
                    FT=pikepdf.Name("/Sig"),
                    T=pikepdf.String("Подпись1"),
                    V=value,
                    Type=pikepdf.Name("/Annot"),
                    Subtype=pikepdf.Name("/Widget"),
                    Rect=pikepdf.Array([0, 0, 10, 10]),
                )
            )
            pdf.Root["/AcroForm"] = pikepdf.Dictionary(Fields=pikepdf.Array([field]))
            pdf.save(str(signed))

        report = check_file(str(signed))
        self.assertEqual(len(report.signatures), 1)
        signature = report.signatures[0]
        self.assertEqual(signature.field_name, "Подпись1")
        self.assertFalse(signature.covers_whole_file)
        self.assertTrue(
            any("подписан" in warning for warning in report.warnings), report.warnings
        )


class CompareFilesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_samples()
        cls.source = SAMPLES / "sample_contract.pdf"

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.tmp = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_file_equals_itself(self):
        copy = self.tmp / "copy.pdf"
        copy.write_bytes(self.source.read_bytes())
        report = compare_files(str(self.source), str(copy))
        self.assertTrue(report.structure_equal, report.describe())
        self.assertEqual(report.differences, [])
        self.assertEqual(report.changed_streams, [])
        self.assertEqual(report.changed_text_pages, [])
        self.assertTrue(report.fonts_equal)
        self.assertTrue(report.images_equal)
        self.assertTrue(report.original_bytes_kept)

    def test_text_edit_shows_changed_page(self):
        target = self.tmp / "edited.pdf"
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("Иванов", "Петров", count=1)
            editor.save(str(target), incremental=True)
        report = compare_files(str(self.source), str(target))
        self.assertIn(1, report.changed_text_pages)
        self.assertTrue(report.changed_streams)
        self.assertEqual(report.unexpected_differences, [])

    def test_full_rebuild_renumbers_objects(self):
        """Полная пересборка меняет номера объектов — сверка это показывает."""
        target = self.tmp / "rebuilt.pdf"
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("Иванов", "Петров", count=1)
            editor.save(str(target))
        report = compare_files(str(self.source), str(target))
        self.assertFalse(report.original_bytes_kept)
        self.assertEqual(report.unexpected_differences, [])

    def test_lost_page_is_reported(self):
        """Пропавшая страница — расхождение, которое нельзя списать на правку."""
        target = self.tmp / "shorter.pdf"
        with pikepdf.open(str(self.source)) as pdf:
            del pdf.pages[1]
            pdf.save(str(target))
        report = compare_files(str(self.source), str(target))
        self.assertFalse(report.page_count_equal)
        self.assertFalse(report.structure_equal)

    def test_added_key_is_unexpected(self):
        """Посторонний ключ в словаре страницы не спишется на правку текста."""
        target = self.tmp / "extra.pdf"
        with pikepdf.open(str(self.source)) as pdf:
            pdf.pages[0].obj["/UserUnit"] = 2
            pdf.save(str(target))
        report = compare_files(str(self.source), str(target))
        self.assertTrue(
            any("/UserUnit" in item for item in report.unexpected_differences),
            report.unexpected_differences,
        )
        self.assertFalse(report.structure_equal)


class ExternalRisksTest(unittest.TestCase):
    """Замечания строгих внешних проверок и их происхождение."""

    @classmethod
    def setUpClass(cls):
        ensure_samples()
        cls.source = SAMPLES / "sample_simple_tt.pdf"

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.tmp = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_missing_pdfa_parts_reported(self):
        from pdfedit.validate import external_risks

        data = self.source.read_bytes()
        with pikepdf.open(str(self.source)) as pdf:
            risks = external_risks(pdf, data)
        # У обычного документа нет ни XMP, ни цветового профиля — PDF/A он не
        # является, и строгая проверка на это укажет
        self.assertIn("no-xmp", risks)
        self.assertIn("no-output-intent", risks)

    def test_inherited_risks_are_separated_from_new_ones(self):
        """Замечания оригинала не должны выглядеть как следствие правки."""
        from pdfedit.validate import compare_external_risks

        target = self.tmp / "edited.pdf"
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("order 42", "order 4", count=1)
            editor.save(str(target), in_place=True)

        inherited, introduced = compare_external_risks(str(self.source), str(target))
        self.assertIn("no-xmp", inherited)
        self.assertEqual(introduced, {}, introduced)

    def test_incremental_layer_is_a_new_risk(self):
        """Дописанный слой — замечание, которого в оригинале не было."""
        from pdfedit.validate import compare_external_risks

        target = self.tmp / "layered.pdf"
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("order 42", "order 4", count=1)
            editor.save(str(target), incremental=True)

        _inherited, introduced = compare_external_risks(str(self.source), str(target))
        self.assertIn("revisions", introduced, introduced)


class HashReportTest(unittest.TestCase):
    """Отчёт должен называть, ЧТО изменилось, а не только номер объекта."""

    @classmethod
    def setUpClass(cls):
        ensure_samples()
        cls.source = SAMPLES / "sample_simple_tt.pdf"

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.tmp = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_roles_name_the_objects(self):
        with pikepdf.open(str(self.source)) as pdf:
            roles = object_roles(pdf)
            contents = pdf.pages[0].obj["/Contents"]
            stream = contents[0] if isinstance(contents, pikepdf.Array) else contents
            self.assertEqual(roles[stream.objgen], "содержимое страницы 1")
            self.assertEqual(roles[pdf.pages[0].obj.objgen], "страница 1")
            self.assertEqual(roles[pdf.Root.objgen], "каталог документа /Root")

    def test_changed_object_is_described(self):
        """Для изменённого содержимого показывается сам текст: было → стало."""
        target = self.tmp / "edited.pdf"
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("order 42", "order 4", count=1)
            editor.save(str(target), in_place=True)

        report = compare_object_hashes(str(self.source), str(target))
        self.assertEqual(len(report.changed), 1, report.describe())
        objgen = report.changed[0][0]
        self.assertEqual(report.roles[objgen], "содержимое страницы 1")
        details = " ".join(report.details[objgen])
        self.assertIn("order 42", details)
        self.assertIn("order 4", details)
        self.assertIn("Tj", details)

    def test_font_program_change_is_named(self):
        """Расширение шрифта называется программой шрифта, а не номером."""
        target = self.tmp / "font.pdf"
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("order 42", "order 42 ЫЪЬ", count=1)
            editor.save(str(target), incremental=True)

        report = compare_object_hashes(str(self.source), str(target))
        roles = " | ".join(report.roles.values())
        self.assertIn("шрифт", roles, report.describe())

    def test_array_change_reports_length(self):
        """У длинных массивов сообщается длина, а не обрезанное содержимое."""
        target = self.tmp / "array.pdf"
        with pikepdf.open(str(self.source)) as pdf:
            page = pdf.pages[0].obj
            page["/TestArray"] = pikepdf.Array([1, 2, 3, 4, 5, 6, 7, 8])
            pdf.save(str(target))
        with pikepdf.open(str(target), allow_overwriting_input=True) as pdf:
            pdf.pages[0].obj["/TestArray"] = pikepdf.Array([1, 2, 3])
            pdf.save(str(target))

        report = compare_object_hashes(str(self.source), str(target))
        self.assertTrue(report.comparable)


if __name__ == "__main__":
    unittest.main()
