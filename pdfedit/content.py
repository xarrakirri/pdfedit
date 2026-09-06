"""Разбор потоков содержимого PDF и построение модели текстовых фрагментов.

Модуль превращает поток операторов страницы в список **текстовых фрагментов**
(:class:`TextRun`) — визуально связных кусочков текста с известными шрифтом,
кеглем, цветом и прямоугольником на странице. Каждый символ фрагмента помнит,
из какого места какого оператора он пришёл (:class:`GlyphRef`), поэтому замену
можно выполнить хирургически: переписать ровно нужные байты в операндах
``Tj``/``TJ``, не трогая всё остальное.

Отслеживается полное состояние, влияющее на положение и вид текста:
матрица преобразования (``cm``, ``q``/``Q``), текстовые матрицы ``Tm``/``Tlm``,
кегль, межсимвольный и межсловный интервал, горизонтальное сжатие, смещение
базовой линии и цвет заливки.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import pikepdf

from .fonts import FontInfo, load_page_fonts

Matrix = tuple[float, float, float, float, float, float]

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

#: Номера фрагментов выдаются по формуле ``страница * RUN_ID_STRIDE + номер``.
#: Благодаря этому идентификатор фрагмента не зависит от того, какие ещё
#: страницы разбирались, и сохранённый список правок остаётся применимым.
RUN_ID_STRIDE = 1_000_000


def mat_mul(m1: Matrix, m2: Matrix) -> Matrix:
    """Произведение матриц PDF: сначала ``m1``, затем ``m2``."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def mat_apply(m: Matrix, x: float, y: float) -> tuple[float, float]:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class GlyphRef:
    """Один глиф: где он нарисован и откуда взят в потоке содержимого."""

    op_index: int          # индекс инструкции в списке инструкций потока
    elem_index: int        # индекс элемента внутри массива TJ (0 для Tj)
    byte_start: int        # смещение кода внутри строки-операнда
    byte_len: int          # длина кода в байтах
    code: int
    text: str              # что этот код означает (может быть лигатурой)
    width1000: float       # ширина глифа в тысячных долях кегля
    advance: float         # полное продвижение пера в текстовых единицах
    trm: Matrix            # матрица отображения текста для этого глифа
    size: float

    def bbox(self, ascent: float, descent: float) -> tuple[float, float, float, float]:
        """Прямоугольник глифа в координатах страницы."""
        x0, y0 = mat_apply(self.trm, 0.0, descent / 1000.0)
        x1, y1 = mat_apply(self.trm, self.width1000 / 1000.0, ascent / 1000.0)
        x2, y2 = mat_apply(self.trm, 0.0, ascent / 1000.0)
        x3, y3 = mat_apply(self.trm, self.width1000 / 1000.0, descent / 1000.0)
        return (min(x0, x1, x2, x3), min(y0, y1, y2, y3),
                max(x0, x1, x2, x3), max(y0, y1, y2, y3))


@dataclass
class TextRun:
    """Визуально связный фрагмент текста, пригодный для редактирования."""

    run_id: int
    stream_id: str             # к какому потоку содержимого относится
    page_index: int
    font_res: str
    font: FontInfo
    size: float
    fill_color: tuple[float, float, float] | None
    glyphs: list[GlyphRef] = field(default_factory=list)
    text: str = ""
    #: для каждого символа ``text`` — индекс глифа, либо ``-1`` для пробела,
    #: который отсутствует в потоке и вставлен по величине зазора
    char_to_glyph: list[int] = field(default_factory=list)
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    hscale: float = 1.0
    render_mode: int = 0
    #: Индекс оператора показа, с которого начинается строка (первый после
    #: оператора позиционирования). Нужен, чтобы сдвинуть строку целиком:
    #: у выключки по центру и по правому краю неизменным должно оставаться
    #: не начало строки, а её середина или правый край.
    line_start_op: int = -1

    @property
    def editable(self) -> bool:
        """Можно ли редактировать фрагмент (Type3 и «безглифовые» — нельзя)."""
        return bool(self.glyphs) and not self.font.is_type3

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        if not self.glyphs:
            return (0.0, 0.0, 0.0, 0.0)
        asc, desc = self.font.ascent, self.font.descent
        boxes = [g.bbox(asc, desc) for g in self.glyphs]
        return (
            min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes),
        )

    def glyph_span_for_chars(self, start: int, end: int) -> tuple[int, int, bool]:
        """Переводит диапазон символов текста в диапазон глифов.

        Возвращает ``(первый глиф, последний+1, расширен ли диапазон)``.
        Расширение происходит, когда совпадение попадает в середину лигатуры.
        """
        indices = [
            self.char_to_glyph[i]
            for i in range(start, min(end, len(self.char_to_glyph)))
            if self.char_to_glyph[i] >= 0
        ]
        if not indices:
            return (0, 0, False)
        g0, g1 = min(indices), max(indices) + 1
        covered = "".join(self.glyphs[i].text for i in range(g0, g1))
        return (g0, g1, covered != self.text[start:end])


