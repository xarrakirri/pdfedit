"""Проверка целостности PDF и сверка результата с оригиналом по структуре.

Два независимых вопроса, на которые отвечает этот модуль:

1. **Файл сам по себе цел?** Открывается ли, нет ли ссылок в пустоту, все ли
   потоки распаковываются, разбирается ли содержимое страниц, на месте ли
   шрифты и дескрипторы, что с подписями. Это :func:`check_file`.

2. **Что именно изменилось по сравнению с оригиналом?** Оба документа
   обходятся одновременно от корня по одинаковым путям, и различия называются
   адресом внутри дерева (``/Root/Pages/Kids[0]/Contents``), а не номером
   объекта — номера при полной пересборке меняются, а путь остаётся. Это
   :func:`compare_files`.

Проверка идёт только по локальным файлам и ничего никуда не отправляет.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field

import pikepdf

#: Глубже этого дерево не обходится: защита от вырожденных документов
MAX_DEPTH = 64
#: Сколько расхождений показывать в отчёте, прежде чем свернуть остальные
MAX_LISTED = 40

#: Фильтры, которые pikepdf не разворачивает и не должен: данные под ними —
#: это готовые JPEG, JPEG 2000, факсимильные и JBIG2-изображения, они и лежат
#: в файле в своём формате. Такой поток проверяется чтением как есть; попытка
#: «распаковать» его выдала бы ошибку на совершенно исправном документе.
OPAQUE_FILTERS = frozenset(
    {"/DCTDecode", "/DCT", "/JPXDecode", "/CCITTFaxDecode", "/CCF", "/JBIG2Decode", "/Crypt"}
)

#: Жалобы qpdf, которые означают настоящую поломку, а не мелкую вольность
#: в оформлении файла. Разбор содержимого qpdf молча «чинит», поэтому иначе
#: испорченный поток страницы прошёл бы проверку незамеченным.
CRITICAL_QPDF_MARKS = (
    "parse error",
    "EOF while reading",
    "expected endstream",
    "unable to find",
    "attempting to reconstruct",
    "stream data is invalid",
    "damaged",
)

#: Что меняется от самой правки текста и потому расхождением не считается:
#: содержимое страниц, программы шрифтов и их метрики, таблицы соответствия
#: кодов символам, копии текста для экранных дикторов.
EXPECTED_KEYS = (
    "/Contents", "/ToUnicode", "/FontFile", "/FontFile2", "/FontFile3",
    "/W", "/Widths", "/Length1", "/Length2", "/Length3", "/DW",
    "/ActualText", "/Alt", "/E", "/CIDToGIDMap", "/LastChar", "/FirstChar",
    "/CharSet", "/MissingWidth",
)


# ----------------------------------------------------------------------
# Проверка одного файла
# ----------------------------------------------------------------------

@dataclass
class SignatureInfo:
    """Поле цифровой подписи и то, что она покрывает."""

    field_name: str
    sub_filter: str
    signed_at: str
    byte_range: list[int]
    covers_whole_file: bool
    file_size: int

    def describe(self) -> str:
        covered = sum(self.byte_range[1::2]) if len(self.byte_range) >= 4 else 0
        status = (
            "покрывает файл целиком"
            if self.covers_whole_file
            else f"покрывает {covered} из {self.file_size} байт — файл изменён после подписания"
        )
        name = self.field_name or "без имени"
        stamp = f", подписано {self.signed_at}" if self.signed_at else ""
        return f"поле «{name}» ({self.sub_filter}{stamp}): {status}"


@dataclass
class StructureReport:
    """Итог проверки целостности одного файла."""

    path: str
    opens: bool = False
    page_count: int = 0
    version: str = ""
    encrypted: bool = False
    linearized: bool = False
    object_count: int = 0
    stream_count: int = 0
    font_count: int = 0
    image_count: int = 0
    annotation_count: int = 0
    revisions: int = 1
    has_info: bool = False
    has_xmp: bool = False
    has_id: bool = False
    signatures: list[SignatureInfo] = field(default_factory=list)
    #: критические неисправности — с таким файлом что-то не так
    errors: list[str] = field(default_factory=list)
    #: то, на что стоит обратить внимание, но файл читается
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.opens and not self.errors

    def describe(self) -> str:
        mark = lambda ok: "OK " if ok else "!! "  # noqa: E731
        lines = [f"Файл: {self.path}"]
        if not self.opens:
            lines.append("!! документ не открывается")
            lines += [f"     {problem}" for problem in self.errors]
            return "\n".join(lines)

        lines.append(
            f"     версия {self.version}, страниц {self.page_count}, "
            f"объектов {self.object_count} (потоков {self.stream_count}), "
            f"редакций в файле {self.revisions}"
        )
        lines.append(
            f"     шрифтов {self.font_count}, изображений {self.image_count}, "
            f"аннотаций {self.annotation_count}"
            + (", зашифрован" if self.encrypted else "")
            + (", линеаризован" if self.linearized else "")
        )
        lines.append(
            f"     /Info: {'есть' if self.has_info else 'нет'}, "
            f"XMP: {'есть' if self.has_xmp else 'нет'}, "
            f"/ID: {'есть' if self.has_id else 'нет'}"
        )
        lines.append(f"{mark(not self.errors)}нарушений структуры: {len(self.errors)}")
        for problem in self.errors[:MAX_LISTED]:
            lines.append(f"     {problem}")
        if len(self.errors) > MAX_LISTED:
            lines.append(f"     … и ещё {len(self.errors) - MAX_LISTED}")
        if self.signatures:
            lines.append(f"     цифровых подписей: {len(self.signatures)}")
            for signature in self.signatures:
                lines.append(f"     {signature.describe()}")
        for warning in self.warnings[:MAX_LISTED]:
            lines.append(f"     ! {warning}")
        return "\n".join(lines)


def stream_filters(stream: pikepdf.Stream) -> set[str]:
    """Имена фильтров потока — один фильтр или цепочка."""
    raw = stream.stream_dict.get("/Filter")
    if raw is None:
        return set()
    if isinstance(raw, pikepdf.Array):
        return {str(item) for item in raw}
    return {str(raw)}


def stream_payload(stream: pikepdf.Stream) -> bytes:
    """Содержимое потока: распакованное, а для готовых изображений — как есть.

    Разделение важно для проверки: если поток под обычным фильтром (Flate, LZW)
    не разворачивается — это поломка, о которой надо сказать. А если поток
    под ``/DCTDecode`` не разворачивается — это норма, читать его надо сырым.
    """
    if stream_filters(stream) & OPAQUE_FILTERS:
        return stream.read_raw_bytes()
    return stream.read_bytes()


def _shorten_qpdf_note(note: str) -> str:
    """Убирает из сообщения qpdf перечисление всех потоков страницы."""
    note = re.sub(r"stream \d+ \d+(, stream \d+ \d+)+", "потоки страницы", note)
    return note if len(note) <= 200 else note[:197] + "…"


def _walk_objects(pdf: pikepdf.Pdf) -> dict[tuple[int, int], pikepdf.Object]:
    """Все объекты, достижимые по ссылкам из трейлера."""
    from .incremental import live_objects

    return live_objects(pdf)


def _collect_signatures(pdf: pikepdf.Pdf, file_size: int) -> list[SignatureInfo]:
    """Находит поля подписи и смотрит, какую часть файла они покрывают."""
    signatures: list[SignatureInfo] = []
    acroform = pdf.Root.get("/AcroForm")
    if acroform is None:
        return signatures
    fields = acroform.get("/Fields")
    if fields is None:
        return signatures

    queue = list(fields)
    seen: set[tuple[int, int]] = set()
    while queue:
        field_obj = queue.pop(0)
        if not isinstance(field_obj, pikepdf.Dictionary):
            continue
        objgen = field_obj.objgen
        if objgen != (0, 0):
            if objgen in seen:
                continue
            seen.add(objgen)
        kids = field_obj.get("/Kids")
        if kids is not None:
            queue += list(kids)
        if str(field_obj.get("/FT", "")) != "/Sig":
            continue
        value = field_obj.get("/V")
        if not isinstance(value, pikepdf.Dictionary):
            continue

        byte_range = [int(x) for x in value.get("/ByteRange", [])]
        covered = sum(byte_range[1::2]) if len(byte_range) >= 4 else 0
        end_of_signed = (byte_range[2] + byte_range[3]) if len(byte_range) >= 4 else 0
        signatures.append(
            SignatureInfo(
                field_name=str(field_obj.get("/T", "")).strip("()"),
                sub_filter=str(value.get("/SubFilter", "?")),
                signed_at=str(value.get("/M", "")).strip("()"),
                byte_range=byte_range,
                covers_whole_file=end_of_signed >= file_size and covered > 0,
                file_size=file_size,
            )
        )
    return signatures


def check_file(path: str, password: str = "") -> StructureReport:
    """Проверяет файл на целостность структуры, ничего в нём не меняя."""
    with open(path, "rb") as handle:
        data = handle.read()

    report = StructureReport(path=path)
    header = re.match(rb"%PDF-(\d+\.\d+)", data[:1024])
    report.version = header.group(1).decode("ascii") if header else "?"
    if header is None:
        report.errors.append("файл не начинается с заголовка %PDF-")
    if b"%%EOF" not in data[-2048:]:
        report.warnings.append("в конце файла нет метки %%EOF")
    report.revisions = max(1, data.count(b"startxref"))

    try:
        pdf = pikepdf.open(io.BytesIO(data), password=password)
    except Exception as exc:
        report.errors.append(f"не открывается: {exc}")
        return report

    with pdf:
        report.opens = True
        report.encrypted = pdf.is_encrypted
        report.linearized = pdf.is_linearized
        report.has_id = "/ID" in pdf.trailer
        report.has_info = "/Info" in pdf.trailer
        report.has_xmp = "/Metadata" in pdf.Root

        # Претензии самой библиотеки qpdf: битые смещения, лишние байты и т. п.
        # Одна и та же жалоба приходит по нескольку раз и тянет за собой список
        # всех потоков страницы, поэтому текст сокращается, а повторы убираются.
        try:
            seen_notes: set[str] = set()
            for note in pdf.check_pdf_syntax():
                text = _shorten_qpdf_note(str(note))
                if text in seen_notes:
                    continue
                seen_notes.add(text)
                if any(mark in text for mark in CRITICAL_QPDF_MARKS):
                    report.errors.append(f"qpdf: {text}")
                else:
                    report.warnings.append(f"qpdf: {text}")
        except Exception as exc:
            report.warnings.append(f"qpdf: проверка не выполнена ({exc})")

        try:
            report.page_count = len(pdf.pages)
        except Exception as exc:
            report.errors.append(f"дерево страниц не читается: {exc}")

        objects = _walk_objects(pdf)
        report.object_count = len(objects)

        # Ссылки в пустоту: объект есть в дереве, но в файле его нет
        for objgen, obj in objects.items():
            if obj is None:
                report.errors.append(f"объект {objgen[0]} {objgen[1]} R не найден в файле")
                continue
            if isinstance(obj, pikepdf.Stream):
                report.stream_count += 1
                try:
                    stream_payload(obj)
                except Exception as exc:
                    report.errors.append(
                        f"поток {objgen[0]} {objgen[1]} R не распаковывается: {exc}"
                    )

        _check_pages(pdf, report)
        report.signatures = _collect_signatures(pdf, len(data))
        for signature in report.signatures:
            if not signature.covers_whole_file:
                report.warnings.append(
                    "документ подписан, и подпись не покрывает файл целиком: "
                    "проверяющая программа сообщит, что после подписания документ "
                    "изменяли. Подпись предыдущей редакции при этом остаётся "
                    "проверяемой — см. раздел о подписях в README"
                )

    return report


def _check_pages(pdf: pikepdf.Pdf, report: StructureReport) -> None:
    """Разбирает каждую страницу так же, как это сделает ридер."""
    fonts_seen: set[tuple[int, int]] = set()
    images_seen: set[tuple[int, int]] = set()

    for index, page in enumerate(pdf.pages):
        number = index + 1
        try:
            if str(page.obj.get("/Type", "/Page")) != "/Page":
                report.errors.append(f"страница {number}: /Type не /Page")
            if page.obj.get("/MediaBox") is None and page.obj.get("/Parent") is None:
                report.errors.append(f"страница {number}: нет /MediaBox и наследовать не от кого")
        except Exception as exc:
            report.errors.append(f"страница {number}: словарь не читается ({exc})")
            continue

        # Главная проверка после правки текста: содержимое должно разбираться
        # на инструкции без остатка
        try:
            pikepdf.parse_content_stream(page)
        except Exception as exc:
            report.errors.append(f"страница {number}: содержимое не разбирается ({exc})")

        try:
            resources = page.obj.get("/Resources")
            if resources is not None:
                fonts = resources.get("/Font")
                if fonts is not None:
                    for name, font in fonts.items():
                        objgen = font.objgen if isinstance(font, pikepdf.Object) else (0, 0)
                        if objgen in fonts_seen:
                            continue
                        fonts_seen.add(objgen)
                        _check_font(font, f"страница {number}, шрифт {name}", report)
                xobjects = resources.get("/XObject")
                if xobjects is not None:
                    for _name, xobject in xobjects.items():
                        if str(xobject.get("/Subtype", "")) == "/Image":
                            images_seen.add(xobject.objgen)
        except Exception as exc:
            report.warnings.append(f"страница {number}: ресурсы не читаются ({exc})")

        try:
            annotations = page.obj.get("/Annots")
            if annotations is not None:
                report.annotation_count += len(annotations)
                for annotation in annotations:
                    if not isinstance(annotation, pikepdf.Dictionary):
                        report.errors.append(f"страница {number}: аннотация не словарь")
                    elif "/Subtype" not in annotation:
                        report.errors.append(f"страница {number}: у аннотации нет /Subtype")
        except Exception as exc:
            report.warnings.append(f"страница {number}: аннотации не читаются ({exc})")

    report.font_count = len(fonts_seen)
    report.image_count = len(images_seen)


def _check_font(font: pikepdf.Object, where: str, report: StructureReport) -> None:
    """Проверяет, что шрифт цел: есть подтип, имя и читается программа шрифта."""
    if not isinstance(font, pikepdf.Dictionary):
        report.errors.append(f"{where}: не словарь")
        return
    if "/Subtype" not in font:
        report.errors.append(f"{where}: нет /Subtype")
    subtype = str(font.get("/Subtype", ""))

    descendants = font.get("/DescendantFonts")
    if subtype == "/Type0" and descendants is not None and len(descendants) > 0:
        font = descendants[0]

    descriptor = font.get("/FontDescriptor")
    if descriptor is None:
        return  # стандартные 14 шрифтов дескриптора не имеют — это законно
    for key in ("/FontFile", "/FontFile2", "/FontFile3"):
        program = descriptor.get(key)
        if program is None:
            continue
        try:
            data = stream_payload(program)
        except Exception as exc:
            report.errors.append(f"{where}: {key} не распаковывается ({exc})")
            continue
        if not data:
            report.errors.append(f"{where}: {key} пуст")


# ----------------------------------------------------------------------
# Сравнение двух файлов
# ----------------------------------------------------------------------

@dataclass
class ComparisonReport:
    """Чем результат отличается от оригинала по структуре."""

    original: str
    result: str
    #: расхождения вида «путь: было → стало»
    differences: list[str] = field(default_factory=list)
    #: потоки с изменившимся содержимым (путь → размеры)
    changed_streams: list[str] = field(default_factory=list)
    #: страницы, текст которых изменился
    changed_text_pages: list[int] = field(default_factory=list)
    fonts_equal: bool = True
    images_equal: bool = True
    annotations_equal: bool = True
    metadata_equal: bool = True
    id_equal: bool = True
    page_count_equal: bool = True
    object_numbers_kept: bool = False
    original_bytes_kept: bool = False
    #: длина файла не изменилась — признак правки потоков на месте
    same_length: bool = False
    #: исходные байты тронуты лишь точечно — правка потоков на месте
    patched_in_place: bool = False
    #: сколько байт начала файла отличается от оригинала
    byte_diff: int | None = None
    notes: list[str] = field(default_factory=list)

    @staticmethod
    def _expected(item: str) -> bool:
        """Вызвано ли расхождение самой правкой текста."""
        path, _, rest = item.partition(":")
        # Новый шрифт в ресурсах страницы — след внедрения недостающих глифов:
        # шрифт документа при этом остаётся прежним объектом, рядом добавляется
        # ещё один
        if "/Font/" in path and rest.strip().startswith("добавлен"):
            return True
        tail = path.rsplit("/", 1)[-1].split("[", 1)[0]
        return "/" + tail in EXPECTED_KEYS

    @property
    def unexpected_differences(self) -> list[str]:
        """Расхождения, которых правка текста объяснить не может."""
        return [item for item in self.differences if not self._expected(item)]

    @property
    def expected_differences(self) -> list[str]:
        return [item for item in self.differences if self._expected(item)]

    @property
    def structure_equal(self) -> bool:
        """Совпадает ли всё, кроме следствий самой правки текста."""
        return (
            not self.unexpected_differences and self.images_equal
            and self.annotations_equal and self.page_count_equal
        )

    def describe(self) -> str:
        mark = lambda ok: "OK " if ok else "!! "  # noqa: E731
        unexpected = self.unexpected_differences
        expected = self.expected_differences
        lines = [f"Сверка: {self.original} → {self.result}"]
        lines.append(f"{mark(self.page_count_equal)}число страниц совпадает")
        lines.append(
            f"{mark(not unexpected)}посторонних изменений в дереве объектов: "
            f"{len(unexpected)}"
        )
        for item in unexpected[:MAX_LISTED]:
            lines.append(f"     {item}")
        if len(unexpected) > MAX_LISTED:
            lines.append(f"     … и ещё {len(unexpected) - MAX_LISTED}")
        if expected:
            lines.append(
                f"     от самой правки текста изменилось (ожидаемо): {len(expected)}"
            )
            for item in expected[:MAX_LISTED]:
                lines.append(f"       {item}")
        lines.append(
            f"{mark(self.fonts_equal)}программы шрифтов: "
            + ("те же" if self.fonts_equal else "изменились (подробности ниже)")
        )
        lines.append(
            f"{mark(self.images_equal)}изображения: "
            + ("те же" if self.images_equal else "изменились")
        )
        lines.append(f"{mark(self.annotations_equal)}аннотации на месте")
        lines.append(f"{mark(self.metadata_equal)}метаданные не менялись")
        lines.append(f"{mark(self.id_equal)}идентификатор /ID сохранён")
        # Ниже — две строки о способе записи. Они описывают выбранный режим
        # сохранения, а не дефект, поэтому «пересобран» помечается нейтрально:
        # метка «!!» здесь читалась бы как провал проверки, хотя проверка
        # пройдена
        if self.original_bytes_kept:
            lines.append(
                "OK байты файла: оригинал сохранён целиком, правки дописаны в конец"
            )
        elif self.same_length and self.patched_in_place:
            lines.append(
                f"OK байты файла: длина не изменилась, различий "
                f"{self.byte_diff if self.byte_diff is not None else '?'} байт "
                f"— это правка потоков на месте"
            )
        elif self.patched_in_place:
            lines.append(
                f"OK байты файла: правка потоков на месте (различий "
                f"{self.byte_diff} байт в исходной части), остальное дописано слоем"
            )
        else:
            lines.append(
                "-- байты файла: файл пересобран целиком (так работает режим по "
                "умолчанию; чтобы сохранить исходные байты, нужен --incremental "
                "или --inplace)"
            )
        lines.append(
            "OK номера объектов: те же, ссылки ведут к тем же объектам"
            if self.object_numbers_kept
            else "-- номера объектов: перенумерованы qpdf (неизбежно при пересборке; "
                 "ссылки внутри документа при этом согласованы)"
        )
        if self.changed_streams:
            lines.append(f"     изменённых потоков: {len(self.changed_streams)}")
            for item in self.changed_streams[:MAX_LISTED]:
                lines.append(f"       {item}")
        if self.changed_text_pages:
            pages = ", ".join(str(number) for number in self.changed_text_pages[:20])
            lines.append(f"     страницы с изменённым текстом: {pages}")
        for note in self.notes:
            lines.append(f"     {note}")
        return "\n".join(lines)


@dataclass
class HashReport:
    """Пообъектное сравнение двух файлов по хешам.

    Имеет смысл, когда номера объектов сохранены, — то есть после дописывания
    или правки на месте. После полной пересборки qpdf перенумеровывает объекты,
    и сравнивать по номерам нечего: там работает :func:`compare_files`.
    """

    original: str
    result: str
    comparable: bool = True
    identical: list[tuple[int, int]] = field(default_factory=list)
    changed: list[tuple[tuple[int, int], str]] = field(default_factory=list)
    added: list[tuple[int, int]] = field(default_factory=list)
    removed: list[tuple[int, int]] = field(default_factory=list)
    #: чем объект является в документе: «содержимое страницы 1», «программа
    #: шрифта Arial» и так далее — по номеру объекта это не видно
    roles: dict[tuple[int, int], str] = field(default_factory=dict)
    #: что именно разошлось внутри объекта: изменённые инструкции, строки текста
    details: dict[tuple[int, int], list[str]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.identical) + len(self.changed) + len(self.removed)

    def describe(self) -> str:
        if not self.comparable:
            return (
                "Хеши объектов сравнить нельзя: номера объектов в результате другие "
                "(файл пересобран целиком). Сравнение по номерам имеет смысл только "
                "для дописывания и правки на месте."
            )
        mark = "OK " if not self.removed else "!! "
        lines = [
            f"Хеши объектов: {len(self.identical)} из {self.total} совпадают "
            f"байт в байт",
            f"{mark}объектов исчезло: {len(self.removed)}",
        ]
        if self.changed:
            lines.append(f"     изменено: {len(self.changed)}")
            for objgen, what in self.changed[:MAX_LISTED]:
                role = self.roles.get(objgen)
                title = f"{objgen[0]} {objgen[1]} R"
                if role:
                    title += f" — {role}"
                lines.append(f"       {title}: {what}")
                for detail in self.details.get(objgen, []):
                    lines.append(f"           {detail}")
            if len(self.changed) > MAX_LISTED:
                lines.append(f"       … и ещё {len(self.changed) - MAX_LISTED}")
        if self.added:
            listed = ", ".join(f"{num} {gen} R" for num, gen in self.added[:12])
            lines.append(f"     добавлено объектов: {len(self.added)} ({listed})")
        for objgen in self.removed[:MAX_LISTED]:
            lines.append(f"     ИСЧЕЗ объект {objgen[0]} {objgen[1]} R")
        return "\n".join(lines)


def object_hashes(pdf: pikepdf.Pdf) -> dict[tuple[int, int], tuple[str, str]]:
    """Хеш каждого объекта документа: ``(хеш словаря, хеш данных)``.

    Словарь и данные считаются раздельно, чтобы в отчёте можно было сказать,
    что именно разошлось: описание объекта или его содержимое.
    """
    from .incremental import live_objects

    result: dict[tuple[int, int], tuple[str, str]] = {}
    for objgen, obj in live_objects(pdf).items():
        try:
            if isinstance(obj, pikepdf.Stream):
                head = pikepdf.Dictionary(obj.stream_dict)
                if "/Length" in head:
                    del head["/Length"]
                head_hash = hashlib.sha256(head.unparse(resolved=True)).hexdigest()
                body_hash = hashlib.sha256(stream_payload(obj)).hexdigest()
            else:
                head_hash = hashlib.sha256(obj.unparse(resolved=True)).hexdigest()
                body_hash = ""
        except Exception as exc:
            head_hash, body_hash = f"ошибка: {exc}", ""
        result[objgen] = (head_hash, body_hash)
    return result


def external_risks(pdf: pikepdf.Pdf, data: bytes) -> dict[str, str]:
    """За что документ обычно бракуют строгие внешние проверки.

    Наш :func:`check_file` отвечает на вопрос «файл цел». Внешние проверки —
    veraPDF, Preflight в Acrobat, приёмные системы — спрашивают другое:
    соответствует ли файл профилю (чаще всего PDF/A) и внутренним требованиям
    организации. Файл может быть безупречно целым и всё равно не проходить их.

    Здесь перечислены признаки, на которых такие проверки спотыкаются чаще
    всего. Это не приговор: большинство пунктов — свойства исходного документа,
    и понять, кто виноват, помогает сверка с оригиналом (ключ ``--original``).
    """
    risks: dict[str, str] = {}

    if pdf.is_encrypted:
        risks["encrypted"] = "документ зашифрован — PDF/A это запрещает"

    metadata = pdf.Root.get("/Metadata")
    if metadata is None:
        risks["no-xmp"] = (
            "нет XMP-метаданных (/Metadata) — PDF/A требует их обязательно"
        )
    else:
        try:
            xmp = stream_payload(metadata).decode("utf-8", "replace")
        except Exception:
            xmp = ""
        if "pdfaid" not in xmp:
            risks["no-pdfaid"] = (
                "в XMP нет pdfaid:part — документ не заявляет себя как PDF/A"
            )

    if "/OutputIntents" not in pdf.Root:
        risks["no-output-intent"] = (
            "нет /OutputIntents с цветовым профилем — обязателен для PDF/A"
        )

    if "/ID" not in pdf.trailer:
        risks["no-id"] = "в трейлере нет /ID"

    revisions = data.count(b"%%EOF")
    if revisions > 1:
        risks["revisions"] = (
            f"в файле {revisions} редакции: приёмные системы иногда требуют "
            f"документ без дописанных слоёв"
        )

    not_embedded: list[str] = []
    no_tounicode: list[str] = []
    transparency = False
    for page in pdf.pages:
        resources = page.obj.get("/Resources")
        if resources is None:
            continue
        fonts = resources.get("/Font")
        if fonts is not None:
            for _name, font in fonts.items():
                if not isinstance(font, pikepdf.Dictionary):
                    continue
                base = str(font.get("/BaseFont", "?"))
                target = font
                descendants = font.get("/DescendantFonts")
                if descendants is not None and len(descendants) > 0:
                    target = descendants[0]
                descriptor = target.get("/FontDescriptor")
                embedded = descriptor is not None and any(
                    key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")
                )
                if not embedded and base not in not_embedded:
                    not_embedded.append(base)
                if "/ToUnicode" not in font and base not in no_tounicode:
                    no_tounicode.append(base)
        states = resources.get("/ExtGState")
        if states is not None:
            for _name, state in states.items():
                if not isinstance(state, pikepdf.Dictionary):
                    continue
                if "/SMask" in state and str(state.get("/SMask", "/None")) != "/None":
                    transparency = True
                for key in ("/CA", "/ca"):
                    try:
                        if key in state and float(state[key]) < 1:
                            transparency = True
                    except Exception:
                        pass
        group = page.obj.get("/Group")
        if group is not None and str(group.get("/S", "")) == "/Transparency":
            transparency = True

    if not_embedded:
        risks["fonts-not-embedded"] = (
            "шрифты без внедрения: " + ", ".join(not_embedded[:5])
            + " — PDF/A требует внедрять все"
        )
    if no_tounicode:
        risks["fonts-no-tounicode"] = (
            "шрифты без /ToUnicode: " + ", ".join(no_tounicode[:5])
            + " — текст такими шрифтами извлекается неверно"
        )
    if transparency:
        risks["transparency"] = "используется прозрачность — PDF/A-1 её запрещает"

    return risks


def compare_external_risks(
    original: str, result: str, password: str = ""
) -> tuple[dict[str, str], dict[str, str]]:
    """Делит замечания на «были и в оригинале» и «появились после правки»."""
    with open(original, "rb") as handle:
        original_data = handle.read()
    with open(result, "rb") as handle:
        result_data = handle.read()

    with pikepdf.open(io.BytesIO(original_data), password=password) as before:
        before_risks = external_risks(before, original_data)
    with pikepdf.open(io.BytesIO(result_data), password=password) as after:
        after_risks = external_risks(after, result_data)

    inherited = {key: text for key, text in after_risks.items() if key in before_risks}
    introduced = {key: text for key, text in after_risks.items() if key not in before_risks}
    return inherited, introduced


def object_roles(pdf: pikepdf.Pdf) -> dict[tuple[int, int], str]:
    """Что каждый объект означает в документе.

    Номер объекта сам по себе ничего не говорит: «изменился 12 0 R» — это не
    ответ на вопрос, что изменилось. Здесь дерево обходится один раз и каждому
    номеру ставится в соответствие его роль: содержимое такой-то страницы,
    программа такого-то шрифта, изображение, аннотация и так далее.
    """
    roles: dict[tuple[int, int], str] = {}

    def note(obj, text: str) -> None:
        if isinstance(obj, pikepdf.Object) and obj.objgen != (0, 0):
            roles.setdefault(obj.objgen, text)

    note(pdf.Root, "каталог документа /Root")
    note(pdf.trailer.get("/Info"), "словарь сведений /Info")
    note(pdf.Root.get("/Metadata"), "метаданные XMP")
    note(pdf.Root.get("/AcroForm"), "формы /AcroForm")
    note(pdf.Root.get("/StructTreeRoot"), "дерево структуры документа")

    for index, page in enumerate(pdf.pages):
        number = index + 1
        note(page.obj, f"страница {number}")

        contents = page.obj.get("/Contents")
        if contents is not None:
            items = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
            for position, item in enumerate(items):
                suffix = f", поток {position + 1} из {len(items)}" if len(items) > 1 else ""
                note(item, f"содержимое страницы {number}{suffix}")

        resources = page.obj.get("/Resources")
        if resources is None:
            continue
        note(resources, f"ресурсы страницы {number}")

        fonts = resources.get("/Font")
        if fonts is not None:
            note(fonts, f"набор шрифтов страницы {number}")
            for name, font in fonts.items():
                if not isinstance(font, pikepdf.Dictionary):
                    continue
                base = str(font.get("/BaseFont", name))
                note(font, f"шрифт {base} (ресурс {name})")
                note(font.get("/ToUnicode"), f"таблица /ToUnicode шрифта {base}")
                targets = [font]
                descendants = font.get("/DescendantFonts")
                if descendants is not None and len(descendants) > 0:
                    note(descendants[0], f"составная часть шрифта {base}")
                    targets.append(descendants[0])
                for target in targets:
                    descriptor = target.get("/FontDescriptor")
                    if descriptor is None:
                        continue
                    note(descriptor, f"описание шрифта {base}")
                    for key in ("/FontFile", "/FontFile2", "/FontFile3"):
                        note(descriptor.get(key), f"программа шрифта {base}")

        xobjects = resources.get("/XObject")
        if xobjects is not None:
            note(xobjects, f"набор объектов страницы {number}")
            for name, xobject in xobjects.items():
                # Проверять надо и Stream: изображения и формы — это потоки, а
                # pikepdf.Stream не является pikepdf.Dictionary, из-за чего все
                # картинки раньше назывались формами
                kind = (
                    str(xobject.get("/Subtype", ""))
                    if isinstance(xobject, (pikepdf.Dictionary, pikepdf.Stream))
                    else ""
                )
                what = "изображение" if kind == "/Image" else "форма"
                note(xobject, f"{what} {name} на странице {number}")
                if isinstance(xobject, pikepdf.Stream) and "/SMask" in xobject:
                    note(xobject["/SMask"], f"маска прозрачности {name} на странице {number}")

        states = resources.get("/ExtGState")
        if states is not None:
            note(states, f"графические состояния страницы {number}")
            for name, state in states.items():
                note(state, f"графическое состояние {name} на странице {number}")

        annotations = page.obj.get("/Annots")
        if annotations is not None:
            for position, annotation in enumerate(annotations):
                kind = str(annotation.get("/Subtype", "?")) if isinstance(
                    annotation, pikepdf.Dictionary
                ) else "?"
                note(annotation, f"аннотация {kind} №{position + 1} на странице {number}")

    return roles


def _readable_operand(operand, font=None) -> str:
    """Читаемая запись операнда: текстовые строки — текстом, если получится.

    Строку показа текста нельзя просто раскодировать как UTF-16 или cp1251:
    в составных шрифтах это коды глифов, и осмысленный текст из них получается
    только через кодировку самого шрифта. Поэтому сюда передаётся шрифт,
    действующий на момент инструкции; без него остаётся шестнадцатеричный вид.
    """
    if isinstance(operand, pikepdf.String):
        raw = bytes(operand)
        if font is not None:
            try:
                text = font.decode(raw)
                if text.strip():
                    return f"«{text}»"
            except Exception:
                pass
        try:
            text = raw.decode("utf-8")
            if text.isprintable() and text.strip():
                return f"«{text}»"
        except Exception:
            pass
        return f"<{raw.hex()}>" if len(raw) <= 24 else f"<{raw[:12].hex()}…> ({len(raw)} байт)"
    if isinstance(operand, pikepdf.Array):
        return "[" + " ".join(_readable_operand(item, font) for item in operand) + "]"
    if isinstance(operand, pikepdf.Object):
        return operand.unparse().decode("latin-1", "replace")
    return str(operand)


def _readable_instruction(instruction, font=None) -> str:
    try:
        operands = " ".join(
            _readable_operand(item, font) for item in instruction.operands
        )
        return f"{operands} {instruction.operator}".strip()
    except Exception:
        return "?"


def _fonts_in_effect(instructions: list, fonts: dict) -> list:
    """Для каждой инструкции — шрифт, действующий на этот момент (оператор ``Tf``)."""
    active = []
    current = None
    for instruction in instructions:
        try:
            if str(instruction.operator) == "Tf" and len(instruction.operands) >= 1:
                current = fonts.get(str(instruction.operands[0]))
        except Exception:
            pass
        active.append(current)
    return active


def instruction_changes(
    before: pikepdf.Stream, after: pikepdf.Stream, fonts: dict | None = None
) -> list[str]:
    """Какие именно инструкции потока содержимого изменились."""
    import difflib

    from .streampatch import _key

    try:
        old = list(pikepdf.parse_content_stream(before))
        new = list(pikepdf.parse_content_stream(after))
    except Exception as exc:
        return [f"содержимое не разбирается ({exc})"]

    fonts = fonts or {}
    old_fonts = _fonts_in_effect(old, fonts)
    new_fonts = _fonts_in_effect(new, fonts)

    old_keys = [_key(item) for item in old]
    new_keys = [_key(item) for item in new]
    changes: list[str] = []
    matcher = difflib.SequenceMatcher(a=old_keys, b=new_keys, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        was = [
            _readable_instruction(old[index], old_fonts[index]) for index in range(i1, i2)
        ]
        now = [
            _readable_instruction(new[index], new_fonts[index]) for index in range(j1, j2)
        ]
        if len(was) == len(now):
            # Замена один в один: пара «было → стало» читается лучше всего
            for offset, (before_text, after_text) in enumerate(zip(was, now)):
                changes.append(
                    f"инструкция #{i1 + offset + 1}: {before_text}  →  {after_text}"
                )
        else:
            # Инструкций стало больше или меньше — показываем блоками, иначе
            # попарное сопоставление сдвигается и вводит в заблуждение
            place = f"начиная с инструкции #{i1 + 1}"
            changes.append(f"{place}: было {' | '.join(was) if was else '—'}")
            changes.append(f"{' ' * len(place)}  стало {' | '.join(now) if now else '—'}")
        if len(changes) >= 10:
            changes.append("…")
            return changes
    if not changes:
        changes.append("инструкции те же, различается только упаковка потока")
    return changes


def _page_fonts_for(pdf: pikepdf.Pdf, objgen: tuple[int, int]) -> dict:
    """Шрифты той страницы, чьим содержимым является объект."""
    from .fonts import load_page_fonts

    for page in pdf.pages:
        contents = page.obj.get("/Contents")
        if contents is None:
            continue
        items = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
        if any(isinstance(item, pikepdf.Object) and item.objgen == objgen for item in items):
            try:
                return load_page_fonts(page.obj.get("/Resources"))
            except Exception:
                return {}
    return {}


def _same_value(left, right) -> bool:
    """Сравнение значений целиком, без сокращений.

    Сокращённые для показа записи сравнивать нельзя: два разных массива ширин
    после обрезки выглядят одинаково, и изменение осталось бы незамеченным.
    """
    try:
        return left.unparse(resolved=True) == right.unparse(resolved=True)
    except Exception:
        return repr(left) == repr(right)


def _dictionary_changes(before, after) -> list[str]:
    """Какие ключи словаря разошлись."""
    changes: list[str] = []
    before_keys, after_keys = set(before.keys()), set(after.keys())
    for key in sorted(before_keys - after_keys):
        changes.append(f"ключ {key} пропал")
    for key in sorted(after_keys - before_keys):
        changes.append(f"ключ {key} добавлен: {_describe_value(after[key])}")
    for key in sorted(before_keys & after_keys):
        if key == "/Length":
            continue
        if _same_value(before[key], after[key]):
            continue
        left, right = before[key], after[key]
        if isinstance(left, pikepdf.Array) and isinstance(right, pikepdf.Array):
            # У длинных массивов (ширины глифов, /Index) сокращённая запись
            # обеих сторон выглядит одинаково и ничего не объясняет
            if len(left) != len(right):
                changes.append(f"ключ {key}: массив {len(left)} → {len(right)} элементов")
            else:
                changes.append(f"ключ {key}: массив из {len(left)} элементов изменился")
            continue
        changes.append(f"ключ {key}: {_describe_value(left)} → {_describe_value(right)}")
    return changes[:10]


def describe_change(before, after, role: str, fonts: dict | None = None) -> list[str]:
    """Что именно разошлось внутри объекта — понятным языком."""
    details: list[str] = []
    if isinstance(before, pikepdf.Stream) and isinstance(after, pikepdf.Stream):
        details += _dictionary_changes(before.stream_dict, after.stream_dict)
        if role.startswith("содержимое страницы"):
            details += instruction_changes(before, after, fonts)
        else:
            try:
                was, now = len(stream_payload(before)), len(stream_payload(after))
                if was != now:
                    details.append(f"данные: {was} → {now} байт")
                elif not details:
                    details.append(f"данные того же размера ({was} байт) изменились")
            except Exception as exc:
                details.append(f"данные не читаются ({exc})")
        return details
    if isinstance(before, pikepdf.Dictionary) and isinstance(after, pikepdf.Dictionary):
        return _dictionary_changes(before, after)
    return [f"{_describe_value(before)} → {_describe_value(after)}"]


def compare_object_hashes(
    original: str, result: str, password: str = ""
) -> HashReport:
    """Сверяет два файла объект за объектом по номерам и хешам."""
    report = HashReport(original=original, result=result)
    with pikepdf.open(original, password=password) as before, \
         pikepdf.open(result, password=password) as after:
        before_hashes = object_hashes(before)
        after_hashes = object_hashes(after)

        # Номера объектов должны означать то же самое в обоих файлах: сверяем
        # это по корню и страницам, иначе сравнение бессмысленно
        report.comparable = before.Root.objgen == after.Root.objgen and all(
            page_before.obj.objgen == page_after.obj.objgen
            for page_before, page_after in zip(before.pages, after.pages)
        )
        if not report.comparable:
            return report

        roles = object_roles(before)
        after_roles = object_roles(after)

        for objgen, (head, body) in before_hashes.items():
            if objgen not in after_hashes:
                report.removed.append(objgen)
                report.roles[objgen] = roles.get(objgen, "")
                continue
            new_head, new_body = after_hashes[objgen]
            if (head, body) == (new_head, new_body):
                report.identical.append(objgen)
                continue
            if head != new_head and body != new_body:
                what = "изменились словарь и данные"
            elif head != new_head:
                what = "изменился словарь"
            else:
                what = "изменились данные"
            report.changed.append((objgen, what))

            role = roles.get(objgen) or after_roles.get(objgen, "")
            report.roles[objgen] = role
            try:
                fonts = (
                    _page_fonts_for(before, objgen)
                    if role.startswith("содержимое страницы") else None
                )
                report.details[objgen] = describe_change(
                    before.get_object(objgen), after.get_object(objgen), role, fonts
                )
            except Exception as exc:
                report.details[objgen] = [f"разобрать изменение не удалось ({exc})"]

        report.added = sorted(set(after_hashes) - set(before_hashes))
        for objgen in report.added:
            report.roles[objgen] = after_roles.get(objgen, "")

    report.identical.sort()
    report.changed.sort()
    report.removed.sort()
    return report


def _describe_value(obj) -> str:
    if isinstance(obj, pikepdf.Stream):
        return "поток"
    if isinstance(obj, pikepdf.Object):
        text = obj.unparse(resolved=True).decode("latin-1", "replace")
        return text if len(text) <= 60 else text[:57] + "…"
    return repr(obj)


def _compare_objects(
    left,
    right,
    path: str,
    report: ComparisonReport,
    seen: set[tuple[tuple[int, int], tuple[int, int]]],
    depth: int = 0,
) -> None:
    """Рекурсивно сверяет два поддерева, называя расхождения путём в дереве."""
    if depth > MAX_DEPTH or len(report.differences) > 500:
        return

    left_is_object = isinstance(left, pikepdf.Object)
    right_is_object = isinstance(right, pikepdf.Object)
    if left_is_object and right_is_object:
        pair = (left.objgen, right.objgen)
        if pair != ((0, 0), (0, 0)):
            if pair in seen:
                return
            seen.add(pair)

    if type(left).__name__ != type(right).__name__:
        report.differences.append(f"{path}: разный тип объекта")
        return

    if isinstance(left, pikepdf.Stream) and isinstance(right, pikepdf.Stream):
        _compare_dictionaries(left, right, path, report, seen, depth)
        try:
            before, after = stream_payload(left), stream_payload(right)
        except Exception as exc:
            report.differences.append(f"{path}: поток не распаковывается ({exc})")
            return
        if before != after:
            report.changed_streams.append(
                f"{path}: {len(before)} → {len(after)} байт"
            )
        return

    if isinstance(left, pikepdf.Dictionary) and isinstance(right, pikepdf.Dictionary):
        _compare_dictionaries(left, right, path, report, seen, depth)
        return

    if isinstance(left, pikepdf.Array) and isinstance(right, pikepdf.Array):
        if len(left) != len(right):
            report.differences.append(
                f"{path}: длина массива {len(left)} → {len(right)}"
            )
        for index in range(min(len(left), len(right))):
            _compare_objects(
                left[index], right[index], f"{path}[{index}]", report, seen, depth + 1
            )
        return

    before, after = _describe_value(left), _describe_value(right)
    if before != after:
        report.differences.append(f"{path}: {before} → {after}")


def _compare_dictionaries(left, right, path, report, seen, depth) -> None:
    left_keys, right_keys = set(left.keys()), set(right.keys())
    for key in sorted(left_keys - right_keys):
        report.differences.append(f"{path}{key}: пропал")
    for key in sorted(right_keys - left_keys):
        report.differences.append(f"{path}{key}: добавлен ({_describe_value(right[key])})")
    for key in sorted(left_keys & right_keys):
        if key == "/Length":
            continue  # производное от данных, сравнивать нечего
        if key == "/Parent":
            # Ссылка вверх по дереву: этот узел всё равно будет пройден сверху,
            # а через /Parent путь получался бы бессмысленным
            continue
        _compare_objects(left[key], right[key], f"{path}{key}", report, seen, depth + 1)


def _font_programs(pdf: pikepdf.Pdf) -> dict[str, str]:
    """Отпечатки программ шрифтов по имени ``/BaseFont``."""
    from .incremental import live_objects

    programs: dict[str, str] = {}
    for obj in live_objects(pdf).values():
        if not isinstance(obj, pikepdf.Dictionary):
            continue
        if str(obj.get("/Type", "")) != "/FontDescriptor":
            continue
        name = str(obj.get("/FontName", "?"))
        for key in ("/FontFile", "/FontFile2", "/FontFile3"):
            program = obj.get(key)
            if program is None:
                continue
            try:
                digest = hashlib.sha256(stream_payload(program)).hexdigest()[:16]
            except Exception:
                digest = "не читается"
            programs[f"{name}{key}"] = digest
    return programs


def _image_digests(pdf: pikepdf.Pdf) -> dict[str, str]:
    """Отпечатки изображений: имя ресурса на странице → хеш сырых данных."""
    digests: dict[str, str] = {}
    for index, page in enumerate(pdf.pages):
        resources = page.obj.get("/Resources")
        if resources is None:
            continue
        xobjects = resources.get("/XObject")
        if xobjects is None:
            continue
        for name, xobject in xobjects.items():
            if str(xobject.get("/Subtype", "")) != "/Image":
                continue
            try:
                digest = hashlib.sha256(xobject.read_raw_bytes()).hexdigest()[:16]
            except Exception:
                digest = "не читается"
            digests[f"с.{index + 1}{name}"] = digest
    return digests


def _annotation_summary(pdf: pikepdf.Pdf) -> list[str]:
    summary: list[str] = []
    for index, page in enumerate(pdf.pages):
        annotations = page.obj.get("/Annots")
        if annotations is None:
            continue
        for annotation in annotations:
            if not isinstance(annotation, pikepdf.Dictionary):
                continue
            rect = annotation.get("/Rect")
            coordinates = (
                ",".join(f"{float(value):.1f}" for value in rect) if rect is not None else "?"
            )
            summary.append(
                f"с.{index + 1} {annotation.get('/Subtype', '?')} [{coordinates}]"
            )
    return summary


def _page_texts(path: str) -> list[str] | None:
    """Текст страниц глазами стороннего ридера (PyMuPDF)."""
    try:
        from .mupdf import fitz
    except Exception:
        return None
    try:
        with fitz.open(path) as document:
            return [page.get_text() for page in document]
    except Exception:
        return None


def compare_files(
    original: str, result: str, password: str = "", compare_text: bool = True
) -> ComparisonReport:
    """Сверяет два файла по структуре объектов, шрифтам, картинкам и тексту."""
    report = ComparisonReport(original=original, result=result)

    with open(original, "rb") as handle:
        original_data = handle.read()
    with open(result, "rb") as handle:
        result_data = handle.read()
    report.original_bytes_kept = result_data.startswith(original_data)
    report.same_length = len(result_data) == len(original_data)
    if not report.original_bytes_kept and len(result_data) >= len(original_data):
        # Считаем расхождение по началу результата: если тронуты единицы
        # процентов байт, это правка потоков на месте, а не пересборка. Без
        # этого гибридный случай (часть на месте, остальное дописано слоем)
        # выглядел бы в отчёте как полностью переписанный файл
        report.byte_diff = sum(
            1 for before, after in zip(original_data, result_data) if before != after
        )
        report.patched_in_place = report.byte_diff < max(64, len(original_data) // 5)

    with pikepdf.open(io.BytesIO(original_data), password=password) as before, \
         pikepdf.open(io.BytesIO(result_data), password=password) as after:
        report.page_count_equal = len(before.pages) == len(after.pages)
        if not report.page_count_equal:
            report.differences.append(
                f"число страниц: {len(before.pages)} → {len(after.pages)}"
            )

        seen: set[tuple[tuple[int, int], tuple[int, int]]] = set()
        # Сначала страницы по порядку — тогда расхождения называются понятным
        # адресом («страница 3/Contents»), а не первым попавшимся путём в дереве,
        # каким страница оказалась достижима (например, через дерево имён)
        for index, (page_before, page_after) in enumerate(zip(before.pages, after.pages)):
            _compare_objects(
                page_before.obj, page_after.obj, f"страница {index + 1}", report, seen
            )
        _compare_objects(before.Root, after.Root, "/Root", report, seen)

        # Номера объектов: сохранены ли ссылки на те же объекты
        report.object_numbers_kept = before.Root.objgen == after.Root.objgen and all(
            page_before.obj.objgen == page_after.obj.objgen
            for page_before, page_after in zip(before.pages, after.pages)
        )

        before_fonts, after_fonts = _font_programs(before), _font_programs(after)
        report.fonts_equal = before_fonts == after_fonts
        for name in sorted(set(before_fonts) | set(after_fonts)):
            if before_fonts.get(name) != after_fonts.get(name):
                report.notes.append(
                    f"шрифт {name}: {before_fonts.get(name, 'нет')} → "
                    f"{after_fonts.get(name, 'нет')}"
                )

        before_images, after_images = _image_digests(before), _image_digests(after)
        report.images_equal = before_images == after_images
        for name in sorted(set(before_images) | set(after_images)):
            if before_images.get(name) != after_images.get(name):
                report.notes.append(f"изображение {name} изменилось")

        before_annots, after_annots = _annotation_summary(before), _annotation_summary(after)
        report.annotations_equal = before_annots == after_annots
        if not report.annotations_equal:
            report.notes.append(
                f"аннотаций было {len(before_annots)}, стало {len(after_annots)}"
            )

        report.id_equal = (
            [bytes(x) for x in before.trailer.get("/ID", [])]
            == [bytes(x) for x in after.trailer.get("/ID", [])]
        )

    from .saving import verify as verify_metadata

    metadata = verify_metadata(original, result)
    report.metadata_equal = not metadata.info_diff and not metadata.xmp_diff
    for key, (was, now) in sorted(metadata.info_diff.items()):
        report.notes.append(f"метаданные {key}: {was!r} → {now!r}")

    if compare_text:
        before_text, after_text = _page_texts(original), _page_texts(result)
        if before_text is not None and after_text is not None:
            for index in range(min(len(before_text), len(after_text))):
                if before_text[index] != after_text[index]:
                    report.changed_text_pages.append(index + 1)
        else:
            report.notes.append("текст страниц не сверялся: PyMuPDF недоступен")

    return report
