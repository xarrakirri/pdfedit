"""Правка потоков прямо в файле, без изменения его длины.

Самый бережный из трёх способов сохранить результат. Дописывание
(:mod:`pdfedit.incremental`) не трогает исходные байты, но добавляет в конец
файла слой и второй ``%%EOF``. Полная пересборка (:mod:`pdfedit.saving`) не
оставляет следов слоя, но переписывает контейнер целиком. Здесь — третий путь:
изменённые данные записываются **поверх старых, на то же место и той же
длины**. Тогда в файле не меняется ничего, кроме самих байтов потока: ни
смещения объектов, ни таблица ссылок, ни трейлер, ни ``/ID``, ни даты, ни один
посторонний объект. Побайтовое сравнение с оригиналом показывает ровно те
участки, которые несут изменённый текст.

Как удаётся сохранить длину. Поток содержимого почти всегда сжат Flate, и
новые данные после сжатия обычно оказываются немного короче исходных. Разницу
добивает заполнитель: декодер Flate останавливается на конце сжатого потока, а
хвост за ним не читает — поэтому заполнитель на данные не влияет, а ``/Length``
остаётся прежним. Это же свойство делает способ применимым не всегда:

* новые данные должны помещаться в исходную длину (текст стал длиннее — может
  не поместиться);
* словарь потока должен остаться прежним: изменись он, изменилась бы длина
  заголовка объекта, а с ней и все последующие смещения;
* объект должен лежать в файле отдельно, а не внутри сжатого объектного потока
  ``/ObjStm``: там объекты упакованы вместе, и правка на месте задела бы соседей;
* заполнитель безопасен либо когда данные сжаты (хвост вне сжатого потока), либо
  когда это поток содержимого (лишние пробелы там — просто разделители).

Что не поместилось, дописывается слоем поверх пропатченного файла — способы
складываются: смещения от правки на месте не сдвигаются, поэтому дописанный
слой ложится на неизменную основу.
"""

from __future__ import annotations

import base64
import io
import re
import zlib
from dataclasses import dataclass, field

import pikepdf

from .errors import PdfEditError
from .pdfcrypt import DocumentCipher

#: Фильтры, для которых хвост за концом закодированных данных не читается,
#: а значит заполнитель безвреден
PADDABLE_FILTERS = frozenset({"/FlateDecode", "/Fl", "/LZWDecode", "/LZW"})

#: Чем добивать данные до исходной длины: пробел безопаснее нуля — он не
#: обрывает разбор у программ, которые читают поток как текст
PAD_BYTE = b" "


class InPlacePatchError(PdfEditError):
    """Правка на месте для этого файла невозможна."""


@dataclass
class InPlaceReport:
    """Что удалось записать на месте, а что пришлось дописать слоем."""

    patched: list[tuple[int, int]] = field(default_factory=list)
    #: из них — потоки содержимого, записанные точечной правкой инструкций
    patched_content: set[tuple[int, int]] = field(default_factory=set)
    #: объекты, которые на месте не поместились: (номер, причина)
    deferred: list[tuple[tuple[int, int], str]] = field(default_factory=list)
    #: сколько байт заполнителя добавлено суммарно
    padding: int = 0
    #: объекты, за данными которых остался заполнитель
    padded: list[tuple[int, int]] = field(default_factory=list)
    #: объекты, которые пришлось сжать не тем уровнем, что был у оригинала
    recompressed: list[tuple[int, int]] = field(default_factory=list)
    #: объекты, где ради длины пришлось отступить от манеры записи оригинала
    restyled: list[tuple[int, int]] = field(default_factory=list)
    #: осталась ли длина файла прежней (то есть всё легло на место)
    length_kept: bool = True

    def describe(self) -> str:
        lines = [
            f"записано на месте объектов: {len(self.patched)}"
            + (f" (заполнителя {self.padding} байт)" if self.padding else ""),
        ]
        if self.padded:
            lines.append(
                f"     заполнитель за данными остался у {len(self.padded)}: "
                + ", ".join(f"{num} {gen} R" for num, gen in self.padded[:6])
            )
        if self.recompressed:
            lines.append(
                f"     сжато не тем уровнем, что в оригинале: {len(self.recompressed)} — "
                + ", ".join(f"{num} {gen} R" for num, gen in self.recompressed[:6])
            )
        if self.restyled:
            lines.append(
                f"     записано не в манере оригинала (ради длины): {len(self.restyled)} — "
                + ", ".join(f"{num} {gen} R" for num, gen in self.restyled[:6])
            )
        if self.patched:
            listed = ", ".join(f"{num} {gen} R" for num, gen in self.patched[:12])
            more = "" if len(self.patched) <= 12 else f" и ещё {len(self.patched) - 12}"
            lines.append(f"     {listed}{more}")
        if self.deferred:
            lines.append(f"дописано слоем: {len(self.deferred)} — на месте не помещаются:")
            for objgen, reason in self.deferred[:12]:
                lines.append(f"     {objgen[0]} {objgen[1]} R: {reason}")
            if len(self.deferred) > 12:
                lines.append(f"     … и ещё {len(self.deferred) - 12}")
        lines.append(
            "длина файла: "
            + ("не изменилась" if self.length_kept else "изменилась (часть правок легла слоем)")
        )
        return "\n".join(lines)