@dataclass
class StreamContext:
    """Поток содержимого вместе с его ресурсами и разобранными инструкциями."""

    stream_id: str
    owner: pikepdf.Object       # страница или Form XObject
    instructions: list
    resources: pikepdf.Object
    fonts: dict[str, FontInfo]
    is_page: bool
    dirty: bool = False
    #: Исходные байты потока и разбор до правок. Нужны, чтобы записать поток
    #: обратно точечной заменой инструкций, сохранив манеру записи оригинала:
    #: пересборка целиком отдала бы форматирование на откуп библиотеке
    original_data: bytes = b""
    original_instructions: list = field(default_factory=list)


class _TextState:
    """Текстовое состояние PDF (раздел 9.3 спецификации)."""

    __slots__ = ("font_res", "font", "size", "char_spacing", "word_spacing",
                 "hscale", "leading", "rise", "render_mode")

    def __init__(self) -> None:
        self.font_res: str | None = None
        self.font: FontInfo | None = None
        self.size = 0.0
        self.char_spacing = 0.0
        self.word_spacing = 0.0
        self.hscale = 1.0
        self.leading = 0.0
        self.rise = 0.0
        self.render_mode = 0

    def copy(self) -> "_TextState":
        clone = _TextState()
        for slot in self.__slots__:
            setattr(clone, slot, getattr(self, slot))
        return clone


class _GraphicsState:
    __slots__ = ("ctm", "text", "fill_color")

    def __init__(self, ctm: Matrix = IDENTITY) -> None:
        self.ctm = ctm
        self.text = _TextState()
        self.fill_color: tuple[float, float, float] | None = (0.0, 0.0, 0.0)

    def copy(self) -> "_GraphicsState":
        clone = _GraphicsState(self.ctm)
        clone.text = self.text.copy()
        clone.fill_color = self.fill_color
        return clone


def _cmyk_to_rgb(c: float, m: float, y: float, k: float) -> tuple[float, float, float]:
    return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))


