"""Сохранение выключки строк при изменении их ширины.

В PDF выключки не существует — есть только координаты, куда поставлена каждая
строка. «По правому краю» означает, что вёрстка посчитала ширину строки и
сдвинула её начало влево.

Поэтому при замене текста на более длинный нельзя просто оставить начало
строки на месте: у выключки по левому краю это правильно, а у выключки по
правому или по центру строка полезет за поля. Здесь проверяется, что край
или середина остаются на месте.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdfedit import PdfEditor, layout
from pdfedit.mupdf import fitz

FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
LEFT_MARGIN, RIGHT_MARGIN = 60.0, 400.0
SIZE = 13


def _build(groups) -> bytes:
    """Собирает страницу из абзацев с заданной выключкой."""
    doc = fitz.open()
    page = doc.new_page(width=460, height=330)
    font = fitz.Font(fontfile=FONT)
    for align, rows, top in groups:
        for index, text in enumerate(rows):
            width = font.text_length(text, fontsize=SIZE)
            if align == "left":
                x = LEFT_MARGIN
            elif align == "center":
                x = LEFT_MARGIN + (RIGHT_MARGIN - LEFT_MARGIN - width) / 2
            else:
                x = RIGHT_MARGIN - width
            page.insert_text((x, top + index * 22), text,
                             fontname="F0", fontfile=FONT, fontsize=SIZE)
    doc.subset_fonts()
    data = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    return data


def _line_edges(data: bytes) -> dict[str, tuple[float, float]]:
    """Левый и правый край каждой строки готового документа."""
    doc = fitz.open(stream=data, filetype="pdf")
    edges: dict[str, tuple[float, float]] = {}
    for block in doc[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            # В извлечённом тексте пробел нередко оказывается неразрывным —
            # для сопоставления строк это несущественно
            text = "".join(span["text"] for span in line["spans"])
            text = text.replace("\xa0", " ").strip()
            if text:
                edges[text] = (line["bbox"][0], line["bbox"][2])
    doc.close()
    return edges


SAMPLE = [
    ("left", ["Первая строка абзаца", "ЗАМЕНА левая", "третья строка тут"], 50),
    ("center", ["Заголовок раздела", "ЗАМЕНА центр", "подзаголовок ниже"], 140),
    ("right", ["Подпись сверху", "ЗАМЕНА право", "должность ниже"], 230),
]

LONGER = "ЗНАЧИТЕЛЬНО ДЛИННЕЕ ПРЕЖНЕГО"


@unittest.skipUnless(os.path.exists(FONT), "нужен системный Arial")
class AlignmentDetectionTest(unittest.TestCase):
    """Выключка определяется по согласованности краёв соседних строк."""

    def setUp(self):
        self.data = _build(SAMPLE)

    def test_выключка_определяется_верно(self):
        editor = PdfEditor(self.data, use_font_library=False)
        editor.parse()
        lines = editor._page_lines(0)
        self.assertTrue(lines, "строки страницы не разобраны")

        expected = {
            "Первая строка абзаца": "left",
            "Заголовок раздела": "center",
            "Подпись сверху": "right",
        }
        for run in editor.runs:
            want = expected.get(run.text.strip())
            if want is None:
                continue
            line = layout.find_line(run.bbox, lines)
            self.assertIsNotNone(line, f"строка «{run.text}» не найдена на странице")
            self.assertEqual(layout.alignment_of(line, lines), want,
                             f"неверная выключка у «{run.text}»")
        editor.close()

    def test_строка_фрагмента_находится(self):
        """Координаты фрагмента и строки должны быть в одной системе.

        Модель документа считает вертикаль снизу вверх, средства извлечения
        текста — сверху вниз. Без пересчёта строки не находятся вовсе.
        """
        editor = PdfEditor(self.data, use_font_library=False)
        editor.parse()
        lines = editor._page_lines(0)
        found = [layout.find_line(run.bbox, lines) for run in editor.runs]
        editor.close()
        self.assertTrue(all(line is not None for line in found),
                        "часть фрагментов не сопоставилась со строками")


@unittest.skipUnless(os.path.exists(FONT), "нужен системный Arial")
class AlignmentPreservedTest(unittest.TestCase):
    """При удлинении текста выключка должна сохраняться."""

    def setUp(self):
        self.data = _build(SAMPLE)
        self.before = _line_edges(self.data)
        editor = PdfEditor(self.data, use_font_library=False)
        for suffix in ("левая", "центр", "право"):
            editor.replace(f"ЗАМЕНА {suffix}", f"{LONGER} {suffix}")
        self.after_bytes = editor.to_bytes()
        editor.close()
        self.after = _line_edges(self.after_bytes)

    def _edges(self, mapping, needle):
        for text, edges in mapping.items():
            if needle in text:
                return edges
        self.fail(f"строка с «{needle}» не найдена: {list(mapping)}")

    def test_у_выключки_по_левому_краю_держится_левый_край(self):
        before = self._edges(self.before, "ЗАМЕНА левая")
        after = self._edges(self.after, "левая")
        self.assertAlmostEqual(before[0], after[0], delta=0.6)
        self.assertGreater(after[1], before[1], "строка не стала длиннее")

    def test_у_выключки_по_правому_краю_держится_правый_край(self):
        before = self._edges(self.before, "ЗАМЕНА право")
        after = self._edges(self.after, "право")
        self.assertAlmostEqual(
            before[1], after[1], delta=0.6,
            msg="правый край уехал — выключка не сохранена",
        )

    def test_у_выключки_по_центру_держится_середина(self):
        before = self._edges(self.before, "ЗАМЕНА центр")
        after = self._edges(self.after, "центр")
        self.assertAlmostEqual(
            (before[0] + before[1]) / 2, (after[0] + after[1]) / 2, delta=0.6,
            msg="середина уехала — выключка не сохранена",
        )

    def test_текст_не_выходит_за_поля(self):
        for text, (x0, x1) in self.after.items():
            self.assertLessEqual(x1, RIGHT_MARGIN + 1.0, f"«{text}» вышла вправо")
            self.assertGreaterEqual(x0, LEFT_MARGIN - 1.0, f"«{text}» вышла влево")

    def test_соседние_строки_не_сдвинулись(self):
        """Сдвиг обязан затрагивать только изменённую строку."""
        for text in ("Первая строка абзаца", "третья строка тут",
                     "Заголовок раздела", "подзаголовок ниже",
                     "Подпись сверху", "должность ниже"):
            self.assertIn(text, self.after, f"строка «{text}» пропала")
            self.assertAlmostEqual(self.before[text][0], self.after[text][0], delta=0.3,
                                   msg=f"«{text}» сдвинулась по горизонтали")

    def test_режим_можно_отключить(self):
        """Без выравнивания строка растёт вправо — это и был исходный дефект.

        Текст уезжает так далеко, что часть его оказывается за краем листа и
        при извлечении теряется, поэтому строка ищется по началу.
        """
        editor = PdfEditor(self.data, use_font_library=False, align_aware=False)
        editor.replace("ЗАМЕНА право", f"{LONGER} право")
        plain = _line_edges(editor.to_bytes())
        editor.close()
        before = self._edges(self.before, "ЗАМЕНА право")
        after = self._edges(plain, "ЗНАЧИТЕЛЬНО")
        self.assertAlmostEqual(before[0], after[0], delta=0.6,
                               msg="без выравнивания начало строки обязано остаться")
        self.assertGreater(after[1], before[1] + 1.0,
                           "без выравнивания правый край должен уехать")


@unittest.skipUnless(os.path.exists(FONT), "нужен системный Arial")
class LonelyRightAlignedTest(unittest.TestCase):
    """Одиночное число, прижатое к правому полю листа.

    Самый частый случай в счетах и таблицах: сумма стоит у правого края, а
    соседей по колонке, на которых можно опереться, нет. Строка не примыкает
    ни к чему из остального текста, и выключку приходится определять по полям
    листа, считая их симметричными.
    """

    PAGE_WIDTH = 400.0
    RIGHT = 360.0
    LEFT = 40.0

    def _build(self, lonely: str) -> bytes:
        doc = fitz.open()
        page = doc.new_page(width=self.PAGE_WIDTH, height=200)
        font = fitz.Font(fontfile=FONT)
        for index, text in enumerate(
            ["Обычный текст абзаца", "продолжение строки", "и ещё строка"]
        ):
            page.insert_text((self.LEFT, 60 + index * 22), text,
                             fontname="F0", fontfile=FONT, fontsize=SIZE)
        width = font.text_length(lonely, fontsize=SIZE)
        page.insert_text((self.RIGHT - width, 150), lonely,
                         fontname="F0", fontfile=FONT, fontsize=SIZE)
        doc.subset_fonts()
        data = doc.tobytes(garbage=4, deflate=True)
        doc.close()
        return data

    def test_одиночное_число_у_правого_поля_распознаётся(self):
        data = self._build("2 180")
        editor = PdfEditor(data, use_font_library=False)
        editor.parse()
        lines = editor._page_lines(0)
        target = next(run for run in editor.runs if "2" in run.text)
        line = layout.find_line(target.bbox, lines)
        editor.close()
        self.assertIsNotNone(line)
        self.assertEqual(layout.alignment_of(line, lines), "right")

    def test_после_удлинения_правый_край_на_месте(self):
        data = self._build("2 180")
        editor = PdfEditor(data, use_font_library=False)
        editor.replace("2 180", "1 234 567")
        result = editor.to_bytes()
        editor.close()

        edges = _line_edges(result)
        after = next(value for text, value in edges.items() if "234" in text)
        self.assertAlmostEqual(
            after[1], self.RIGHT, delta=0.6,
            msg="правый край числа уехал — оно вылезет за поле",
        )
        self.assertLess(after[0], self.RIGHT - 40,
                        "число должно было вырасти влево")

    def test_абзац_слева_не_тронут(self):
        data = self._build("2 180")
        before = _line_edges(data)
        editor = PdfEditor(data, use_font_library=False)
        editor.replace("2 180", "1 234 567")
        after = _line_edges(editor.to_bytes())
        editor.close()
        for text in ("Обычный текст абзаца", "продолжение строки", "и ещё строка"):
            self.assertAlmostEqual(before[text][0], after[text][0], delta=0.3)
            self.assertAlmostEqual(before[text][1], after[text][1], delta=0.3)


if __name__ == "__main__":
    unittest.main()
