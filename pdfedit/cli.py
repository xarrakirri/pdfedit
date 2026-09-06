"""Интерфейс командной строки pdfedit."""

from __future__ import annotations

import argparse
import os
import json
import sys
from pathlib import Path

from . import __version__
from .editor import FIT_MODES, EditSpec, PdfEditor
from .errors import PdfEditError
from .metadata import INFO_FIELDS, apply_metadata, read_metadata
from .saving import profile_source, verify

#: Понятные пользователю имена полей метаданных
FIELD_ALIASES = {
    "title": "/Title", "заголовок": "/Title", "название": "/Title",
    "author": "/Author", "автор": "/Author",
    "subject": "/Subject", "тема": "/Subject",
    "keywords": "/Keywords", "ключевые": "/Keywords", "ключевыеслова": "/Keywords",
    "creator": "/Creator", "создатель": "/Creator", "приложение": "/Creator",
    "producer": "/Producer", "производитель": "/Producer",
    "creationdate": "/CreationDate", "created": "/CreationDate",
    "датасоздания": "/CreationDate", "создан": "/CreationDate",
    "moddate": "/ModDate", "modified": "/ModDate",
    "датаизменения": "/ModDate", "изменён": "/ModDate", "изменен": "/ModDate",
    "trapped": "/Trapped",
}


def normalize_field(name: str) -> str:
    """Приводит имя поля метаданных к виду ``/Title``."""
    if name.startswith("/"):
        return name
    key = name.strip().lower().replace(" ", "").replace("_", "")
    if key in FIELD_ALIASES:
        return FIELD_ALIASES[key]
    return "/" + name[:1].upper() + name[1:]


def parse_pages(spec: str | None, page_count: int) -> list[int] | None:
    """Разбирает список страниц вида ``1,3-5`` в индексы (с нуля)."""
    if not spec:
        return None
    pages: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            try:
                first, last = int(start), int(end)
            except ValueError:
                raise PdfEditError(f"не разобран диапазон страниц: {chunk!r}")
            pages.update(range(first - 1, last))
        else:
            try:
                pages.add(int(chunk) - 1)
            except ValueError:
                raise PdfEditError(f"не разобран номер страницы: {chunk!r}")
    invalid = [p + 1 for p in pages if not (0 <= p < page_count)]
    if invalid:
        raise PdfEditError(f"в документе нет страниц: {invalid}")
    return sorted(pages)


def parse_assignments(values: list[str] | None) -> dict[str, str]:
    """Разбирает пары ``ключ=значение`` из командной строки."""
    result: dict[str, str] = {}
    for item in values or []:
        if "=" not in item:
            raise PdfEditError(f"ожидалось «поле=значение», получено {item!r}")
        key, _, value = item.partition("=")
        result[normalize_field(key)] = value
    return result


# ----------------------------------------------------------------------
# Команды
# ----------------------------------------------------------------------

def cmd_inspect(args: argparse.Namespace) -> int:
    """Показывает структуру документа: метаданные, шрифты, текст."""
    editor = PdfEditor(args.input, extra_font_dirs=args.font_dir or (),
                       use_document_fonts=not args.no_document_fonts,
                       use_font_library=not args.no_font_library,
                       password=args.password)
    profile = profile_source(editor.original_bytes)
    print(f"Файл: {args.input}")
    print(f"Страниц: {editor.page_count}; {profile.describe()}")
    for warning in editor.document_warnings:
        print(f"  ! {warning}")

    snapshot = read_metadata(editor.pdf)
    print()
    print(snapshot.describe())

    pages = parse_pages(args.pages, editor.page_count)
    editor.parse(pages)
    if editor.warnings:
        print("\nПредупреждения разбора:")
        for warning in editor.warnings:
            print(f"  ! {warning}")

    seen_fonts: dict[str, object] = {}
    for run in editor.runs:
        seen_fonts.setdefault(f"{run.stream_id}:{run.font_res}", run.font)
    print(f"\nШрифты ({len(seen_fonts)}):")
    for key, font in seen_fonts.items():
        print(f"  {font.describe()}")
        for warning in font.warnings:
            print(f"      ! {warning}")

    print(f"\nТекстовые фрагменты ({len(editor.runs)}):")
    for run in editor.runs:
        x0, y0, x1, y1 = run.bbox
        flag = "" if run.editable else "  [нередактируем]"
        print(
            f"  с.{run.page_index + 1} #{run.run_id} {run.font_res} {run.size:g}пт "
            f"({x0:.0f},{y0:.0f})-({x1:.0f},{y1:.0f}){flag}"
        )
        print(f"      {run.text!r}")
    editor.close()
    return 0


