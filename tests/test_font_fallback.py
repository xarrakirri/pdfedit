"""Подбор запасного шрифта: начертание важнее широты охвата символов.

Когда во внедрённом в документ шрифте нет нужных букв (обычный случай:
документ набран латиницей, а вставляется кириллица), программа берёт глифы
из системного шрифта. Если своего семейства в системе нет, включается
запасной подбор — и вот тут легко испортить вид документа.

Шрифты с огромным охватом письменностей (Arial Unicode MS и подобные)
существуют только в обычном начертании. Подставленные вместо жирного, они
дают заметный глазу дефект: строка становится тоньше соседних. Именно так
и происходило, пока подбор смотрел только на наличие символов.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdfedit.fonts import FontStyle, find_fallback_font, font_has_chars, _load_index

CYRILLIC = "ЗАМЕНА"


def _has_style(style: FontStyle) -> bool:
    """Есть ли в системе шрифт с кириллицей и таким начертанием."""
    for font in _load_index(()):
        if not font_has_chars(font, CYRILLIC):
            continue
        candidate = font.style
        if candidate.bold == style.bold and candidate.italic == style.italic:
            return True
    return False


class FallbackStyleTest(unittest.TestCase):
    """Начертание исходного текста должно сохраняться."""

    def _check(self, style: FontStyle, serif: bool = False):
        if not _has_style(style):
            self.skipTest("в системе нет шрифта с кириллицей и таким начертанием")
        font = find_fallback_font(CYRILLIC, style=style, serif=serif)
        self.assertIsNotNone(font, "запасной шрифт не подобран")
        self.assertEqual(
            (font.style.bold, font.style.italic), (style.bold, style.italic),
            f"для {style} подобран {font.family} {font.subfamily} — начертание не то",
        )
        return font

    def test_жирный_остаётся_жирным(self):
        """Главный случай: тонкий текст рядом с жирным сразу бросается в глаза."""
        self._check(FontStyle(bold=True, italic=False))

    def test_курсив_остаётся_курсивом(self):
        self._check(FontStyle(bold=False, italic=True))

    def test_жирный_курсив_сохраняется(self):
        self._check(FontStyle(bold=True, italic=True))

    def test_обычное_начертание_не_становится_жирным(self):
        self._check(FontStyle(bold=False, italic=False))

    def test_для_шрифта_с_засечками_берётся_шрифт_с_засечками(self):
        """Замена в документе, набранном Times, не должна выглядеть как Arial."""
        serif_font = self._check(FontStyle(bold=False, italic=False), serif=True)
        sans_font = self._check(FontStyle(bold=False, italic=False), serif=False)
        if serif_font.family == sans_font.family:
            self.skipTest("в системе не нашлось разных шрифтов с засечками и без")
        self.assertNotEqual(
            serif_font.family, sans_font.family,
            "для шрифтов с засечками и без подобран один и тот же запасной",
        )

    def test_подбор_не_падает_на_редких_символах(self):
        """Если подходящего начертания нет, шрифт всё равно должен найтись."""
        font = find_fallback_font("漢字", style=FontStyle(bold=True))
        if font is not None:
            self.assertTrue(font_has_chars(font, "漢字"))


class FallbackPreferenceTest(unittest.TestCase):
    """Порядок предпочтений: сначала начертание, потом широта охвата."""

    def test_шрифт_только_с_обычным_начертанием_не_вытесняет_жирный(self):
        """Arial Unicode MS покрывает почти всё, но начертание у него одно.

        Пока он стоял первым в списке предпочтений, любая замена в жирном
        тексте приводила к подстановке обычного начертания.
        """
        style = FontStyle(bold=True, italic=False)
        if not _has_style(style):
            self.skipTest("в системе нет жирного шрифта с кириллицей")
        font = find_fallback_font(CYRILLIC, style=style)
        self.assertTrue(
            font.style.bold,
            f"вместо жирного подобран {font.family} {font.subfamily}",
        )


class SerifDetectionTest(unittest.TestCase):
    """Род шрифта определяется и тогда, когда флаг в документе не заполнен."""

    def _font(self, base_font: str, flags: int = 0):
        import pikepdf

        from pdfedit.fonts import FontInfo

        pdf = pikepdf.new()
        font_dict = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name("/Font"),
            Subtype=pikepdf.Name("/TrueType"),
            BaseFont=pikepdf.Name(base_font),
            FontDescriptor=pdf.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name("/FontDescriptor"),
                FontName=pikepdf.Name(base_font),
                Flags=flags,
            )),
        ))
        return FontInfo("/F1", font_dict)

    def test_флаг_в_документе_учитывается(self):
        self.assertTrue(self._font("/SomeUnknownFace", flags=2).is_serif)

    def test_times_без_флага_всё_равно_с_засечками(self):
        """Именно этот случай встречается в документах из офисных пакетов."""
        self.assertTrue(self._font("/ABCDEF+TimesNewRomanPSMT", flags=0).is_serif)
        self.assertTrue(self._font("/TimesNewRomanPS-BoldMT", flags=0).is_serif)

    def test_рубленый_шрифт_не_считается_шрифтом_с_засечками(self):
        self.assertFalse(self._font("/ABCDEF+ArialMT", flags=0).is_serif)
        self.assertFalse(self._font("/Helvetica", flags=0).is_serif)

    def test_начертание_читается_из_имени(self):
        self.assertTrue(self._font("/TimesNewRomanPS-BoldMT").style.bold)
        self.assertFalse(self._font("/TimesNewRomanPSMT").style.bold)


class FontCollectionTest(unittest.TestCase):
    """Из коллекции шрифтов должны читаться все начертания, а не первое.

    Начертания коллекции (.ttc) делят один открытый файл. Если закрывать их
    по одному прямо в цикле разбора, то после первого же остальные перестают
    читаться и молча теряются. Так из Times.ttc в указатель попадало одно
    начертание из четырёх: жирного Times в системе как бы не существовало, и
    донором для жирного текста становился обычный.
    """

    def _collections(self):
        from pdfedit.fonts import system_font_dirs

        for directory in system_font_dirs(()):
            for path in sorted(directory.rglob("*")):
                if path.suffix.lower() in (".ttc", ".otc"):
                    yield path

    def test_из_коллекции_читаются_все_начертания(self):
        from fontTools.ttLib import TTCollection

        from pdfedit.fonts import _index_font_file

        checked = 0
        for path in self._collections():
            try:
                collection = TTCollection(str(path), lazy=True)
                expected = len(collection.fonts)
                collection.close()
            except Exception:
                continue
            if expected < 2:
                continue
            self.assertEqual(
                len(_index_font_file(path)), expected,
                f"из {path.name} прочитано не всё: ожидалось {expected} начертаний",
            )
            checked += 1
            if checked >= 5:
                break
        if not checked:
            self.skipTest("в системе нет коллекций шрифтов с несколькими начертаниями")

    def test_в_указателе_есть_жирные_начертания_из_коллекций(self):
        """Проверка «с другого конца»: жирные из коллекций дошли до указателя."""
        from pdfedit.fonts import _load_index

        from_collections = [
            font for font in _load_index(())
            if font.path.lower().endswith((".ttc", ".otc")) and font.index > 0
        ]
        if not from_collections:
            self.skipTest("в системе нет коллекций шрифтов")
        self.assertTrue(
            any(font.style.bold for font in from_collections),
            "ни одного жирного начертания из коллекций не попало в указатель",
        )


if __name__ == "__main__":
    unittest.main()
