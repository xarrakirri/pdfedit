"""Точечная замена инструкций внутри потока содержимого.

Обычный путь записи потока — собрать его заново из разобранных инструкций
(``pikepdf.unparse_content_stream``). Результат правильный, но переписан целиком:
пробелы, переводы строк и запись чисел получаются такими, как их печатает
библиотека, а не такими, как их напечатала программа, создавшая документ.
На реальных файлах это стабильно даёт +2…4 % длины даже там, где текст не
менялся вовсе.

Здесь поток правится иначе: в исходных байтах находятся границы каждой
инструкции, изменённые участки заменяются, а всё остальное остаётся ровно тем,
чем было. Тогда после сжатия поток почти той же длины, что и раньше, — и
помещается на своё место в файле (:mod:`pdfedit.inplace`), а различие между
старым и новым файлом сводится к самому изменённому тексту.

Границы инструкций ищет разборщик в :func:`instruction_spans`. Он обязан
считать инструкции ровно так же, как их считает qpdf, иначе замена попадёт не
туда — поэтому результат каждой правки проверяется обратным разбором, и при
малейшем расхождении патч отбрасывается, а поток пересобирается обычным путём.
"""

from __future__ import annotations

import difflib
from collections import Counter

import pikepdf

from . import style as style_mod

#: Пробельные байты PDF (ISO 32000-1, таблица 1)
WHITESPACE = b"\x00\t\n\x0c\r "
#: Байты, начинающие или заканчивающие самостоятельный объект
DELIMITERS = b"()<>[]{}/%"


def _skip_string(data: bytes, position: int) -> int:
    """Конец литеральной строки ``(…)`` с учётом вложенных скобок и экранов."""
    depth = 0
    while position < len(data):
        byte = data[position]
        if byte == 0x5C:  # обратная косая черта экранирует следующий байт
            position += 2
            continue
        if byte == 0x28:  # (
            depth += 1
        elif byte == 0x29:  # )
            depth -= 1
            if depth == 0:
                return position + 1
        position += 1
    return len(data)


def _skip_inline_image(data: bytes, position: int) -> int:
    """Конец встроенного изображения: данные между ``ID`` и ``EI`` не токенизируются."""
    marker = data.find(b"EI", position)
    while marker != -1:
        before_ok = marker == 0 or data[marker - 1] in WHITESPACE
        after = marker + 2
        after_ok = after >= len(data) or data[after] in WHITESPACE or data[after] in DELIMITERS
        if before_ok and after_ok:
            return after
        marker = data.find(b"EI", marker + 1)
    return len(data)


def instruction_spans(data: bytes) -> list[tuple[int, int]]:
    """Байтовые границы каждой инструкции потока: ``[(начало, конец), …]``.

    Инструкция — это операнды вместе со своим оператором. Начало отсчитывается
    от первого операнда, конец — сразу за оператором; пробелы и переводы строк
    между инструкциями в границы не входят и потому остаются нетронутыми.
    """
    spans: list[tuple[int, int]] = []
    position = 0
    start: int | None = None

    while position < len(data):
        byte = data[position]

        if byte in WHITESPACE:
            position += 1
            continue

        if byte == 0x25:  # % — комментарий до конца строки
            end = position
            while end < len(data) and data[end] not in b"\r\n":
                end += 1
            position = end
            continue

        if start is None:
            start = position

        if byte == 0x28:  # (
            position = _skip_string(data, position)
            continue
        if byte == 0x3C:  # < — словарь << или шестнадцатеричная строка
            if data[position : position + 2] == b"<<":
                position += 2
            else:
                end = data.find(b">", position)
                position = len(data) if end == -1 else end + 1
            continue
        if byte == 0x3E:  # >
            position += 2 if data[position : position + 2] == b">>" else 1
            continue
        if byte in b"[]{}":
            position += 1
            continue
        if byte == 0x2F:  # /Имя
            position += 1
            while position < len(data) and data[position] not in WHITESPACE \
                    and data[position] not in DELIMITERS:
                position += 1
            continue

        # Обычный токен: число или оператор
        token_start = position
        while position < len(data) and data[position] not in WHITESPACE \
                and data[position] not in DELIMITERS:
            position += 1
        token = data[token_start:position]

        if _is_number(token):
            continue

        # Токен-оператор завершает инструкцию
        if token == b"BI":
            # Встроенное изображение: словарь и данные до EI — одна инструкция
            position = _skip_inline_image(data, position)
        spans.append((start, position))
        start = None

    return spans


def _is_number(token: bytes) -> bool:
    if not token:
        return False
    body = token.lstrip(b"+-")
    if not body:
        return False
    return body.replace(b".", b"", 1).isdigit()


def _key(instruction) -> bytes:
    """Каноническая запись инструкции — по ней сравниваются старая и новая.

    Именно каноническая, от библиотеки: сравнение и проверка результата не
    должны зависеть от того, как записываем строки мы сами.
    """
    try:
        return pikepdf.unparse_content_stream([instruction]).strip()
    except Exception:
        return repr(instruction).encode("utf-8", "replace")


#: Стиль, которым пишут, когда важна не манера, а длина: строки в скобках
#: (для составных шрифтов это вдвое короче шестнадцатеричной записи), числа
#: без лишних нулей, пробелы только там, где без них разбор слипнется
COMPACT_STYLE = style_mod.Style(
    hex_strings=False,
    decimals=3,
    force_decimal=False,
    space_before_operator=False,
    array_gaps=False,
    samples=1,
)


