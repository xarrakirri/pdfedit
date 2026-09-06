"""Инкрементальное обновление: оригинал не переписывается ни одним байтом.

Обычный путь сохранения (:mod:`pdfedit.saving`) отдаёт документ библиотеке
qpdf, и та собирает контейнер заново: перенумеровывает объекты, пересжимает
потоки, строит новую таблицу ссылок. Содержимое при этом сохраняется, но
структура файла — уже другая, и байтовое сравнение с оригиналом бессмысленно.

Здесь другой подход, предусмотренный самой спецификацией PDF (ISO 32000-1,
раздел 7.5.6): исходные байты остаются на месте, а в конец файла дописываются
новые редакции только тех объектов, которые изменились, и новая таблица
ссылок со ссылкой ``/Prev`` на предыдущую. Ридер читает последнюю таблицу,
находит там новые смещения для изменённых объектов, а всё остальное — потоки
нетронутых страниц, шрифты, изображения, аннотации, дерево структуры — берёт
из исходной части файла. Она физически та же самая.

Что это даёт:

* объекты оригинала невозможно повредить — они не переписываются;
* номера объектов сохраняются, поэтому все ссылки (``/Font``, ``/XObject``,
  ``/Parent``, именованные адресаты) остаются теми же самыми;
* ``/ID``, ``/Info``, XMP, ``/CreationDate``, ``/ModDate``, версия в заголовке
  остаются исходными, пока их не меняют сознательно;
* предыдущие редакции документа сохраняются целиком, включая уже имевшиеся
  инкрементальные слои и подписанные ревизии.

Чего это НЕ даёт: файл заведомо отличается от оригинала — он длиннее, и правка
видна в hex-редакторе как второй ``%%EOF`` и слой дописанных объектов. Если
задача обратная (никаких видимых следов дописывания), нужен режим полной
пересборки из :mod:`pdfedit.saving`.

Ограничения режима:

* зашифрованные документы не поддерживаются: дописанные строки и потоки
  пришлось бы шифровать ключом документа, а это отдельная задача — для таких
  файлов режим отказывает явной ошибкой, вместо того чтобы выдать битый файл;
* линеаризация («быстрый просмотр в вебе») после дописывания перестаёт быть
  достоверной — об этом выдаётся предупреждение.
"""

from __future__ import annotations

import hashlib
import io
import re
import zlib
from dataclasses import dataclass, field

import pikepdf

from .errors import PdfEditError
from .pdfcrypt import DocumentCipher, UnsupportedEncryption, shield_strings

#: Потоки этих типов не сжимаются: XMP по стандарту читается как открытый текст
#: (PDF/A-1 прямо требует несжатый ``/Metadata``), а таблицы ссылок мы собираем
#: сами.
NEVER_COMPRESS = frozenset({"/Metadata", "/XRef"})


class IncrementalUpdateError(PdfEditError):
    """Инкрементальное обновление для этого файла невозможно."""


@dataclass
class IncrementalReport:
    """Что было дописано в конец файла."""

    #: номера объектов новых редакций (уже существовавших в оригинале)
    rewritten: list[tuple[int, int]] = field(default_factory=list)
    #: номера объектов, которых в оригинале не было (например, внедрённый шрифт)
    added: list[tuple[int, int]] = field(default_factory=list)
    #: "table" — классическая таблица ``xref``, "stream" — поток ``/XRef``
    xref_kind: str = "table"
    #: насколько вырос файл, байт
    bytes_added: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def object_count(self) -> int:
        return len(self.rewritten) + len(self.added)

    def describe(self) -> str:
        lines = [
            f"дописано объектов: {self.object_count} "
            f"(изменено {len(self.rewritten)}, добавлено {len(self.added)})",
            f"таблица ссылок: {'поток /XRef' if self.xref_kind == 'stream' else 'xref'}",
            f"прирост файла: {self.bytes_added} байт",
        ]
        if self.rewritten:
            listed = ", ".join(f"{num} {gen} R" for num, gen in self.rewritten[:12])
            more = "" if len(self.rewritten) <= 12 else f" и ещё {len(self.rewritten) - 12}"
            lines.append(f"новые редакции: {listed}{more}")
        for warning in self.warnings:
            lines.append(f"! {warning}")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# Разбор исходного файла
# ----------------------------------------------------------------------

_STARTXREF_RE = re.compile(rb"startxref\s+(\d+)")