class ContentParser:
    """Разбирает поток содержимого и собирает текстовые фрагменты."""

    #: зазор (в долях кегля), с которого между глифами подразумевается пробел
    SPACE_GAP = 0.19
    #: зазор, с которого фрагмент разрывается на два
    BREAK_GAP = 2.5

    def __init__(self, page_index: int, run_counter_start: int = 0) -> None:
        self.page_index = page_index
        self.runs: list[TextRun] = []
        self._run_counter = run_counter_start
        self.warnings: list[str] = []

    # ------------------------------------------------------------------
    def parse_stream(
        self,
        ctx: StreamContext,
        base_ctm: Matrix = IDENTITY,
        xobject_stack: tuple[str, ...] = (),
        contexts: dict[str, StreamContext] | None = None,
        pdf: pikepdf.Pdf | None = None,
    ) -> None:
        """Проходит по инструкциям потока, накапливая фрагменты текста."""
        gs = _GraphicsState(base_ctm)
        stack: list[_GraphicsState] = []
        tm: Matrix = IDENTITY
        tlm: Matrix = IDENTITY
        in_text = False

        current: TextRun | None = None
        # позиция и параметры конца предыдущего глифа — для решения о разрыве
        prev_end_x = 0.0
        prev_baseline: tuple[float, float] | None = None

        def flush() -> None:
            nonlocal current
            if current is not None and current.glyphs:
                self.runs.append(current)
            current = None

        line_start_op: int | None = None

        for op_index, instr in enumerate(ctx.instructions):
            operator = str(instr.operator)
            operands = list(instr.operands)

            # --- состояние графики -------------------------------------
            if operator == "q":
                stack.append(gs.copy())
                continue
            if operator == "Q":
                if stack:
                    gs = stack.pop()
                flush()
                continue
            if operator == "cm" and len(operands) >= 6:
                gs.ctm = mat_mul(tuple(_num(v) for v in operands[:6]), gs.ctm)  # type: ignore[arg-type]
                continue

            # --- цвет заливки ------------------------------------------
            if operator in ("g", "rg", "k", "sc", "scn", "cs"):
                new_color = gs.fill_color
                nums = [_num(v) for v in operands if isinstance(v, (int, float))]
                if operator == "g" and len(nums) == 1:
                    new_color = (nums[0], nums[0], nums[0])
                elif operator == "rg" and len(nums) == 3:
                    new_color = (nums[0], nums[1], nums[2])
                elif operator == "k" and len(nums) == 4:
                    new_color = _cmyk_to_rgb(*nums[:4])
                elif operator in ("sc", "scn"):
                    if len(nums) == 1:
                        new_color = (nums[0], nums[0], nums[0])
                    elif len(nums) == 3:
                        new_color = (nums[0], nums[1], nums[2])
                    elif len(nums) == 4:
                        new_color = _cmyk_to_rgb(*nums[:4])
                    else:
                        new_color = None  # узор или ICC с иным числом компонент
                if new_color != gs.fill_color:
                    flush()
                gs.fill_color = new_color
                continue

            # --- текстовые объекты -------------------------------------
            if operator == "BT":
                in_text = True
                tm = tlm = IDENTITY
                flush()
                prev_baseline = None
                continue
            if operator == "ET":
                in_text = False
                flush()
                prev_baseline = None
                continue

            ts = gs.text
            if operator == "Tf" and len(operands) >= 2:
                ts.font_res = str(operands[0])
                ts.size = _num(operands[1])
                ts.font = ctx.fonts.get(ts.font_res)
                if ts.font is None and ts.font_res:
                    self.warnings.append(
                        f"шрифт {ts.font_res} не найден в ресурсах — текст пропущен"
                    )
                flush()
                continue
            if operator == "Tc" and operands:
                ts.char_spacing = _num(operands[0]); flush(); continue
            if operator == "Tw" and operands:
                ts.word_spacing = _num(operands[0]); flush(); continue
            if operator == "Tz" and operands:
                ts.hscale = _num(operands[0]) / 100.0; flush(); continue
            if operator == "TL" and operands:
                ts.leading = _num(operands[0]); continue
            if operator == "Ts" and operands:
                ts.rise = _num(operands[0]); flush(); continue
            if operator == "Tr" and operands:
                ts.render_mode = int(_num(operands[0])); flush(); continue

            if operator in ("Td", "TD", "Tm", "T*", "BT", "'", '"'):
                # Позиционирование начинает новую строку: следующий оператор
                # показа станет её началом
                line_start_op = None

            if operator == "Td" and len(operands) >= 2:
                tlm = mat_mul((1, 0, 0, 1, _num(operands[0]), _num(operands[1])), tlm)
                tm = tlm
                continue
            if operator == "TD" and len(operands) >= 2:
                ts.leading = -_num(operands[1])
                tlm = mat_mul((1, 0, 0, 1, _num(operands[0]), _num(operands[1])), tlm)
                tm = tlm
                continue
            if operator == "Tm" and len(operands) >= 6:
                tlm = tuple(_num(v) for v in operands[:6])  # type: ignore[assignment]
                tm = tlm
                continue
            if operator == "T*":
                tlm = mat_mul((1, 0, 0, 1, 0, -ts.leading), tlm)
                tm = tlm
                continue

            # --- операторы показа текста -------------------------------
            if operator in ("Tj", "TJ", "'", '"'):
                if line_start_op is None:
                    line_start_op = op_index
                if operator == "'":
                    tlm = mat_mul((1, 0, 0, 1, 0, -ts.leading), tlm)
                    tm = tlm
                elif operator == '"':
                    if len(operands) >= 3:
                        ts.word_spacing = _num(operands[0])
                        ts.char_spacing = _num(operands[1])
                    tlm = mat_mul((1, 0, 0, 1, 0, -ts.leading), tlm)
                    tm = tlm

                if not in_text:
                    # Показ текста вне BT/ET — повреждённый поток; пропускаем
                    continue
                font = ts.font
                if font is None:
                    continue

                # Приводим операнд к единому виду: список элементов массива TJ
                if operator == "TJ":
                    elements = list(operands[0]) if operands and isinstance(
                        operands[0], pikepdf.Array
                    ) else []
                elif operator == "Tj":
                    elements = [operands[0]] if operands else []
                elif operator == "'":
                    elements = [operands[0]] if operands else []
                else:  # '"'
                    elements = [operands[2]] if len(operands) >= 3 else []

                for elem_index, element in enumerate(elements):
                    if isinstance(element, (int, float)):
                        # Кернинг/пробел, заданный числом
                        shift = -_num(element) / 1000.0 * ts.size * ts.hscale
                        if current is not None and ts.size:
                            gap_em = abs(shift) / ts.size
                            if shift > 0 and gap_em >= self.SPACE_GAP:
                                if not current.text.endswith(" "):
                                    current.text += " "
                                    current.char_to_glyph.append(-1)
                        tm = mat_mul((1, 0, 0, 1, shift, 0), tm)
                        continue
                    if not isinstance(element, pikepdf.String):
                        continue

                    raw = bytes(element)
                    byte_pos = 0
                    for code, code_len in font.split_codes(raw):
                        trm = mat_mul(
                            (ts.size * ts.hscale, 0.0, 0.0, ts.size, 0.0, ts.rise),
                            mat_mul(tm, gs.ctm),
                        )
                        width1000 = font.width(code)
                        is_space_code = code_len == 1 and code == 32
                        advance = (
                            width1000 / 1000.0 * ts.size
                            + ts.char_spacing
                            + (ts.word_spacing if is_space_code else 0.0)
                        )
                        text = font.code_to_text(code)

                        origin_x, origin_y = mat_apply(mat_mul(tm, gs.ctm), 0.0, 0.0)
                        baseline = (round(origin_y, 2), round(gs.ctm[3], 4))

                        # Решаем, продолжается ли текущий фрагмент
                        need_new = (
                            current is None
                            or current.font_res != ts.font_res
                            or abs(current.size - ts.size) > 1e-6
                            or current.fill_color != gs.fill_color
                            or prev_baseline != baseline
                        )
                        gap = origin_x - prev_end_x if prev_baseline == baseline else 0.0
                        if not need_new and ts.size:
                            if gap / ts.size > self.BREAK_GAP or gap / ts.size < -0.05:
                                need_new = True
                        if need_new:
                            flush()
                            self._run_counter += 1
                            current = TextRun(
                                run_id=self._run_counter,
                                stream_id=ctx.stream_id,
                                page_index=self.page_index,
                                font_res=ts.font_res or "",
                                font=font,
                                size=ts.size,
                                fill_color=gs.fill_color,
                                char_spacing=ts.char_spacing,
                                word_spacing=ts.word_spacing,
                                hscale=ts.hscale,
                                render_mode=ts.render_mode,
                                line_start_op=(
                                    line_start_op if line_start_op is not None else op_index
                                ),
                            )
                        elif ts.size and gap / ts.size >= self.SPACE_GAP:
                            if not current.text.endswith(" "):
                                current.text += " "
                                current.char_to_glyph.append(-1)

                        glyph = GlyphRef(
                            op_index=op_index,
                            elem_index=elem_index,
                            byte_start=byte_pos,
                            byte_len=code_len,
                            code=code,
                            text=text,
                            width1000=width1000,
                            advance=advance,
                            trm=trm,
                            size=ts.size,
                        )
                        glyph_index = len(current.glyphs)
                        current.glyphs.append(glyph)
                        shown = text if text else "�"
                        current.text += shown
                        current.char_to_glyph += [glyph_index] * len(shown)

                        tm = mat_mul((1, 0, 0, 1, advance * ts.hscale, 0), tm)
                        prev_end_x = mat_apply(mat_mul(tm, gs.ctm), 0.0, 0.0)[0]
                        prev_baseline = baseline
                        byte_pos += code_len
                continue

            # --- вложенные Form XObject --------------------------------
            if operator == "Do" and operands and contexts is not None and pdf is not None:
                name = str(operands[0])
                xobjects = ctx.resources.get("/XObject") if ctx.resources is not None else None
                if xobjects is None or name not in xobjects:
                    continue
                xobj = xobjects[name]
                if str(xobj.get("/Subtype", "")) != "/Form":
                    continue
                key = _object_key(xobj)
                if key in xobject_stack:
                    self.warnings.append(f"циклическая ссылка на XObject {name} — пропущено")
                    continue
                sub_ctx = contexts.get(key)
                if sub_ctx is None:
                    sub_ctx = build_stream_context(pdf, xobj, key, is_page=False,
                                                   parent_resources=ctx.resources)
                    contexts[key] = sub_ctx
                inner_ctm = gs.ctm
                if "/Matrix" in xobj:
                    inner_ctm = mat_mul(
                        tuple(_num(v) for v in list(xobj.Matrix)[:6]), gs.ctm  # type: ignore[arg-type]
                    )
                flush()
                self.parse_stream(sub_ctx, inner_ctm, xobject_stack + (key,), contexts, pdf)
                continue

        flush()