def cmd_replace(args: argparse.Namespace) -> int:
    """Заменяет текст и сохраняет новый файл."""
    if not args.old and not args.edits:
        raise PdfEditError("укажите хотя бы одну пару --old/--new или файл --edits")
    if args.old and len(args.old) != len(args.new or []):
        raise PdfEditError(
            f"число --old ({len(args.old)}) и --new ({len(args.new or [])}) должно совпадать"
        )

    editor = PdfEditor(
        args.input,
        extra_font_dirs=args.font_dir or (),
        use_document_fonts=not args.no_document_fonts,
        use_font_library=not args.no_font_library,
        allow_font_extension=not args.no_font_extension,
        allow_fallback_font=not args.no_fallback_font,
        fit_mode=args.fit,
        password=args.password,
        exact=getattr(args, "exact", False),
        donor_pdf=getattr(args, "donor", None),
    )
    pages = parse_pages(args.pages, editor.page_count)
    editor.parse(pages)

    total_applied = 0
    problems = 0

    # Пакетный режим: список правок из JSON-файла
    if args.edits:
        blob = json.loads(Path(args.edits).read_text("utf-8"))
        specs = [EditSpec.from_dict(item) for item in blob]
        report = editor.apply_edits(specs)
        total_applied += len(report.applied)
        problems += len(report.skipped)
        _print_report(report, args.quiet)

    for old, new in zip(args.old or [], args.new or []):
        if args.dry_run:
            matches = editor.find(
                old, regex=args.regex, ignore_case=args.ignore_case,
                pages=pages, whole_word=args.whole_word,
            )
            print(f"«{old}» → «{new}»: найдено вхождений: {len(matches)}")
            for match in matches:
                x0, y0, _x1, _y1 = match.run.bbox
                print(
                    f"    с.{match.page_index + 1} фрагмент #{match.run.run_id} "
                    f"({x0:.0f},{y0:.0f}) в контексте: {match.run.text!r}"
                )
            continue
        try:
            report = editor.replace(
                old, new, count=args.count, regex=args.regex,
                ignore_case=args.ignore_case, pages=pages, whole_word=args.whole_word,
            )
        except PdfEditError as exc:
            print(f"[ошибка] «{old}»: {exc}", file=sys.stderr)
            problems += 1
            continue
        if not args.quiet:
            print(f"«{old}» → «{new}»: заменено {len(report.applied)}")
        total_applied += len(report.applied)
        problems += len(report.skipped)
        _print_report(report, args.quiet)

    if args.dry_run:
        editor.close()
        return 0

    changes = parse_assignments(args.set)
    for field in args.delete or []:
        changes[normalize_field(field)] = None  # type: ignore[assignment]
    if changes:
        notes = apply_metadata(editor.pdf, changes, sync_xmp=args.xmp)
        if not args.quiet:
            print("Метаданные:")
            for note in notes:
                print(f"  {note}")
    elif args.touch_moddate:
        notes = apply_metadata(editor.pdf, {"/ModDate": "now"}, sync_xmp=args.xmp)
        if not args.quiet:
            for note in notes:
                print(f"  {note}")

    if args.inplace:
        editor.save(args.output, in_place=True)
    elif args.incremental:
        editor.save(args.output, incremental=True)
    else:
        editor.save(
            args.output,
            preserve_id=not args.new_id,
            mirror_structure=not args.no_mirror,
            linearize=args.linearize,
            keep_encryption=args.keep_encryption,
            owner_password=args.owner_password,
        )
    incremental_report = editor.last_incremental_report
    in_place_report = editor.last_in_place_report
    encryption_notes = editor.last_encryption_notes
    editor.close()

    if not args.quiet:
        print(f"\nСохранено: {args.output} (заменено фрагментов: {total_applied})")
        if in_place_report is not None:
            print("\nПравка на месте:")
            print(in_place_report.describe())
        if incremental_report is not None:
            print("\nДописанный слой правок:")
            print(incremental_report.describe())
        for note in encryption_notes:
            print(f"  защита: {note}")
        expected = set(changes) | ({"/ModDate"} if args.touch_moddate else set())
        report = verify(args.input, args.output, expected)
        print("\nСверка с исходным файлом:")
        print(report.describe())
    return 1 if problems else 0