def header_offset(data: bytes) -> int:
    """Смещение ``%PDF-`` от начала файла.

    Спецификация разрешает мусор перед заголовком (так делают, например,
    самораспаковывающиеся архивы с PDF внутри). Смещения в таблице ссылок в
    таких файлах отсчитываются от ``%PDF-``, а не от начала файла, и наши
    записи должны считаться так же, иначе ссылки разъедутся.
    """
    position = data.find(b"%PDF-", 0, 4096)
    return position if position > 0 else 0


def last_startxref(data: bytes) -> int:
    """Смещение последней таблицы ссылок, записанное в конце файла."""
    tail_start = max(0, len(data) - 2048)
    matches = list(_STARTXREF_RE.finditer(data, tail_start))
    if not matches:
        # Хвост мог быть длиннее 2 КБ (много %%EOF) — ищем по всему файлу
        matches = list(_STARTXREF_RE.finditer(data))
    if not matches:
        raise IncrementalUpdateError(
            "в файле нет записи startxref: таблица ссылок восстанавливается "
            "разбором, дописывать к такому файлу нельзя — сохраните обычным способом"
        )
    return int(matches[-1].group(1))


def xref_kind(data: bytes, offset: int, base: int = 0) -> str:
    """Различает классическую таблицу ``xref`` и поток ``/XRef``.

    Тип новой секции обязан совпадать с типом старой: ридер, не понимающий
    потоков ссылок (PDF ниже 1.5), не должен внезапно встретить их в файле,
    который до правки читал.
    """
    absolute = base + offset
    if absolute < 0 or absolute >= len(data):
        raise IncrementalUpdateError(
            f"startxref указывает за пределы файла ({absolute} при длине {len(data)})"
        )
    head = data[absolute : absolute + 64].lstrip(b"\r\n \t")
    if head.startswith(b"xref"):
        return "table"
    if re.match(rb"\d+\s+\d+\s+obj", head):
        return "stream"
    raise IncrementalUpdateError(
        "по смещению startxref нет ни таблицы xref, ни объекта потока ссылок — "
        "файл собран нестандартно, дописывать к нему небезопасно"
    )


# ----------------------------------------------------------------------
# Поиск изменившихся объектов
# ----------------------------------------------------------------------

def _fingerprint(obj) -> bytes:
    """Отпечаток объекта для сравнения «было/стало».

    Для обычных объектов это их запись в файл: ``unparse(resolved=True)``
    разворачивает верхний уровень, но вложенные ссылки оставляет ссылками —
    ровно то, что нужно (иначе сравнение утянуло бы за собой весь документ).

    У потоков сравниваются словарь без ``/Length`` и сами данные в том виде,
    в каком они лежат в файле. ``/Length`` исключён потому, что он производный:
    он меняется вслед за данными и отдельного смысла не несёт.
    """
    if isinstance(obj, pikepdf.Stream):
        head = pikepdf.Dictionary(obj.stream_dict)
        if "/Length" in head:
            del head["/Length"]
        return head.unparse(resolved=True) + b"|" + hashlib.sha256(obj.read_raw_bytes()).digest()
    return obj.unparse(resolved=True)


def live_objects(pdf: pikepdf.Pdf) -> dict[tuple[int, int], pikepdf.Object]:
    """Все объекты, до которых можно дойти по ссылкам из трейлера.

    Обход именно по ссылкам, а не по ``Pdf.objects``: во-первых, так в набор
    попадают объекты, созданные уже после открытия документа (внедрённый шрифт,
    новый поток содержимого), во-вторых, ``Pdf.objects`` отдаёт скалярные
    объекты как обычные числа Python, у которых номера уже не спросишь.
    """
    found: dict[tuple[int, int], pikepdf.Object] = {}
    stack: list[pikepdf.Object] = [pdf.trailer]

    while stack:
        obj = stack.pop()
        if not isinstance(obj, pikepdf.Object):
            continue
        try:
            objgen = obj.objgen
        except Exception:
            continue
        if objgen != (0, 0):
            # Повторный приход по ссылке — единственный источник петель:
            # прямой объект физически вложен в родителя и назад ссылаться не может
            if objgen in found:
                continue
            found[objgen] = obj

        try:
            if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
                # Именно items(), а не values(): последнего нет в pikepdf 9
                stack.extend(value for _key, value in obj.items())
            elif isinstance(obj, pikepdf.Array):
                stack.extend(list(obj))
        except Exception:
            # Битую ветку пропускаем: она и так попадёт в отчёт проверки
            continue
    return found