# ----------------------------------------------------------------------
# Разбор записи объекта в файле
# ----------------------------------------------------------------------

_STREAM_START = re.compile(rb">>\s*stream(\r\n|\n|\r)")


def stream_data_span(data: bytes, offset: int, declared_length: int) -> tuple[int, int]:
    """Границы данных потока, записанного по смещению ``offset``.

    Возвращает ``(начало, длина)``. Длина берётся из ``/Length`` словаря: она и
    определяет, сколько байт лежит между ``stream`` и ``endstream``. Результат
    проверяется по хвосту — за данными обязано идти ``endstream``, иначе мы
    ошиблись в разборе и трогать файл нельзя.
    """
    window = data[offset : offset + 65536]
    match = _STREAM_START.search(window)
    if match is None:
        raise InPlacePatchError("в записи объекта не найдено начало потока")
    start = offset + match.end()
    tail = data[start + declared_length : start + declared_length + 20]
    if b"endstream" not in tail:
        raise InPlacePatchError(
            "за данными потока нет endstream — запись объекта разобрана неверно"
        )
    return start, declared_length


def _dictionary_unchanged(current: pikepdf.Stream, original: pikepdf.Stream) -> bool:
    """Совпадают ли словари потоков во всём, кроме упаковки данных.

    Если словарь изменился, заголовок объекта стал бы другой длины — а вместе с
    ним поехали бы смещения всех последующих объектов. Такой объект правится
    только дописыванием.

    Из сравнения исключены ``/Length``, ``/Filter`` и ``/DecodeParms``: словарь
    в файле мы вообще не переписываем, он остаётся исходным, а новые данные
    кодируются тем фильтром, который в нём записан. Исключать их обязательно
    ещё и потому, что pikepdf, принимая новые данные потока, снимает ``/Filter``
    у объекта в памяти — сравнение «как есть» не совпало бы никогда.
    """
    left = pikepdf.Dictionary(current.stream_dict)
    right = pikepdf.Dictionary(original.stream_dict)
    for head in (left, right):
        for key in ("/Length", "/Filter", "/DecodeParms"):
            if key in head:
                del head[key]
    return left.unparse(resolved=True) == right.unparse(resolved=True)


_OBJ_HEAD = re.compile(rb"\d+\s+\d+\s+obj\b")


def object_body_span(data: bytes, offset: int) -> tuple[int, int]:
    """Границы тела объекта — между заголовком ``N G obj`` и ``endobj``.

    Нужны для правки на месте объектов, которые потоками не являются:
    узлов структурного дерева с ``/ActualText``, закладок, полей форм.
    """
    head = _OBJ_HEAD.match(data, offset) or _OBJ_HEAD.search(data, offset, offset + 64)
    if head is None:
        raise InPlacePatchError("заголовок объекта не найден по своему смещению")
    start = head.end()
    end = data.find(b"endobj", start)
    if end == -1:
        raise InPlacePatchError("у объекта нет endobj — запись разобрана неверно")
    while end > start and data[end - 1] in b"\r\n \t":
        end -= 1
    while start < end and data[start] in b"\r\n \t":
        start += 1
    return start, end - start


def plain_object_payload(
    obj: pikepdf.Object,
    objgen: tuple[int, int],
    cipher: DocumentCipher | None,
) -> bytes | None:
    """Запись объекта-не-потока так, как она ляжет в файл."""
    try:
        if cipher is not None:
            from .pdfcrypt import shield_strings

            obj = shield_strings(obj, cipher, objgen)
        return obj.unparse(resolved=True)
    except Exception:
        return None


class _ObjectLocator:
    """Где в файле лежит запись объекта.

    Обычно это знает таблица ссылок, которую отдаёт pikepdf. В pikepdf 9 такого
    метода ещё нет, поэтому есть запасной путь — поиск заголовка ``N G obj`` по
    байтам файла. Он принимается только при единственном совпадении: два
    совпадения означают, что мы наткнулись на похожие байты внутри потока, и
    трогать файл по такому адресу нельзя.
    """

    def __init__(self, pdf: pikepdf.Pdf, data: bytes):
        self.data = data
        self.table = None
        getter = getattr(pdf, "get_xref_table", None)
        if getter is not None:
            try:
                self.table = getter()
            except Exception:
                self.table = None

    def offset(self, objgen: tuple[int, int]) -> tuple[int | None, str]:
        if self.table is not None:
            entry = self.table.get(objgen)
            if entry is None:
                return None, "объекта нет в таблице ссылок"
            if entry.type != 1:
                return None, "лежит внутри объектного потока /ObjStm"
            return entry.offset, ""

        pattern = re.compile(
            rb"(?<![0-9])%d\s+%d\s+obj\b" % (objgen[0], objgen[1])
        )
        found = list(pattern.finditer(self.data))
        if len(found) != 1:
            return None, (
                "заголовок объекта не найден в файле"
                if not found
                else "заголовок объекта встречается несколько раз"
            )
        return found[0].start(), ""