def _print_report(report, quiet: bool) -> None:
    for change in report.font_changes:
        print(f"    шрифт: {change}")
    for warning in report.warnings:
        print(f"    ! {warning}")
    for spec, reason in report.skipped:
        print(f"    [пропущено] «{spec.old_text or spec.new_text}»: {reason}", file=sys.stderr)


def cmd_meta(args: argparse.Namespace) -> int:
    """Правит только метаданные, не трогая содержимое страниц."""
    changes = parse_assignments(args.set)
    for field in args.delete or []:
        changes[normalize_field(field)] = None  # type: ignore[assignment]
    if not changes and not args.show:
        raise PdfEditError("укажите --set/--del для правки или --show для просмотра")

    editor = PdfEditor(args.input, password=args.password)
    if args.show and not changes:
        print(read_metadata(editor.pdf).describe())
        editor.close()
        return 0

    notes = apply_metadata(editor.pdf, changes, sync_xmp=args.xmp)
    for note in notes:
        print(f"  {note}")
    if args.inplace:
        editor.save(args.output, in_place=True)
    elif args.incremental:
        editor.save(args.output, incremental=True)
    else:
        editor.save(
            args.output,
            preserve_id=not args.new_id,
            mirror_structure=not args.no_mirror,
            keep_encryption=args.keep_encryption,
            owner_password=args.owner_password,
        )
    if editor.last_in_place_report is not None:
        print()
        print(editor.last_in_place_report.describe())
    if editor.last_incremental_report is not None:
        print()
        print(editor.last_incremental_report.describe())
    for note in editor.last_encryption_notes:
        print(f"  защита: {note}")
    editor.close()
    print(f"\nСохранено: {args.output}")
    if args.show:
        with PdfEditor(args.output) as check:
            print()
            print(read_metadata(check.pdf).describe())
    return 0


