"""Сохранение результата без лишних следов правки.

Выходной файл всегда создаётся заново, целиком (никаких «инкрементальных
обновлений», по которым правка видна невооружённым глазом в hex-редакторе).
Чтобы новый файл не отличался от исходного ничем, кроме самого текста,
воспроизводятся структурные признаки оригинала:

* версия PDF в заголовке;
* наличие или отсутствие сжатых объектных потоков (``/ObjStm``);
* линеаризация («быстрый просмотр в вебе»);
* идентификатор документа ``/ID`` в трейлере;
* словарь ``/Info`` и XMP — если пользователь не менял их сознательно.

Отдельно подавляется привычка библиотек подписывать свою работу: pikepdf по
умолчанию прописывает себя в ``pdf:Producer`` и ``xmp:CreatorTool``.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pikepdf


@dataclass
class SourceProfile:
    """Структурные признаки исходного файла, которые нужно воспроизвести."""

    version: str
    has_object_streams: bool
    linearized: bool
    encrypted: bool
    doc_id: tuple[bytes, bytes] | None
    has_xmp: bool
    #: что пришлось разменять при воспроизведении защиты документа
    encryption_notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        bits = [f"версия {self.version}"]
        bits.append("объектные потоки: " + ("да" if self.has_object_streams else "нет"))
        bits.append("линеаризован: " + ("да" if self.linearized else "нет"))
        bits.append("XMP: " + ("есть" if self.has_xmp else "нет"))
        if self.encrypted:
            bits.append("зашифрован")
        return ", ".join(bits)


def profile_source(original_bytes: bytes) -> SourceProfile:
    """Определяет структурные признаки исходного файла по его байтам."""
    header = re.match(rb"%PDF-(\d+\.\d+)", original_bytes[:1024])
    version = header.group(1).decode("ascii") if header else "1.7"

    # Признаки ищем в исходных байтах: после разбора часть из них теряется
    has_object_streams = b"/ObjStm" in original_bytes
    linearized = b"/Linearized" in original_bytes[:4096]

    encrypted = False
    doc_id: tuple[bytes, bytes] | None = None
    has_xmp = False
    try:
        with pikepdf.open(io.BytesIO(original_bytes)) as pdf:
            encrypted = pdf.is_encrypted
            raw_id = pdf.trailer.get("/ID")
            if raw_id is not None and len(raw_id) >= 2:
                doc_id = (bytes(raw_id[0]), bytes(raw_id[1]))
            has_xmp = "/Metadata" in pdf.Root
    except Exception:
        pass

    return SourceProfile(
        version=version,
        has_object_streams=has_object_streams,
        linearized=linearized,
        encrypted=encrypted,
        doc_id=doc_id,
        has_xmp=has_xmp,
    )


def _save_options(
    profile: SourceProfile,
    preserve_id: bool,
    mirror_structure: bool,
    linearize: bool | None,
    compress: bool,
) -> dict:
    options: dict = {
        # Содержимое не переписываем «канонически»: чем меньше отличий, тем лучше
        "normalize_content": False,
        "compress_streams": compress,
        # Не даём библиотеке править версию PDF в XMP
        "fix_metadata_version": False,
        # PDF/A-обвязку добавляем, только если она была в оригинале
        "preserve_pdfa": profile.has_xmp,
        "force_version": profile.version if mirror_structure else None,
    }
    # ``preserve`` сохраняет ту же раскладку объектных потоков, что была в
    # оригинале. Прежде здесь стоял ``generate``, который перепаковывает
    # документ по-своему: на 12-страничном файле из Word объектных потоков
    # становилось 32 вместо семи, а кое-где они, наоборот, пропадали. На чтение
    # это не влияет, но структура файла — такой же различимый признак
    # обработки, как и метаданные, и совпадать она должна тоже. Разница в
    # размере — порядка полутора килобайт.
    options["object_stream_mode"] = pikepdf.ObjectStreamMode.preserve

    should_linearize = profile.linearized if linearize is None else linearize
    if should_linearize:
        options["linearize"] = True
    if options["force_version"] is None:
        options.pop("force_version")
    return options


def encryption_like(
    pdf: pikepdf.Pdf, owner_password: str = ""
) -> tuple[object | None, list[str]]:
    """Воспроизводит защиту документа при полной пересборке.

    Возвращает ``(параметры шифрования, предупреждения)``. Права доступа
    (``/P``), алгоритм и длина ключа берутся из оригинала, поэтому файл
    останется защищённым тем же способом.

    Чего воспроизвести НЕЛЬЗЯ: владельческий пароль. В файле он хранится не сам
    по себе, а в виде проверочного значения ``/O``, из которого исходный пароль
    не восстанавливается. Если своего пароля не задать, у результата он будет
    пустым: права формально останутся прежними, но снять их сможет кто угодно.
    Если такой размен не годится, сохраняйте инкрементально — там словарь
    ``/Encrypt`` остаётся исходным, вместе с паролем.
    """
    if not pdf.is_encrypted:
        return None, []

    info = pdf.encryption
    method = str(info.stream_method).rsplit(".", 1)[-1].lower()
    revision = int(info.R)
    warnings = [
        "защита документа воспроизведена: алгоритм, длина ключа и права те же",
        "идентификатор /ID при этом меняется: из него выводится ключ шифрования, "
        "и подменить его в готовом файле нельзя — документ перестал бы открываться",
    ]
    if owner_password:
        warnings.append(
            "владельческий пароль задан вами: исходный восстановить нельзя, "
            "в файле он хранится только в виде проверочного значения /O"
        )
    else:
        warnings.append(
            "владельческий пароль пуст: права записаны те же, но снять их сможет "
            "кто угодно. Задайте свой ключом --owner-password, если это важно"
        )
    if not pdf.allow.accessibility and revision >= 4:
        warnings.append(
            "запрет на извлечение текста для экранных дикторов не воспроизводится: "
            "начиная с R4 qpdf всегда разрешает его (в PDF 2.0 этот бит отменён)"
        )

    try:
        encryption = pikepdf.Encryption(
            owner=owner_password,
            user="",
            R=revision,
            allow=pdf.allow,
            aes=method in ("aes", "aesv3"),
            metadata=True,
        )
    except ValueError as exc:
        if "PDFDocEncoding" in str(exc):
            return None, [
                f"пароль не записывается в документ этой версии (R{revision}): "
                f"там допустима только латиница и знаки PDFDocEncoding. "
                f"Файл будет сохранён без защиты"
            ]
        return None, [f"защиту воспроизвести не удалось ({exc}): файл будет открытым"]
    except Exception as exc:
        return None, [f"защиту воспроизвести не удалось ({exc}): файл будет открытым"]
    return encryption, warnings


def _restore_document_id(pdf: pikepdf.Pdf, profile: SourceProfile) -> None:
    """Возвращает в трейлер исходный ``/ID``.

    ``/ID`` — пара строк, вторая из которых по спецификации меняется при каждом
    изменении файла. Сохранение исходного значения избавляет от расхождения
    между ``/ID`` документа и ссылками на него во внешних системах.
    """
    if profile.doc_id is None:
        return
    pdf.trailer["/ID"] = pikepdf.Array(
        [pikepdf.String(profile.doc_id[0]), pikepdf.String(profile.doc_id[1])]
    )


def save_clean(
    pdf: pikepdf.Pdf,
    path: str,
    original_bytes: bytes,
    preserve_id: bool = True,
    mirror_structure: bool = True,
    linearize: bool | None = None,
    compress: bool = True,
    keep_encryption: bool = False,
    owner_password: str = "",
) -> SourceProfile:
    """Сохраняет документ в новый файл, воспроизводя структуру оригинала."""
    profile = profile_source(original_bytes)
    if preserve_id:
        _restore_document_id(pdf, profile)
    options = _save_options(profile, preserve_id, mirror_structure, linearize, compress)
    if preserve_id:
        # qpdf генерирует новый /ID, если его не попросить об обратном
        options["deterministic_id"] = False
    if keep_encryption:
        encryption, notes = encryption_like(pdf, owner_password)
        profile.encryption_notes = notes
        if encryption is not None:
            options["encryption"] = encryption
            # Правка /ID в готовом файле требует его перечитать, а зашифрованный
            # файл после такой правки уже не откроется: ключ считается от /ID
            options.pop("deterministic_id", None)
            preserve_id = False
    pdf.save(path, **options)
    if preserve_id:
        _force_trailer_id_in_file(path, profile.doc_id)
    return profile


def save_clean_to_bytes(
    pdf: pikepdf.Pdf,
    original_bytes: bytes,
    preserve_id: bool = True,
    mirror_structure: bool = True,
    linearize: bool | None = None,
    compress: bool = True,
    keep_encryption: bool = False,
    owner_password: str = "",
) -> bytes:
    """То же, что :func:`save_clean`, но результат возвращается байтами."""
    profile = profile_source(original_bytes)
    if preserve_id:
        _restore_document_id(pdf, profile)
    options = _save_options(profile, preserve_id, mirror_structure, linearize, compress)
    if preserve_id:
        options["deterministic_id"] = False
    if keep_encryption:
        encryption, notes = encryption_like(pdf, owner_password)
        profile.encryption_notes = notes
        if encryption is not None:
            options["encryption"] = encryption
            options.pop("deterministic_id", None)
            preserve_id = False
    buffer = io.BytesIO()
    pdf.save(buffer, **options)
    data = buffer.getvalue()
    if preserve_id:
        data = _replace_trailer_id_bytes(data, profile.doc_id)
    return data


# ----------------------------------------------------------------------
# Восстановление /ID после записи
# ----------------------------------------------------------------------

def _encode_pdf_string(raw: bytes) -> bytes:
    """Записывает двоичную строку в шестнадцатеричном виде ``<...>``."""
    return b"<" + raw.hex().upper().encode("ascii") + b">"


_ID_RE = re.compile(rb"/ID\s*\[\s*(<[0-9A-Fa-f\s]*>|\([^)]*\))\s*(<[0-9A-Fa-f\s]*>|\([^)]*\))\s*\]")


def _document_opens(data: bytes) -> int | None:
    """Проверяет, что файл читается, и возвращает число страниц."""
    try:
        with pikepdf.open(io.BytesIO(data)) as pdf:
            return len(pdf.pages)
    except Exception:
        return None


def _replace_trailer_id_bytes(data: bytes, doc_id: tuple[bytes, bytes] | None) -> bytes:
    """Приводит ``/ID`` готового файла к тому виду, что был в оригинале.

    qpdf вычисляет ``/ID`` сам на этапе записи: первую строку он обычно берёт
    из исходного файла, вторую генерирует заново, а если ``/ID`` не было
    вовсе — добавляет его. И то и другое — заметное отличие от оригинала,
    поэтому значение восстанавливается правкой уже записанных байтов.

    Если исходные строки той же длины, что и записанные, правка не меняет
    размер файла и заведомо безопасна. Иначе смещения объектов могли бы
    сдвинуться, поэтому результат такой правки проверяется повторным
    открытием документа; при малейших сомнениях возвращается неправленый файл.
    """
    matches = list(_ID_RE.finditer(data))
    if not matches:
        return data
    # Значение, которое записал qpdf, — в последнем (активном) трейлере.
    # Правим только вхождения ровно с этим значением: так исключаются
    # посторонние /ID, например у вложенных документов.
    generated = (matches[-1].group(1), matches[-1].group(2))
    targets = [m for m in matches if (m.group(1), m.group(2)) == generated]
    if not targets:
        return data

    if doc_id is None:
        # В оригинале идентификатора не было — убираем добавленный
        result = bytearray(data)
        for match in reversed(targets):
            result[match.start() : match.end()] = b""
        candidate = bytes(result)
    else:
        new_first = _encode_pdf_string(doc_id[0])
        new_second = _encode_pdf_string(doc_id[1])
        same_length = (
            len(targets[0].group(1)) == len(new_first)
            and len(targets[0].group(2)) == len(new_second)
        )
        result = bytearray(data)
        for match in reversed(targets):
            result[match.start(2) : match.end(2)] = new_second
            result[match.start(1) : match.end(1)] = new_first
        candidate = bytes(result)
        if same_length:
            return candidate  # длина не изменилась — проверять нечего

    # Длина файла изменилась: убеждаемся, что документ по-прежнему читается
    pages_before = _document_opens(data)
    pages_after = _document_opens(candidate)
    if pages_after is None or pages_after != pages_before:
        return data
    return candidate


def _force_trailer_id_in_file(path: str, doc_id: tuple[bytes, bytes] | None) -> None:
    with open(path, "rb") as handle:
        data = handle.read()
    updated = _replace_trailer_id_bytes(data, doc_id)
    if updated != data:
        with open(path, "wb") as handle:
            handle.write(updated)


# ----------------------------------------------------------------------
# Проверка результата
# ----------------------------------------------------------------------

@dataclass
class VerifyReport:
    """Сравнение исходного и полученного файлов."""

    info_equal: bool
    info_diff: dict[str, tuple[str | None, str | None]]
    xmp_present_equal: bool
    xmp_diff: dict[str, tuple[object, object]]
    id_equal: bool
    version_equal: bool
    structure_notes: list[str]
    page_count_equal: bool
    #: поля, которые пользователь менял сознательно — их расхождение ожидаемо
    expected_fields: frozenset[str] = frozenset()

    def _unexpected_info(self) -> dict[str, tuple[str | None, str | None]]:
        return {k: v for k, v in self.info_diff.items() if k not in self.expected_fields}

    def _unexpected_xmp(self) -> dict[str, tuple[object, object]]:
        expected_xmp = {
            XMP_FOR_FIELD[field] for field in self.expected_fields if field in XMP_FOR_FIELD
        }
        return {
            key: value for key, value in self.xmp_diff.items()
            if not any(key.endswith(suffix.split(":")[-1]) for suffix in expected_xmp)
        }

    @property
    def clean(self) -> bool:
        """Нет ли расхождений сверх тех, что пользователь внёс намеренно."""
        return (
            not self._unexpected_info() and not self._unexpected_xmp()
            and self.xmp_present_equal and self.id_equal
            and self.version_equal and self.page_count_equal
        )

    def describe(self) -> str:
        lines: list[str] = []
        mark = lambda ok: "OK " if ok else "!! "  # noqa: E731
        unexpected_info = self._unexpected_info()
        lines.append(f"{mark(not unexpected_info)}словарь /Info: посторонних изменений нет")
        for key, (before, after) in sorted(self.info_diff.items()):
            tag = "изменено намеренно" if key in self.expected_fields else "РАСХОЖДЕНИЕ"
            lines.append(f"     {key}: было {before!r} → стало {after!r}  [{tag}]")
        unexpected_xmp = self._unexpected_xmp()
        lines.append(
            f"{mark(self.xmp_present_equal and not unexpected_xmp)}"
            f"XMP-метаданные: посторонних изменений нет"
        )
        for key, (before, after) in sorted(self.xmp_diff.items()):
            tag = "РАСХОЖДЕНИЕ" if key in unexpected_xmp else "изменено намеренно"
            lines.append(f"     {key}: было {before!r} → стало {after!r}  [{tag}]")
        lines.append(f"{mark(self.id_equal)}идентификатор /ID сохранён")
        lines.append(f"{mark(self.version_equal)}версия PDF совпадает")
        lines.append(f"{mark(self.page_count_equal)}число страниц совпадает")
        for note in self.structure_notes:
            lines.append(f"     {note}")
        return "\n".join(lines)


#: Соответствие полей /Info свойствам XMP (для трактовки ожидаемых изменений)
XMP_FOR_FIELD = {
    "/Title": "dc:title", "/Author": "dc:creator", "/Subject": "dc:description",
    "/Keywords": "pdf:Keywords", "/Creator": "xmp:CreatorTool",
    "/Producer": "pdf:Producer", "/CreationDate": "xmp:CreateDate",
    "/ModDate": "xmp:ModifyDate",
}


def verify(
    original_path: str, result_path: str, expected_fields: frozenset[str] | set[str] = frozenset()
) -> VerifyReport:
    """Сравнивает исходный и полученный файлы по метаданным и структуре.

    ``expected_fields`` — поля ``/Info``, изменённые пользователем осознанно;
    их расхождение не считается следом постороннего вмешательства.
    """
    from .metadata import read_metadata

    with open(original_path, "rb") as handle:
        original_bytes = handle.read()
    with open(result_path, "rb") as handle:
        result_bytes = handle.read()

    before_profile = profile_source(original_bytes)
    after_profile = profile_source(result_bytes)

    with pikepdf.open(io.BytesIO(original_bytes)) as before_pdf, \
         pikepdf.open(io.BytesIO(result_bytes)) as after_pdf:
        before = read_metadata(before_pdf)
        after = read_metadata(after_pdf)
        page_count_equal = len(before_pdf.pages) == len(after_pdf.pages)

    info_diff: dict[str, tuple[str | None, str | None]] = {}
    for key in set(before.info) | set(after.info):
        if before.info.get(key) != after.info.get(key):
            info_diff[key] = (before.info.get(key), after.info.get(key))

    xmp_diff: dict[str, tuple[object, object]] = {}
    for key in set(before.xmp) | set(after.xmp):
        if before.xmp.get(key) != after.xmp.get(key):
            xmp_diff[key] = (before.xmp.get(key), after.xmp.get(key))

    notes: list[str] = []
    if before_profile.has_object_streams != after_profile.has_object_streams:
        notes.append(
            "объектные потоки: было "
            f"{before_profile.has_object_streams} → стало {after_profile.has_object_streams}"
        )
    if before_profile.linearized != after_profile.linearized:
        notes.append(
            f"линеаризация: было {before_profile.linearized} → стало {after_profile.linearized}"
        )
    if b"%%EOF" in result_bytes:
        eof_count = result_bytes.count(b"%%EOF")
        if eof_count > 1:
            notes.append(
                f"в файле {eof_count} меток %%EOF — возможен инкрементальный "
                f"дописанный слой правок"
            )

    return VerifyReport(
        info_equal=not info_diff,
        info_diff=info_diff,
        xmp_present_equal=before.has_xmp == after.has_xmp,
        xmp_diff=xmp_diff,
        id_equal=before.doc_id == after.doc_id,
        version_equal=before_profile.version == after_profile.version,
        structure_notes=notes,
        page_count_equal=page_count_equal,
        expected_fields=frozenset(expected_fields),
    )
