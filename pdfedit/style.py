"""Стиль сериализации потока содержимого: как именно генератор пишет операнды.

Одну и ту же инструкцию можно записать десятком равнозначных способов, и
каждый генератор PDF выбирает свой. Word пишет строки шестнадцатерично
(``<0048> Tj``), reportlab — в скобках (``(H) Tj``), LibreOffice ставит числа
с фиксированным числом знаков (``72.00``), fpdf — как получится (``72``).
Разборщику всё равно: и то и другое даёт одинаковые инструкции.

Но правка, записанная не в том стиле, — это след. Если во всём документе
строки шестнадцатеричные, а в одном месте вдруг круглые скобки, то место
правки видно с первого взгляда на распакованный поток, даже не сравнивая с
оригиналом. То же с числами: ``700.00`` среди ``700`` бросается в глаза.

Поэтому здесь стиль снимается **с исходных байтов той самой инструкции**,
которую заменяем (:func:`sniff`), и новая инструкция пишется в нём же
(:func:`render`). Стиль всего потока (:func:`document_style`) нужен для
инструкций, которых в оригинале не было вовсе, — им подражать нечему, и они
берут манеру большинства.

Модуль намеренно не зависит ни от чего, кроме pikepdf: его вызывает и
:mod:`pdfedit.streampatch` при записи, и :mod:`pdfedit.validate` при проверке.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace

import pikepdf

#: Пробельные байты PDF (ISO 32000-1, таблица 1)
WHITESPACE = b"\x00\t\n\x0c\r "
#: Байты, начинающие или заканчивающие самостоятельный объект
DELIMITERS = b"()<>[]{}/%"

_NUMBER = re.compile(rb"^[+-]?(?:\d+\.?\d*|\.\d+)$")


@dataclass(frozen=True)
class Style:
    """Манера записи операндов, снятая с готовых байтов.

    Все поля описывают выбор между равнозначными записями — тот самый выбор,
    по которому и опознают чужую руку в потоке.
    """

    #: строки записаны шестнадцатерично (``<0048>``), а не в скобках
    hex_strings: bool = False
    #: шестнадцатеричные цифры в верхнем регистре
    hex_upper: bool = True
    #: знаков после запятой у дробных чисел
    decimals: int = 2
    #: целые значения тоже пишутся с точкой (``700.00``, а не ``700``)
    force_decimal: bool = False
    #: ноль перед точкой опускается (``.5`` вместо ``0.5``)
    strip_leading_zero: bool = False
    #: перед оператором стоит пробел (``(x) Tj``, а не ``(x)Tj``)
    space_before_operator: bool = True
    #: элементы массива разделены пробелами (``[(a) -20 (b)]``)
    array_gaps: bool = True
    #: сколько инструкций участвовало в замере (0 — стиль не снят, а взят по умолчанию)
    samples: int = 0

    @property
    def known(self) -> bool:
        """Снят ли стиль с настоящих байтов, а не взят по умолчанию."""
        return self.samples > 0


#: Чем писать, когда подражать нечему: манера самой распространённой из
#: библиотек — строки в скобках, числа без лишних знаков
DEFAULT_STYLE = Style()


# ----------------------------------------------------------------------
# Разбор готовых байтов
# ----------------------------------------------------------------------

def tokens(raw: bytes) -> list[tuple[str, int, int]]:
    """Токены инструкции: ``[(вид, начало, конец), …]``.

    Виды: ``hex``, ``string``, ``number``, ``name``, ``open``/``close``
    (скобки массива или словаря), ``word`` (оператор или ключевое слово).
    """
    found: list[tuple[str, int, int]] = []
    position = 0
    length = len(raw)

    while position < length:
        byte = raw[position]
        if byte in WHITESPACE:
            position += 1
            continue

        start = position
        if byte == 0x28:  # ( — литеральная строка
            depth = 0
            while position < length:
                current = raw[position]
                if current == 0x5C:
                    position += 2
                    continue
                if current == 0x28:
                    depth += 1
                elif current == 0x29:
                    depth -= 1
                    if depth == 0:
                        position += 1
                        break
                position += 1
            found.append(("string", start, position))
            continue

        if byte == 0x3C:  # < — словарь или шестнадцатеричная строка
            if raw[position : position + 2] == b"<<":
                position += 2
                found.append(("open", start, position))
                continue
            end = raw.find(b">", position)
            position = length if end == -1 else end + 1
            found.append(("hex", start, position))
            continue

        if byte == 0x3E:  # >
            position += 2 if raw[position : position + 2] == b">>" else 1
            found.append(("close", start, position))
            continue

        if byte in b"[]{}":
            position += 1
            found.append(("open" if byte in b"[{" else "close", start, position))
            continue

        if byte == 0x2F:  # /Имя
            position += 1
            while position < length and raw[position] not in WHITESPACE \
                    and raw[position] not in DELIMITERS:
                position += 1
            found.append(("name", start, position))
            continue

        while position < length and raw[position] not in WHITESPACE \
                and raw[position] not in DELIMITERS:
            position += 1
        word = raw[start:position]
        found.append(("number" if _NUMBER.match(word) else "word", start, position))

    return found


def sniff(raw: bytes) -> Style:
    """Снимает стиль с байтов одной инструкции (или нескольких подряд).

    Поля, о которых байты ничего не говорят (например, регистр
    шестнадцатеричных цифр, когда шестнадцатеричных строк нет вовсе),
    остаются равными значению по умолчанию.
    """
    marks = tokens(raw)
    if not marks:
        return DEFAULT_STYLE

    hex_count = sum(1 for kind, _s, _e in marks if kind == "hex")
    literal_count = sum(1 for kind, _s, _e in marks if kind == "string")

    hex_upper = DEFAULT_STYLE.hex_upper
    if hex_count:
        digits = b"".join(
            raw[start + 1 : end - 1] for kind, start, end in marks if kind == "hex"
        )
        letters = [byte for byte in digits if byte in b"abcdefABCDEF"]
        # Регистр определяют только буквы: у строки из одних цифр его нет
        if letters:
            hex_upper = sum(1 for byte in letters if byte in b"ABCDEF") * 2 >= len(letters)

    decimals = DEFAULT_STYLE.decimals
    force_decimal = False
    strip_leading_zero = False
    fractions: list[int] = []
    for kind, start, end in marks:
        if kind != "number":
            continue
        word = raw[start:end]
        body = word.lstrip(b"+-")
        if b"." in body:
            whole, _dot, frac = body.partition(b".")
            fractions.append(len(frac))
            # Целое значение, записанное с точкой (700.00), — верный признак
            # генератора с фиксированной разрядностью
            if frac.strip(b"0") == b"":
                force_decimal = True
            if whole == b"":
                strip_leading_zero = True
    if fractions:
        # Самая частая разрядность, а при равенстве — большая: терять точность
        # хуже, чем написать лишний ноль
        counts = Counter(fractions)
        decimals = max(counts, key=lambda value: (counts[value], value))

    space_before_operator = DEFAULT_STYLE.space_before_operator
    if marks and marks[-1][0] == "word":
        operator_start = marks[-1][1]
        if operator_start > 0:
            space_before_operator = raw[operator_start - 1] in WHITESPACE
        if len(marks) == 1:
            # Оператор без операндов о разделителе ничего не сообщает
            space_before_operator = DEFAULT_STYLE.space_before_operator

    array_gaps = DEFAULT_STYLE.array_gaps
    opens = [end for kind, _s, end in marks if kind == "open" and raw[_s : _s + 1] == b"["]
    if opens:
        gaps = 0
        pairs = 0
        for index in range(len(marks) - 1):
            _kind, _start, end = marks[index]
            next_start = marks[index + 1][1]
            if marks[index + 1][0] == "close":
                continue
            if end <= next_start:
                pairs += 1
                if raw[end:next_start].strip(WHITESPACE) == b"" and next_start > end:
                    gaps += 1
        if pairs:
            array_gaps = gaps * 2 >= pairs

    return Style(
        hex_strings=hex_count > literal_count,
        hex_upper=hex_upper,
        decimals=decimals,
        force_decimal=force_decimal,
        strip_leading_zero=strip_leading_zero,
        space_before_operator=space_before_operator,
        array_gaps=array_gaps,
        samples=1,
    )


def document_style(data: bytes, spans: list[tuple[int, int]] | None = None) -> Style:
    """Преобладающий стиль потока — для инструкций, которых в оригинале не было.

    Считается по голосам: каждая инструкция подаёт свой голос за
    шестнадцатеричные строки, за разрядность чисел и так далее, побеждает
    большинство. Одиночная инструкция нетипичного вида погоды не делает.
    """
    if spans is None:
        from .streampatch import instruction_spans

        spans = instruction_spans(data)
    votes: list[Style] = []
    for start, end in spans:
        piece = sniff(data[start:end])
        if piece.known:
            votes.append(piece)
    if not votes:
        return DEFAULT_STYLE

    def majority(getter, default):
        counts = Counter(getter(style) for style in votes)
        return max(counts, key=lambda value: (counts[value], value == default))

    # Разрядность считаем только по инструкциям, где дробные числа вообще были:
    # иначе «0 знаков» у операторов без чисел заглушит настоящую разрядность
    with_numbers = [style for style in votes if style.decimals != DEFAULT_STYLE.decimals]
    decimals = (
        Counter(style.decimals for style in with_numbers).most_common(1)[0][0]
        if with_numbers
        else DEFAULT_STYLE.decimals
    )
    return Style(
        hex_strings=majority(lambda style: style.hex_strings, False),
        hex_upper=majority(lambda style: style.hex_upper, True),
        decimals=decimals,
        force_decimal=majority(lambda style: style.force_decimal, False),
        strip_leading_zero=majority(lambda style: style.strip_leading_zero, False),
        space_before_operator=majority(lambda style: style.space_before_operator, True),
        array_gaps=majority(lambda style: style.array_gaps, True),
        samples=len(votes),
    )


# ----------------------------------------------------------------------
# Запись в снятом стиле
# ----------------------------------------------------------------------

def _literal_string(raw: bytes) -> bytes:
    """Строка в скобках с экранированием того, что обязано экранироваться."""
    out = bytearray(b"(")
    for byte in raw:
        if byte in (0x5C, 0x28, 0x29):      # \ ( )
            out += b"\\" + bytes([byte])
        elif byte == 0x0D:                   # иначе возврат каретки станет \n
            out += b"\\r"
        else:
            out.append(byte)
    out += b")"
    return bytes(out)


def write_string(raw: bytes, style: Style) -> bytes:
    """Строка в манере потока: шестнадцатеричная или в скобках."""
    if style.hex_strings:
        digits = raw.hex()
        return b"<" + (digits.upper() if style.hex_upper else digits).encode("ascii") + b">"
    return _literal_string(raw)


def write_number(value, style: Style) -> bytes:
    """Число в манере потока: та же разрядность, тот же вид у целых."""
    if isinstance(value, int):
        number = float(value)
        was_integer = True
    else:
        number = float(value)
        was_integer = float(number).is_integer()

    if was_integer and not style.force_decimal:
        text = b"%d" % int(round(number))
    else:
        text = (b"%.*f" % (style.decimals, number))
        if style.decimals > 0 and not style.force_decimal:
            # Незначащие нули генераторы обычно не пишут, если не пишут их везде
            trimmed = text.rstrip(b"0").rstrip(b".")
            text = trimmed if trimmed not in (b"", b"-") else b"0"
    if style.strip_leading_zero:
        if text.startswith(b"0."):
            text = text[1:]
        elif text.startswith(b"-0."):
            text = b"-" + text[2:]
    return text


def write_operand(operand, style: Style) -> bytes:
    """Операнд в манере потока."""
    if isinstance(operand, pikepdf.String):
        return write_string(bytes(operand), style)
    if isinstance(operand, pikepdf.Array):
        separator = b" " if style.array_gaps else b""
        items = [write_operand(item, style) for item in operand]
        if not style.array_gaps:
            # Без пробелов склеивать можно только там, где разбор не слипнется:
            # два числа подряд обязаны быть разделены
            glued = bytearray()
            for item in items:
                if glued and _needs_separator(bytes(glued[-1:]), item[:1]):
                    glued += b" "
                glued += item
            return b"[" + bytes(glued) + b"]"
        return b"[" + separator.join(items) + b"]"
    if isinstance(operand, (int, float)) and not isinstance(operand, bool):
        return write_number(operand, style)
    if isinstance(operand, pikepdf.Object):
        try:
            if operand.type_code in (pikepdf.ObjectType.integer, pikepdf.ObjectType.real):
                return write_number(float(operand), style)
        except Exception:
            pass
        return operand.unparse()
    return str(operand).encode()


def _needs_separator(left: bytes, right: bytes) -> bool:
    """Слипнутся ли два токена, если поставить их вплотную."""
    if not left or not right:
        return False
    if left in (b")", b">", b"]"):
        return False
    return right not in (b"(", b"<", b"[", b"/")


def render(instruction, style: Style) -> bytes:
    """Инструкция целиком, записанная в снятом стиле."""
    try:
        operands = list(instruction.operands)
        operator = str(instruction.operator).encode("ascii")
    except Exception:
        return pikepdf.unparse_content_stream([instruction]).strip()

    if not operands:
        return operator

    out = bytearray()
    for operand in operands:
        piece = write_operand(operand, style)
        if out and _needs_separator(bytes(out[-1:]), piece[:1]):
            out += b" "
        out += piece
    if style.space_before_operator or _needs_separator(bytes(out[-1:]), operator[:1]):
        out += b" "
    out += operator
    return bytes(out)


# ----------------------------------------------------------------------
# Выбор между Tj и TJ
# ----------------------------------------------------------------------

#: Операторы показа текста, между которыми возможен равнозначный перевод
SHOW_OPERATORS = frozenset({"Tj", "TJ"})


def harmonise_operator(instruction, original_raw: bytes):
    """Возвращает инструкцию, записанную тем же оператором показа, что и оригинал.

    ``(текст) Tj`` и ``[(текст)] TJ`` рисуют одно и то же. Редактор собирает
    показ текста по своим правилам и легко может выдать ``TJ`` там, где было
    ``Tj``, — а это заметная смена почерка: во всём документе ``Tj``, и вдруг
    один ``TJ``. Перевод возможен только когда он ничего не теряет: массив
    ``TJ`` без чисел равнозначен ``Tj``, а массив с кернингом — нет.
    """
    try:
        operator = str(instruction.operator)
        operands = list(instruction.operands)
    except Exception:
        return instruction
    if operator not in SHOW_OPERATORS:
        return instruction

    marks = tokens(original_raw)
    if not marks or marks[-1][0] != "word":
        return instruction
    original_operator = original_raw[marks[-1][1] : marks[-1][2]].decode("latin-1")
    if original_operator not in SHOW_OPERATORS or original_operator == operator:
        return instruction

    if original_operator == "Tj" and operator == "TJ":
        if len(operands) != 1 or not isinstance(operands[0], pikepdf.Array):
            return instruction
        elements = list(operands[0])
        # Числа в массиве несут кернинг: без них Tj равнозначен, с ними — нет
        if len(elements) != 1 or not isinstance(elements[0], pikepdf.String):
            return instruction
        return pikepdf.ContentStreamInstruction(
            [elements[0]], pikepdf.Operator("Tj")
        )

    if original_operator == "TJ" and operator == "Tj":
        if len(operands) != 1 or not isinstance(operands[0], pikepdf.String):
            return instruction
        return pikepdf.ContentStreamInstruction(
            [pikepdf.Array([operands[0]])], pikepdf.Operator("TJ")
        )
    return instruction


def merged(base: Style, sample: Style) -> Style:
    """Стиль инструкции поверх стиля потока: чего не видно в образце — от потока."""
    if not sample.known:
        return base
    if not base.known:
        return sample
    # Строки и разрядность образец знает лучше: он с того же места
    return replace(
        base,
        hex_strings=sample.hex_strings,
        hex_upper=sample.hex_upper,
        decimals=sample.decimals,
        force_decimal=sample.force_decimal,
        strip_leading_zero=sample.strip_leading_zero,
        space_before_operator=sample.space_before_operator,
        array_gaps=sample.array_gaps,
        samples=sample.samples,
    )