def content_payloads(stream: pikepdf.Stream, base: pikepdf.Stream) -> list[bytes]:
    """Варианты данных потока — от самого бережного к самому короткому.

    Пересобранный поток стабильно на 2–4 % длиннее исходного просто из-за
    другого форматирования, и этих процентов хватает, чтобы правка перестала
    помещаться на своё место. Точечная замена (:mod:`pdfedit.streampatch`)
    оставляет нетронутые инструкции ровно теми байтами, какими они были, —
    тогда поток растёт лишь на длину самой правки.

    Вариантов два, и между ними приходится выбирать. Первый пишет правку в
    манере оригинала: те же шестнадцатеричные строки, та же разрядность чисел.
    Второй жертвует манерой ради длины — шестнадцатеричная запись вдвое
    длиннее, и для составных шрифтов это решает, поместится правка на своё
    место или уйдёт в дописанный слой. Первый предпочтительнее, второй
    берётся, только когда первый не влез.
    """
    from .streampatch import patch_content

    try:
        old_instructions = list(pikepdf.parse_content_stream(base))
        new_instructions = list(pikepdf.parse_content_stream(stream))
        original = base.read_bytes()
    except Exception:
        return [stream.read_bytes()]

    variants: list[bytes] = []
    for compact in (False, True):
        try:
            patched = patch_content(
                original, old_instructions, new_instructions, compact=compact
            )
        except Exception:
            patched = None
        if patched is not None and patched not in variants:
            variants.append(patched)
    rebuilt = stream.read_bytes()
    if rebuilt not in variants:
        variants.append(rebuilt)
    return variants


def content_payload(stream: pikepdf.Stream, base: pikepdf.Stream) -> bytes:
    """Один, самый бережный вариант данных потока (см. :func:`content_payloads`)."""
    return content_payloads(stream, base)[0]


#: Пустой stored-блок deflate: пять байт в сжатом потоке и ноль — в
#: распакованных данных. Ими добивают длину, ничего не меняя по существу
_EMPTY_STORED_BLOCK = b"\x00\x00\x00\xff\xff"


def zlib_header_levels() -> dict[bytes, list[int]]:
    """Двухбайтовый заголовок zlib → уровни сжатия, которые его дают.

    В заголовке (RFC 1950) два бита FLEVEL сообщают, насколько старательно
    сжимали: 6 даёт ``78 9c``, 7–9 — ``78 da``, 1 — ``78 01``. Сравнить эти
    два байта у оригинала и результата — самый дешёвый способ увидеть, что
    файл пересжали чужой программой, и первое, на что смотрят при разборе.
    """
    global _HEADER_LEVELS
    if _HEADER_LEVELS is None:
        table: dict[bytes, list[int]] = {}
        for level in range(1, 10):
            packer = zlib.compressobj(level, zlib.DEFLATED, 15, 9)
            head = (packer.compress(b"proba") + packer.flush())[:2]
            table.setdefault(head, []).append(level)
        _HEADER_LEVELS = table
    return _HEADER_LEVELS


_HEADER_LEVELS: dict[bytes, list[int]] | None = None


