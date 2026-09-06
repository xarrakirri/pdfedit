"""Чтение и правка метаданных PDF.

В PDF метаданные живут в двух местах, и они обязаны совпадать:

* словарь ``/Info`` в трейлере — «классические» поля (``/Title``, ``/Author``,
  ``/CreationDate`` и т. д.);
* поток XMP в ``/Root /Metadata`` — те же сведения в виде RDF/XML.

Рассогласование между ними — первое, что показывают программы проверки
документов, поэтому при любой правке значения синхронизируются.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pikepdf

#: Поля ``/Info``, которыми оперирует программа.
INFO_FIELDS = [
    "/Title", "/Author", "/Subject", "/Keywords",
    "/Creator", "/Producer", "/CreationDate", "/ModDate", "/Trapped",
]

#: Соответствие полей ``/Info`` свойствам XMP.
XMP_EQUIVALENTS = {
    "/Title": "dc:title",
    "/Author": "dc:creator",
    "/Subject": "dc:description",
    "/Keywords": "pdf:Keywords",
    "/Creator": "xmp:CreatorTool",
    "/Producer": "pdf:Producer",
    "/CreationDate": "xmp:CreateDate",
    "/ModDate": "xmp:ModifyDate",
}

DATE_FIELDS = {"/CreationDate", "/ModDate"}

_PDF_DATE_RE = re.compile(
    r"^D?:?"
    r"(?P<year>\d{4})(?P<month>\d{2})?(?P<day>\d{2})?"
    r"(?P<hour>\d{2})?(?P<minute>\d{2})?(?P<second>\d{2})?"
    r"(?P<tzsign>[+\-Z])?(?P<tzhour>\d{2})?'?(?P<tzminute>\d{2})?'?$"
)


def parse_user_date(value: str) -> datetime:
    """Разбирает дату, введённую пользователем.

    Принимаются формат PDF (``D:20210305120000+03'00'``), ISO 8601,
    ``ГГГГ-ММ-ДД ЧЧ:ММ:СС``, ``ГГГГ-ММ-ДД`` и слово ``now``.
    """
    text = value.strip()
    if text.lower() in ("now", "сейчас"):
        return datetime.now(timezone.utc).astimezone()

    match = _PDF_DATE_RE.match(text.replace("'", "").replace(" ", ""))
    if match and text[:2].upper() == "D:":
        parts = match.groupdict()
        offset = None
        if parts["tzsign"] == "Z":
            offset = timezone.utc
        elif parts["tzsign"] in ("+", "-"):
            hours = int(parts["tzhour"] or 0)
            minutes = int(parts["tzminute"] or 0)
            delta = timedelta(hours=hours, minutes=minutes)
            offset = timezone(-delta if parts["tzsign"] == "-" else delta)
        return datetime(
            int(parts["year"]), int(parts["month"] or 1), int(parts["day"] or 1),
            int(parts["hour"] or 0), int(parts["minute"] or 0), int(parts["second"] or 0),
            tzinfo=offset,
        )

    normalized = text.replace("/", "-")
    for pattern in (
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(normalized, pattern)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"не удалось разобрать дату {value!r}; примеры допустимых значений: "
            f"«2021-03-05 12:00:00», «2021-03-05T12:00:00+03:00», "
            f"«D:20210305120000+03'00'», «now»"
        ) from exc


def format_pdf_date(moment: datetime) -> str:
    """Записывает дату в формате словаря ``/Info``."""
    base = moment.strftime("D:%Y%m%d%H%M%S")
    offset = moment.utcoffset()
    if offset is None:
        return base
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{base}{sign}{total_minutes // 60:02d}'{total_minutes % 60:02d}'"


def format_xmp_date(moment: datetime) -> str:
    """Записывает дату в формате XMP (ISO 8601)."""
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    offset = moment.utcoffset()
    if offset is None:
        return text
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{text}{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def pdf_date_to_datetime(value: str) -> datetime | None:
    try:
        return parse_user_date(value if value.startswith("D:") else "D:" + value.lstrip(":"))
    except Exception:
        return None


@dataclass
class MetadataSnapshot:
    """Снимок метаданных документа."""

    info: dict[str, str]
    xmp: dict[str, object]
    has_xmp: bool
    doc_id: tuple[str, str] | None

    def describe(self) -> str:
        lines = ["Словарь /Info:"]
        if self.info:
            width = max(len(k) for k in self.info)
            for key, value in self.info.items():
                lines.append(f"  {key.ljust(width)} = {value}")
        else:
            lines.append("  (пусто)")
        lines.append(f"XMP-метаданные: {'есть' if self.has_xmp else 'нет'}")
        if self.has_xmp:
            for key in sorted(self.xmp):
                lines.append(f"  {key} = {self.xmp[key]}")
        if self.doc_id:
            lines.append(f"Идентификатор /ID: {self.doc_id[0][:16]}… / {self.doc_id[1][:16]}…")
        return "\n".join(lines)


def read_metadata(pdf: pikepdf.Pdf) -> MetadataSnapshot:
    """Считывает ``/Info``, XMP и идентификатор документа."""
    info: dict[str, str] = {}
    try:
        docinfo = pdf.trailer.get("/Info")
        if docinfo is not None:
            for key, value in docinfo.items():
                try:
                    info[str(key)] = str(value)
                except Exception:
                    info[str(key)] = repr(value)
    except Exception:
        pass

    xmp: dict[str, object] = {}
    has_xmp = "/Metadata" in pdf.Root
    if has_xmp:
        try:
            with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
                xmp = dict(meta)
        except Exception:
            has_xmp = "/Metadata" in pdf.Root

    doc_id = None
    try:
        raw = pdf.trailer.get("/ID")
        if raw is not None and len(raw) >= 2:
            doc_id = (bytes(raw[0]).hex(), bytes(raw[1]).hex())
    except Exception:
        pass

    return MetadataSnapshot(info=info, xmp=xmp, has_xmp=has_xmp, doc_id=doc_id)


def apply_metadata(
    pdf: pikepdf.Pdf,
    changes: dict[str, str | None],
    sync_xmp: str = "auto",
) -> list[str]:
    """Применяет изменения метаданных.

    ``changes`` — словарь вида ``{"/Author": "Иванов", "/ModDate": "now"}``.
    Значение ``None`` удаляет поле. ``sync_xmp`` управляет XMP-потоком:
    ``auto`` — обновлять, только если он уже есть; ``always`` — создать при
    отсутствии; ``never`` — не трогать.
    """
    notes: list[str] = []
    if not changes:
        return notes

    if "/Info" not in pdf.trailer:
        pdf.trailer["/Info"] = pdf.make_indirect(pikepdf.Dictionary())
    docinfo = pdf.trailer["/Info"]

    prepared: dict[str, str | None] = {}
    for key, value in changes.items():
        field = key if key.startswith("/") else "/" + key
        if value is None:
            prepared[field] = None
            continue
        if field in DATE_FIELDS:
            moment = parse_user_date(value)
            prepared[field] = format_pdf_date(moment)
        else:
            prepared[field] = value

    for field, value in prepared.items():
        if value is None:
            if field in docinfo:
                del docinfo[field]
                notes.append(f"{field}: удалено")
        else:
            docinfo[field] = pikepdf.String(value)
            notes.append(f"{field}: {value}")

    if sync_xmp == "never":
        return notes
    has_xmp = "/Metadata" in pdf.Root
    if not has_xmp and sync_xmp != "always":
        return notes

    try:
        _sync_xmp(pdf, prepared)
        notes.append("XMP-метаданные приведены в соответствие со словарём /Info")
    except Exception as exc:
        notes.append(f"XMP не обновлён: {exc}")
    return notes


def _sync_xmp(pdf: pikepdf.Pdf, prepared: dict[str, str | None]) -> None:
    """Переносит изменения из ``/Info`` в XMP-поток."""
    # set_pikepdf_as_editor=False — иначе pikepdf пропишет себя в pdf:Producer
    # и xmp:CreatorTool, а это как раз лишний след правки.
    with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
        for field, value in prepared.items():
            xmp_key = XMP_EQUIVALENTS.get(field)
            if xmp_key is None:
                continue
            if value is None:
                if xmp_key in meta:
                    del meta[xmp_key]
                continue
            if field in DATE_FIELDS:
                moment = pdf_date_to_datetime(value)
                meta[xmp_key] = format_xmp_date(moment) if moment else value
            elif xmp_key == "dc:creator":
                meta[xmp_key] = [value]  # dc:creator — упорядоченный список
            else:
                meta[xmp_key] = value


def copy_metadata(source: pikepdf.Pdf, target: pikepdf.Pdf) -> None:
    """Копирует ``/Info`` и XMP из одного документа в другой без изменений."""
    source_info = source.trailer.get("/Info")
    if source_info is not None:
        new_info = target.make_indirect(pikepdf.Dictionary())
        for key, value in source_info.items():
            new_info[key] = value
        target.trailer["/Info"] = new_info
    if "/Metadata" in source.Root:
        data = source.Root["/Metadata"].read_bytes()
        stream = target.make_stream(data)
        stream["/Type"] = pikepdf.Name("/Metadata")
        stream["/Subtype"] = pikepdf.Name("/XML")
        target.Root["/Metadata"] = stream
