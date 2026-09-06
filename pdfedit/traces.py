"""Поиск следов правки: чем правленый файл выдаёт себя при разборе.

Этот модуль отвечает на вопрос, которого не задают ни :func:`check_file`
(«файл цел?»), ни :func:`external_risks` («пройдёт ли veraPDF?»), ни
:func:`compare_files` («что изменилось по существу?»). Вопрос здесь другой:
**что в файле выглядит не так, как выглядело бы, если бы его не правили.**

Разница принципиальная. Документ может быть безупречно целым, проходить любые
внешние проверки и содержать ровно те изменения, которые задумывались, — и всё
равно нести на себе десяток мелких признаков чужой руки:

* шрифт «изменён» сегодня, хотя создан в 2002 году;
* один поток сжат уровнем 9, а все остальные — шестым;
* в одной строке текст записан в скобках, во всём остальном файле —
  шестнадцатерично;
* за сжатыми данными потока лежат неиспользуемые байты;
* ``/ActualText`` рядом со строкой не совпадает с самой строкой;
* в шрифте есть глиф, на который не ссылается ни один код.

Ни одно из этих наблюдений не мешает документу работать. Все они мешают ему
выглядеть нетронутым — а для архивов, юридически значимых документов и
исследования целостности важно именно это.

Проверки делятся на два вида. Одни видны по самому файлу
(:func:`self_traces`): хвосты в потоках, глифы-сироты, расхождение ширин,
разнобой в записи. Другие требуют оригинала (:func:`compare_traces`): смена
``/ID``, ``/Producer``, дат, уровня сжатия, порядка таблиц в шрифте, стиля
таблицы ссылок, нумерации объектов.
"""

from __future__ import annotations

import io
import re
import zlib
from dataclasses import dataclass, field

import pikepdf

from . import style as style_mod

#: Насколько подробным делать перечисление однотипных находок
MAX_LISTED = 12

#: Порог, ниже которого расхождение ширин считается округлением, а не ошибкой.
#: Ширины в PDF пишут с двумя знаками, в hmtx они целые в единицах шрифта —
#: расхождение до половины последнего знака неизбежно и следом не является
WIDTH_TOLERANCE = 1.0


@dataclass
class Trace:
    """Одна находка: чем именно файл выдаёт правку."""

    key: str            # короткое имя признака, по нему находки группируются
    text: str           # объяснение для человека
    where: str = ""     # объект или место в файле
    severity: str = "след"   # «след» | «расхождение» | «замечание»

    def line(self) -> str:
        place = f" [{self.where}]" if self.where else ""
        return f"{self.text}{place}"


@dataclass
class TraceReport:
    """Итог поиска следов."""

    traces: list[Trace] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    def add(self, key: str, text: str, where: str = "", severity: str = "след") -> None:
        self.traces.append(Trace(key=key, text=text, where=where, severity=severity))

    @property
    def clean(self) -> bool:
        return not self.traces

    def by_key(self) -> dict[str, list[Trace]]:
        groups: dict[str, list[Trace]] = {}
        for trace in self.traces:
            groups.setdefault(trace.key, []).append(trace)
        return groups

    def describe(self) -> str:
        lines: list[str] = []
        if self.clean:
            lines.append("Следов правки не найдено. Проверено:")
            for item in self.checked:
                lines.append(f"  OK {item}")
            return "\n".join(lines)

        lines.append("Признаки правки:")
        for key, group in self.by_key().items():
            head = group[0]
            lines.append(f"  !! {head.text}" + (f" [{head.where}]" if head.where else ""))
            for trace in group[1:MAX_LISTED]:
                lines.append(f"       {trace.line()}")
            if len(group) > MAX_LISTED:
                lines.append(f"       … и ещё {len(group) - MAX_LISTED}")
        done = [item for item in self.checked]
        if done:
            lines.append("")
            lines.append("Прошло проверку:")
            for item in done:
                lines.append(f"  OK {item}")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# Разбор файла на уровне байтов
# ----------------------------------------------------------------------

