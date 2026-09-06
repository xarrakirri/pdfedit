"""Простой интерфейс: один вызов — замена текста и правка метаданных.

Модуль для тех случаев, когда не нужны ни разбор документа, ни выбор режимов:
дать путь к файлу, что на что заменить, при необходимости новые метаданные —
и получить готовый PDF.

    from pdfedit.simple import replace_in_pdf

    отчёт = replace_in_pdf(
        "договор.pdf", "договор-исправленный.pdf",
        "Иванов", "Петров",
        metadata={"author": "Сидоров С. С."},
    )

За этим вызовом стоит обычный :class:`pdfedit.PdfEditor`, поэтому действуют
все его правила:

* текст переписывается **в потоках содержимого**, а не рисуется поверх
  страницы, поэтому шрифты, цвета, интервалы и структура остаются на месте;
* внедрённые шрифты не подменяются: если нужных букв в шрифте нет, его
  подмножество дополняется глифами из донора, а сам шрифт остаётся тем же
  объектом документа;
* ширина строки пересчитывается, а выключка сохраняется — текст, выключенный
  по правому краю или по центру, не поедет за поля;
* метаданные исходного файла переносятся полностью, меняется только то, что
  указано явно.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .editor import PdfEditor
from .metadata import apply_metadata
from .saving import verify

#: Понятные имена полей метаданных вместо ключей PDF
METADATA_ALIASES = {
    "author": "/Author",
    "title": "/Title",
    "subject": "/Subject",
    "keywords": "/Keywords",
    "creator": "/Creator",
    "producer": "/Producer",
    "created": "/CreationDate",
    "modified": "/ModDate",
    # Русские названия — на случай, если так удобнее
    "автор": "/Author",
    "заголовок": "/Title",
    "тема": "/Subject",
    "ключевые слова": "/Keywords",
    "создано": "/CreationDate",
    "изменено": "/ModDate",
}


@dataclass
class ReplaceReport:
    """Итог работы: что заменено, что нет и что случилось со шрифтами."""

    output: str = ""
    replaced: int = 0
    skipped: list[str] = field(default_factory=list)
    font_changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata_changes: dict[str, str] = field(default_factory=dict)
    #: сверка результата с оригиналом: не появилось ли следов правки
    verified_clean: bool | None = None
    verification: str = ""

    @property
    def ok(self) -> bool:
        return self.replaced > 0 and not self.skipped

    def describe(self) -> str:
        """Человекочитаемый отчёт — годится для печати в терминал."""
        lines = [f"Файл: {self.output}", f"Заменено вхождений: {self.replaced}"]
        if self.metadata_changes:
            lines.append("Изменены метаданные:")
            lines += [f"  {key} = {value}" for key, value in self.metadata_changes.items()]
        if self.font_changes:
            lines.append("Шрифты:")
            lines += [f"  {note}" for note in self.font_changes]
        if self.skipped:
            lines.append("Не заменено:")
            lines += [f"  {note}" for note in self.skipped]
        if self.warnings:
            lines.append("Предупреждения:")
            lines += [f"  {note}" for note in self.warnings]
        if self.verified_clean is not None:
            lines.append("Сверка с оригиналом: " + (
                "посторонних изменений нет" if self.verified_clean
                else "ЕСТЬ РАСХОЖДЕНИЯ (подробности ниже)"
            ))
            if not self.verified_clean and self.verification:
                lines += [f"  {row}" for row in self.verification.splitlines()]
        return "\n".join(lines)


def normalize_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    """Приводит понятные имена полей к ключам словаря ``/Info``."""
    if not metadata:
        return {}
    result: dict[str, str] = {}
    for key, value in metadata.items():
        name = key.strip()
        result[METADATA_ALIASES.get(name.lower(), name if name.startswith("/")
                                    else "/" + name.capitalize())] = value
    return result


def replace_in_pdf(
    source: str,
    output: str,
    search: str,
    replacement: str,
    metadata: dict[str, str] | None = None,
    *,
    regex: bool = False,
    ignore_case: bool = False,
    pages: list[int] | None = None,
    count: int = 0,
    fit_mode: str = "auto",
    align_aware: bool = True,
    keep_id: bool = True,
    password: str = "",
    donor_pdfs: list[str] | None = None,
    verify_result: bool = True,
    incremental: bool = False,
) -> ReplaceReport:
    """Заменяет текст в PDF и сохраняет результат в новый файл.

    :param source: путь к исходному документу (не изменяется)
    :param output: куда записать результат; исходный файл перезаписывать нельзя
    :param search: что искать
    :param replacement: на что заменить
    :param metadata: поля ``/Info``, которые нужно изменить; остальные
        переносятся из оригинала без изменений. Имена можно писать понятно —
        ``author``, ``created``, ``автор`` — или ключами PDF: ``/Author``
    :param regex: считать ``search`` регулярным выражением
    :param ignore_case: искать без учёта регистра
    :param pages: номера страниц (с единицы), если правка нужна не везде
    :param count: сколько вхождений заменить; 0 — все
    :param fit_mode: что делать с разницей ширин: ``auto``, ``natural``,
        ``preserve`` или ``squeeze``
    :param align_aware: сохранять выключку строк (см. :mod:`pdfedit.layout`)
    :param keep_id: сохранить идентификатор ``/ID`` исходного документа
    :param password: пароль, если документ защищён
    :param donor_pdfs: другие PDF, откуда можно брать недостающие глифы
    :param verify_result: сверить результат с оригиналом и вернуть расхождения
    :param incremental: не пересобирать файл, а дописать изменённые объекты в
        конец (см. :mod:`pdfedit.incremental`). Тогда исходные байты, номера
        объектов и защита документа сохраняются дословно, но правка видна в
        структуре файла как второй слой
    """
    import os

    if os.path.abspath(source) == os.path.abspath(output):
        raise ValueError(
            "результат нужно сохранять в новый файл, а не поверх исходного"
        )

    if donor_pdfs:
        from .donors import add_pdf_to_library

        for donor in donor_pdfs:
            add_pdf_to_library(donor)

    report = ReplaceReport(output=output)
    changes: dict[str, str] = {}
    editor = PdfEditor(
        source, fit_mode=fit_mode, align_aware=align_aware, password=password
    )
    try:
        page_indices = [number - 1 for number in pages] if pages else None
        result = editor.replace(
            search, replacement, regex=regex, ignore_case=ignore_case,
            pages=page_indices, count=count,
        )
        report.replaced = len(result.applied)
        report.skipped = [f"«{spec.old_text[:40]}»: {why}" for spec, why in result.skipped]
        report.font_changes = list(result.font_changes)
        report.warnings = list(result.warnings)

        changes = normalize_metadata(metadata)
        if changes:
            report.warnings += apply_metadata(editor.pdf, changes)
            report.metadata_changes = changes

        if incremental:
            data = editor.to_bytes(incremental=True)
        else:
            data = editor.to_bytes(preserve_id=keep_id)
    finally:
        editor.close()

    with open(output, "wb") as handle:
        handle.write(data)

    if verify_result:
        # Поля, изменённые сознательно, расхождением не считаются
        result = verify(source, output, expected_fields=frozenset(changes))
        report.verified_clean = result.clean
        report.verification = result.describe()
    return report


def main(argv: list[str] | None = None) -> int:
    """Запуск из командной строки: ``python -m pdfedit.simple …``"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="pdfedit.simple",
        description="Замена текста в PDF с сохранением шрифтов, вёрстки и метаданных",
    )
    parser.add_argument("source", help="исходный PDF")
    parser.add_argument("output", help="куда записать результат")
    parser.add_argument("search", help="что искать")
    parser.add_argument("replacement", help="на что заменить")
    parser.add_argument("--set", action="append", metavar="ПОЛЕ=ЗНАЧЕНИЕ", default=[],
                        help="изменить метаданные: author=Иванов, created=D:20210305093000")
    parser.add_argument("--donor", action="append", metavar="PDF", default=[],
                        help="PDF, откуда можно взять недостающие глифы")
    parser.add_argument("--regex", action="store_true", help="искать регулярным выражением")
    parser.add_argument("--ignore-case", action="store_true", help="без учёта регистра")
    parser.add_argument("--count", type=int, default=0, help="сколько вхождений заменить")
    parser.add_argument("--fit", default="auto",
                        choices=("auto", "natural", "preserve", "squeeze"),
                        help="что делать с разницей ширин")
    parser.add_argument("--no-align", action="store_true",
                        help="не сохранять выключку строк")
    parser.add_argument("--password", default="", help="пароль документа")
    parser.add_argument("--incremental", action="store_true",
                        help="дописать правку в конец файла, не пересобирая его")
    args = parser.parse_args(argv)

    metadata = {}
    for item in args.set:
        if "=" not in item:
            parser.error(f"метаданные задаются как ПОЛЕ=ЗНАЧЕНИЕ, получено: {item!r}")
        key, value = item.split("=", 1)
        metadata[key] = value

    report = replace_in_pdf(
        args.source, args.output, args.search, args.replacement,
        metadata=metadata or None, regex=args.regex, ignore_case=args.ignore_case,
        count=args.count, fit_mode=args.fit, align_aware=not args.no_align,
        password=args.password, donor_pdfs=args.donor or None,
        incremental=args.incremental,
    )
    print(report.describe())
    return 0 if report.replaced else 1


if __name__ == "__main__":
    raise SystemExit(main())