def cmd_fonts(args: argparse.Namespace) -> int:
    """Управляет библиотекой шрифтов-доноров."""
    from .donors import (
        add_pdf_to_library, clear_library, extract_embedded_fonts,
        library_dir, library_fonts, remove_from_library,
    )

    action = getattr(args, "action", None) or "list"

    if action == "add":
        total = 0
        for path in args.input:
            print(f"{os.path.basename(path)}:")
            for font in add_pdf_to_library(path, password=args.password):
                mark = "+" if font.usable else "-"
                print(f"  {mark} {font.name:32s} {font.note}")
                total += int(font.usable)
        print(f"\nдобавлено пригодных шрифтов: {total}")
        print(f"библиотека: {library_dir()}")
        return 0

    if action == "show":
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            for font in extract_embedded_fonts(args.input, tmp, password=args.password):
                mark = "+" if font.usable else "-"
                print(f"  {mark} {font.name:32s} {font.kind:10s} {font.note}")
        return 0

    if action == "remove":
        removed = remove_from_library(args.pattern)
        for name in removed:
            print("удалён", name)
        print(f"удалено файлов: {len(removed)}")
        return 0

    if action == "clear":
        print(f"удалено файлов: {clear_library()}")
        return 0

    fonts = library_fonts()
    print(f"Библиотека доноров: {library_dir()}")
    if not fonts:
        print("(пусто; пополнить: python -m pdfedit fonts add документ.pdf)")
        return 0
    print(f"Шрифтов: {len(fonts)}\n")
    for font in fonts:
        # Показываем настоящее начертание из шрифта: «обычный» для Medium
        # ввело бы в заблуждение — насыщенность у них разная
        marks = font.subfamily or "Regular"
        if font.style.italic and "talic" not in marks:
            marks += " курсив"
        print(f"  {font.family:26s} {marks:18s} вес {font.style.weight:3d}  "
              f"{os.path.basename(font.path)}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Сравнивает исходный и полученный файлы."""
    report = verify(args.original, args.result)
    print(report.describe())
    print()
    print("Вывод:", "различий в метаданных и структуре не найдено"
          if report.clean else "есть расхождения (см. выше)")
    return 0 if report.clean else 2


def cmd_check(args: argparse.Namespace) -> int:
    """Проверяет целостность файла и, если задан оригинал, сверяет с ним."""
    from .validate import (
        check_file,
        compare_external_risks,
        compare_files,
        compare_object_hashes,
        external_risks,
    )

    report = check_file(args.input, password=args.password)
    print(report.describe())
    print()
    print("Вывод:", "структура цела" if report.valid else "структура нарушена (см. выше)")

    if args.strict:
        from .traces import compare_traces, self_traces

        print()
        print("Следы правки — то, чем файл выдаёт себя при разборе:")
        own = self_traces(args.input, password=args.password)
        print(own.describe())
        if args.original:
            print()
            print("Следы, заметные при сверке с оригиналом:")
            against = compare_traces(args.original, args.input, password=args.password)
            print(against.describe())
        else:
            print()
            print("  Часть признаков (смена /ID, дат, уровня сжатия, порядка таблиц")
            print("  в шрифтах) видна только в сравнении — укажите --original.")

        print()
        if args.original:
            inherited, introduced = compare_external_risks(
                args.original, args.input, password=args.password
            )
            print("Замечания строгих внешних проверок (PDF/A, приёмные системы):")
            if not inherited and not introduced:
                print("     таких замечаний нет")
            for text in introduced.values():
                print(f"  !! появилось после правки: {text}")
            for text in inherited.values():
                print(f"  -- было и в оригинале:     {text}")
            if inherited and not introduced:
                print(
                    "\n  Все замечания унаследованы от исходного файла: правка их не "
                    "добавила. Если внешняя проверка бракует результат, прогоните "
                    "через неё оригинал — скорее всего, он не проходит тоже."
                )
        else:
            import io as _io

            import pikepdf as _pikepdf

            with open(args.input, "rb") as handle:
                data = handle.read()
            with _pikepdf.open(_io.BytesIO(data), password=args.password) as pdf:
                risks = external_risks(pdf, data)
            print("Замечания строгих внешних проверок (PDF/A, приёмные системы):")
            if not risks:
                print("     таких замечаний нет")
            for text in risks.values():
                print(f"  -- {text}")
            print(
                "\n  Чтобы понять, чьи это замечания — исходного файла или правки, "
                "запустите с ключом --original."
            )

    if args.original:
        if args.hashes:
            hashes = compare_object_hashes(args.original, args.input, password=args.password)
            print()
            print(hashes.describe())
        comparison = compare_files(args.original, args.input, password=args.password)
        print()
        print(comparison.describe())
        print()
        print(
            "Вывод:",
            "структура совпадает с оригиналом, изменилось только то, "
            "что затронула правка текста"
            if comparison.structure_equal
            else "в структуре есть посторонние изменения (см. выше)",
        )
        if not comparison.structure_equal:
            return 2
    return 0 if report.valid else 2


def cmd_gui(args: argparse.Namespace) -> int:
    """Запускает графический редактор."""
    from .gui import run_gui

    return run_gui(args.input)


# ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdfedit",
        description=(
            "Редактирование текста и метаданных PDF на уровне объектов документа. "
            "Текст заменяется прямо в потоках содержимого (операторы Tj/TJ), "
            "внедрённые шрифты, форматирование и структура сохраняются."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  pdfedit inspect договор.pdf\n"
            "  pdfedit replace договор.pdf -o итог.pdf --old Иванов --new Петров\n"
            "  pdfedit replace вход.pdf -o выход.pdf --old 2021 --new 2022 \\\n"
            "      --set author='И. И. Иванов' --set moddate='2022-04-12 10:00:00'\n"
            "  pdfedit meta вход.pdf -o выход.pdf --set created='2019-01-01' --show\n"
            "  pdfedit replace вход.pdf -o выход.pdf --old Иванов --new Петров --incremental\n"
            "  pdfedit check выход.pdf --original вход.pdf\n"
            "  pdfedit gui договор.pdf\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"pdfedit {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_password_option(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--password", default="", metavar="ПАРОЛЬ",
                         help="пароль документа, если он защищён")

    def add_font_options(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--font-dir", action="append", metavar="КАТАЛОГ",
            help="дополнительный каталог со шрифтами (можно указывать несколько раз)",
        )
        sub.add_argument(
            "--no-document-fonts", action="store_true",
            help="не брать недостающие глифы из других шрифтов того же документа",
        )
        sub.add_argument(
            "--no-font-library", action="store_true",
            help="не использовать библиотеку доноров (см. команду fonts)",
        )

    def add_save_options(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--new-id", action="store_true",
                         help="создать новый /ID вместо сохранения исходного")
        sub.add_argument("--no-mirror", action="store_true",
                         help="не воспроизводить структуру оригинала (версия, объектные потоки)")
        sub.add_argument("--xmp", choices=("auto", "always", "never"), default="auto",
                         help="как поступать с XMP: auto — обновлять существующий (по умолчанию), "
                              "always — создать при отсутствии, never — не трогать")
        sub.add_argument(
            "--incremental", action="store_true",
            help="не пересобирать файл: оставить исходные байты нетронутыми и "
                 "дописать в конец только изменённые объекты. Сохраняет номера "
                 "объектов, шифрование и предыдущие редакции; правка при этом "
                 "видна в структуре файла",
        )
        sub.add_argument(
            "--inplace", action="store_true",
            help="записать изменённые потоки поверх старых, не меняя длину файла: "
                 "смещения, таблица ссылок, трейлер и все прочие объекты остаются "
                 "нетронутыми байт в байт. Что не поместится — допишется слоем",
        )
        sub.add_argument(
            # default=None обязателен: без него argparse поставил бы True и
            # линеаризация включалась бы там, где её не просили
            "--no-linearize", dest="linearize", action="store_false", default=None,
            help="не линеаризовать результат, даже если оригинал был линеаризован",
        )
        sub.add_argument(
            "--keep-encryption", action="store_true",
            help="при обычном сохранении воспроизвести защиту документа "
                 "(владельческий пароль восстановить нельзя — он станет пустым "
                 "или таким, как задано ключом --owner-password)",
        )
        sub.add_argument(
            "--owner-password", default="", metavar="ПАРОЛЬ",
            help="владельческий пароль для результата при --keep-encryption",
        )

    # --- inspect ---
    inspect = subparsers.add_parser(
        "inspect", help="показать метаданные, шрифты и текстовые фрагменты",
        description="Показывает, что внутри документа: метаданные, шрифты и текст "
                    "с координатами. Отсюда удобно брать строки для замены.",
    )
    inspect.add_argument("input", help="исходный PDF")
    inspect.add_argument("--pages", metavar="СПИСОК", help="страницы, например 1,3-5")
    add_font_options(inspect)
    add_password_option(inspect)
    inspect.set_defaults(func=cmd_inspect)

    # --- replace ---
    replace = subparsers.add_parser(
        "replace", help="заменить текст в документе",
        description="Заменяет текст в потоках содержимого, сохраняя шрифты и вёрстку.",
    )
    replace.add_argument("input", help="исходный PDF")
    replace.add_argument("-o", "--output", required=True, help="файл результата")
    replace.add_argument("--old", action="append", metavar="ТЕКСТ", help="что заменить")
    replace.add_argument("--new", action="append", metavar="ТЕКСТ", help="на что заменить")
    replace.add_argument("--edits", metavar="ФАЙЛ.json",
                         help="файл со списком правок (формат — как у графического режима)")
    replace.add_argument("--count", type=int, default=0, metavar="N",
                         help="заменить только первые N вхождений (0 — все)")
    replace.add_argument("--regex", action="store_true",
                         help="считать --old регулярным выражением")
    replace.add_argument("--ignore-case", action="store_true", help="без учёта регистра")
    replace.add_argument("--whole-word", action="store_true", help="только целые слова")
    replace.add_argument("--pages", metavar="СПИСОК", help="страницы, например 1,3-5")
    replace.add_argument(
        "--fit", choices=FIT_MODES, default="auto",
        help="подгонка ширины: auto — сжать, если разница мала, иначе переверстать "
             "(по умолчанию); natural — переверстать строку; preserve — сохранить "
             "положение последующего текста; squeeze — вписать в исходную ширину",
    )
    replace.add_argument("--set", action="append", metavar="ПОЛЕ=ЗНАЧЕНИЕ",
                         help="изменить поле метаданных (author, title, moddate, …)")
    replace.add_argument("--del", dest="delete", action="append", metavar="ПОЛЕ",
                         help="удалить поле метаданных")
    replace.add_argument("--touch-moddate", action="store_true",
                         help="проставить текущую дату изменения (по умолчанию она не трогается)")
    replace.add_argument("--no-font-extension", action="store_true",
                         help="не дописывать недостающие глифы во внедрённые шрифты")
    replace.add_argument("--no-fallback-font", action="store_true",
                         help="не внедрять запасной шрифт, если своих глифов не хватает")
    replace.add_argument(
        "--exact", action="store_true",
        help="точный режим: на странице окажется ровно заданный текст. Ширина "
             "не подгоняется (ни сжатием Tz, ни кернингом), строка не сдвигается "
             "ради выключки, системные шрифты не используются. Не хватает "
             "глифов — правка отклоняется с перечислением недостающих символов",
    )
    replace.add_argument(
        "--donor", metavar="ФАЙЛ.pdf",
        help="донорский PDF: недостающие глифы берутся только из его шрифтов. "
             "Вместе с --exact других источников не остаётся вовсе",
    )
    replace.add_argument("--linearize", action="store_true", default=None,
                         help="линеаризовать результат (по умолчанию — как в оригинале)")
    replace.add_argument("--dry-run", action="store_true",
                         help="только показать, что будет заменено")
    replace.add_argument("-q", "--quiet", action="store_true", help="меньше сообщений")
    add_font_options(replace)
    add_password_option(replace)
    add_save_options(replace)
    replace.set_defaults(func=cmd_replace)

    # --- meta ---
    meta = subparsers.add_parser(
        "meta", help="править метаданные отдельно от текста",
        description="Изменяет поля /Info и синхронизирует XMP. Содержимое страниц не трогается.",
        epilog="Поля: " + ", ".join(f.lstrip('/').lower() for f in INFO_FIELDS),
    )
    meta.add_argument("input", help="исходный PDF")
    meta.add_argument("-o", "--output", help="файл результата")
    meta.add_argument("--set", action="append", metavar="ПОЛЕ=ЗНАЧЕНИЕ",
                      help="например --set author='И. Иванов' --set created='2019-01-01 10:00'")
    meta.add_argument("--del", dest="delete", action="append", metavar="ПОЛЕ",
                      help="удалить поле")
    meta.add_argument("--show", action="store_true", help="показать метаданные")
    add_password_option(meta)
    add_save_options(meta)
    meta.set_defaults(func=cmd_meta)

    # --- verify ---
    verify_parser = subparsers.add_parser(
        "verify", help="сравнить исходный и полученный файлы",
        description="Проверяет, что в результате не появилось следов правки: "
                    "метаданные, /ID, версия и структура сравниваются с оригиналом.",
    )
    verify_parser.add_argument("original", help="исходный PDF")
    verify_parser.add_argument("result", help="полученный PDF")
    verify_parser.set_defaults(func=cmd_verify)

    # --- check ---
    check_parser = subparsers.add_parser(
        "check", help="проверить структуру файла (и сверить её с оригиналом)",
        description="Проверяет, что документ цел: открывается, ссылки никуда не "
                    "теряются, потоки распаковываются, содержимое страниц "
                    "разбирается, шрифты на месте. С ключом --original вдобавок "
                    "сверяет структуру с исходным файлом — объект за объектом.",
    )
    check_parser.add_argument("input", help="проверяемый PDF")
    check_parser.add_argument("--original", metavar="ФАЙЛ",
                              help="исходный PDF для структурного сравнения")
    check_parser.add_argument(
        "--strict", action="store_true",
        help="показать, за что документ обычно бракуют внешние проверки "
             "(PDF/A, приёмные системы), и что из этого было уже в оригинале",
    )
    check_parser.add_argument("--hashes", action="store_true",
                              help="сверить объекты по хешам: сколько совпадает байт "
                                   "в байт (имеет смысл после --incremental и --inplace)")
    add_password_option(check_parser)
    check_parser.set_defaults(func=cmd_check)

    # --- fonts ---
    fonts_parser = subparsers.add_parser(
        "fonts", help="библиотека шрифтов-доноров из других PDF",
        description="Документы несут не весь шрифт, а только использованные "
                    "глифы. Если нужных букв нет, их берут из шрифта-донора. "
                    "Сюда можно сложить шрифты из других PDF — например, из "
                    "документов того же издателя, где нужные символы есть.",
    )
    fonts_sub = fonts_parser.add_subparsers(dest="action", metavar="ДЕЙСТВИЕ")

    fonts_add = fonts_sub.add_parser(
        "add", help="добавить в библиотеку шрифты из PDF",
        description="Извлекает внедрённые программы шрифтов и складывает их "
                    "в библиотеку. Дальше они используются как доноры "
                    "автоматически — раньше системных шрифтов.",
    )
    fonts_add.add_argument("input", nargs="+", help="PDF, откуда взять шрифты")
    fonts_add.add_argument("--password", default="", metavar="ПАРОЛЬ")

    fonts_sub.add_parser("list", help="показать содержимое библиотеки")

    fonts_remove = fonts_sub.add_parser("remove", help="удалить шрифты по части имени")
    fonts_remove.add_argument("pattern", help="часть имени файла шрифта")

    fonts_sub.add_parser("clear", help="очистить библиотеку целиком")

    fonts_show = fonts_sub.add_parser(
        "show", help="какие шрифты внедрены в PDF (без добавления в библиотеку)",
    )
    fonts_show.add_argument("input", help="PDF для осмотра")
    fonts_show.add_argument("--password", default="", metavar="ПАРОЛЬ")

    fonts_parser.set_defaults(func=cmd_fonts)

    # --- gui ---
    gui = subparsers.add_parser(
        "gui", help="графический редактор: правка текста прямо на странице",
        description="Открывает окно с изображением страницы. Текст правится "
                    "щелчком по нему, изменения подсвечиваются.",
    )
    gui.add_argument("input", nargs="?", help="PDF для открытия")
    gui.set_defaults(func=cmd_gui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) == "meta" and not args.output and not args.show:
        parser.error("для команды meta нужен -o/--output либо --show")
    if getattr(args, "command", None) == "meta" and (args.set or args.delete) and not args.output:
        parser.error("для изменения метаданных нужен -o/--output")
    try:
        return args.func(args)
    except PdfEditError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"Файл не найден: {exc.filename}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nПрервано пользователем", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