def flate_exact(
    payload: bytes, capacity: int, level: int, strategy: int = zlib.Z_DEFAULT_STRATEGY
) -> bytes | None:
    """Сжимает ``payload`` ровно в ``capacity`` байт — без хвоста и без потерь.

    Обычное сжатие даёт длину, какая получится, и разницу до ``/Length``
    приходится добивать мусором за концом сжатых данных. Такой хвост декодер
    не читает, но он остаётся в файле: неиспользуемые байты внутри потока —
    готовая улика, их видно любым разбором.

    Здесь длина набирается средствами самого формата. После ``Z_SYNC_FLUSH``
    поток выровнен по границе байта, и к нему можно дописать сколько угодно
    пустых stored-блоков: каждый стоит пять байт и не несёт ни одного байта
    данных. Оставшиеся один-четыре байта добираются переносом хвоста
    ``payload`` в последний блок нетронутым (``m`` в цикле). Распакованные
    данные при этом совпадают с исходными байт в байт, а хвоста нет вовсе.

    Возвращает ``None``, если ровной длины не выходит: тогда остаётся обычное
    сжатие с заполнителем.
    """
    if capacity < 0:
        return None
    limit = min(len(payload), 64)
    checksum = zlib.adler32(payload).to_bytes(4, "big")

    for keep in range(0, limit + 1):
        packer = zlib.compressobj(level, zlib.DEFLATED, 15, 9, strategy)
        head = packer.compress(payload[: len(payload) - keep])
        head += packer.flush(zlib.Z_SYNC_FLUSH)
        # 5 байт — заголовок последнего stored-блока, 4 — контрольная сумма
        gap = capacity - len(head) - 5 - keep - 4
        if gap < 0 or gap % 5:
            continue
        tail = payload[len(payload) - keep :] if keep else b""
        final = (
            b"\x01"
            + len(tail).to_bytes(2, "little")
            + (len(tail) ^ 0xFFFF).to_bytes(2, "little")
            + tail
        )
        result = head + _EMPTY_STORED_BLOCK * (gap // 5) + final + checksum
        if len(result) != capacity:
            continue
        try:
            unpacker = zlib.decompressobj()
            if unpacker.decompress(result) != payload or unpacker.unused_data:
                continue
        except zlib.error:
            continue
        return result
    return None


def _plaintext_capacity(
    cipher: DocumentCipher | None,
    objgen: tuple[int, int],
    capacity: int,
    stream_type: str,
) -> int | None:
    """Сколько байт должно быть ДО шифрования, чтобы после него вышло ровно ``capacity``.

    AES дописывает вектор инициализации и дополнение, поэтому длина растёт
    ступеньками. Размер ступеньки и надбавку выясняем двумя пробами, а не
    предположениями о шифре: так же верно выйдет и для RC4, где длина не
    меняется вовсе.
    """
    if cipher is None:
        return capacity
    try:
        short = len(cipher.encrypt_stream(objgen, b"\x00" * 16, stream_type))
        long = len(cipher.encrypt_stream(objgen, b"\x00" * 32, stream_type))
    except Exception:
        return None
    step = long - short
    if step <= 0:
        return None
    if short == 16:
        # Поточный шифр (RC4): длина не меняется вовсе
        return capacity
    # Блочный шифр: длина растёт ступеньками, ``out(n) = base + step*(n // step)``.
    # Базу выводим из первой пробы, а не из предположений о вкладе вектора
    # инициализации и дополнения
    base = short - step * (16 // step)
    steps, remainder = divmod(capacity - base, step)
    if remainder or steps < 0:
        return None
    # В ступеньку укладывается step значений длины; берём наибольшее —
    # на нём больше всего места под данные
    return step * steps + step - 1


#: Имена фильтров, приведённые к одному виду
_FILTER_ALIASES = {
    "/Fl": "/FlateDecode", "/A85": "/ASCII85Decode", "/AHx": "/ASCIIHexDecode",
    "/LZW": "/LZWDecode",
}


def filter_chain(stream: pikepdf.Stream) -> list[str]:
    """Фильтры потока по порядку применения — как записано в ``/Filter``.

    Порядок важен: ``/Filter [/ASCII85Decode /FlateDecode]`` означает, что
    данные сначала расшифровывает ASCII85, а уже потом Flate. Чтобы записать
    их обратно, действия выполняются в обратном порядке. Так пишет, в
    частности, reportlab, и множество ``{"/ASCII85Decode", "/FlateDecode"}``
    без порядка тут не годится.
    """
    try:
        value = stream.stream_dict.get("/Filter")
    except Exception:
        return []
    if value is None:
        return []
    names = [value] if not isinstance(value, pikepdf.Array) else list(value)
    return [_FILTER_ALIASES.get(str(name), str(name)) for name in names]


def _a85_length(size: int) -> int:
    """Длина ASCII85-записи для ``size`` байт (без завершающего ``~>``)."""
    full, rest = divmod(size, 4)
    return full * 5 + (rest + 1 if rest else 0)


def _flate_size_for_a85(body_length: int) -> int | None:
    """Сколько сжатых байт дают ASCII85-строку ровно такой длины."""
    guess = body_length * 4 // 5
    for size in range(max(0, guess - 4), guess + 5):
        if _a85_length(size) == body_length:
            return size
    return None


def _encode_for_slot(
    payload: bytes,
    objgen: tuple[int, int],
    filters: list[str] | set[str],
    capacity: int,
    cipher: DocumentCipher | None,
    stream_type: str,
    original_header: bytes = b"",
) -> tuple[bytes, int, bool] | None:
    """Готовит данные потока так, чтобы они поместились в исходную длину.

    Возвращает ``(байты, сколько добить заполнителем, сохранён ли стиль сжатия)``
    либо ``None``, если данные не помещаются никак.

    Порядок предпочтений отвечает трём разным требованиям сразу:

    1. уровень сжатия берётся тот же, что был у исходного потока, — иначе
       двухбайтовый заголовок zlib выдаёт пересжатие чужой программой;
    2. длина набирается ровно (:func:`flate_exact`), чтобы за сжатыми данными
       не оставалось неиспользуемых байт;
    3. и только если ни то ни другое не выходит, данные жмутся как угодно
       сильно и добиваются заполнителем — иначе правка вообще не ляжет на
       место и уйдёт в дописанный слой, а это след куда заметнее.
    """
    target = _plaintext_capacity(cipher, objgen, capacity, stream_type)
    chain = list(filters) if isinstance(filters, list) else sorted(filters)

    def sealed(data: bytes) -> bytes | None:
        if cipher is None:
            return data
        try:
            return cipher.encrypt_stream(objgen, data, stream_type)
        except Exception:
            return None

    def fits(data: bytes, exactly: bool) -> bytes | None:
        encoded = sealed(data)
        if encoded is None:
            return None
        if len(encoded) == capacity if exactly else len(encoded) <= capacity:
            return encoded
        return None

    if not chain:
        encoded = fits(payload, exactly=False)
        return (encoded, capacity - len(encoded), True) if encoded else None

    # Поддержаны ровно две цепочки: один Flate и «ASCII85 поверх Flate» (так
    # пишет reportlab). Всё прочее не трогаем: перекодировать незнакомый
    # фильтр вслепую — значит испортить поток
    if chain == ["/FlateDecode"]:
        ascii85 = False
    elif chain == ["/ASCII85Decode", "/FlateDecode"]:
        ascii85 = True
    else:
        return None

    def dressed(compressed: bytes) -> bytes:
        """Сжатые данные, одетые в остальные фильтры цепочки."""
        if not ascii85:
            return compressed
        return base64.a85encode(compressed) + b"~>"

    def inner_size(slot: int) -> int | None:
        """Сколько должно выйти сжатых байт, чтобы одетые заняли ровно ``slot``."""
        if not ascii85:
            return slot
        return _flate_size_for_a85(slot - 2) if slot >= 2 else None

    native = zlib_header_levels().get(bytes(original_header[:2]), [])
    others = [level for level in (9, 8, 7, 6, 5, 4, 3, 2, 1) if level not in native]
    strategies = (zlib.Z_DEFAULT_STRATEGY, zlib.Z_FILTERED, zlib.Z_RLE)

    def compressed(level: int, strategy: int) -> bytes:
        packer = zlib.compressobj(level, zlib.DEFLATED, 15, 9, strategy)
        return packer.compress(payload) + packer.flush()

    # 1. Свой уровень, ровная длина: ни хвоста, ни смены стиля сжатия
    wanted = inner_size(target) if target is not None else None
    if wanted is not None and wanted >= 0:
        for level in native:
            for strategy in strategies:
                exact = flate_exact(payload, wanted, level, strategy)
                if exact is None:
                    continue
                encoded = fits(dressed(exact), exactly=True)
                if encoded is not None:
                    return encoded, 0, True

    # 1а. Ровная длина ценой более сильного сжатия, но с исходным заголовком.
    #
    # Ровная длина требует запаса: под завершающий stored-блок и контрольную
    # сумму нужно около десятка байт, и когда данные сжимаются почти в
    # исходный размер, запаса не хватает. Запас даёт более сильное сжатие — но
    # оно меняет два бита FLEVEL в заголовке zlib, и поток становится видно.
    #
    # Эти два бита декодер не читает: по RFC 1950 они справочные, inflate
    # смотрит только на метод и размер окна (первый байт). Поэтому заголовок
    # возвращается исходный — и целиком, чтобы контрольная сумма FCHECK в нём
    # осталась той же, что была. Меняем лишь тогда, когда первый байт совпал:
    # разойдись размер окна — и поток бы не распаковался.
    if wanted is not None and wanted >= 0 and len(original_header) >= 2 and not ascii85:
        for level in others:
            for strategy in strategies:
                exact = flate_exact(payload, wanted, level, strategy)
                if exact is None or exact[0] != original_header[0]:
                    continue
                restored = bytes(original_header[:2]) + exact[2:]
                try:
                    if zlib.decompress(restored) != payload:
                        continue
                except zlib.error:
                    continue
                encoded = fits(restored, exactly=True)
                if encoded is not None:
                    return encoded, 0, True

    # 2. Свой уровень, обычное сжатие: стиль сохранён, но останется хвост
    for level in native:
        for strategy in strategies:
            encoded = fits(dressed(compressed(level, strategy)), exactly=False)
            if encoded is not None:
                return encoded, capacity - len(encoded), True

    # 3. Чужой уровень: перебор не роскошь, победитель меняется от данных к
    #    данным, а спор идёт за единицы байт
    variants = sorted(
        (compressed(level, strategy) for level in others for strategy in strategies),
        key=len,
    )
    for variant in variants:
        encoded = fits(dressed(variant), exactly=False)
        if encoded is not None:
            return encoded, capacity - len(encoded), False
    return None


# ----------------------------------------------------------------------
# Правка
# ----------------------------------------------------------------------

def patch_in_place(
    pdf: pikepdf.Pdf,
    original_bytes: bytes,
    cipher: DocumentCipher | None = None,
) -> tuple[bytes, InPlaceReport]:
    """Записывает изменённые потоки поверх старых, не меняя длину файла.

    Возвращает ``(байты файла, отчёт)``. Объекты, которые на место не влезли
    или лежат в объектном потоке, остаются неизменёнными — их перечисляет
    ``report.deferred``, и их нужно дописать слоем.
    """
    from .incremental import changed_objects

    report = InPlaceReport()
    to_write, _rewritten, _added = changed_objects(pdf, original_bytes)
    if not to_write:
        return original_bytes, report

    result = bytearray(original_bytes)
    with pikepdf.open(io.BytesIO(original_bytes)) as original:
        locator = _ObjectLocator(original, original_bytes)

        # По убыванию смещения: даже если разбор где-то ошибётся, уже
        # записанные участки не сдвинутся — длина каждого участка сохраняется
        candidates = sorted(to_write.items(), key=lambda item: item[0], reverse=True)
        for objgen, obj in candidates:
            base = original.get_object(objgen)

            if base is None:
                report.deferred.append((objgen, "объекта не было в оригинале"))
                continue
            offset, reason = locator.offset(objgen)
            if offset is None:
                report.deferred.append((objgen, reason))
                continue

            if not isinstance(obj, pikepdf.Stream):
                # Не поток, а обычный объект: узел структурного дерева с
                # /ActualText, закладка, поле формы. Правка скрытых копий
                # текста меняет именно их, и без этой ветки любой тегированный
                # документ уходил бы в дописанный слой целиком — при том, что
                # исправленная копия почти всегда той же длины, что была
                written = _patch_plain_object(
                    result, original_bytes, offset, objgen, obj, cipher, report
                )
                if written:
                    report.patched.append(objgen)
                continue
            if not _dictionary_unchanged(obj, base):
                report.deferred.append((objgen, "изменился словарь потока"))
                continue

            # Цепочку берём со ПОРЯДКОМ, а не множеством: у reportlab это
            # [/ASCII85Decode /FlateDecode], и записать туда голый zlib —
            # значит испортить поток
            chain = filter_chain(base)
            filters = set(chain)
            stream_type = (
                str(base.stream_dict.get("/Type", "")) if "/Type" in base.stream_dict else ""
            )
            if chain and chain not in (["/FlateDecode"], ["/ASCII85Decode", "/FlateDecode"]):
                report.deferred.append(
                    (objgen, f"цепочка фильтров {' '.join(chain)} не пересобирается")
                )
                continue
            if "/DecodeParms" in base.stream_dict:
                # Предиктор и прочие параметры декодирования: закодировать
                # данные так, чтобы декодер прочёл их обратно, здесь нечем
                report.deferred.append((objgen, "у потока есть /DecodeParms"))
                continue

            declared = int(base.stream_dict.get("/Length", 0))
            try:
                start, length = stream_data_span(original_bytes, offset, declared)
            except InPlacePatchError as exc:
                report.deferred.append((objgen, str(exc)))
                continue

            is_content = _is_content_stream(pdf, objgen)
            if not filters and not is_content:
                # Несжатый поток: заполнитель стал бы частью данных. Для
                # содержимого страницы пробелы безвредны (это разделители),
                # для всего остального — нет
                report.deferred.append((objgen, "несжатый поток вне содержимого страницы"))
                continue

            header = original_bytes[start : start + 2]
            payloads = (
                content_payloads(obj, base) if is_content else [obj.read_bytes()]
            )
            prepared = None
            for index, payload in enumerate(payloads):
                prepared = _encode_for_slot(
                    payload, objgen, chain, length, cipher, stream_type, header
                )
                if prepared is not None:
                    if index:
                        # Стилизованная запись не поместилась, пошла компактная
                        report.restyled.append(objgen)
                    break
            if prepared is None:
                report.deferred.append((objgen, "данные не помещаются в исходную длину"))
                continue

            encoded, padding, style_kept = prepared
            result[start : start + length] = encoded + PAD_BYTE * padding
            report.patched.append(objgen)
            if is_content:
                report.patched_content.add(objgen)
            report.padding += padding
            if padding:
                report.padded.append(objgen)
            if not style_kept:
                report.recompressed.append(objgen)

    report.patched.sort()
    report.length_kept = len(result) == len(original_bytes)
    return bytes(result), report


def _patch_strings_in_object(original: bytes, rebuilt: bytes) -> bytes | None:
    """Переносит изменившиеся строки в исходные байты записи объекта.

    Тот же приём, что и для потоков содержимого: всё, кроме самих строк,
    остаётся исходными байтами. Это важно вдвойне — и чтобы запись
    поместилась на своё место, и чтобы строка осталась записана в манере
    оригинала: ``/ActualText`` в тегированном PDF пишут шестнадцатерично, а
    библиотека при пересборке легко напишет её в скобках.

    Возвращает ``None``, если перенести не удалось; тогда остаётся полная
    пересборка записи.
    """
    from . import style as style_mod

    def strings(data: bytes) -> dict[str, tuple[int, int]]:
        """Строки записи, помеченные ключом, под которым лежат.

        Сопоставлять их по порядку нельзя: пересобирая словарь, библиотека
        сортирует ключи по алфавиту, и вторая строка в оригинале запросто
        окажется первой в пересборке. Ключ же остаётся ключом.
        """
        found: dict[str, tuple[int, int]] = {}
        key = ""
        depth: list[str] = []
        for kind, start, end in style_mod.tokens(data):
            if kind == "name":
                key = data[start:end].decode("latin-1")
                continue
            if kind in ("hex", "string"):
                path = "/".join(depth + [key])
                if path in found:
                    return {}   # одинаковые пути: сопоставить однозначно нельзя
                found[path] = (start, end)
            if kind == "open" and data[start:start + 2] == b"<<":
                depth.append(key)
            elif kind == "close" and data[start:start + 2] == b">>" and depth:
                depth.pop()
        return found

    old_marks = strings(original)
    new_marks = strings(rebuilt)
    if not old_marks or set(old_marks) != set(new_marks):
        return None

    result = bytearray(original)
    # С конца, чтобы уже посчитанные границы не поехали от предыдущих замен
    for path in sorted(old_marks, key=lambda name: -old_marks[name][0]):
        old_start, old_end = old_marks[path]
        new_start, new_end = new_marks[path]
        was = original[old_start:old_end]
        now = rebuilt[new_start:new_end]
        if was == now:
            continue
        try:
            value = bytes(pikepdf.Object.parse(now))
        except Exception:
            return None
        written = style_mod.write_string(value, style_mod.sniff(was))
        # Записанное обязано читаться обратно тем же значением: манеру
        # оригинала мы воспроизводим, а смысл менять не имеем права
        try:
            if bytes(pikepdf.Object.parse(written)) != value:
                return None
        except Exception:
            return None
        result[old_start:old_end] = written

    patched = bytes(result)
    # Сверка разбором — самая надёжная, но возможна не всегда: запись со
    # ссылками вида «25 0 R» в отрыве от документа не разбирается вовсе.
    # Тогда полагаемся на то, что строки сопоставлены по ключам (пути
    # повторяться не могли — иначе сопоставление отказалось бы работать),
    # каждая проверена обратным разбором, а вне строк байты не тронуты
    try:
        return patched if pikepdf.Object.parse(patched).unparse(resolved=True) == \
            pikepdf.Object.parse(rebuilt).unparse(resolved=True) else None
    except Exception:
        return patched


def _patch_plain_object(
    result: bytearray,
    original_bytes: bytes,
    offset: int,
    objgen: tuple[int, int],
    obj: pikepdf.Object,
    cipher: DocumentCipher | None,
    report: InPlaceReport,
) -> bool:
    """Переписывает объект-не-поток поверх старого, не меняя его длину.

    Помещается он далеко не всегда: длина записи зависит от самих значений.
    Зато очень часто помещается ровно — исправленная копия текста той же
    длины, что была. Разницу добиваем пробелами перед ``endobj``: там они
    разделитель и на разбор не влияют.
    """
    payload = plain_object_payload(obj, objgen, cipher)
    if payload is None:
        report.deferred.append((objgen, "объект не сериализуется"))
        return False
    try:
        start, length = object_body_span(original_bytes, offset)
    except InPlacePatchError as exc:
        report.deferred.append((objgen, str(exc)))
        return False

    # Сначала — точечная замена строк прямо в исходных байтах записи. Целиком
    # пересобранный словарь почти всегда чуть другой длины: библиотека пишет
    # строки и пробелы по-своему, и запись перестаёт помещаться на своё место
    # даже когда сам текст не стал длиннее
    spot = _patch_strings_in_object(original_bytes[start : start + length], payload)
    if spot is not None:
        payload = spot

    if len(payload) > length:
        report.deferred.append(
            (objgen, f"запись объекта длиннее исходной на {len(payload) - length} б")
        )
        return False

    padding = length - len(payload)
    result[start : start + length] = payload + b" " * padding
    report.padding += padding
    if padding:
        report.padded.append(objgen)
    return True


def build_in_place(
    pdf: pikepdf.Pdf, original_bytes: bytes, password: str = ""
) -> tuple[bytes, InPlaceReport, object | None]:
    """Правка на месте, а что не поместилось — дописанным слоем.

    Возвращает ``(байты, отчёт о правке на месте, отчёт о дописанном слое)``.
    Второй отчёт равен ``None``, когда слой не понадобился: тогда файл той же
    длины, что и оригинал, и отличается от него только внутри изменённых потоков.
    """
    from .incremental import build_update, verify_update

    cipher = DocumentCipher.from_pdf(pdf) if pdf.is_encrypted else None
    data, report = patch_in_place(pdf, original_bytes, cipher)

    layer_report = None
    if report.deferred:
        # Слой ложится поверх уже пропатченных байтов: смещения от правки на
        # месте не сдвинулись, поэтому основа для него — неизменная. Сами
        # пропатченные объекты в слой не берём: в файле они уже правильные, а
        # побайтово от того, что лежит в памяти, отличаются упаковкой
        patched_base = data
        data, layer_report = build_update(pdf, patched_base, skip=set(report.patched))
        verify_update(data, patched_base, pdf, layer_report, password=password)
        report.length_kept = False

    _verify_patched(data, pdf, report, password)
    return data, report, layer_report


def _instruction_keys(stream: pikepdf.Stream) -> list[bytes]:
    from .streampatch import _key

    return [_key(item) for item in pikepdf.parse_content_stream(stream)]


def _verify_patched(
    data: bytes, pdf: pikepdf.Pdf, report: InPlaceReport, password: str
) -> None:
    """Перечитывает готовый файл и сверяет пропатченные объекты с памятью.

    Потоки содержимого сверяются по инструкциям, а не по байтам: точечная
    правка сохраняет исходное форматирование, поэтому байты у неё заведомо
    другие, чем у потока, пересобранного в памяти, — а вот последовательность
    инструкций обязана совпадать до последнего операнда.
    """
    from .incremental import _semantic_fingerprint

    try:
        with pikepdf.open(io.BytesIO(data), password=password) as after:
            for objgen in report.patched:
                written = after.get_object(objgen)
                expected = pdf.get_object(objgen)
                if written is None:
                    raise InPlacePatchError(
                        f"объект {objgen[0]} {objgen[1]} R не читается из готового файла"
                    )
                if objgen in report.patched_content:
                    same = _instruction_keys(written) == _instruction_keys(expected)
                else:
                    same = _semantic_fingerprint(written) == _semantic_fingerprint(expected)
                if not same:
                    raise InPlacePatchError(
                        f"объект {objgen[0]} {objgen[1]} R прочитан из файла не тем, "
                        f"чем был в памяти — правка отменена"
                    )
    except InPlacePatchError:
        raise
    except Exception as exc:
        raise InPlacePatchError(
            f"после правки на месте файл не читается ({exc}) — правка отменена, "
            f"исходный файл не тронут"
        ) from exc


def save_in_place(
    pdf: pikepdf.Pdf, path: str, original_bytes: bytes, password: str = ""
) -> tuple[InPlaceReport, object | None]:
    """Записывает файл, правя потоки на месте (остаток — дописанным слоем)."""
    data, report, layer_report = build_in_place(pdf, original_bytes, password)
    with open(path, "wb") as handle:
        handle.write(data)
    return report, layer_report


def _stream_filters(stream: pikepdf.Stream) -> set[str]:
    raw = stream.stream_dict.get("/Filter")
    if raw is None:
        return set()
    if isinstance(raw, pikepdf.Array):
        return {str(item) for item in raw}
    return {str(raw)}


def _is_content_stream(pdf: pikepdf.Pdf, objgen: tuple[int, int]) -> bool:
    """Является ли объект потоком содержимого страницы или Form XObject."""
    for page in pdf.pages:
        contents = page.obj.get("/Contents")
        if contents is None:
            continue
        items = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
        for item in items:
            if isinstance(item, pikepdf.Object) and item.objgen == objgen:
                return True
        resources = page.obj.get("/Resources")
        xobjects = resources.get("/XObject") if resources is not None else None
        if xobjects is None:
            continue
        for _name, xobject in xobjects.items():
            if (
                isinstance(xobject, pikepdf.Object)
                and xobject.objgen == objgen
                and str(xobject.get("/Subtype", "")) == "/Form"
            ):
                return True

    # Внешний вид аннотации — тоже Form XObject и тоже поток содержимого,
    # только доступен он не через /Resources страницы, а через саму
    # аннотацию. Без этой ветки правка поля формы не ложилась на место:
    # заполнитель в таком потоке считался опасным, хотя пробелы там —
    # такие же разделители инструкций, как и на странице
    from .content import appearance_streams

    for page in pdf.pages:
        for _annot, stream in appearance_streams(page.obj):
            if stream.objgen == objgen:
                return True
    return False
