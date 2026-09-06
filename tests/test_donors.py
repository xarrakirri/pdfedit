"""Шрифты-доноры: из самого документа и из других PDF.

Документ несёт не весь шрифт, а только использованные глифы. Когда нужных
букв нет, программа ищет их на стороне. Здесь проверяются два источника,
которые точнее системных шрифтов:

* другие шрифты того же документа — это буквально те же контуры;
* личная библиотека, собранная пользователем из других PDF.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdfedit import PdfEditor
from pdfedit.donors import extract_embedded_fonts
from pdfedit.fonts import font_has_chars, fonts_in_dirs
from pdfedit.mupdf import fitz

ARIAL = "/System/Library/Fonts/Supplemental/Arial.ttf"


def _page_pdf(text: str, fontfile: str = ARIAL) -> bytes:
    """Собирает одностраничный PDF с внедрённым подмножеством шрифта."""
    doc = fitz.open()
    page = doc.new_page(width=460, height=100)
    page.insert_text((30, 60), text, fontname="F0", fontfile=fontfile, fontsize=22)
    doc.subset_fonts()
    data = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    return data


def _merged(*documents: bytes) -> bytes:
    """Склеивает документы, сохраняя их шрифты раздельными."""
    out = fitz.open()
    for blob in documents:
        part = fitz.open(stream=blob, filetype="pdf")
        out.insert_pdf(part)
        part.close()
    data = out.tobytes()
    out.close()
    return data


@unittest.skipUnless(os.path.exists(ARIAL), "нужен системный Arial")
class ExtractionTest(unittest.TestCase):
    """Извлечение программ шрифтов из PDF."""

    def test_шрифт_извлекается_и_пригоден(self):
        data = _page_pdf("Пример текста")
        with tempfile.TemporaryDirectory() as tmp:
            fonts = extract_embedded_fonts(data, tmp)
            usable = [f for f in fonts if f.usable]
            self.assertTrue(usable, f"ни один шрифт не признан пригодным: "
                                    f"{[f.note for f in fonts]}")
            self.assertTrue(usable[0].path.exists())

    def test_таблица_символов_восстанавливается_по_документу(self):
        """Создающие программы выбрасывают cmap — её надо вернуть.

        Без таблицы соответствия символов файл шрифта не может ответить, есть
        ли у него нужная буква, и как донор бесполезен.
        """
        data = _page_pdf("Кириллица")
        with tempfile.TemporaryDirectory() as tmp:
            extract_embedded_fonts(data, tmp)
            pool = fonts_in_dirs((tmp,))
            self.assertTrue(pool, "извлечённый шрифт не попал в индекс")
            self.assertTrue(
                any(font_has_chars(f, "Кир") for f in pool),
                "по извлечённому шрифту не находятся его же символы",
            )

    def test_одинаковые_шрифты_не_задваиваются(self):
        data = _page_pdf("Текст")
        with tempfile.TemporaryDirectory() as tmp:
            extract_embedded_fonts(data, tmp)
            extract_embedded_fonts(data, tmp)
            files = [p for p in os.listdir(tmp) if p.endswith((".ttf", ".otf"))]
            self.assertEqual(len(files), len(set(files)))
            self.assertLessEqual(len(files), 2, "один и тот же шрифт сохранён дважды")


@unittest.skipUnless(os.path.exists(ARIAL), "нужен системный Arial")
class DocumentFontsTest(unittest.TestCase):
    """Глифы берутся из других шрифтов того же документа."""

    def setUp(self):
        # Первая страница — только латиница, вторая — кириллица отдельным
        # шрифтом. Правим первую: нужных букв в её шрифте нет, но они есть
        # в том же файле.
        self.data = _merged(_page_pdf("HEADING"), _page_pdf("ЗАГОЛОВОК ПРИМЕР"))

    def _donor_of(self, use_document_fonts: bool) -> str:
        editor = PdfEditor(self.data, use_document_fonts=use_document_fonts,
                           use_font_library=False)
        report = editor.replace("HEADING", "ЗАГОЛОВОК", count=1)
        editor.close()
        self.assertTrue(report.applied, "замена не применилась")
        self.assertTrue(report.font_changes, "глифы не добавлялись")
        return report.font_changes[0]

    def test_донор_берётся_из_документа(self):
        note = self._donor_of(use_document_fonts=True)
        self.assertIn(
            "pdfedit-fonts", note,
            f"донор взят не из документа: {note}",
        )

    def test_без_разрешения_донор_берётся_из_системы(self):
        note = self._donor_of(use_document_fonts=False)
        self.assertNotIn("pdfedit-fonts", note)

    def test_текст_после_замены_читается(self):
        editor = PdfEditor(self.data, use_font_library=False)
        editor.replace("HEADING", "ЗАГОЛОВОК", count=1)
        result = editor.to_bytes()
        editor.close()
        doc = fitz.open(stream=result, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
        self.assertIn("ЗАГОЛОВОК", text)

    def test_временный_каталог_убирается_за_собой(self):
        editor = PdfEditor(self.data, use_font_library=False)
        editor.replace("HEADING", "ЗАГОЛОВОК", count=1)
        directory = editor._document_font_dir
        self.assertTrue(directory and os.path.isdir(directory))
        editor.close()
        self.assertFalse(os.path.isdir(directory), "временные шрифты не удалены")


@unittest.skipUnless(os.path.exists(ARIAL), "нужен системный Arial")
class FontLibraryTest(unittest.TestCase):
    """Библиотека доноров, собранная из других PDF."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = os.environ.get("PDFEDIT_FONT_LIBRARY")
        os.environ["PDFEDIT_FONT_LIBRARY"] = self._tmp.name
        fonts_in_dirs.cache_clear()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("PDFEDIT_FONT_LIBRARY", None)
        else:
            os.environ["PDFEDIT_FONT_LIBRARY"] = self._saved
        fonts_in_dirs.cache_clear()
        self._tmp.cleanup()

    def test_библиотека_пополняется_и_перечисляется(self):
        from pdfedit.donors import add_pdf_to_library, library_fonts

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(_page_pdf("Пример"))
            source = handle.name
        try:
            added = add_pdf_to_library(source)
            self.assertTrue(any(f.usable for f in added))
            fonts_in_dirs.cache_clear()
            self.assertTrue(library_fonts(), "библиотека осталась пустой")
        finally:
            os.unlink(source)

    def test_очистка_библиотеки(self):
        from pdfedit.donors import add_pdf_to_library, clear_library, library_fonts

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(_page_pdf("Пример"))
            source = handle.name
        try:
            add_pdf_to_library(source)
            self.assertGreater(clear_library(), 0)
            fonts_in_dirs.cache_clear()
            self.assertEqual(library_fonts(), [])
        finally:
            os.unlink(source)


