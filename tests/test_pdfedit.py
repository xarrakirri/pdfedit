"""Проверки pdfedit.

Запуск::

    python -m unittest discover -s tests -v

Тесты работают на документах из каталога ``samples``; при необходимости они
создаются автоматически. Ключевая проверка — «глифы действительно рисуются»:
текст, который извлекается из PDF, но не виден на странице, — это самая
коварная ошибка при правке шрифтовых подмножеств, поэтому она проверяется
подсчётом краски на отрисованном изображении, а не сравнением строк.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pdfedit.mupdf import fitz  # noqa: E402
import pikepdf  # noqa: E402

from pdfedit import PdfEditor, verify  # noqa: E402
from pdfedit.cmap import build_tounicode_cmap, parse_tounicode  # noqa: E402
from pdfedit.content import parse_page  # noqa: E402
from pdfedit.editor import EditSpec, minimal_edit, normalize_for_search  # noqa: E402
from pdfedit.metadata import (  # noqa: E402
    apply_metadata,
    format_pdf_date,
    parse_user_date,
    read_metadata,
)

SAMPLES = ROOT / "samples"


def ensure_samples() -> None:
    if not (SAMPLES / "sample_contract.pdf").is_file():
        subprocess.run(
            [sys.executable, str(SAMPLES / "make_samples.py")], check=True, cwd=str(ROOT)
        )


def render_text(data: bytes, page: int = 0) -> str:
    """Текст страницы, приведённый к «поисковому» виду.

    Нормализация нужна потому, что пробел в документе нередко закодирован
    глифом, которому /ToUnicode сопоставляет неразрывный пробел; новый текст
    использует тот же глиф, и в извлечении он выглядит так же.
    """
    with fitz.open(stream=data, filetype="pdf") as doc:
        return normalize_for_search(doc[page].get_text())[0]


def ink_amount(data: bytes, page: int, rect: tuple[float, float, float, float]) -> int:
    """Считает «количество краски» в прямоугольнике отрисованной страницы.

    Позволяет отличить настоящий текст от пустого места: если глиф в шрифте
    отсутствует, средство извлечения текста всё равно вернёт строку, а вот
    краски на странице не окажется.
    """
    with fitz.open(stream=data, filetype="pdf") as doc:
        clip = fitz.Rect(*rect)
        pixmap = doc[page].get_pixmap(dpi=150, clip=clip, colorspace=fitz.csGRAY, alpha=False)
        return sum(1 for value in pixmap.samples if value < 128)


class TestCMap(unittest.TestCase):
    def test_roundtrip(self) -> None:
        mapping = {1: "А", 2: "Б", 3: "В", 100: "ﬁ", 500: "я", 65535: "Ω"}
        data = build_tounicode_cmap(mapping, code_bytes=2)
        parsed = parse_tounicode(data)
        self.assertEqual(parsed.mapping, mapping)

    def test_ranges_are_compacted(self) -> None:
        mapping = {i: chr(0x41 + i) for i in range(26)}
        data = build_tounicode_cmap(mapping, 2)
        self.assertIn(b"beginbfrange", data)
        self.assertEqual(parse_tounicode(data).mapping, mapping)

    def test_parses_bfrange_with_array(self) -> None:
        source = (
            b"begincmap\n1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
            b"1 beginbfrange\n<01> <03> [<0041> <0042> <0043>]\nendbfrange\nendcmap\n"
        )
        self.assertEqual(parse_tounicode(source).mapping, {1: "A", 2: "B", 3: "C"})


class TestFontEncoding(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_samples()

    def test_encode_decode_roundtrip_is_byte_identical(self) -> None:
        """Перекодирование исходного текста должно давать исходные же байты."""
        with pikepdf.open(SAMPLES / "sample_contract.pdf") as pdf:
            runs, _ctx, _warn = parse_page(pdf, pdf.pages[0], 0)
            self.assertTrue(runs)
            for run in runs:
                original = b"".join(
                    g.code.to_bytes(g.byte_len, "big") for g in run.glyphs
                )
                text = "".join(g.text for g in run.glyphs)
                encoded, _codes, missing = run.font.encode(text)
                self.assertEqual(missing, "", f"не закодировано: {missing!r}")
                self.assertEqual(encoded, original, f"фрагмент {run.text!r}")

    def test_missing_glyphs_are_detected(self) -> None:
        with pikepdf.open(SAMPLES / "sample_subset.pdf") as pdf:
            runs, _ctx, _warn = parse_page(pdf, pdf.pages[0], 0)
            font = runs[0].font
            # В подмножестве оставлена только латиница
            self.assertEqual(font.missing_chars("Invoice"), "")
            self.assertNotEqual(font.missing_chars("Ромашка"), "")

    def test_widths_are_positive(self) -> None:
        with pikepdf.open(SAMPLES / "sample_contract.pdf") as pdf:
            runs, _ctx, _warn = parse_page(pdf, pdf.pages[0], 0)
            for run in runs:
                for glyph in run.glyphs:
                    self.assertGreater(glyph.width1000, 0, f"нулевая ширина у {glyph.text!r}")


class TestGeometry(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_samples()

    def test_bbox_matches_pymupdf(self) -> None:
        """Наша геометрия должна совпадать с независимым разборщиком."""
        path = SAMPLES / "sample_contract.pdf"
        with pikepdf.open(path) as pdf:
            runs, _ctx, _warn = parse_page(pdf, pdf.pages[0], 0)
        with fitz.open(path) as doc:
            page = doc[0]
            matrix = page.transformation_matrix
            words = page.get_text("words")
            for run in runs[:3]:
                x0, y0, x1, y1 = run.bbox
                ours = fitz.Rect(x0, y0, x1, y1) * matrix
                same_line = [
                    w for w in words
                    if abs(w[1] - ours.y0) < 2 and abs(w[3] - ours.y1) < 2
                ]
                self.assertTrue(same_line, f"строка не найдена для {run.text!r}")
                self.assertAlmostEqual(min(w[0] for w in same_line), ours.x0, delta=1.0)
                self.assertAlmostEqual(max(w[2] for w in same_line), ours.x1, delta=2.0)


class TestReplacement(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_samples()

    def test_simple_replacement(self) -> None:
        with PdfEditor(str(SAMPLES / "sample_contract.pdf")) as editor:
            report = editor.replace("Ромашка", "Василёк")
            self.assertEqual(len(report.applied), 1)
            self.assertFalse(report.skipped)
            data = editor.to_bytes()
        text = render_text(data)
        self.assertIn("Василёк", text)
        self.assertNotIn("Ромашка", text)

    def test_replacement_keeps_other_text(self) -> None:
        source = SAMPLES / "sample_contract.pdf"
        before = render_text(source.read_bytes())
        with PdfEditor(str(source)) as editor:
            editor.replace("Ромашка", "Василёк")
            data = editor.to_bytes()
        after = render_text(data)
        for line in before.splitlines():
            if "Ромашка" in line or not line.strip():
                continue
            self.assertIn(line.strip(), after.replace("\n", " "), f"потеряна строка: {line!r}")

    def test_glyphs_are_actually_drawn(self) -> None:
        """Кириллица, добавленная в латинское подмножество, должна рисоваться."""
        source = SAMPLES / "sample_subset.pdf"
        with PdfEditor(str(source)) as editor:
            editor.parse()
            run = next(r for r in editor.runs if "Romashka" in r.text)
            box = run.bbox
            editor.replace("Romashka LLC", "Ромашка ООО")
            data = editor.to_bytes()
        # Область строки на странице (в координатах PyMuPDF, ось Y вниз)
        with fitz.open(source) as doc:
            height = doc[0].rect.height
        rect = (box[0] - 2, height - box[3] - 2, box[2] + 60, height - box[1] + 2)
        ink_before = ink_amount(source.read_bytes(), 0, rect)
        ink_after = ink_amount(data, 0, rect)
        self.assertIn("Ромашка", render_text(data))
        # Краски должно остаться сопоставимо много: пустое место выдало бы себя
        self.assertGreater(ink_after, ink_before * 0.5,
                           "текст извлекается, но на странице почти нет краски")

    def test_replacement_in_xobject(self) -> None:
        with PdfEditor(str(SAMPLES / "sample_xobject.pdf")) as editor:
            report = editor.replace("APPROVED by Smith", "REJECTED by Brown")
            self.assertEqual(len(report.applied), 1)
            data = editor.to_bytes()
        self.assertIn("REJECTED by Brown", render_text(data))

    def test_simple_font_gets_new_glyphs(self) -> None:
        with PdfEditor(str(SAMPLES / "sample_simple_tt.pdf")) as editor:
            report = editor.replace("order 42", "заказ 77")
            self.assertFalse(report.skipped, report.skipped)
            self.assertTrue(report.font_changes)
            data = editor.to_bytes()
        self.assertIn("заказ 77", render_text(data))

    def test_fallback_font_for_non_embedded(self) -> None:
        with PdfEditor(str(SAMPLES / "sample_xobject.pdf")) as editor:
            report = editor.replace("contract 2021", "договор 2022")
            self.assertFalse(report.skipped, report.skipped)
            data = editor.to_bytes()
        self.assertIn("договор 2022", render_text(data))

    def test_not_found_raises(self) -> None:
        from pdfedit import TextNotFoundError

        with PdfEditor(str(SAMPLES / "sample_contract.pdf")) as editor:
            with self.assertRaises(TextNotFoundError):
                editor.replace("такого текста нет", "неважно")

    def test_count_limits_replacements(self) -> None:
        with PdfEditor(str(SAMPLES / "sample_contract.pdf")) as editor:
            editor.parse()
            matches = editor.find("Иванов")
            self.assertGreaterEqual(len(matches), 2)
            report = editor.replace("Иванов", "Петров", count=1)
            self.assertEqual(len(report.applied), 1)

    def test_regex_replacement(self) -> None:
        with PdfEditor(str(SAMPLES / "sample_contract.pdf")) as editor:
            report = editor.replace(r"\d{4} года", "2030 года", regex=True)
            self.assertTrue(report.applied)
            data = editor.to_bytes()
        self.assertIn("2030 года", render_text(data))

    def test_find_text_with_unusual_hyphen(self) -> None:
        with PdfEditor(str(SAMPLES / "sample_subset.pdf")) as editor:
            report = editor.replace("Invoice No. 17-A", "Счёт № 17-А")
            self.assertFalse(report.skipped, report.skipped)
            data = editor.to_bytes()
        self.assertIn("Счёт № 17-А", render_text(data))

    def test_regex_group_references(self) -> None:
        """Ссылки на группы должны браться из своего совпадения, не из первого."""
        with PdfEditor(str(SAMPLES / "sample_contract.pdf")) as editor:
            report = editor.replace(r"(\d{2}) июня (\d{4})", r"\1 августа \2", regex=True)
            self.assertTrue(report.applied)
            data = editor.to_bytes()
        self.assertIn("30 августа 2021", render_text(data))

    def test_cross_fragment_match_explains_itself(self) -> None:
        from pdfedit import TextNotFoundError

        with PdfEditor(str(SAMPLES / "sample_contract.pdf")) as editor:
            with self.assertRaises(TextNotFoundError) as caught:
                editor.replace("рублей Срок выполнения", "нечто")
        self.assertIn("разорванный на несколько фрагментов", str(caught.exception))

    def test_page_filter(self) -> None:
        with PdfEditor(str(SAMPLES / "sample_contract.pdf")) as editor:
            editor.parse()
            report = editor.replace("Иванов", "Сидоров", pages=[1])
            for spec in report.applied:
                self.assertEqual(spec.page_index, 1)


class TestFitModes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_samples()

    def _tail_position(self, data: bytes, word: str) -> float:
        with fitz.open(stream=data, filetype="pdf") as doc:
            for w in doc[0].get_text("words"):
                if w[4] == word:
                    return w[0]
        raise AssertionError(f"слово {word!r} не найдено")

    def test_preserve_keeps_following_text_in_place(self) -> None:
        source = SAMPLES / "sample_contract.pdf"
        base = self._tail_position(source.read_bytes(), "года")
        with PdfEditor(str(source), fit_mode="preserve") as editor:
            editor.replace("5 марта 2021", "12 сентября 2022")
            data = editor.to_bytes()
        self.assertAlmostEqual(self._tail_position(data, "года"), base, delta=0.5)

    def test_squeeze_keeps_following_text_in_place(self) -> None:
        source = SAMPLES / "sample_contract.pdf"
        base = self._tail_position(source.read_bytes(), "года")
        with PdfEditor(str(source), fit_mode="squeeze") as editor:
            editor.replace("5 марта 2021", "12 сентября 2022")
            data = editor.to_bytes()
        self.assertAlmostEqual(self._tail_position(data, "года"), base, delta=0.5)

    def test_natural_reflows_line(self) -> None:
        source = SAMPLES / "sample_contract.pdf"
        base = self._tail_position(source.read_bytes(), "года")
        with PdfEditor(str(source), fit_mode="natural") as editor:
            editor.replace("5 марта 2021", "12 сентября 2022")
            data = editor.to_bytes()
        self.assertGreater(self._tail_position(data, "года"), base + 1.0)

    def test_auto_squeezes_small_difference(self) -> None:
        """Замена почти той же длины не должна двигать соседний текст."""
        source = SAMPLES / "sample_contract.pdf"
        base = self._tail_position(source.read_bytes(), "года")
        with PdfEditor(str(source), fit_mode="auto") as editor:
            editor.replace("5 марта 2021", "6 марта 2021")
            data = editor.to_bytes()
        self.assertAlmostEqual(self._tail_position(data, "года"), base, delta=0.5)


class TestMetadata(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_samples()

    def test_metadata_preserved_by_default(self) -> None:
        source = SAMPLES / "sample_contract.pdf"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.pdf"
            with PdfEditor(str(source)) as editor:
                editor.replace("Ромашка", "Василёк")
                editor.save(str(target))
            report = verify(str(source), str(target))
            self.assertTrue(report.clean, report.describe())
            self.assertTrue(report.id_equal)
            self.assertFalse(report.info_diff, report.info_diff)

    def test_no_incremental_update_layer(self) -> None:
        """В результате не должно быть дописанного слоя правок."""
        source = SAMPLES / "sample_contract.pdf"
        with PdfEditor(str(source)) as editor:
            editor.replace("Ромашка", "Василёк")
            data = editor.to_bytes()
        self.assertEqual(data.count(b"%%EOF"), 1)

    def test_producer_not_overwritten(self) -> None:
        source = SAMPLES / "sample_contract.pdf"
        with PdfEditor(str(source)) as editor:
            editor.replace("Ромашка", "Василёк")
            data = editor.to_bytes()
        with pikepdf.open(fitz.io.BytesIO(data) if hasattr(fitz, "io") else _bio(data)) as pdf:
            info = read_metadata(pdf)
        self.assertEqual(info.info.get("/Producer"), "DocSystem 4.2")
        self.assertNotIn("pikepdf", str(info.xmp).lower())
        self.assertNotIn("qpdf", str(info.info).lower())

    def test_explicit_metadata_change(self) -> None:
        source = SAMPLES / "sample_contract.pdf"
        with PdfEditor(str(source)) as editor:
            apply_metadata(editor.pdf, {"/Author": "Петров П. П.",
                                        "/ModDate": "2022-04-12 10:30:00"})
            data = editor.to_bytes()
        with pikepdf.open(_bio(data)) as pdf:
            snapshot = read_metadata(pdf)
        self.assertEqual(snapshot.info["/Author"], "Петров П. П.")
        self.assertTrue(snapshot.info["/ModDate"].startswith("D:20220412103000"))
        # XMP обязан совпасть со словарём /Info
        self.assertEqual(snapshot.xmp.get("{http://purl.org/dc/elements/1.1/}creator"),
                         ["Петров П. П."])

    def test_date_parsing_formats(self) -> None:
        cases = [
            "2021-03-05 12:00:00", "2021-03-05T12:00:00", "2021-03-05",
            "D:20210305120000+03'00'",
        ]
        for value in cases:
            with self.subTest(value=value):
                moment = parse_user_date(value)
                self.assertEqual(moment.year, 2021)
                self.assertTrue(format_pdf_date(moment).startswith("D:20210305"))

    def test_bad_date_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_user_date("вчера в обед")

    def test_absent_id_is_not_added(self) -> None:
        """Если в оригинале не было /ID, он не должен появиться в результате."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "no_id.pdf"
            source.write_bytes(minimal_pdf_without_id())
            self.assertNotIn(b"/ID", source.read_bytes())
            with PdfEditor(str(source)) as editor:
                report = editor.replace("Hello", "Привет")
                self.assertTrue(report.applied, report.skipped)
                data = editor.to_bytes()
            self.assertNotIn(b"/ID", data)
            self.assertEqual(_pages_of(data), 1)

    def test_id_preserved_when_lengths_differ(self) -> None:
        """Идентификатор нестандартной длины тоже должен сохраняться."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "long_id.pdf"
            long_id = bytes(range(32))
            with pikepdf.open(SAMPLES / "sample_base14.pdf") as pdf:
                pdf.trailer["/ID"] = pikepdf.Array(
                    [pikepdf.String(long_id), pikepdf.String(long_id)]
                )
                pdf.save(str(source), deterministic_id=False)
            with pikepdf.open(source) as pdf:
                before = [bytes(x) for x in pdf.trailer["/ID"]]
            with PdfEditor(str(source)) as editor:
                editor.replace("AB-1234", "CD-5678")
                data = editor.to_bytes()
            with pikepdf.open(_bio(data)) as pdf:
                after = [bytes(x) for x in pdf.trailer["/ID"]]
            self.assertEqual(before, after)
            self.assertEqual(_pages_of(data), 1)

    def test_encrypted_document_needs_password(self) -> None:
        from pdfedit import PdfEditError

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "locked.pdf"
            with pikepdf.open(SAMPLES / "sample_base14.pdf") as pdf:
                pdf.save(str(source), encryption=pikepdf.Encryption(user="тайна", owner="тайна"))
            with self.assertRaises(PdfEditError) as caught:
                PdfEditor(str(source))
            self.assertIn("паролем", str(caught.exception))

            with PdfEditor(str(source), password="тайна") as editor:
                self.assertTrue(editor.document_warnings)
                editor.replace("AB-1234", "CD-5678")
                data = editor.to_bytes()
            self.assertIn("CD-5678", render_text(data))

    def test_structure_mirrored(self) -> None:
        """Объектные потоки не должны появляться там, где их не было."""
        source = SAMPLES / "sample_base14.pdf"
        self.assertNotIn(b"/ObjStm", source.read_bytes())
        with PdfEditor(str(source)) as editor:
            editor.replace("AB-1234", "CD-5678")
            data = editor.to_bytes()
        self.assertNotIn(b"/ObjStm", data)
        self.assertTrue(data.startswith(b"%PDF-1."))


class TestSearchHelpers(unittest.TestCase):
    def test_normalization_maps_nbsp(self) -> None:
        norm, index_map = normalize_for_search("а\xa0б")
        self.assertEqual(norm, "а б")
        self.assertEqual(index_map, [0, 1, 2])

    def test_normalization_treats_u00ad_as_hyphen(self) -> None:
        """Дефис, попавший в /ToUnicode как U+00AD, должен находиться поиском."""
        norm, _ = normalize_for_search("17\xadA")
        self.assertEqual(norm, "17-A")

    def test_normalization_handles_ligature(self) -> None:
        norm, index_map = normalize_for_search("oﬃce")
        self.assertEqual(norm, "office")
        self.assertEqual(index_map, [0, 1, 1, 1, 2, 3])

    def test_minimal_edit_finds_middle_change(self) -> None:
        self.assertEqual(minimal_edit("договор 2021", "договор 2022"), (11, 12, "2"))

    def test_minimal_edit_insertion(self) -> None:
        change = minimal_edit("abc", "abXc")
        self.assertIsNotNone(change)
        start, end, replacement = change
        self.assertEqual("abc"[:start] + replacement + "abc"[end:], "abXc")

    def test_minimal_edit_identical(self) -> None:
        self.assertIsNone(minimal_edit("одно и то же", "одно и то же"))


class TestEditSpecs(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_samples()

    def test_edit_specs_survive_json_roundtrip(self) -> None:
        with PdfEditor(str(SAMPLES / "sample_contract.pdf")) as editor:
            editor.parse()
            match = editor.find("Ромашка")[0]
            spec = match.to_edit("Василёк")
        restored = EditSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
        self.assertEqual(restored, spec)

        with PdfEditor(str(SAMPLES / "sample_contract.pdf")) as editor:
            editor.parse()
            report = editor.apply_edits([restored])
            self.assertEqual(len(report.applied), 1)
            data = editor.to_bytes()
        self.assertIn("Василёк", render_text(data))

    def test_run_ids_stable_across_partial_parse(self) -> None:
        """Идентификаторы фрагментов не должны зависеть от набора страниц."""
        with PdfEditor(str(SAMPLES / "sample_contract.pdf")) as editor:
            editor.parse()
            full = {r.run_id: r.text for r in editor.runs if r.page_index == 1}
        with PdfEditor(str(SAMPLES / "sample_contract.pdf")) as editor:
            editor.parse([1])
            partial = {r.run_id: r.text for r in editor.runs}
        self.assertEqual(full, partial)


class TestCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_samples()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "pdfedit", *args],
            cwd=str(ROOT), capture_output=True, text=True,
        )

    def test_inspect(self) -> None:
        result = self._run("inspect", str(SAMPLES / "sample_contract.pdf"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Текстовые фрагменты", result.stdout)
        self.assertIn("Ромашка", result.stdout)

    def test_replace_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.pdf"
            result = self._run(
                "replace", str(SAMPLES / "sample_contract.pdf"), "-o", str(target),
                "--old", "Ромашка", "--new", "Одуванчик",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(target.is_file())
            self.assertIn("Одуванчик", render_text(target.read_bytes()))

            check = self._run("verify", str(SAMPLES / "sample_contract.pdf"), str(target))
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_meta_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.pdf"
            result = self._run(
                "meta", str(SAMPLES / "sample_contract.pdf"), "-o", str(target),
                "--set", "author=Сидоров С. С.", "--set", "created=2019-01-01 10:00:00",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            with pikepdf.open(target) as pdf:
                info = read_metadata(pdf).info
            self.assertEqual(info["/Author"], "Сидоров С. С.")
            self.assertTrue(info["/CreationDate"].startswith("D:20190101100000"))

    def test_dry_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.pdf"
            result = self._run(
                "replace", str(SAMPLES / "sample_contract.pdf"), "-o", str(target),
                "--old", "Ромашка", "--new", "Одуванчик", "--dry-run",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("найдено вхождений", result.stdout)
            self.assertFalse(target.exists())


def _bio(data: bytes):
    import io

    return io.BytesIO(data)


def minimal_pdf_without_id() -> bytes:
    """Собирает вручную простейший корректный PDF без ``/ID`` в трейлере.

    Библиотеки почти всегда добавляют ``/ID`` при записи, поэтому файл для
    проверки собирается побайтово: только так можно убедиться, что программа
    не добавляет идентификатор туда, где его не было.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        None,  # поток содержимого подставляется ниже
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
    ]
    stream = b"BT\n/F1 18 Tf\n30 120 Td\n(Hello World) Tj\nET\n"
    objects[3] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
    out += b"startxref\n" + str(xref_at).encode() + b"\n%%EOF\n"
    return bytes(out)


def _pages_of(data: bytes) -> int | None:
    """Число страниц, если документ вообще открывается."""
    try:
        with pikepdf.open(_bio(data)) as pdf:
            return len(pdf.pages)
    except Exception:
        return None


if __name__ == "__main__":
    unittest.main(verbosity=2)
