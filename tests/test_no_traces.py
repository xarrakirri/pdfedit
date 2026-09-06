"""Проверки того, что правка не оставляет следов и воспроизводима.

Требование к программе — «после редактирования никакие следы изменений не
должны быть заметны при проверке стандартными средствами». Метаданные
документа этому подчинены давно, но есть менее очевидное место: программы
шрифтов.

Когда в новом тексте встречается символ, которого нет во внедрённом
подмножестве шрифта, программа дописывает глиф во внедрённую программу и
сохраняет её заново. Библиотека fontTools при обычном сохранении записывает
в таблицу ``head`` текущее время. Получается документ, который сам о правке
молчит, а внедрённый в него шрифт — создан в 2002 году и изменён сегодня.
Здесь проверяется, что этого не происходит.
"""

from __future__ import annotations

import io
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pikepdf
from fontTools.ttLib import TTFont

from pdfedit import PdfEditor

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "samples")


def embedded_font_timestamps(data: bytes) -> list[int]:
    """Собирает даты изменения всех внедрённых программ шрифтов."""
    stamps: list[int] = []
    with pikepdf.open(io.BytesIO(data)) as pdf:
        for index in range(1, len(pdf.objects)):
            try:
                obj = pdf.objects[index]
                if not isinstance(obj, pikepdf.Stream):
                    continue
                raw = bytes(obj.read_bytes())
                if raw[:4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
                    continue
                font = TTFont(io.BytesIO(raw), recalcTimestamp=False, lazy=True)
                stamps.append(font["head"].modified)
                font.close()
            except Exception:
                continue
    return stamps


class FontTimestampTest(unittest.TestCase):
    """Дата правки не должна попадать во внедрённый шрифт."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(SAMPLES, "sample_subset.pdf")
        if not os.path.exists(path):
            from samples.make_samples import main as build_samples

            build_samples()
        with open(path, "rb") as handle:
            cls.data = handle.read()

    def _edit_requiring_new_glyphs(self):
        """Правка, ради которой во внедрённый шрифт придётся дописать глифы."""
        editor = PdfEditor(self.data)
        report = editor.replace("Romashka LLC", "ООО «Ромашка»")
        data = editor.to_bytes()
        editor.close()
        return report, data

    def test_шрифт_действительно_расширялся(self):
        """Без добавления глифов остальные проверки ничего не значат."""
        report, _ = self._edit_requiring_new_glyphs()
        self.assertTrue(report.applied, "замена не применилась")
        self.assertTrue(report.font_changes, "глифы не добавлялись — проверка не по адресу")

    def test_во_внедрённых_шрифтах_нет_сегодняшней_даты(self):
        _, data = self._edit_requiring_new_glyphs()
        now = time.time()
        # Отсчёт дат в шрифтах ведётся с 1904 года
        epoch_1904 = 2082844800
        for stamp in embedded_font_timestamps(data):
            seconds_ago = now - (stamp - epoch_1904)
            self.assertGreater(
                seconds_ago, 86400,
                "во внедрённом шрифте стоит сегодняшняя дата — это след правки",
            )

    def test_даты_шрифтов_совпадают_с_исходными(self):
        _, data = self._edit_requiring_new_glyphs()
        self.assertEqual(
            sorted(embedded_font_timestamps(self.data)),
            sorted(embedded_font_timestamps(data)),
            "даты изменения внедрённых шрифтов не совпадают с исходными",
        )

    def test_две_одинаковые_сборки_совпадают_побайтово(self):
        """Воспроизводимость — следствие отсутствия меток времени.

        Если результат зависит от момента сборки, значит куда-то записано
        текущее время, а это и есть след.
        """
        _, first = self._edit_requiring_new_glyphs()
        time.sleep(1.1)  # заведомо переходим через границу секунды
        _, second = self._edit_requiring_new_glyphs()
        self.assertEqual(first, second, "две одинаковые сборки дали разные файлы")


class StructureTextTest(unittest.TestCase):
    """Текстовые копии из слоя доступности должны правиться вместе с текстом.

    Документы из офисных пакетов несут структурное дерево, где рядом с
    элементами лежит ``/ActualText`` — то же содержимое словами, для экранных
    дикторов. Если её не обновить, исходная формулировка останется в файле
    открытым текстом, а часть программ (PyMuPDF в их числе) при извлечении
    предпочтёт её содержимому страницы и покажет старый текст.
    """

    ORIGINAL = "Заказчик: ООО «Ромашка», ИНН 7701234567"
    REPLACEMENT = "Заказчик: ООО «Одуванчик», ИНН 7701234567"

    def _tagged_document(self) -> bytes:
        """Берёт обычный образец и навешивает на него структурное дерево."""
        path = os.path.join(SAMPLES, "sample_contract.pdf")
        pdf = pikepdf.open(path)
        element = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name("/StructElem"),
            S=pikepdf.Name("/P"),
            ActualText=pikepdf.String(self.ORIGINAL),
        ))
        pdf.Root["/StructTreeRoot"] = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name("/StructTreeRoot"),
            K=pikepdf.Array([element]),
        ))
        buffer = io.BytesIO()
        pdf.save(buffer)
        pdf.close()
        return buffer.getvalue()

    def _actual_texts(self, data: bytes) -> list[str]:
        texts = []
        with pikepdf.open(io.BytesIO(data)) as pdf:
            root = pdf.Root.get("/StructTreeRoot")
            if root is None:
                return texts
            for element in root.get("/K", []):
                value = element.get("/ActualText")
                if value is not None:
                    texts.append(str(value))
        return texts

    def test_подготовленный_документ_содержит_копию_текста(self):
        data = self._tagged_document()
        self.assertEqual(self._actual_texts(data), [self.ORIGINAL])

    def test_копия_текста_обновляется_вместе_с_содержимым(self):
        editor = PdfEditor(self._tagged_document())
        report = editor.replace("Ромашка", "Одуванчик")
        data = editor.to_bytes()
        editor.close()
        self.assertTrue(report.applied, "замена не применилась")
        self.assertEqual(self._actual_texts(data), [self.REPLACEMENT])

    def test_исходной_формулировки_в_файле_не_остаётся(self):
        editor = PdfEditor(self._tagged_document())
        editor.replace("Ромашка", "Одуванчик")
        data = editor.to_bytes()
        editor.close()
        for text in self._actual_texts(data):
            self.assertNotIn(
                "Ромашка", text,
                "исходный текст остался в структурном дереве — это след правки",
            )


if __name__ == "__main__":
    unittest.main()