def _write_instruction(instruction, style: style_mod.Style | None = None) -> bytes:
    """Запись инструкции для вставки в поток.

    Без указанного стиля пишем как можно компактнее — так делалось раньше, и
    это по-прежнему нужно, когда правка иначе не помещается на своё место.
    """
    try:
        list(instruction.operands)
    except Exception:
        return _key(instruction)
    return style_mod.render(instruction, style or COMPACT_STYLE)


def _dominant_separator(data: bytes, spans: list[tuple[int, int]]) -> bytes:
    """Чем генератор разделяет инструкции: переводом строки, пробелом, ничем.

    Разделитель — такая же часть почерка, как запись строк. Word ставит
    перевод строки, reportlab — тоже, а вот сжатые до предела потоки
    разделяют инструкции одним пробелом.
    """
    counts: Counter[bytes] = Counter()
    for index in range(len(spans) - 1):
        gap = data[spans[index][1] : spans[index + 1][0]]
        if len(gap) <= 4:
            counts[gap] += 1
    if not counts:
        return b"\n"
    best = counts.most_common(1)[0][0]
    return best if best else b"\n"


def patch_content(
    original_data: bytes,
    original_instructions: list,
    new_instructions: list,
    compact: bool = False,
) -> bytes | None:
    """Переносит правки в исходные байты потока, сохраняя всё остальное.

    Новые инструкции записываются в манере тех, которые они заменяют: те же
    шестнадцатеричные строки или скобки, та же разрядность чисел, тот же
    оператор показа (см. :mod:`pdfedit.style`). Иначе место правки видно в
    распакованном потоке невооружённым глазом, даже без сверки с оригиналом.

    С ``compact=True`` манера приносится в жертву длине — это запасной путь
    для :mod:`pdfedit.inplace`, когда стилизованная запись не помещается в
    исходную длину потока.

    Возвращает новые байты потока либо ``None``, если правку не удалось
    выполнить безопасно — тогда поток надо пересобрать обычным способом.
    """
    spans = instruction_spans(original_data)
    if len(spans) != len(original_instructions):
        # Разборщик посчитал инструкции не так, как qpdf: трогать байты нельзя
        return None

    old_keys = [_key(item) for item in original_instructions]
    new_keys = [_key(item) for item in new_instructions]
    if old_keys == new_keys:
        return original_data

    base_style = COMPACT_STYLE if compact else style_mod.document_style(original_data, spans)
    separator = b"" if compact else _dominant_separator(original_data, spans)
    result = bytearray(original_data)
    matcher = difflib.SequenceMatcher(a=old_keys, b=new_keys, autojunk=False)

    # Приведение оператора к оригинальному (`Tj` вместо `TJ`) меняет саму
    # инструкцию, а значит и её каноническую запись. Проверять результат надо
    # по тому, что мы решили записать, — иначе патч отвергает сам себя
    written_instructions = list(new_instructions)

    # С конца, чтобы уже посчитанные границы не поехали от предыдущих замен
    for tag, i1, i2, j1, j2 in reversed(matcher.get_opcodes()):
        if tag == "equal":
            continue

        pieces: list[bytes] = []
        for offset, instruction in enumerate(new_instructions[j1:j2]):
            # Образец для подражания — та инструкция, что стояла на этом же
            # месте. Её байты говорят и о записи строк, и о разрядности чисел,
            # и о том, каким оператором показывали текст
            model_index = i1 + offset
            if compact or tag == "insert" or model_index >= i2:
                pieces.append(_write_instruction(instruction, base_style))
                continue
            model = original_data[spans[model_index][0] : spans[model_index][1]]
            instruction = style_mod.harmonise_operator(instruction, model)
            written_instructions[j1 + offset] = instruction
            local = style_mod.merged(base_style, style_mod.sniff(model))
            pieces.append(_write_instruction(instruction, local))

        replacement = separator.join(pieces) if separator else _glue(pieces)
        if tag == "insert":
            at = spans[i1][0] if i1 < len(spans) else len(result)
            tail = separator if separator else b" "
            result[at:at] = replacement + tail
        else:  # replace или delete
            start = spans[i1][0]
            end = spans[i2 - 1][1]
            result[start:end] = replacement

    patched = bytes(result)
    if not _patch_is_faithful(patched, [_key(item) for item in written_instructions]):
        return None
    return patched


def _glue(pieces: list[bytes]) -> bytes:
    """Склеивает инструкции без разделителя там, где разбор не слипнется."""
    out = bytearray()
    for piece in pieces:
        if out and style_mod._needs_separator(bytes(out[-1:]), piece[:1]):
            out += b" "
        out += piece
    return bytes(out)


def _patch_is_faithful(patched: bytes, expected_keys: list[bytes]) -> bool:
    """Даёт ли пропатченный поток ровно те инструкции, которые задумывались."""
    try:
        # Документ-носитель нужно удержать переменной: если он останется
        # временным, сборщик мусора освободит его раньше потока, и разбор
        # свалится с невнятным «14 is not a valid ObjectType»
        holder = pikepdf.new()
        parsed = pikepdf.parse_content_stream(pikepdf.Stream(holder, patched))
        keys = [_key(item) for item in parsed]
    except Exception:
        return False
    return keys == expected_keys
