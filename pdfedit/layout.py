"""Определение выключки строк — по левому краю, по центру или по правому.

В PDF выключки не существует: в файле лежат только координаты, куда поставить
каждую строку. «По правому краю» — это не свойство абзаца, а следствие того,
что вёрстка посчитала ширину строки и сдвинула её начало влево.

Отсюда задача при правке текста. Если заменить слово на более длинное, оставив
начало строки на месте, то строка вырастет вправо: у выключки по левому краю
это правильно, а у выключки по правому или по центру — нет. Там неизменным
должен остаться правый край или середина, то есть сдвинуть нужно **начало**
строки.

Выключку приходится восстанавливать по расположению строк на странице:

* строки одного блока, у которых совпадают левые края, выключены влево;
* совпадают правые — вправо;
* совпадают середины — по центру.

Одиночная строка сравнивается с остальным текстом страницы: если её правый
край совпадает с общим правым краем колонки, а левый нет — она выключена
вправо.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Насколько края строк могут разойтись, чтобы их всё ещё считали совпадающими.
#: Полпункта — предел, ниже которого расхождение объясняется округлением
#: координат при вёрстке, а не разной выключкой.
EDGE_TOLERANCE = 0.6

#: Насколько выигрыш одного варианта должен превосходить другой, чтобы решение
#: считалось уверенным. Без запаса короткий блок из двух почти одинаковых строк
#: относило бы то к одной выключке, то к другой.
DECISION_MARGIN = 0.5

#: Допуск при сравнении с полями листа. Он шире, чем допуск на края строк:
#: поля редко бывают идеально симметричными, а вёрстка нередко округляет
#: координаты до целых пунктов.
MARGIN_TOLERANCE = 2.5

LEFT, CENTER, RIGHT = "left", "center", "right"


@dataclass
class LineBox:
    """Строка текста с её положением на странице."""

    x0: float
    y0: float
    x1: float
    y1: float
    block: int
    #: ширина листа — нужна, чтобы судить о выключке одиночной строки
    page_width: float = 0.0

    @property
    def center(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def width(self) -> float:
        return self.x1 - self.x0


def page_lines(page) -> list[LineBox]:
    """Собирает строки страницы вместе с номером блока, к которому они относятся.

    Координаты приводятся к системе PDF — с началом в левом нижнем углу.
    Средства извлечения текста считают вертикаль сверху вниз, а модель
    документа в этой программе — снизу вверх; без пересчёта строки просто не
    нашлись бы друг у друга.
    """
    lines: list[LineBox] = []
    try:
        data = page.get_text("dict")
        height = page.rect.height
        width = page.rect.width
    except Exception:
        return lines
    for block_index, block in enumerate(data.get("blocks", [])):
        for line in block.get("lines", []):
            bbox = line.get("bbox")
            if not bbox:
                continue
            # Пустые и почти пустые строки только портят статистику краёв
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if not text.strip():
                continue
            lines.append(LineBox(
                bbox[0], height - bbox[3], bbox[2], height - bbox[1], block_index,
                page_width=width,
            ))
    return lines


def _spread(values: list[float]) -> float:
    """Разброс значений: насколько далеко они расходятся."""
    return max(values) - min(values) if values else 0.0


def _decide(lines: list[LineBox]) -> str | None:
    """Определяет выключку по согласованности краёв группы строк."""
    if len(lines) < 2:
        return None
    spreads = {
        LEFT: _spread([line.x0 for line in lines]),
        RIGHT: _spread([line.x1 for line in lines]),
        CENTER: _spread([line.center for line in lines]),
    }
    best = min(spreads, key=lambda key: spreads[key])
    if spreads[best] > EDGE_TOLERANCE:
        return None
    # Решение принимается, только если победитель заметно лучше остальных:
    # у строк одинаковой длины совпадает всё сразу, и выбор был бы случайным
    others = [value for key, value in spreads.items() if key != best]
    if min(others) - spreads[best] < DECISION_MARGIN:
        return None
    return best


def alignment_of(target: LineBox, lines: list[LineBox]) -> str:
    """Определяет выключку строки.

    Сначала по соседям того же блока — это самый надёжный признак. Если блок
    состоит из одной строки, она сравнивается с остальным текстом страницы:
    у выключенной вправо правый край совпадёт с общим правым краем, а левый —
    нет.
    """
    siblings = [line for line in lines if line.block == target.block]
    decided = _decide(siblings)
    if decided is not None:
        return decided

    # Одиночная строка: сравниваем с колонкой текста, образованной всей страницей
    others = [line for line in lines if line is not target]
    if not others:
        return LEFT
    column_left = min(line.x0 for line in others)
    column_right = max(line.x1 for line in others)
    if column_right - column_left <= 0:
        return LEFT

    at_left = abs(target.x0 - column_left) <= EDGE_TOLERANCE
    at_right = abs(target.x1 - column_right) <= EDGE_TOLERANCE
    if at_right and not at_left:
        return RIGHT
    if at_left and not at_right:
        return LEFT
    if not at_left and not at_right:
        # Строка не примыкает ни к чему из остального текста — так стоит
        # одинокое число в углу таблицы или подпись. Судить остаётся по полям
        # листа: их считаем симметричными, то есть правое поле — зеркало
        # левого. Именно этот случай и заставлял число уезжать за край.
        by_margin = _by_page_margins(target, column_left)
        if by_margin is not None:
            return by_margin

    column_center = (column_left + column_right) / 2.0
    if abs(target.center - column_center) <= EDGE_TOLERANCE and not (at_left and at_right):
        return CENTER
    return LEFT


def _by_page_margins(target: LineBox, text_left: float) -> str | None:
    """Определяет выключку одиночной строки по полям листа."""
    if not target.page_width:
        return None
    mirrored_right = target.page_width - text_left
    if abs(target.x1 - mirrored_right) <= MARGIN_TOLERANCE:
        return RIGHT
    if abs(target.center - target.page_width / 2.0) <= MARGIN_TOLERANCE:
        return CENTER
    return None


def find_line(bbox: tuple[float, float, float, float],
              lines: list[LineBox]) -> LineBox | None:
    """Находит строку, которой принадлежит указанный прямоугольник фрагмента."""
    x0, y0, x1, y1 = bbox
    center_y = (y0 + y1) / 2.0
    best: LineBox | None = None
    best_score = None
    for line in lines:
        if not (line.y0 - 1.0 <= center_y <= line.y1 + 1.0):
            continue
        # Из строк на нужной высоте берём ту, что ближе по горизонтали
        score = abs(line.x0 - x0) + abs(line.x1 - x1)
        if best_score is None or score < best_score:
            best, best_score = line, score
    return best


def shift_for_alignment(alignment: str, width_delta: float) -> float:
    """Насколько сдвинуть начало строки, чтобы выключка сохранилась.

    ``width_delta`` — насколько строка стала шире (отрицательное значение —
    уже). Результат положителен, когда начало надо сдвинуть влево.
    """
    if alignment == RIGHT:
        return width_delta
    if alignment == CENTER:
        return width_delta / 2.0
    return 0.0