@unittest.skipUnless(os.path.exists(ARIAL), "нужен системный Arial")
class FontNameRestorationTest(unittest.TestCase):
    """Имя шрифта восстанавливается по данным документа.

    Подмножества, вырезанные в PDF, почти всегда идут без таблицы ``name``:
    для показа страницы имя не нужно. Но донор без имени не попадает в
    указатель шрифтов вообще — ни семейства, ни начертания у него нет, и
    подбор «того же шрифта, что в документе» не срабатывает. Пока имя не
    восстанавливалось, шрифты документа как доноры не работали совсем.
    """

    def _stripped_font(self, tmp: str) -> str:
        """Готовит файл шрифта без таблицы имён — как в настоящем PDF."""
        from fontTools.ttLib import TTFont

        path = os.path.join(tmp, "нечто.ttf")
        font = TTFont(ARIAL, recalcTimestamp=False)
        if "name" in font:
            del font["name"]
        font.save(path)
        font.close()
        return path

    def test_шрифт_без_имени_не_попадает_в_указатель(self):
        """Проверка предпосылки: без имени шрифт действительно теряется."""
        from pdfedit.fonts import _index_font_file
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = self._stripped_font(tmp)
            self.assertEqual(len(_index_font_file(Path(path))), 0)

    def test_имя_восстанавливается_из_имени_шрифта_в_документе(self):
        from pathlib import Path

        from pdfedit.donors import _restore_names
        from pdfedit.fonts import _index_font_file

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(self._stripped_font(tmp))
            self.assertTrue(_restore_names(path, "/ABCDEF+TinkoffSans-Medium"))
            entries = _index_font_file(path)
            self.assertEqual(len(entries), 1, "шрифт всё ещё не индексируется")
            entry = entries[0]
            self.assertEqual(entry.family, "TinkoffSans")
            self.assertEqual(entry.subfamily, "Medium")
            self.assertEqual(entry.style.weight, 500)

    def test_готовое_имя_не_перезаписывается(self):
        from pathlib import Path

        from pdfedit.donors import _restore_names

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "arial.ttf"
            path.write_bytes(open(ARIAL, "rb").read())
            self.assertFalse(_restore_names(path, "/Что-Нибудь-Другое"),
                             "имя было переписано, хотя оно уже есть")

    def test_извлечённые_шрифты_попадают_в_указатель(self):
        """Сквозная проверка: после извлечения шрифт можно найти по семейству."""
        data = _page_pdf("Пример текста")
        with tempfile.TemporaryDirectory() as tmp:
            extract_embedded_fonts(data, tmp)
            pool = fonts_in_dirs((tmp,))
            self.assertTrue(pool, "ни один извлечённый шрифт не попал в указатель")
            self.assertTrue(all(font.family for font in pool),
                            "у извлечённого шрифта пустое семейство")


class FontWeightTest(unittest.TestCase):
    """Насыщенность различается тоньше, чем «жирный / не жирный»."""

    def test_насыщенность_по_названию(self):
        from pdfedit.fonts import weight_from_name

        self.assertEqual(weight_from_name("Regular"), 400)
        self.assertEqual(weight_from_name("Medium"), 500)
        self.assertEqual(weight_from_name("SemiBold"), 600)
        self.assertEqual(weight_from_name("Bold"), 700)
        self.assertEqual(weight_from_name("Light"), 300)
        self.assertEqual(weight_from_name("TinkoffSans-Medium"), 500)

    def test_у_неизвестного_названия_обычная_насыщенность(self):
        from pdfedit.fonts import DEFAULT_WEIGHT, weight_from_name

        self.assertEqual(weight_from_name("ЧтоТоСовсемНепонятное"), DEFAULT_WEIGHT)

    def test_semibold_не_путается_с_bold(self):
        """«semibold» содержит «bold» — порядок проверки признаков важен."""
        from pdfedit.fonts import weight_from_name

        self.assertNotEqual(weight_from_name("SemiBold"), weight_from_name("Bold"))


if __name__ == "__main__":
    unittest.main()