def changed_objects(
    pdf: pikepdf.Pdf,
    original_bytes: bytes,
    skip: set[tuple[int, int]] | None = None,
) -> tuple[dict[tuple[int, int], pikepdf.Object], list[tuple[int, int]], list[tuple[int, int]]]:
    """Сравнивает документ в памяти с исходным файлом объект за объектом.

    Возвращает ``(что писать, изменённые, добавленные)``. Ничего не
    «помечается грязным» по ходу правки: набор считается сравнением, поэтому
    он не зависит от того, через сколько модулей прошла правка.

    ``skip`` — объекты, которые уже приведены в нужный вид другим способом
    (например, правкой на месте): побайтово они отличаются от того, что лежит
    в памяти, но по содержанию уже верны, и дописывать их не нужно.
    """
    to_write: dict[tuple[int, int], pikepdf.Object] = {}
    rewritten: list[tuple[int, int]] = []
    added: list[tuple[int, int]] = []
    skip = skip or set()

    with pikepdf.open(io.BytesIO(original_bytes)) as original:
        for objgen, obj in live_objects(pdf).items():
            if objgen in skip:
                continue
            base = original.get_object(objgen)
            if base is None or not isinstance(base, pikepdf.Object):
                # Объекта не было вовсе (или он был скаляром, а стал словарём)
                to_write[objgen] = obj
                added.append(objgen)
                continue
            try:
                same = _fingerprint(obj) == _fingerprint(base)
            except Exception:
                same = False
            if not same:
                to_write[objgen] = obj
                rewritten.append(objgen)

    rewritten.sort()
    added.sort()
    return to_write, rewritten, added


# ----------------------------------------------------------------------
# Запись объектов
# ----------------------------------------------------------------------

def _level_for(wanted_header: bytes, default: int) -> int:
    """Уровень сжатия, дающий такой же заголовок zlib, как у оригинала."""
    if len(wanted_header) < 2:
        return default
    from .inplace import zlib_header_levels

    levels = zlib_header_levels().get(bytes(wanted_header[:2]))
    return levels[0] if levels else default


def _is_plain_flate(value) -> bool:
    """Единственный фильтр потока — Flate (в любой из двух записей ``/Filter``)."""
    if value is None:
        return False
    if isinstance(value, pikepdf.Array):
        return len(value) == 1 and str(value[0]) == "/FlateDecode"
    return str(value) == "/FlateDecode"


def _match_compression(raw: bytes, wanted_header: bytes, obj: pikepdf.Stream) -> bytes:
    """Пережимает данные так, чтобы заголовок zlib совпал с исходным.

    Записывая новую редакцию потока, pikepdf жмёт его своим уровнем. Уровень
    виден в двух битах заголовка zlib, и сравнение оригинала с результатом
    сразу показывает: этот поток пересжат чужой программой. Здесь данные
    пережимаются тем уровнем, что стоял в исходном заголовке.
    """
    if len(wanted_header) < 2 or len(raw) < 2:
        return raw
    if raw[:2] == wanted_header[:2]:
        return raw
    from .inplace import zlib_header_levels

    levels = zlib_header_levels().get(bytes(wanted_header[:2]))
    if not levels:
        return raw
    try:
        plain = obj.read_bytes()
    except Exception:
        return raw
    for level in levels:
        candidate = zlib.compress(plain, level)
        if candidate[:2] == wanted_header[:2]:
            return candidate
    return raw


def _stream_body(
    obj: pikepdf.Stream,
    objgen: tuple[int, int],
    compress: bool,
    cipher: DocumentCipher | None,
    wanted_header: bytes = b"",
) -> bytes:
    """Сериализует поток: словарь, затем данные ровно как они лягут в файл.

    Данные берутся сырыми (``read_raw_bytes``), то есть уже закодированными
    своим фильтром — так поток переносится без перекодирования. Если фильтра
    нет, а сжатие разрешено, данные пакуются Flate: потоки содержимого после
    правки pikepdf оставляет несжатыми, и без этого шага файл распухал бы.

    Порядок обязателен: сначала фильтр, потом шифрование, и только потом
    ``/Length`` — в файле он означает длину того, что лежит между ``stream`` и
    ``endstream``, то есть уже зашифрованных байтов.
    """
    raw = obj.read_raw_bytes()
    head = pikepdf.Dictionary(obj.stream_dict)
    stream_type = str(head.get("/Type", "")) if "/Type" in head else ""
    if compress and "/Filter" not in head and stream_type not in NEVER_COMPRESS and raw:
        # Уровень берём тот, каким поток был сжат в оригинале: правленый
        # поток pikepdf держит распакованным, и, сжав его «как сильнее»,
        # мы бы поменяли два бита FLEVEL в заголовке — по ним пересжатие
        # чужой программой видно с первого взгляда
        raw = zlib.compress(raw, _level_for(wanted_header, default=9))
        head["/Filter"] = pikepdf.Name("/FlateDecode")
    elif wanted_header and _is_plain_flate(head.get("/Filter")):
        raw = _match_compression(raw, wanted_header, obj)
    if cipher is not None:
        head = shield_strings(head, cipher, objgen)
        raw = cipher.encrypt_stream(objgen, raw, stream_type)
    # /Length в файле обязан быть прямым числом: косвенный требовал бы дописать
    # ещё и объект длины, а он в оригинале мог лежать в объектном потоке
    head["/Length"] = len(raw)
    return head.unparse(resolved=True) + b"\nstream\n" + raw + b"\nendstream"


