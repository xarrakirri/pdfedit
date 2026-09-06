"""Рисует значок приложения и собирает из него файл ``.icns``.

Значок строится программно средствами PyMuPDF, поэтому не требует ни готовых
картинок, ни графического редактора: изображение описывается векторно и
отрисовывается в нужных размерах — от 16 до 1024 точек.

Сюжет значка: лист документа со строками текста, одна из строк выделена и
правится — это ровно то, что делает программа.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from pdfedit.mupdf import fitz

#: Размеры, которые macOS ожидает увидеть в наборе значков
ICONSET_SIZES = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]

# Цвета
INK = (0.09, 0.11, 0.16)          # тёмно-синий фон плитки
PAPER = (1.0, 1.0, 1.0)
TEXT_LINE = (0.62, 0.66, 0.74)
EDITED_LINE = (0.18, 0.62, 0.35)  # зелёный — «изменённый фрагмент»
HIGHLIGHT = (0.78, 0.94, 0.84)
ACCENT = (0.29, 0.50, 0.82)       # синий — «фрагмент можно править»


def _rounded_rect(shape: fitz.Shape, rect: fitz.Rect, radius: float) -> None:
    """Рисует прямоугольник со скруглёнными углами.

    Радиус задаётся в точках, а ``draw_rect`` ожидает долю от меньшей стороны,
    поэтому здесь выполняется пересчёт с ограничением сверху: доля больше 0,5
    недопустима.
    """
    shorter = min(rect.width, rect.height)
    fraction = min(radius / shorter, 0.5) if shorter else 0.0
    if fraction <= 0:
        shape.draw_rect(rect)
    else:
        shape.draw_rect(rect, radius=fraction)


def draw_icon(canvas: float = 1024.0) -> fitz.Document:
    """Строит одностраничный документ со значком."""
    doc = fitz.open()
    page = doc.new_page(width=canvas, height=canvas)
    shape = page.new_shape()

    # Плитка приложения: у значков macOS есть поля, значок не занимает весь холст
    inset = canvas * 0.085
    tile = fitz.Rect(inset, inset, canvas - inset, canvas - inset)
    _rounded_rect(shape, tile, canvas * 0.2)
    shape.finish(color=None, fill=INK)

    # Лист документа
    sheet_w = tile.width * 0.62
    sheet_h = sheet_w * 1.3
    sheet = fitz.Rect(
        tile.x0 + (tile.width - sheet_w) / 2,
        tile.y0 + (tile.height - sheet_h) / 2 - tile.height * 0.02,
        tile.x0 + (tile.width + sheet_w) / 2,
        tile.y0 + (tile.height + sheet_h) / 2 - tile.height * 0.02,
    )
    _rounded_rect(shape, sheet, canvas * 0.022)
    shape.finish(color=None, fill=PAPER)

    # Строки текста на листе; третья снизу — «правится»
    margin = sheet.width * 0.13
    line_h = sheet.height * 0.052
    gap = sheet.height * 0.085
    top = sheet.y0 + sheet.height * 0.13
    widths = [0.74, 0.88, 0.62, 0.80, 0.55, 0.84, 0.68]
    edited_index = 4

    highlight_box: fitz.Rect | None = None
    for index, width_ratio in enumerate(widths):
        y = top + index * gap
        if y + line_h > sheet.y1 - sheet.height * 0.08:
            break
        line = fitz.Rect(
            sheet.x0 + margin, y,
            sheet.x0 + margin + (sheet.width - 2 * margin) * width_ratio, y + line_h,
        )
        if index == edited_index:
            # Подсветка изменённого фрагмента и рамка вокруг него —
            # ровно так же, как программа отмечает правку на странице
            pad = line_h * 0.45
            highlight_box = fitz.Rect(
                line.x0 - pad, line.y0 - pad, line.x1 + pad, line.y1 + pad
            )
            _rounded_rect(shape, highlight_box, line_h * 0.45)
            shape.finish(color=EDITED_LINE, fill=HIGHLIGHT, width=canvas * 0.007)
            _rounded_rect(shape, line, line_h * 0.4)
            shape.finish(color=None, fill=EDITED_LINE)
        else:
            _rounded_rect(shape, line, line_h * 0.4)
            shape.finish(color=None, fill=TEXT_LINE)

    # Курсор ввода сразу за подсвеченным фрагментом: текст правится прямо здесь
    if highlight_box is not None:
        caret_x = highlight_box.x1 + line_h * 0.5
        shape.draw_rect(fitz.Rect(
            caret_x, highlight_box.y0, caret_x + canvas * 0.013, highlight_box.y1
        ))
        shape.finish(color=None, fill=ACCENT)

    shape.commit()
    return doc


def build_icns(target: Path) -> Path:
    """Создаёт файл ``.icns`` рядом с указанным путём."""
    doc = draw_icon()
    page = doc[0]
    iconset = target.with_suffix(".iconset")
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)

    for name, size in ICONSET_SIZES:
        zoom = size / page.rect.width
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True)
        pixmap.save(str(iconset / name))
    doc.close()

    result = subprocess.run(
        ["iconutil", "--convert", "icns", str(iconset), "--output", str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"iconutil не справился: {result.stderr.strip()}")
    shutil.rmtree(iconset)
    return target


def build_png(target: Path, size: int = 512) -> Path:
    """Создаёт значок в формате PNG (для систем без ``.icns``)."""
    doc = draw_icon()
    page = doc[0]
    zoom = size / page.rect.width
    page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True).save(str(target))
    doc.close()
    return target


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("pdfedit.icns")
    if out.suffix == ".png":
        print("создан", build_png(out))
    else:
        print("создан", build_icns(out))