def _object_key(obj: pikepdf.Object) -> str:
    """Устойчивый идентификатор объекта PDF (номер и поколение)."""
    try:
        objgen = obj.objgen
        if objgen != (0, 0):
            return f"obj:{objgen[0]}:{objgen[1]}"
    except Exception:
        pass
    return f"direct:{id(obj)}"


def build_stream_context(
    pdf: pikepdf.Pdf,
    owner: pikepdf.Object,
    stream_id: str,
    is_page: bool,
    parent_resources: pikepdf.Object | None = None,
) -> StreamContext:
    """Разбирает поток содержимого страницы или Form XObject."""
    if is_page:
        instructions = list(pikepdf.parse_content_stream(owner))
        resources = owner.get("/Resources")
    else:
        instructions = list(pikepdf.parse_content_stream(owner))
        resources = owner.get("/Resources", parent_resources)
    if resources is None:
        resources = pikepdf.Dictionary()
    return StreamContext(
        stream_id=stream_id,
        owner=owner,
        instructions=instructions,
        resources=resources,
        fonts=load_page_fonts(resources),
        is_page=is_page,
        original_data=_raw_content(owner, is_page),
        # Список копируется поверхностно: правка заменяет его элементы по
        # индексу, и без копии «исходные» инструкции менялись бы вместе с ними
        original_instructions=list(instructions),
    )