_OBJ_HEADER = re.compile(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b")
_STREAM_START = re.compile(rb"stream(\r\n|\n|\r)")


def stream_spans(data: bytes) -> dict[tuple[int, int], tuple[int, int]]:
    """Границы данных каждого потока в файле: ``(номер, поколение) → (начало, длина)``.

    Считаются по байтам файла, а не через библиотеку: нас интересует именно
    то, что лежит в файле, включая неиспользуемый хвост за концом сжатых
    данных, — а библиотека такой хвост как раз и скрывает.
    """
    spans: dict[tuple[int, int], tuple[int, int]] = {}
    for match in _OBJ_HEADER.finditer(data):
        objgen = (int(match.group(1)), int(match.group(2)))
        start = match.end()
        end_obj = data.find(b"endobj", start)
        limit = end_obj if end_obj != -1 else len(data)
        stream_at = _STREAM_START.search(data, start, limit)
        if stream_at is None:
            continue
        body = stream_at.end()
        end_stream = data.find(b"endstream", body)
        if end_stream == -1:
            continue
        length = end_stream - body
        # Перевод строки перед endstream в данные не входит
        while length > 0 and data[body + length - 1] in b"\r\n":
            length -= 1
        spans[objgen] = (body, length)
    return spans


def flate_tail(payload: bytes) -> int:
    """Сколько байт в конце данных потока не участвуют в распаковке.

    Ровно те байты, что остаются за концом сжатого потока. Декодер их не
    читает, и на содержимое они не влияют, — но лежат в файле и означают,
    что поток переписали, а разницу в длине добили заполнителем.
    """
    try:
        unpacker = zlib.decompressobj()
        unpacker.decompress(payload)
        unpacker.flush()
        return len(unpacker.unused_data)
    except zlib.error:
        return 0


def zlib_header(payload: bytes) -> bytes:
    """Двухбайтовый заголовок zlib, если данные им и начинаются."""
    if len(payload) < 2:
        return b""
    if payload[0] & 0x0F != 8:          # метод сжатия обязан быть deflate
        return b""
    if (payload[0] << 8 | payload[1]) % 31:  # контрольная сумма заголовка
        return b""
    return bytes(payload[:2])


def compression_level_hint(header: bytes) -> str:
    """Как назывался бы уровень сжатия по заголовку (биты FLEVEL)."""
    if len(header) < 2:
        return "?"
    return {0: "самый быстрый (1)", 1: "быстрый (2–5)",
            2: "обычный (6)", 3: "сильный (7–9)"}.get((header[1] >> 6) & 0x03, "?")


# ----------------------------------------------------------------------
# Следы, видные по самому файлу
# ----------------------------------------------------------------------

def self_traces(path: str, password: str = "") -> TraceReport:
    """Признаки правки, для которых оригинал не нужен.

    Проверки независимы и выполняются каждая под своей защитой: документы в
    природе разнообразнее любых предположений о них, и шрифт без таблицы
    соответствия кодов или потерянная страница не должны отменять весь
    остальной разбор. О сорвавшейся проверке отчёт говорит прямо — молчание
    приняли бы за «следов нет».
    """
    report = TraceReport()
    with open(path, "rb") as handle:
        data = handle.read()

    _guarded(report, "неиспользуемые байты в потоках", _check_tails, data, report)
    with pikepdf.open(io.BytesIO(data), password=password) as pdf:
        _guarded(report, "ширины шрифтов", _check_font_metrics, pdf, report)
        _guarded(report, "глифы-сироты", _check_orphan_glyphs, pdf, report)
        _guarded(report, "скрытые копии текста", _check_hidden_text, pdf, report)
        _guarded(report, "манера записи", _check_serialization_style, pdf, report)
        _guarded(report, "число редакций", _check_revisions, data, report)
    return report


def _guarded(report: TraceReport, what: str, check, *args) -> None:
    """Выполняет проверку, не давая ей уронить остальные."""
    try:
        check(*args)
    except Exception as exc:
        report.add(
            "проверка-сорвалась",
            f"проверку «{what}» выполнить не удалось: {type(exc).__name__}: {exc}",
            severity="замечание",
        )


def _check_tails(data: bytes, report: TraceReport) -> None:
    """Неиспользуемые байты за концом сжатых данных потоков."""
    total = 0
    for objgen, (start, length) in sorted(stream_spans(data).items()):
        payload = data[start : start + length]
        if not zlib_header(payload):
            continue
        tail = flate_tail(payload)
        if tail:
            total += 1
            report.add(
                "хвост-в-потоке",
                f"за сжатыми данными потока лежит {tail} неиспользуемых байт — "
                f"признак правки на месте с добивкой длины",
                where=f"{objgen[0]} {objgen[1]} R",
            )
    if not total:
        report.checked.append("в потоках нет неиспользуемых байт за сжатыми данными")


def _check_revisions(data: bytes, report: TraceReport) -> None:
    revisions = data.count(b"%%EOF")
    if revisions > 1:
        report.add(
            "редакции",
            f"в файле {revisions} редакции: поверх исходной дописан слой правок",
            severity="замечание",
        )
    else:
        report.checked.append("файл состоит из одной редакции")


def font_programs(pdf: pikepdf.Pdf):
    """Все внедрённые программы шрифтов: ``(имя, словарь шрифта, байты)``."""
    seen: set[tuple[int, int]] = set()
    for objgen, obj in _all_objects(pdf).items():
        if not isinstance(obj, pikepdf.Dictionary):
            continue
        if str(obj.get("/Type", "")) != "/FontDescriptor":
            continue
        for key in ("/FontFile2", "/FontFile3", "/FontFile"):
            stream = obj.get(key)
            if not isinstance(stream, pikepdf.Stream):
                continue
            mark = stream.objgen
            if mark in seen:
                continue
            seen.add(mark)
            try:
                yield str(obj.get("/FontName", "?")), obj, stream.read_bytes()
            except Exception:
                continue
            break
        _ = objgen


def _all_objects(pdf: pikepdf.Pdf) -> dict[tuple[int, int], pikepdf.Object]:
    found: dict[tuple[int, int], pikepdf.Object] = {}
    stack = [pdf.trailer]
    seen: set[tuple[int, int]] = set()
    while stack:
        obj = stack.pop()
        try:
            objgen = obj.objgen
        except Exception:
            objgen = (0, 0)
        if objgen != (0, 0):
            if objgen in seen:
                continue
            seen.add(objgen)
            found[objgen] = obj
        try:
            if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
                stack.extend(obj[key] for key in obj.keys())
            elif isinstance(obj, pikepdf.Array):
                stack.extend(list(obj))
        except Exception:
            continue
    return found


def _open_program(program: bytes):
    from fontTools.ttLib import TTFont

    return TTFont(io.BytesIO(program), lazy=True, fontNumber=0,
                  recalcTimestamp=False, recalcBBoxes=False)


def _check_font_metrics(pdf: pikepdf.Pdf, report: TraceReport) -> None:
    """Совпадают ли ширины в ``/W`` (``/Widths``) с таблицей ``hmtx`` шрифта."""
    checked = 0
    for objgen, obj in _all_objects(pdf).items():
        if not isinstance(obj, pikepdf.Dictionary):
            continue
        if str(obj.get("/Type", "")) != "/Font":
            continue
        subtype = str(obj.get("/Subtype", ""))
        widths = _pdf_widths(obj)
        if not widths:
            continue
        program = _program_of(obj)
        if program is None:
            continue
        try:
            font = _open_program(program)
            upem = font["head"].unitsPerEm
            order = font.getGlyphOrder()
            hmtx = font["hmtx"]
            glyf = font["glyf"] if "glyf" in font else None
        except Exception:
            continue
        checked += 1
        mismatches: list[str] = []
        for code, width in sorted(widths.items()):
            gid = _gid_for(obj, subtype, code, font)
            if gid is None or gid >= len(order):
                continue
            try:
                advance = hmtx[order[gid]][0]
            except Exception:
                continue
            if advance == 0 and _is_gutted(glyf, order, gid):
                # Выпотрошенный глиф: программы, делающие подмножества (в том
                # числе PyMuPDF), выбрасывают контуры и обнуляют ширину, а
                # записи в /W и /ToUnicode оставляют на месте. Такой код
                # ничего не рисует и расхождением не является — это обычный
                # вид подмножества, а не след правки
                continue
            from_font = advance * 1000.0 / upem if upem else 0.0
            if abs(from_font - width) > WIDTH_TOLERANCE:
                mismatches.append(
                    f"код {code}: в словаре {width:g}, в шрифте {from_font:.2f}"
                )
        if mismatches:
            report.add(
                "ширины-расходятся",
                f"ширины в словаре шрифта не совпадают с таблицей hmtx "
                f"({len(mismatches)} шт.): {'; '.join(mismatches[:3])}",
                where=f"{objgen[0]} {objgen[1]} R {obj.get('/BaseFont', '')}",
            )
        try:
            font.close()
        except Exception:
            pass
    if checked and not any(t.key == "ширины-расходятся" for t in report.traces):
        report.checked.append(
            f"ширины в словаре и в hmtx совпадают (проверено шрифтов: {checked})"
        )


def _is_gutted(glyf, order, gid: int) -> bool:
    """Глиф без контуров — выброшенная подмножеством заготовка."""
    if glyf is None:
        return True
    try:
        return getattr(glyf[order[gid]], "numberOfContours", 0) == 0
    except Exception:
        return True


def _pdf_widths(font: pikepdf.Dictionary) -> dict[int, float]:
    """Ширины из словаря шрифта: ``/W`` составного или ``/Widths`` простого."""
    result: dict[int, float] = {}
    descendants = font.get("/DescendantFonts")
    if descendants is not None and len(descendants) > 0:
        warr = descendants[0].get("/W")
        if warr is None:
            return result
        items = list(warr)
        index = 0
        while index < len(items):
            try:
                first = int(items[index])
            except Exception:
                break
            if index + 1 >= len(items):
                break
            follow = items[index + 1]
            if isinstance(follow, pikepdf.Array):
                for offset, value in enumerate(follow):
                    result[first + offset] = float(value)
                index += 2
            else:
                if index + 2 >= len(items):
                    break
                last, value = int(follow), float(items[index + 2])
                if last - first <= 65535:
                    for cid in range(first, last + 1):
                        result[cid] = value
                index += 3
        return result

    widths = font.get("/Widths")
    if widths is None:
        return result
    try:
        first = int(font.get("/FirstChar", 0))
    except Exception:
        first = 0
    for offset, value in enumerate(widths):
        try:
            result[first + offset] = float(value)
        except Exception:
            continue
    return result


def _program_of(font: pikepdf.Dictionary) -> bytes | None:
    target = font
    descendants = font.get("/DescendantFonts")
    if descendants is not None and len(descendants) > 0:
        target = descendants[0]
    descriptor = target.get("/FontDescriptor")
    if not isinstance(descriptor, pikepdf.Dictionary):
        return None
    for key in ("/FontFile2", "/FontFile3", "/FontFile"):
        stream = descriptor.get(key)
        if isinstance(stream, pikepdf.Stream):
            try:
                return stream.read_bytes()
            except Exception:
                return None
    return None


def _gid_for(font: pikepdf.Dictionary, subtype: str, code: int, program) -> int | None:
    """Номер глифа для кода: у составных шрифтов это CID, у простых — через cmap."""
    if subtype == "/Type0":
        descendants = font.get("/DescendantFonts")
        if descendants is None or len(descendants) == 0:
            return None
        mapping = descendants[0].get("/CIDToGIDMap")
        if mapping is None or str(mapping) == "/Identity":
            return code
        return None  # явная таблица: без её разбора судить о ширинах нельзя
    # Простой шрифт: ширина относится к коду, а глиф ищется через cmap шрифта
    try:
        table = program.getBestCmap()
    except Exception:
        return None
    if not table:
        return None
    name = table.get(code) or table.get(0xF000 + code)
    if name is None:
        return None
    try:
        return program.getGlyphOrder().index(name)
    except ValueError:
        return None


def _check_orphan_glyphs(pdf: pikepdf.Pdf, report: TraceReport) -> None:
    """Глифы, на которые не ссылается ни один код документа.

    Такой глиф ничего не рисует, но лежит в файле и прямо показывает, что
    шрифт расширяли: видно даже, какие именно буквы для этого понадобились.
    Проверяются только шрифты-подмножества — у полного шрифта неиспользуемые
    глифы это норма, там их тысячи.
    """
    checked = 0
    for objgen, obj in _all_objects(pdf).items():
        if not isinstance(obj, pikepdf.Dictionary):
            continue
        if str(obj.get("/Type", "")) != "/Font":
            continue
        # Составной шрифт описан ДВУМЯ словарями: /Type0 и его потомком
        # /CIDFontType2. Оба помечены /Type /Font, но ширины и соответствие
        # кодов глифам есть только у родителя. Потомка, взятого отдельно,
        # пришлось бы разбирать как простой шрифт — и все его глифы оказались
        # бы сиротами
        if str(obj.get("/Subtype", "")) in ("/CIDFontType0", "/CIDFontType2"):
            continue
        base = str(obj.get("/BaseFont", ""))
        if not _is_subset_name(base):
            continue
        program = _program_of(obj)
        if program is None:
            continue
        try:
            font = _open_program(program)
            total = font["maxp"].numGlyphs
            glyf = font["glyf"] if "glyf" in font else None
            order = font.getGlyphOrder()
        except Exception:
            continue
        checked += 1
        reachable = _reachable_gids(obj, font, total)
        # Составной глиф (буква из основы и знака) ссылается на другие глифы.
        # Такая составляющая не сопоставлена ни одному коду и сиротой выглядит,
        # хотя используется — через ссылку
        reachable |= _components_of(glyf, order, reachable)
        orphans = []
        for gid in range(total):
            if gid in reachable or gid == 0:
                continue
            if glyf is None:
                continue
            try:
                glyph = glyf[order[gid]]
                # Пустой глиф сиротой не считается: подмножества оставляют
                # выпотрошенные заготовки на месте выброшенных, это норма
                if getattr(glyph, "numberOfContours", 0) == 0:
                    continue
            except Exception:
                continue
            orphans.append(gid)
        if orphans:
            report.add(
                "глиф-сирота",
                f"в шрифте-подмножестве {len(orphans)} непустых глифов, на которые "
                f"не ссылается ни один код: {orphans[:8]}",
                where=f"{objgen[0]} {objgen[1]} R {base}",
            )
        try:
            font.close()
        except Exception:
            pass
    if checked and not any(t.key == "глиф-сирота" for t in report.traces):
        report.checked.append(
            f"в шрифтах-подмножествах нет глифов-сирот (проверено: {checked})"
        )


def _components_of(glyf, order: list[str], roots: set[int]) -> set[int]:
    """Глифы, на которые ссылаются составные глифы из ``roots`` (по цепочке)."""
    if glyf is None:
        return set()
    index = {name: gid for gid, name in enumerate(order)}
    found: set[int] = set()
    queue = list(roots)
    while queue:
        gid = queue.pop()
        if gid >= len(order):
            continue
        try:
            glyph = glyf[order[gid]]
            if not glyph.isComposite():
                continue
            components = list(glyph.components)
        except Exception:
            continue
        for component in components:
            child = index.get(getattr(component, "glyphName", None))
            if child is not None and child not in found and child not in roots:
                found.add(child)
                queue.append(child)
    return found


def _is_subset_name(base: str) -> bool:
    """``ABCDEF+Arial`` — подмножество; ``Arial`` — шрифт целиком."""
    name = base.lstrip("/")
    return len(name) > 7 and name[6] == "+" and name[:6].isupper()


def _reachable_gids(font: pikepdf.Dictionary, program, total: int) -> set[int]:
    """Номера глифов, достижимые по таблицам PDF-словаря шрифта."""
    reachable: set[int] = set()
    subtype = str(font.get("/Subtype", ""))
    if subtype == "/Type0":
        descendants = font.get("/DescendantFonts")
        mapping = None
        if descendants is not None and len(descendants) > 0:
            mapping = descendants[0].get("/CIDToGIDMap")
        if mapping is None or str(mapping) == "/Identity":
            # CID равен номеру глифа: достижимо всё, что перечислено в /W и
            # в /ToUnicode — то есть всё, чем документ пользуется
            reachable |= set(_pdf_widths(font))
            reachable |= _tounicode_codes(font)
        elif isinstance(mapping, pikepdf.Stream):
            try:
                raw = mapping.read_bytes()
                for index in range(0, len(raw) - 1, 2):
                    reachable.add(raw[index] << 8 | raw[index + 1])
            except Exception:
                pass
        return reachable

    try:
        table = program.getBestCmap()
        order = program.getGlyphOrder()
        index = {name: gid for gid, name in enumerate(order)}
    except Exception:
        return set(range(total))
    if not table:
        # Подмножества, извлечённые из PDF, часто идут вовсе без юникодной
        # cmap: соответствие кодов глифам живёт тогда в /Differences словаря.
        # Судить о достижимости глифов по самому шрифту тут нельзя
        return set(range(total))
    for name in table.values():
        gid = index.get(name)
        if gid is not None:
            reachable.add(gid)
    return reachable


def _tounicode_codes(font: pikepdf.Dictionary) -> set[int]:
    stream = font.get("/ToUnicode")
    if not isinstance(stream, pikepdf.Stream):
        return set()
    try:
        from .cmap import parse_tounicode

        return set(parse_tounicode(stream.read_bytes()).mapping)
    except Exception:
        return set()


def _check_hidden_text(pdf: pikepdf.Pdf, report: TraceReport) -> None:
    """Расхождения между видимым текстом страницы и его скрытыми копиями.

    Копия текста для экранных дикторов (``/ActualText``) должна говорить то
    же, что нарисовано на странице. Если правка дошла до содержимого, но не
    до копии, то в файле остаётся прежняя формулировка — и программы,
    предпочитающие копию, показывают именно её.
    """
    mismatches = 0
    for index, page in enumerate(pdf.pages):
        try:
            drawn = _page_text(pdf, page, index)
        except Exception:
            continue
        if drawn is None:
            continue
        for actual in _inline_actual_text(page):
            if actual and actual.strip() and actual.strip() not in drawn:
                mismatches += 1
                report.add(
                    "скрытая-копия",
                    f"/ActualText не совпадает с текстом страницы: {actual[:60]!r} "
                    f"на странице нет",
                    where=f"страница {index + 1}",
                )
    if not mismatches:
        report.checked.append("скрытые копии текста совпадают с содержимым страниц")


def _page_text(pdf: pikepdf.Pdf, page, index: int = 0) -> str | None:
    """Текст, действительно нарисованный на странице.

    Именно нарисованный, а не извлечённый готовой библиотекой: PyMuPDF при
    извлечении предпочитает ``/ActualText`` содержимому страницы, и проверка
    на нём сравнивала бы копию сама с собой. Поэтому строки раскодируются
    через шрифт (:mod:`pdfedit.content`) — так же, как это делает редактор.
    """
    try:
        from .content import parse_page

        runs, _contexts, _warnings = parse_page(pdf, page.obj, index)
        return "".join(run.text for run in runs)
    except Exception:
        return None


def _inline_actual_text(page) -> list[str]:
    """Значения ``/ActualText`` из разметки внутри потока страницы."""
    found: list[str] = []
    try:
        for instruction in pikepdf.parse_content_stream(page):
            if str(instruction.operator) not in ("BDC", "DP"):
                continue
            operands = list(instruction.operands)
            if len(operands) < 2 or not isinstance(operands[1], pikepdf.Dictionary):
                continue
            value = operands[1].get("/ActualText")
            if isinstance(value, pikepdf.String):
                found.append(str(value))
    except Exception:
        return found
    return found


def _check_serialization_style(pdf: pikepdf.Pdf, report: TraceReport) -> None:
    """Разнобой в записи внутри одного потока.

    Генератор пишет весь поток одной рукой: либо везде шестнадцатеричные
    строки, либо везде круглые скобки. Одна строка, записанная иначе, чем все
    соседние, — самый заметный признак того, что её вписали позже.
    """
    for index, page in enumerate(pdf.pages):
        contents = page.obj.get("/Contents")
        streams = []
        if isinstance(contents, pikepdf.Stream):
            streams = [contents]
        elif isinstance(contents, pikepdf.Array):
            streams = [s for s in contents if isinstance(s, pikepdf.Stream)]
        for stream in streams:
            try:
                data = stream.read_bytes()
            except Exception:
                continue
            odd = _odd_strings(data)
            if odd:
                report.add(
                    "разнобой-в-записи",
                    f"строк, записанных не так, как все остальные в потоке: "
                    f"{len(odd)} из {odd[-1]} (перемешаны скобки и шестнадцатеричная запись)",
                    where=f"страница {index + 1}",
                )
    if not any(t.key == "разнобой-в-записи" for t in report.traces):
        report.checked.append("строки во всех потоках записаны единообразно")


def _odd_strings(data: bytes) -> list[int]:
    """Индексы строк, записанных не в манере большинства строк потока."""
    marks = style_mod.tokens(data)
    kinds = [kind for kind, _s, _e in marks if kind in ("hex", "string")]
    if len(kinds) < 4:
        return []
    hexed = kinds.count("hex")
    literal = kinds.count("string")
    if not hexed or not literal:
        return []
    minority = "hex" if hexed < literal else "string"
    return [index for index, kind in enumerate(kinds) if kind == minority] + [len(kinds)]


# ----------------------------------------------------------------------
# Следы, видные только при сверке с оригиналом
# ----------------------------------------------------------------------

def compare_traces(original: str, result: str, password: str = "") -> TraceReport:
    """Признаки правки, заметные при сравнении с исходным файлом."""
    report = TraceReport()
    with open(original, "rb") as handle:
        before_data = handle.read()
    with open(result, "rb") as handle:
        after_data = handle.read()

    with pikepdf.open(io.BytesIO(before_data), password=password) as before, \
            pikepdf.open(io.BytesIO(after_data), password=password) as after:
        _guarded(report, "идентификатор и производитель", _compare_identity,
                 before, after, report)
        _guarded(report, "даты", _compare_dates, before, after, report)
        _guarded(report, "нумерация объектов", _compare_numbering, before, after, report)
        _guarded(report, "шрифты", _compare_fonts, before, after, report)
    _guarded(report, "уровень сжатия", _compare_compression,
             before_data, after_data, report)
    _guarded(report, "оформление контейнера", _compare_container,
             before_data, after_data, report)
    return report


def _compare_identity(before: pikepdf.Pdf, after: pikepdf.Pdf, report: TraceReport) -> None:
    """``/ID``, ``/Producer``, ``/Creator`` и XMP — то, по чему документ узнают."""
    before_id = _trailer_id(before)
    after_id = _trailer_id(after)
    if before_id != after_id:
        report.add(
            "id-изменён",
            f"идентификатор /ID изменился: {before_id} → {after_id}",
            severity="след",
        )
    else:
        report.checked.append("идентификатор /ID тот же")

    for key in ("/Producer", "/Creator"):
        was = str(before.docinfo.get(key, "")) if before.docinfo is not None else ""
        now = str(after.docinfo.get(key, "")) if after.docinfo is not None else ""
        if was != now:
            report.add(
                "производитель-изменён",
                f"{key} изменился: {was!r} → {now!r} — в файле осталось имя "
                f"программы, которая его правила",
            )
    if not any(t.key == "производитель-изменён" for t in report.traces):
        report.checked.append("/Producer и /Creator не тронуты")

    was_xmp = _xmp(before)
    now_xmp = _xmp(after)
    if was_xmp != now_xmp:
        report.add("xmp-изменён", "XMP-метаданные изменились")
    else:
        report.checked.append("XMP-метаданные не тронуты")


def _trailer_id(pdf: pikepdf.Pdf) -> str:
    try:
        value = pdf.trailer.get("/ID")
        if value is None:
            return "(нет)"
        return "".join(bytes(item).hex() for item in value)
    except Exception:
        return "(нет)"


def _xmp(pdf: pikepdf.Pdf) -> bytes:
    stream = pdf.Root.get("/Metadata")
    if not isinstance(stream, pikepdf.Stream):
        return b""
    try:
        return stream.read_bytes()
    except Exception:
        return b""


def _compare_dates(before: pikepdf.Pdf, after: pikepdf.Pdf, report: TraceReport) -> None:
    """``/CreationDate`` и ``/ModDate``: обе даты обязаны пережить правку."""
    changed = False
    for key in ("/CreationDate", "/ModDate"):
        was = str(before.docinfo.get(key, "")) if before.docinfo is not None else ""
        now = str(after.docinfo.get(key, "")) if after.docinfo is not None else ""
        if was != now:
            changed = True
            report.add(
                "дата-изменена",
                f"{key} изменилась: {was!r} → {now!r}",
            )
    if not changed:
        report.checked.append("/CreationDate и /ModDate не тронуты")


def _compare_numbering(before: pikepdf.Pdf, after: pikepdf.Pdf, report: TraceReport) -> None:
    """Нумерация объектов: номера не должны разъезжаться, а объекты — плодиться."""
    was = set(_all_objects(before))
    now = set(_all_objects(after))
    added = now - was
    lost = was - now
    if added:
        report.add(
            "новые-объекты",
            f"в документе появилось {len(added)} новых объектов: "
            f"{sorted(added)[:6]}",
        )
    if lost:
        report.add(
            "объекты-исчезли",
            f"из документа исчезло {len(lost)} объектов: {sorted(lost)[:6]}",
        )
    if not added and not lost:
        report.checked.append(f"нумерация объектов сохранена ({len(was)} шт.)")


def _compare_fonts(before: pikepdf.Pdf, after: pikepdf.Pdf, report: TraceReport) -> None:
    """Даты внутри шрифтов и физический порядок таблиц."""
    was = {name: program for name, _d, program in font_programs(before)}
    now = {name: program for name, _d, program in font_programs(after)}
    timestamps_ok = orders_ok = True

    for name, program in now.items():
        original = was.get(name)
        if original is None or original == program:
            continue
        try:
            before_font = _open_program(original)
            after_font = _open_program(program)
        except Exception:
            continue
        try:
            if before_font["head"].modified != after_font["head"].modified:
                timestamps_ok = False
                report.add(
                    "дата-шрифта",
                    f"в шрифте изменилась дата head.modified — шрифт пересобран",
                    where=name,
                )
            from .fontops import physical_table_order

            before_order = physical_table_order(original)
            after_order = physical_table_order(program)
            common_before = [t for t in before_order if t in set(after_order)]
            common_after = [t for t in after_order if t in set(before_order)]
            if common_before != common_after:
                orders_ok = False
                report.add(
                    "порядок-таблиц",
                    f"порядок таблиц в шрифте изменился: было "
                    f"{' '.join(common_before[:6])}…, стало {' '.join(common_after[:6])}…",
                    where=name,
                )
            for tag in ("fpgm", "prep", "cvt "):
                try:
                    if (tag in before_font) != (tag in after_font):
                        report.add("хинтинг", f"таблица хинтинга {tag!r} появилась или "
                                   f"исчезла", where=name)
                    elif tag in before_font and \
                            before_font.reader[tag] != after_font.reader[tag]:
                        report.add("хинтинг", f"таблица хинтинга {tag!r} изменилась",
                                   where=name)
                except Exception:
                    continue
        finally:
            for handle in (before_font, after_font):
                try:
                    handle.close()
                except Exception:
                    pass

    if timestamps_ok:
        report.checked.append("даты внутри шрифтов (head.modified) не тронуты")
    if orders_ok:
        report.checked.append("порядок таблиц в шрифтах сохранён")


def _compare_compression(before: bytes, after: bytes, report: TraceReport) -> None:
    """Уровень сжатия каждого потока — по заголовку zlib."""
    before_spans = stream_spans(before)
    after_spans = stream_spans(after)
    changed = 0
    for objgen, (start, length) in sorted(after_spans.items()):
        source = before_spans.get(objgen)
        if source is None:
            continue
        was = zlib_header(before[source[0] : source[0] + source[1]])
        now = zlib_header(after[start : start + length])
        if not was or not now or was == now:
            continue
        changed += 1
        report.add(
            "уровень-сжатия",
            f"поток пересжат другим уровнем: было {was.hex()} "
            f"({compression_level_hint(was)}), стало {now.hex()} "
            f"({compression_level_hint(now)})",
            where=f"{objgen[0]} {objgen[1]} R",
        )
    if not changed:
        report.checked.append("уровень сжатия потоков не изменился")


def _compare_container(before: bytes, after: bytes, report: TraceReport) -> None:
    """Оформление контейнера: длина файла, версия, вид таблицы ссылок."""
    if len(before) != len(after):
        report.add(
            "длина-файла",
            f"длина файла изменилась: {len(before)} → {len(after)} байт "
            f"({len(after) - len(before):+d})",
            severity="замечание",
        )
    else:
        report.checked.append("длина файла не изменилась")

    was_xref = _xref_kind(before)
    now_xref = _xref_kind(after)
    if was_xref != now_xref:
        report.add(
            "стиль-xref",
            f"вид таблицы ссылок изменился: {was_xref} → {now_xref}",
        )
    else:
        report.checked.append(f"вид таблицы ссылок прежний ({now_xref})")

    if before[:9] != after[:9]:
        report.add(
            "версия",
            f"заголовок файла изменился: {before[:8]!r} → {after[:8]!r}",
        )


def _xref_kind(data: bytes) -> str:
    """Таблица ссылок старого образца или поток ссылок."""
    tail = data[-2048:]
    if b"\nxref" in tail or tail.startswith(b"xref"):
        return "таблица xref"
    if b"/XRef" in data[-4096:]:
        return "поток xref"
    return "таблица xref" if b"\nxref" in data else "поток xref"
