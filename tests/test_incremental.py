"""Проверки инкрементального сохранения и структурной сверки.

Запуск::

    python -m unittest tests.test_incremental -v

Проверяется главное обещание режима: исходные байты файла не меняются, номера
объектов сохраняются, а дописанный слой читается штатным ридером — включая
защищённые документы, где дописанное приходится шифровать самим.
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
from pdfedit.editor import normalize_for_search  # noqa: E402
from pdfedit.incremental import (  # noqa: E402
    IncrementalUpdateError,
    build_update,
    changed_objects,
    header_offset,
    last_startxref,
    live_objects,
    xref_kind,
)
from pdfedit.mupdf import fitz  # noqa: E402
from pdfedit.pdfcrypt import AES, DocumentCipher, aes_cbc_encrypt, rc4  # noqa: E402
from pdfedit.validate import check_file, compare_files  # noqa: E402

SAMPLES = ROOT / "samples"


def ensure_samples() -> None:
    if not (SAMPLES / "sample_contract.pdf").is_file():
        subprocess.run(
            [sys.executable, str(SAMPLES / "make_samples.py")], check=True, cwd=str(ROOT)
        )


def page_text(data: bytes, page: int = 0) -> str:
    with fitz.open(stream=data, filetype="pdf") as document:
        return normalize_for_search(document[page].get_text())[0]


class AesTest(unittest.TestCase):
    """Контрольные примеры из FIPS-197 и RFC 6229 — шифр обязан их повторять."""

    PLAINTEXT = bytes.fromhex("00112233445566778899aabbccddeeff")

    def test_aes128(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        self.assertEqual(
            AES(key).encrypt_block(self.PLAINTEXT).hex(),
            "69c4e0d86a7b0430d8cdb78070b4c55a",
        )

    def test_aes192(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f1011121314151617")
        self.assertEqual(
            AES(key).encrypt_block(self.PLAINTEXT).hex(),
            "dda97ca4864cdfe06eaf70a0ec0d7191",
        )

    def test_aes256(self):
        key = bytes.fromhex(
            "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
        )
        self.assertEqual(
            AES(key).encrypt_block(self.PLAINTEXT).hex(),
            "8ea2b7ca516745bfeafc49904b496089",
        )

    def test_rc4(self):
        self.assertEqual(rc4(b"Key", b"Plaintext").hex(), "bbf316e8d940af0ad3")

    def test_cbc_adds_iv_and_padding(self):
        """Вектор инициализации идёт первым блоком, дополнение — всегда."""
        key = bytes(16)
        for length in (0, 1, 15, 16, 17):
            result = aes_cbc_encrypt(key, b"x" * length)
            self.assertEqual(len(result) % 16, 0)
            # 16 байт вектора + данные, дополненные до кратности блоку
            self.assertEqual(len(result), 16 + (length // 16 + 1) * 16)


class UpdateStructureTest(unittest.TestCase):
    """Разбор исходного файла: где таблица ссылок и какая она."""

    @classmethod
    def setUpClass(cls):
        ensure_samples()
        cls.data = (SAMPLES / "sample_contract.pdf").read_bytes()

    def test_startxref_found(self):
        offset = last_startxref(self.data)
        self.assertGreater(offset, 0)
        self.assertLess(offset, len(self.data))

    def test_xref_kind_detected(self):
        kind = xref_kind(self.data, last_startxref(self.data), header_offset(self.data))
        self.assertIn(kind, ("table", "stream"))

    def test_not_a_pdf_refused(self):
        with self.assertRaises(IncrementalUpdateError):
            last_startxref(b"not a pdf at all")

    def test_live_objects_reach_pages(self):
        with pikepdf.open(io.BytesIO(self.data)) as pdf:
            objects = live_objects(pdf)
            for page in pdf.pages:
                self.assertIn(page.obj.objgen, objects)


class IncrementalSaveTest(unittest.TestCase):
    """Сохранение дописыванием на обычном (незашифрованном) документе."""

    @classmethod
    def setUpClass(cls):
        ensure_samples()
        cls.source = SAMPLES / "sample_contract.pdf"
        cls.data = cls.source.read_bytes()

    def test_no_edits_no_layer(self):
        """Без правок дописывать нечего — файл обязан остаться прежним."""
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            result, report = build_update(editor.pdf, self.data)
        self.assertEqual(result, self.data)
        self.assertEqual(report.object_count, 0)

    def test_original_bytes_untouched(self):
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("Иванов", "Петров", count=1)
            result = editor.to_bytes(incremental=True)
        self.assertTrue(result.startswith(self.data))
        self.assertGreater(len(result), len(self.data))

    def test_text_replaced_and_readable(self):
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("Иванов", "Петров", count=1)
            result = editor.to_bytes(incremental=True)
        text = page_text(result)
        self.assertIn("Петров", text)

    def test_object_numbers_preserved(self):
        """Ссылки на шрифты и страницы обязаны остаться теми же объектами."""
        with pikepdf.open(io.BytesIO(self.data)) as before:
            page_ids = [page.obj.objgen for page in before.pages]
            root_id = before.Root.objgen

        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("Иванов", "Петров", count=1)
            result = editor.to_bytes(incremental=True)

        with pikepdf.open(io.BytesIO(result)) as after:
            self.assertEqual([page.obj.objgen for page in after.pages], page_ids)
            self.assertEqual(after.Root.objgen, root_id)

    def test_only_touched_objects_written(self):
        """Дописывается лишь то, что изменилось, а не документ целиком."""
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("Иванов", "Петров", count=1)
            to_write, _rewritten, _added = changed_objects(editor.pdf, self.data)
            with pikepdf.open(io.BytesIO(self.data)) as original:
                total = len(live_objects(original))
        self.assertLess(len(to_write), total)

    def test_result_is_structurally_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "result.pdf"
            with PdfEditor(str(self.source)) as editor:
                editor.parse()
                editor.replace("Иванов", "Петров", count=1)
                editor.save(str(target), incremental=True)
            report = check_file(str(target))
            self.assertTrue(report.valid, report.describe())
            self.assertEqual(report.page_count, 2)

    def test_comparison_shows_only_expected_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "result.pdf"
            with PdfEditor(str(self.source)) as editor:
                editor.parse()
                editor.replace("Иванов", "Петров", count=1)
                editor.save(str(target), incremental=True)
            comparison = compare_files(str(self.source), str(target))
            self.assertTrue(comparison.original_bytes_kept)
            self.assertTrue(comparison.object_numbers_kept)
            self.assertTrue(comparison.id_equal)
            self.assertEqual(comparison.unexpected_differences, [])

    def test_metadata_and_id_kept(self):
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("Иванов", "Петров", count=1)
            result = editor.to_bytes(incremental=True)
        with pikepdf.open(io.BytesIO(self.data)) as before, \
             pikepdf.open(io.BytesIO(result)) as after:
            self.assertEqual(
                [bytes(x) for x in before.trailer["/ID"]],
                [bytes(x) for x in after.trailer["/ID"]],
            )
            self.assertEqual(dict(before.docinfo.items()), dict(after.docinfo.items()))

    def test_second_layer_on_top_of_first(self):
        """Дописывать можно и к уже дописанному: слои накладываются."""
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("Иванов", "Петров", count=1)
            once = editor.to_bytes(incremental=True)
        with PdfEditor(once) as editor:
            editor.parse()
            editor.replace("Петров", "Сидоров", count=1)
            twice = editor.to_bytes(incremental=True)
        self.assertTrue(twice.startswith(once))
        self.assertIn("Сидоров", page_text(twice))
        self.assertEqual(twice.count(b"%%EOF"), 3)


class XrefStreamTest(unittest.TestCase):
    """Файлы с потоком ссылок вместо таблицы обслуживаются своим форматом."""

    def setUp(self):
        ensure_samples()
        self.folder = tempfile.TemporaryDirectory()
        self.source = Path(self.folder.name) / "objstm.pdf"
        with pikepdf.open(SAMPLES / "sample_contract.pdf") as pdf:
            pdf.save(
                str(self.source),
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                force_version="1.6",
            )

    def tearDown(self):
        self.folder.cleanup()

    def test_written_as_xref_stream(self):
        data = self.source.read_bytes()
        self.assertEqual(
            xref_kind(data, last_startxref(data), header_offset(data)), "stream"
        )
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("Иванов", "Петров", count=1)
            result, report = build_update(editor.pdf, data)
        self.assertEqual(report.xref_kind, "stream")
        self.assertTrue(result.startswith(data))
        self.assertIn("Петров", page_text(result))

    def test_object_from_object_stream_can_be_rewritten(self):
        """Объект из /ObjStm переписывается обычным объектом — это законно."""
        data = self.source.read_bytes()
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("Иванов", "Петров", count=1)
            result = editor.to_bytes(incremental=True)
        report = check_file_bytes(result)
        self.assertTrue(report.valid, report.describe())


class EncryptedIncrementalTest(unittest.TestCase):
    """Защищённые документы: дописанное шифруется тем же ключом."""

    def setUp(self):
        ensure_samples()
        self.folder = tempfile.TemporaryDirectory()
        self.source = Path(self.folder.name) / "protected.pdf"
        with pikepdf.open(SAMPLES / "sample_contract.pdf") as pdf:
            pdf.save(
                str(self.source),
                encryption=pikepdf.Encryption(owner="", user="", R=4, aes=True),
            )

    def tearDown(self):
        self.folder.cleanup()

    def test_cipher_matches_document(self):
        with pikepdf.open(str(self.source)) as pdf:
            cipher = DocumentCipher.from_pdf(pdf)
        self.assertIsNotNone(cipher)
        self.assertEqual(cipher.stream_method, "aes")
        self.assertEqual(len(cipher.key), 16)

    def test_encrypted_update_readable(self):
        data = self.source.read_bytes()
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.replace("Иванов", "Петров", count=1)
            result = editor.to_bytes(incremental=True)

        self.assertTrue(result.startswith(data))
        with pikepdf.open(io.BytesIO(result)) as after:
            self.assertTrue(after.is_encrypted, "защита документа должна сохраниться")
            self.assertEqual(len(after.pages), 2)
        self.assertIn("Петров", page_text(result))

    def test_strings_are_encrypted_not_plain(self):
        """Строки в дописанных объектах не должны лежать открытым текстом."""
        data = self.source.read_bytes()
        with PdfEditor(str(self.source)) as editor:
            editor.parse()
            editor.pdf.docinfo["/Title"] = "СЕКРЕТНОЕ НАЗВАНИЕ"
            result = editor.to_bytes(incremental=True)
        appended = result[len(data):]
        self.assertNotIn("СЕКРЕТНОЕ НАЗВАНИЕ".encode("utf-16-be"), appended)
        self.assertNotIn(b"CEKPETHOE", appended)
        with pikepdf.open(io.BytesIO(result)) as after:
            self.assertEqual(str(after.docinfo["/Title"]), "СЕКРЕТНОЕ НАЗВАНИЕ")


def check_file_bytes(data: bytes):
    """Проверяет структуру документа, лежащего в памяти."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
        handle.write(data)
        path = handle.name
    try:
        return check_file(path)
    finally:
        Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