def _raw_content(owner: pikepdf.Object, is_page: bool) -> bytes:
    """Байты потока содержимого до правок — как они лежат в файле.

    Для страницы с несколькими потоками они склеиваются переводом строки:
    так же их склеивает разборщик, и только при точном совпадении склейки
    точечная замена сможет опереться на эти байты. Не совпадёт — замена
    честно откажется работать, и поток соберётся заново.
    """
    try:
        if not is_page:
            return owner.read_bytes()
        contents = owner.get("/Contents")
        if isinstance(contents, pikepdf.Array):
            return b"\n".join(item.read_bytes() for item in contents)
        if isinstance(contents, pikepdf.Stream):
            return contents.read_bytes()
    except Exception:
        return b""
    return b""


def parse_page(
    pdf: pikepdf.Pdf,
    page: pikepdf.Object,
    page_index: int,
    contexts: dict[str, StreamContext] | None = None,
) -> tuple[list[TextRun], dict[str, StreamContext], list[str]]:
    """Возвращает фрагменты текста страницы, её потоки и предупреждения."""
    contexts = contexts if contexts is not None else {}
    page_key = _object_key(page)
    ctx = contexts.get(page_key)
    if ctx is None:
        ctx = build_stream_context(pdf, page, page_key, is_page=True)
        contexts[page_key] = ctx

    parser = ContentParser(page_index, page_index * RUN_ID_STRIDE)
    # Система координат страницы: учитываем /MediaBox со смещённым началом
    base = IDENTITY
    try:
        box = page.get("/MediaBox")
        if box is not None:
            x0, y0 = _num(box[0]), _num(box[1])
            if x0 or y0:
                base = (1.0, 0.0, 0.0, 1.0, -x0, -y0)
    except Exception:
        pass
    parser.parse_stream(ctx, base, (page_key,), contexts, pdf)
    _parse_appearances(pdf, page, parser, contexts)
    return parser.runs, contexts, parser.warnings