def _object_record(
    objgen: tuple[int, int],
    obj: pikepdf.Object,
    compress: bool,
    cipher: DocumentCipher | None = None,
    wanted_header: bytes = b"",
) -> bytes:
    """Полная запись объекта: ``N G obj … endobj``."""
    number, generation = objgen
    if isinstance(obj, pikepdf.Stream):
        body = _stream_body(obj, objgen, compress, cipher, wanted_header)
    else:
        if cipher is not None:
            obj = shield_strings(obj, cipher, objgen)
        body = obj.unparse(resolved=True)
    return b"%d %d obj\n" % (number, generation) + body + b"\nendobj\n"


# ----------------------------------------------------------------------
# Таблицы ссылок
# ----------------------------------------------------------------------

def _index_ranges(numbers: list[int]) -> list[tuple[int, int]]:
    """Группирует номера объектов в подсекции подряд идущих."""
    ranges: list[tuple[int, int]] = []
    for number in sorted(numbers):
        if ranges and number == ranges[-1][0] + ranges[-1][1]:
            start, count = ranges[-1]
            ranges[-1] = (start, count + 1)
        else:
            ranges.append((number, 1))
    return ranges


def _trailer_reference(pdf: pikepdf.Pdf, key: str) -> bytes | None:
    """Ссылка вида ``12 0 R`` на объект из трейлера, если он там есть."""
    value = pdf.trailer.get(key)
    if value is None:
        return None
    try:
        number, generation = value.objgen
    except Exception:
        return None
    if (number, generation) == (0, 0):
        # Прямой словарь в трейлере — редкость, но записать его тоже можно
        return value.unparse(resolved=True)
    return b"%d %d R" % (number, generation)


def _document_id(pdf: pikepdf.Pdf) -> bytes | None:
    raw_id = pdf.trailer.get("/ID")
    if raw_id is None or len(raw_id) < 2:
        return None
    first = bytes(raw_id[0]).hex().upper().encode("ascii")
    second = bytes(raw_id[1]).hex().upper().encode("ascii")
    return b"[ <" + first + b"> <" + second + b"> ]"


def _xref_table(
    entries: dict[int, tuple[int, int]], size: int, prev: int, trailer_extra: bytes
) -> bytes:
    """Классическая таблица ссылок и трейлер за ней.

    Запись занимает ровно 20 байт (``ISO 32000-1``, 7.5.4) — иначе ридеры,
    вычисляющие позицию записи умножением, промахнутся.
    """
    out = bytearray(b"xref\n")
    for start, count in _index_ranges(list(entries)):
        out += b"%d %d\n" % (start, count)
        for number in range(start, start + count):
            offset, generation = entries[number]
            out += b"%010d %05d n \n" % (offset, generation)
    out += b"trailer\n<< /Size %d /Prev %d" % (size, prev) + trailer_extra + b" >>\n"
    return bytes(out)


