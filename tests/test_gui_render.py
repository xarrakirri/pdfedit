"""Проверки отрисовки страницы в графическом режиме.

Здесь проверяется не внешний вид, а условия, при которых страница
перерисовывается. Ошибка в них однажды привела к тому, что холст очищался и
рисовался заново восемь раз в секунду без остановки, и пользователь видел
пустое окно.

Тесты требуют работающего Tk. Там, где графической подсистемы нет (например,
на сервере сборки), они пропускаются.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk

    _root = tk.Tk()
    _root.withdraw()
    _root.destroy()
    TK_AVAILABLE = True
except Exception:  # окружение без графики
    TK_AVAILABLE = False


@unittest.skipUnless(TK_AVAILABLE, "нужен работающий Tk")
class RerenderConditionTest(unittest.TestCase):
    """Условие перерисовки не должно срабатывать в состоянии покоя."""

    def setUp(self):
        from pdfedit.gui import PdfEditApp

        self.root = tk.Tk()
        self.root.withdraw()
        self.app = PdfEditApp(self.root)

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _prepare(self, full_size, rendered, view):
        """Задаёт состояние: размер листа, отрисованный кусок и размер окна."""
        self.app._full_size = full_size
        self.app._rendered_region = rendered
        # Подменяем размеры холста и положение прокрутки: настоящее окно
        # скрыто, и Tk сообщил бы про него единицы
        self.app.canvas.winfo_width = lambda: view[0]
        self.app.canvas.winfo_height = lambda: view[1]
        self.app.canvas.canvasx = lambda _x: view[2]
        self.app.canvas.canvasy = lambda _y: view[3]

    def test_лист_меньше_окна_не_требует_перерисовки(self):
        """Лист 297×421 целиком отрисован, окно 529×722 — перерисовка не нужна.

        Именно этот случай зацикливал отрисовку: за краем листа рисовать
        нечего, но проверка считала, что видимая область шире отрисованной.
        """
        self._prepare(full_size=(297.5, 421.0), rendered=(0, 0, 298, 421),
                      view=(529, 722, 0.0, 0.0))
        self.assertFalse(self.app._needs_rerender())

    def test_лист_больше_окна_и_кусок_покрывает_видимое(self):
        self._prepare(full_size=(1532.0, 2084.0), rendered=(0, 0, 779, 972),
                      view=(529, 722, 0.0, 0.0))
        self.assertFalse(self.app._needs_rerender())

    def test_прокрутка_за_край_отрисованного_требует_перерисовки(self):
        self._prepare(full_size=(1532.0, 2084.0), rendered=(0, 0, 779, 972),
                      view=(529, 722, 0.0, 900.0))
        self.assertTrue(self.app._needs_rerender())

    def test_без_отрисованного_куска_перерисовка_нужна(self):
        self.app._full_size = (100.0, 100.0)
        self.app._rendered_region = None
        self.assertTrue(self.app._needs_rerender())


@unittest.skipUnless(TK_AVAILABLE, "нужен работающий Tk")
class VisibleClipTest(unittest.TestCase):
    """Отрисовывается только видимая часть листа, а не весь лист целиком."""

    def setUp(self):
        from pdfedit.gui import PdfEditApp

        self.root = tk.Tk()
        self.root.withdraw()
        self.app = PdfEditApp(self.root)

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_область_отрисовки_не_растёт_с_увеличением(self):
        """Главная защита от внезапного закрытия программы.

        У отсканированных документов лист бывает 1900×2800 точек. Показ такого
        листа целиком при трёхкратном увеличении — это 47 миллионов точек и
        около полугигабайта памяти; система снимает программу без отчёта о
        сбое. Площадь отрисовки должна оставаться ограниченной.
        """
        from pdfedit.mupdf import fitz

        from pdfedit.gui import RENDER_MARGIN

        doc = fitz.open()
        doc.new_page(width=1888, height=2771)
        page = doc[0]
        self.app.canvas.winfo_width = lambda: 900
        self.app.canvas.winfo_height = lambda: 700
        self.app.canvas.canvasx = lambda _x: 0.0
        self.app.canvas.canvasy = lambda _y: 0.0

        limit = (900 + 2 * RENDER_MARGIN) * (700 + 2 * RENDER_MARGIN)
        for zoom in (0.5, 1.0, 2.0, 3.0):
            clip = self.app._visible_clip(page, zoom, 1888 * zoom, 2771 * zoom, fitz)
            # Площадь считается в точках изображения, а clip задан в единицах листа
            pixels = (clip.width * zoom) * (clip.height * zoom)
            self.assertLessEqual(
                pixels, limit,
                f"при увеличении {zoom:.0%} отрисовывается {pixels/1e6:.1f} млн точек",
            )
        doc.close()

    def test_область_отрисовки_не_выходит_за_лист(self):
        from pdfedit.mupdf import fitz

        doc = fitz.open()
        doc.new_page(width=595, height=842)
        page = doc[0]
        self.app.canvas.winfo_width = lambda: 2000
        self.app.canvas.winfo_height = lambda: 2000
        self.app.canvas.canvasx = lambda _x: 0.0
        self.app.canvas.canvasy = lambda _y: 0.0

        clip = self.app._visible_clip(page, 1.0, 595, 842, fitz)
        self.assertLessEqual(clip.x1, 595 + 0.01)
        self.assertLessEqual(clip.y1, 842 + 0.01)
        doc.close()


if __name__ == "__main__":
    unittest.main()