def appearance_streams(page: pikepdf.Object):
    """Потоки внешнего вида аннотаций страницы: ``(аннотация, поток)``.

    Текст поля формы живёт не в содержимом страницы, а здесь: содержимое
    страницы поле только «прорезает», а рисует его собственный поток из
    ``/AP /N``. Поэтому правка, не дошедшая до внешнего вида, оставляет в
    заполненной форме прежнее значение на экране — при том, что ``/V`` уже
    новое. Расхождение видимого и хранимого — ровно то, что ищут при разборе.
    """
    annots = page.get("/Annots")
    if annots is None:
        return
    try:
        items = list(annots)
    except Exception:
        return
    for annot in items:
        if not isinstance(annot, pikepdf.Dictionary):
            continue
        appearance = annot.get("/AP")
        if not isinstance(appearance, pikepdf.Dictionary):
            continue
        normal = appearance.get("/N")
        if isinstance(normal, pikepdf.Stream):
            yield annot, normal
        elif isinstance(normal, pikepdf.Dictionary):
            # Несколько состояний (флажок, переключатель): у каждого свой поток
            for _state, stream in normal.items():
                if isinstance(stream, pikepdf.Stream):
                    yield annot, stream


def _parse_appearances(
    pdf: pikepdf.Pdf,
    page: pikepdf.Object,
    parser: "ContentParser",
    contexts: dict[str, StreamContext],
) -> None:
    """Разбирает внешний вид аннотаций как обычные Form XObject."""
    for annot, stream in appearance_streams(page):
        key = _object_key(stream)
        if key in contexts:
            continue
        try:
            ctx = build_stream_context(
                pdf, stream, key, is_page=False,
                parent_resources=page.get("/Resources"),
            )
        except Exception as exc:
            parser.warnings.append(
                f"внешний вид аннотации не разобран: {exc}"
            )
            continue
        contexts[key] = ctx
        # Внешний вид рисуется в своей системе координат: /BBox, приведённый
        # матрицей /Matrix, растягивается на прямоугольник /Rect аннотации
        try:
            parser.parse_stream(ctx, _appearance_ctm(annot, stream), (key,), contexts, pdf)
        except Exception as exc:
            parser.warnings.append(f"внешний вид аннотации не разобран: {exc}")


def _appearance_ctm(annot: pikepdf.Object, stream: pikepdf.Object) -> tuple:
    """Преобразование из координат потока внешнего вида в координаты страницы."""
    try:
        rect = [_num(v) for v in list(annot["/Rect"])[:4]]
    except Exception:
        return IDENTITY
    x0, y0, x1, y1 = min(rect[0], rect[2]), min(rect[1], rect[3]), \
        max(rect[0], rect[2]), max(rect[1], rect[3])

    matrix = IDENTITY
    try:
        if "/Matrix" in stream:
            matrix = tuple(_num(v) for v in list(stream["/Matrix"])[:6])
    except Exception:
        matrix = IDENTITY
    try:
        box = [_num(v) for v in list(stream["/BBox"])[:4]]
    except Exception:
        return (1.0, 0.0, 0.0, 1.0, x0, y0)

    # Углы /BBox после /Matrix задают прямоугольник, который и вписывается в /Rect
    corners = [
        mat_apply(matrix, box[0], box[1]), mat_apply(matrix, box[2], box[1]),
        mat_apply(matrix, box[2], box[3]), mat_apply(matrix, box[0], box[3]),
    ]
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    scale_x = (x1 - x0) / width if width else 1.0
    scale_y = (y1 - y0) / height if height else 1.0
    fit = (scale_x, 0.0, 0.0, scale_y,
           x0 - min(xs) * scale_x, y0 - min(ys) * scale_y)
    return mat_mul(matrix, fit)