def _xref_stream(
    entries: dict[int, tuple[int, int]],
    size: int,
    prev: int,
    trailer_extra: bytes,
    self_number: int,
    self_offset: int,
) -> bytes:
    """Поток ссылок ``/Type /XRef`` вместе с записью о самом себе."""
    entries = dict(entries)
    entries[self_number] = (self_offset, 0)

    max_offset = max(offset for offset, _gen in entries.values())
    offset_width = max(4, (max_offset.bit_length() + 7) // 8)

    payload = bytearray()
    index: list[int] = []
    for start, count in _index_ranges(list(entries)):
        index += [start, count]
        for number in range(start, start + count):
            offset, generation = entries[number]
            payload += b"\x01"                                    # тип 1: обычный объект
            payload += offset.to_bytes(offset_width, "big")
            payload += generation.to_bytes(2, "big")

    data = zlib.compress(bytes(payload), 9)
    index_text = b" ".join(b"%d" % value for value in index)
    head = (
        b"<< /Type /XRef /Size %d /Prev %d /Index [ %s ] /W [ 1 %d 2 ]"
        % (size, prev, index_text, offset_width)
        + trailer_extra
        + b" /Filter /FlateDecode /Length %d >>" % len(data)
    )
    return (
        b"%d 0 obj\n" % self_number + head + b"\nstream\n" + data + b"\nendstream\nendobj\n"
    )


# ----------------------------------------------------------------------
# Сборка обновления
# ----------------------------------------------------------------------

def _preflight(pdf: pikepdf.Pdf, data: bytes) -> tuple[DocumentCipher | None, list[str]]:
    """Проверяет применимость режима, готовит шифратор и собирает предупреждения."""
    if not data.startswith(b"%PDF-") and header_offset(data) == 0:
        raise IncrementalUpdateError("файл не начинается с %PDF- — это не PDF")

    warnings: list[str] = []
    cipher: DocumentCipher | None = None
    if pdf.is_encrypted:
        try:
            cipher = DocumentCipher.from_pdf(pdf)
        except UnsupportedEncryption as exc:
            raise IncrementalUpdateError(
                f"{exc}. Сохраните файл обычным способом (без --incremental)"
            ) from exc
        if cipher is not None:
            warnings.append(
                f"документ защищён ({cipher.describe()}); дописанные объекты "
                f"зашифрованы тем же ключом, защита файла сохранена"
            )

    if pdf.is_linearized:
        warnings.append(
            "документ линеаризован («быстрый просмотр в вебе»): после дописывания "
            "слоя правок эта разметка перестаёт быть достоверной. На чтение это не "
            "влияет, но строгая проверка линеаризации (qpdf --check) укажет на неё"
        )
    return cipher, warnings


def build_update(
    pdf: pikepdf.Pdf,
    original_bytes: bytes,
    compress: bool = True,
    skip: set[tuple[int, int]] | None = None,
) -> tuple[bytes, IncrementalReport]:
    """Собирает готовый файл: исходные байты плюс слой правок в конце."""
    cipher, warnings = _preflight(pdf, original_bytes)
    base = header_offset(original_bytes)
    prev = last_startxref(original_bytes)
    kind = xref_kind(original_bytes, prev, base)

    to_write, rewritten, added = changed_objects(pdf, original_bytes, skip=skip)
    report = IncrementalReport(
        rewritten=rewritten, added=added, xref_kind=kind, warnings=warnings
    )
    if not to_write:
        # Ни один объект не изменился — дописывать нечего, и портить файл
        # пустым слоем незачем
        return original_bytes, report

    out = bytearray(original_bytes)
    if not out.endswith(b"\n"):
        out += b"\n"

    # Заголовки zlib исходных потоков: новая редакция обязана лечь тем же
    # уровнем сжатия, иначе пересжатие видно по двум битам заголовка
    from .traces import stream_spans

    try:
        headers = {
            objgen: original_bytes[start : start + 2]
            for objgen, (start, _length) in stream_spans(original_bytes).items()
        }
    except Exception:
        headers = {}

    entries: dict[int, tuple[int, int]] = {}
    for objgen in sorted(to_write):
        number, generation = objgen
        entries[number] = (len(out) - base, generation)
        out += _object_record(
            objgen, to_write[objgen], compress, cipher, headers.get(objgen, b"")
        )

    # Ссылки на корень, сведения о документе и словарь защиты переносятся из
    # трейлера как есть. ``/Encrypt`` обязателен: без него ридер сочтёт файл
    # незашифрованным и не сможет прочитать ни одного объекта — ни старого,
    # ни дописанного
    extra = bytearray()
    for key in ("/Root", "/Info", "/Encrypt"):
        reference = _trailer_reference(pdf, key)
        if reference is not None:
            extra += b" " + key.encode("ascii") + b" " + reference
    doc_id = _document_id(pdf)
    if doc_id is not None:
        extra += b" /ID " + doc_id

    highest = max(number for number, _gen in to_write)
    try:
        declared_size = int(pdf.trailer.get("/Size", 0))
    except Exception:
        declared_size = 0
    size = max(declared_size, highest + 1)

    if kind == "table":
        xref_offset = len(out) - base
        out += _xref_table(entries, size, prev, bytes(extra))
    else:
        # Поток ссылок сам является объектом и занимает следующий свободный номер
        self_number = max(size, highest + 1)
        size = self_number + 1
        xref_offset = len(out) - base
        out += _xref_stream(entries, size, prev, bytes(extra), self_number, xref_offset)

    out += b"startxref\n%d\n%%%%EOF\n" % xref_offset

    result = bytes(out)
    report.bytes_added = len(result) - len(original_bytes)
    return result, report


def save_incremental(
    pdf: pikepdf.Pdf,
    path: str,
    original_bytes: bytes,
    compress: bool = True,
    password: str = "",
) -> IncrementalReport:
    """Записывает файл: копия оригинала плюс дописанный слой правок."""
    data, report = build_update(pdf, original_bytes, compress=compress)
    verify_update(data, original_bytes, pdf, report, password=password)
    with open(path, "wb") as handle:
        handle.write(data)
    return report


def incremental_bytes(
    pdf: pikepdf.Pdf,
    original_bytes: bytes,
    compress: bool = True,
    password: str = "",
) -> bytes:
    """То же, что :func:`save_incremental`, но результат возвращается байтами."""
    data, report = build_update(pdf, original_bytes, compress=compress)
    verify_update(data, original_bytes, pdf, report, password=password)
    return data


def _semantic_fingerprint(obj) -> bytes:
    """Отпечаток по смыслу, а не по способу записи.

    В отличие от :func:`_fingerprint`, здесь у потока сравниваются
    *разжатые* данные, а из словаря убраны поля, описывающие только упаковку
    (``/Length``, ``/Filter``, ``/DecodeParms``). Это нужно при сверке
    записанного файла с документом в памяти: данные при записи могли быть
    сжаты, и совпадать обязано содержимое, а не байты в файле.
    """
    if isinstance(obj, pikepdf.Stream):
        head = pikepdf.Dictionary(obj.stream_dict)
        for key in ("/Length", "/Filter", "/DecodeParms"):
            if key in head:
                del head[key]
        return head.unparse(resolved=True) + b"|" + hashlib.sha256(obj.read_bytes()).digest()
    if isinstance(obj, pikepdf.Object):
        return obj.unparse(resolved=True)
    return repr(obj).encode("utf-8")


def verify_update(
    data: bytes,
    original_bytes: bytes,
    pdf: pikepdf.Pdf,
    report: IncrementalReport,
    password: str = "",
) -> None:
    """Не выпускает наружу файл, который сам же и не читается.

    Смещения в дописанной таблице ссылок и — для защищённых документов —
    шифрование дописанных объектов суть единственные места, где ошибка этого
    модуля осталась бы незамеченной до открытия файла в чужой программе.
    Поэтому результат тут же перечитывается с диска и сверяется: прежние байты
    на месте, число страниц то же, а каждый дописанный объект читается ровно
    тем, чем он был в памяти.
    """
    if not data.startswith(original_bytes):
        raise IncrementalUpdateError(
            "внутренняя ошибка: исходные байты изменились — обновление отменено"
        )
    if data == original_bytes:
        return  # изменений не было, дописывать было нечего

    try:
        with pikepdf.open(io.BytesIO(original_bytes), password=password) as before, \
             pikepdf.open(io.BytesIO(data), password=password) as after:
            if len(before.pages) != len(after.pages):
                raise IncrementalUpdateError(
                    f"внутренняя ошибка: страниц было {len(before.pages)}, "
                    f"стало {len(after.pages)} — обновление отменено"
                )
            for objgen in report.rewritten + report.added:
                written = after.get_object(objgen)
                expected = pdf.get_object(objgen)
                if written is None:
                    raise IncrementalUpdateError(
                        f"внутренняя ошибка: объект {objgen[0]} {objgen[1]} R "
                        f"не читается из готового файла — обновление отменено"
                    )
                if _semantic_fingerprint(written) != _semantic_fingerprint(expected):
                    raise IncrementalUpdateError(
                        f"внутренняя ошибка: объект {objgen[0]} {objgen[1]} R "
                        f"записан не тем, чем был — обновление отменено"
                    )
    except IncrementalUpdateError:
        raise
    except Exception as exc:
        raise IncrementalUpdateError(
            f"внутренняя ошибка: получившийся файл не читается ({exc}) — "
            f"обновление отменено, исходный файл не тронут"
        ) from exc
