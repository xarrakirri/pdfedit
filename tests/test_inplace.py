"""Проверки правки потоков на месте и разборщика инструкций.

Главное обещание режима: длина файла не меняется, а различия сводятся к байтам
внутри изменённых потоков. Проверяется это самым прямым способом — сравнением
хешей всех объектов до и после.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pikepdf  # noqa: E402

from pdfedit import PdfEditor  # noqa: E402
from pdfedit.editor import normalize_for_search  # noqa: E402
from pdfedit.inplace import build_in_place, patch_in_place, stream_data_span  # noqa: E402
from pdfedit.mupdf import fitz  # noqa: E402
from pdfedit.streampatch import (  # noqa: E402
    _key,
    _write_instruction,
    instruction_spans,
    patch_content,
)
from pdfedit.validate import check_file, compare_object_hashes  # noqa: E402

SAMPLES = ROOT / "samples"


def ensure_samples() -> None:
    if not (SAMPLES / "sample_contract.pdf").is_file():
        subprocess.run(
            [sys.executable, str(SAMPLES / "make_samples.py")], check=True, cwd=str(ROOT)
        )


def page_text(data: bytes, page: int = 0) -> str:
    with fitz.open(stream=data, filetype="pdf") as document:
        return normalize_for_search(document[page].get_text())[0]


class InstructionSpansTest(unittest.TestCase):
    """Границы инструкций обязаны совпадать с тем, как их считает qpdf."""

    def parsed_count(self, data: bytes) -> int:
        holder = pikepdf.new()
        return len(list(pikepdf.parse_content_stream(pikepdf.Stream(holder, data))))

    def check(self, data: bytes) -> None:
        spans = instruction_spans(data)
        self.assertEqual(len(spans), self.parsed_count(data), data)

    def test_simple_operators(self):
        self.check(b"q\nBT\n36 483 Td\nET\nQ\n")

    def test_literal_string_with_parentheses(self):
        self.check(rb"BT (a\(b\)c) Tj ((nested)) Tj ET")

    def test_string_with_escaped_backslash(self):
        self.check(rb"BT (back\\slash) Tj ET")

    def test_hex_string_and_array(self):
        self.check(b"BT <00410042> Tj [(a) -250 (b)] TJ ET")

    def test_dictionary_operand(self):
        self.check(b"/OC << /Type /OCMD >> BDC\nEMC\n")

    def test_comment_is_skipped(self):
        self.check("q % это комментарий\nQ\n".encode("utf-8"))

    def test_no_space_before_operator(self):
        self.check(b"BT(text)Tj ET")

    def test_inline_image(self):
        self.check(
            b"q BI /W 2 /H 2 /CS /G /BPC 8 ID \x00\x11\x22\x33 EI Q\n"
        )

    def test_spans_point_at_instructions(self):
        data = b"q\nBT\n36 483 Td\nET\n"
        spans = instruction_spans(data)
        self.assertEqual([data[a:b] for a, b in spans], [b"q", b"BT", b"36 483 Td", b"ET"])


class PatchContentTest(unittest.TestCase):
    """Точечная замена должна менять ровно одну инструкцию и ничего больше."""

    def instructions(self, data: bytes):
        holder = pikepdf.new()
        self.holder = holder  # объект-носитель нельзя отпускать раньше потока
        return list(pikepdf.parse_content_stream(pikepdf.Stream(holder, data)))

    def test_unchanged_returns_original(self):
        data = b"q\nBT (a) Tj ET\nQ\n"
        instructions = self.instructions(data)
        self.assertEqual(patch_content(data, instructions, instructions), data)

    def test_only_edited_bytes_change(self):
        data = b"q\nBT\n1 0 0 1 20 30 Tm\n(Hello) Tj\nET\nQ\n"
        old = self.instructions(data)
        new = self.instructions(b"q\nBT\n1 0 0 1 20 30 Tm\n(Hallo) Tj\nET\nQ\n")
        patched = patch_content(data, old, new)
        self.assertIsNotNone(patched)
        # Не длиннее исходного: замена той же длины, а разделитель перед
        # оператором мы не ставим там, где он не нужен
        self.assertLessEqual(len(patched), len(data))
        self.assertIn(b"Hallo", patched)
        # остальные инструкции остались прежними байтами
        self.assertTrue(patched.startswith(b"q\nBT\n1 0 0 1 20 30 Tm\n"))
        self.assertTrue(patched.endswith(b"ET\nQ\n"))

    def test_result_parses_to_expected_instructions(self):
        data = b"BT\n(one) Tj\n(two) Tj\nET\n"
        old = self.instructions(data)
        new = self.instructions(b"BT\n(one) Tj\n(six) Tj\nET\n")
        patched = patch_content(data, old, new)
        self.assertEqual(
            [_key(item) for item in self.instructions(patched)],
            [_key(item) for item in new],
        )

    def test_binary_string_written_compactly(self):
        """Двоичная строка пишется в скобках: hex вдвое длиннее и не влезал бы."""
        holder = pikepdf.new()
        stream = pikepdf.Stream(holder, b"BT <00f4011f> Tj ET")
        instructions = list(pikepdf.parse_content_stream(stream))
        written = _write_instruction(instructions[1])
        self.assertTrue(written.startswith(b"("), written)
        self.assertLess(len(written), len(b"<00f4011f> Tj"))


class InPlaceEditTest(unittest.TestCase):
    """Правка на месте на настоящем документе.

    Документ взят с одним потоком содержимого: страницы, где ``/Contents`` —
    массив из нескольких потоков, редактор при записи сворачивает в один, и
    словарь страницы меняется, а значит правка на месте для них невозможна
    (см. :meth:`MultiStreamPageTest.test_falls_back_to_layer`).
    """

    @classmethod
    def setUpClass(cls):
        ensure_samples()
        cls.source = SAMPLES / "sample_simple_tt.pdf"
        cls.data = cls.source.read_bytes()

    def test_shorter_text_fits_and_keeps_length(self):
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("order 42", "order 4", count=1)
            result = editor.to_bytes(in_place=True)
            report = editor.last_in_place_report
        self.assertTrue(report.patched, report.describe())
        self.assertTrue(report.length_kept, report.describe())
        self.assertEqual(len(result), len(self.data))
        self.assertIn("order 4", page_text(result))

    def test_only_content_bytes_differ(self):
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("order 42", "order 4", count=1)
            result = editor.to_bytes(in_place=True)
        differing = sum(1 for a, b in zip(self.data, result) if a != b)
        self.assertGreater(differing, 0)
        # различия занимают малую часть файла — это один поток, а не весь документ
        self.assertLess(differing, len(self.data) // 4)

    def test_single_eof_no_layer(self):
        """Слоя нет: файл остаётся одной редакцией, как и был."""
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("order 42", "order 4", count=1)
            result = editor.to_bytes(in_place=True)
        self.assertEqual(result.count(b"%%EOF"), self.data.count(b"%%EOF"))

    def test_object_hashes_identical_except_content(self):
        """Все объекты, кроме изменённого потока, совпадают байт в байт."""
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "result.pdf"
            with PdfEditor(str(self.source)) as editor:
                editor.parse()
                editor.replace("order 42", "order 4", count=1)
                editor.save(str(target), in_place=True)

            structure = check_file(str(target))
            self.assertTrue(structure.valid, structure.describe())

            hashes = compare_object_hashes(str(self.source), str(target))
            self.assertTrue(hashes.comparable)
            self.assertEqual(hashes.removed, [])
            self.assertEqual(hashes.added, [])
            self.assertEqual(len(hashes.changed), 1, hashes.describe())
            self.assertIn("данные", hashes.changed[0][1])

    def test_longer_text_falls_back_to_layer(self):
        """Что не влезло — дописывается слоем, а не портит файл."""
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("order 42", "order 42424242424242", count=1)
            result = editor.to_bytes(in_place=True)
            report = editor.last_in_place_report
        self.assertTrue(result.startswith(self.data))
        self.assertFalse(report.length_kept)
        self.assertIn("42424242", page_text(result))


class MultiStreamPageTest(unittest.TestCase):
    """Страница, чей ``/Contents`` — массив потоков."""

    @classmethod
    def setUpClass(cls):
        ensure_samples()
        cls.source = SAMPLES / "sample_contract.pdf"
        cls.data = cls.source.read_bytes()

    def test_правка_ложится_в_свой_поток_массива(self):
        """Правка попадает в тот поток массива, где текст и лежал.

        Раньше такая страница правке на месте не поддавалась: массив
        разбирается как одно склеенное содержимое, и при записи сворачивался в
        первый поток — менялся словарь страницы, остальные потоки оставались
        без ссылок. Ни то ни другое на месте невыразимо, и правка целиком
        уходила в дописанный слой.

        Теперь инструкции раскладываются обратно по своим потокам, и трогается
        только тот, в котором действительно что-то изменилось: словарь
        страницы и сам массив остаются нетронутыми, а файл — прежней длины.
        """
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("Иванов", "Иван", count=1)
            result = editor.to_bytes(in_place=True)
            report = editor.last_in_place_report

        self.assertTrue(report.length_kept, "длина файла изменилась")
        self.assertEqual(len(result), len(self.data))
        self.assertFalse(report.deferred, f"что-то ушло слоем: {report.deferred}")
        self.assertIn("Иван", page_text(result))

        # Массив /Contents и словарь страницы обязаны остаться прежними
        with pikepdf.open(io.BytesIO(self.data)) as before, \
                pikepdf.open(io.BytesIO(result)) as after:
            was = before.pages[0].obj["/Contents"]
            now = after.pages[0].obj["/Contents"]
            self.assertEqual(len(was), len(now), "число потоков в /Contents изменилось")
            self.assertEqual(
                [item.objgen for item in was], [item.objgen for item in now],
                "номера объектов потоков содержимого изменились",
            )
            # Изменился ровно один поток из массива
            differing = [
                index for index, (left, right) in enumerate(zip(was, now))
                if left.read_bytes() != right.read_bytes()
            ]
            self.assertEqual(len(differing), 1,
                             f"тронуто потоков: {len(differing)}, ожидался один")

    def test_object_streams_still_produce_valid_file(self):
        """Документ со сжатыми объектными потоками правится корректно.

        Правка на месте для него, скорее всего, не сработает — потоки в
        ``/ObjStm`` не лежат, зато словарь страницы лежит, и переписать его на
        месте нельзя. Важно, что результат при этом остаётся целым.
        """
        with tempfile.TemporaryDirectory() as folder:
            packed = Path(folder) / "packed.pdf"
            target = Path(folder) / "edited.pdf"
            with pikepdf.open(str(self.source)) as pdf:
                pdf.save(
                    str(packed),
                    object_stream_mode=pikepdf.ObjectStreamMode.generate,
                    force_version="1.6",
                )
            with PdfEditor(str(packed)) as editor:
                editor.parse()
                editor.replace("Иванов", "Иван", count=1)
                editor.save(str(target), in_place=True)
            self.assertIn("Иван", page_text(target.read_bytes()))
            report = check_file(str(target))
            self.assertTrue(report.valid, report.describe())


class LayeredSampleTest(unittest.TestCase):
    """Самый сложный образец: прозрачность, маски, три вида шрифтов, ссылка."""

    @classmethod
    def setUpClass(cls):
        ensure_samples()
        cls.source = SAMPLES / "sample_layered.pdf"
        if not cls.source.is_file():
            subprocess.run(
                [sys.executable, str(SAMPLES / "make_samples.py")], check=True, cwd=str(ROOT)
            )

    def test_structure_profile(self):
        """Образец обязан сохранять те особенности, ради которых он создан."""
        with pikepdf.open(str(self.source)) as pdf:
            page = pdf.pages[0].obj
            self.assertEqual(str(page["/Group"]["/S"]), "/Transparency")
            fonts = page["/Resources"]["/Font"]
            kinds = {str(font.get("/Subtype")) for _name, font in fonts.items()}
            self.assertIn("/Type0", kinds)
            self.assertIn("/TrueType", kinds)
            # Шрифт без /ToUnicode — текст им не извлекается, это и проверяем
            self.assertTrue(
                any("/ToUnicode" not in font for _name, font in fonts.items())
            )
            images = [
                x for _n, x in page["/Resources"]["/XObject"].items()
                if str(x.get("/Subtype")) == "/Image"
            ]
            self.assertEqual(len(images), 4)
            self.assertEqual(sum(1 for image in images if "/SMask" in image), 2)
            self.assertEqual(len(page["/Annots"]), 1)

    def test_edit_keeps_structure_in_every_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            for mode in ("inplace", "incremental", "rebuild"):
                target = Path(folder) / f"{mode}.pdf"
                with PdfEditor(str(self.source)) as editor:
                    editor.parse()
                    editor.replace("Позиция", "Позици", count=1)
                    if mode == "inplace":
                        editor.save(str(target), in_place=True)
                    elif mode == "incremental":
                        editor.save(str(target), incremental=True)
                    else:
                        editor.save(str(target))
                report = check_file(str(target))
                self.assertTrue(report.valid, f"{mode}: {report.describe()}")
                self.assertIn("Позици", page_text(target.read_bytes()))


class StreamSpanTest(unittest.TestCase):
    def test_span_requires_endstream(self):
        """Разбор записи объекта не должен молча промахиваться мимо данных."""
        data = b"1 0 obj\n<< /Length 5 >>\nstream\nabcde\nendstream\nendobj\n"
        start, length = stream_data_span(data, 0, 5)
        self.assertEqual(data[start:start + length], b"abcde")

    def test_wrong_length_refused(self):
        from pdfedit.inplace import InPlacePatchError

        data = b"1 0 obj\n<< /Length 5 >>\nstream\nabcde\nendstream\nendobj\n"
        with self.assertRaises(InPlacePatchError):
            stream_data_span(data, 0, 500)


if __name__ == "__main__":
    unittest.main()
