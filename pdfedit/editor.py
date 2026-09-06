"""Движок редактирования текста в потоках содержимого PDF.

Замена выполняется на уровне операторов показа текста. Порядок работы:

1. документ разбирается в модель текстовых фрагментов (:mod:`pdfedit.content`);
2. в фрагментах ищется исходный текст, результат — диапазоны глифов;
3. новый текст кодируется теми же шрифтами; недостающие глифы при
   необходимости добавляются в шрифт (:mod:`pdfedit.fontops`);
4. операторы ``Tj``/``TJ`` пересобираются: заменяемые байты удаляются, новые
   вставляются, а разница ширин компенсируется числовым элементом массива
   ``TJ`` — благодаря этому весь последующий текст строки остаётся на месте.

Компенсация считается отдельно для каждого оператора показа: если фрагмент
разорван позиционирующими операторами (``Td``/``Tm``), каждая часть строки
должна сохранить собственную привязку.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import pikepdf

from . import fontops, layout
from .content import StreamContext, TextRun, parse_page
from .errors import PdfEditError, TextNotFoundError
from .fonts import FontInfo, find_fallback_font, find_system_font

#: Символы, которые при поиске считаются эквивалентными более простым.
#: Нужны потому, что в PDF пробел часто оказывается неразрывным, а дефис —
#: коротким тире; пользователь же набирает обычные символы.
SEARCH_EQUIVALENTS = {
    "\xa0": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ", " ": " ", "　": " ",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "−": "-",
    "‘": "'", "’": "'", "‚": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    # U+00AD в извлечённом тексте PDF — почти всегда обычный видимый дефис:
    # в шрифте один и тот же глиф отвечает и за U+002D, и за U+00AD, а какой
    # из них попадёт в /ToUnicode, зависит от программы-создателя. Ищем по
    # тому, что человек видит на странице.
    "\xad": "-",
}

#: Режимы подгонки ширины нового текста.
FIT_MODES = ("auto", "natural", "preserve", "squeeze")

#: В режиме ``auto`` текст сжимается по горизонтали, пока отклонение ширины не
#: превышает этот предел; при большем отклонении сжатие стало бы заметным, и
#: строка просто переверстывается.
AUTO_SQUEEZE_TOLERANCE = 0.08


def normalize_for_search(text: str) -> tuple[str, list[int]]:
    """Приводит текст к «поисковому» виду.

    Возвращает нормализованную строку и карту «индекс в нормализованной строке
    → индекс в исходной», чтобы найденное совпадение можно было перевести
    обратно в позиции глифов.
    """
    out: list[str] = []
    index_map: list[int] = []
    for position, char in enumerate(text):
        replacement = SEARCH_EQUIVALENTS.get(char, char)
        for piece in replacement:
            out.append(piece)
            index_map.append(position)
    return "".join(out), index_map


@dataclass(frozen=True)
class EditSpec:
    """Задание на правку — простые данные, переживающие перезагрузку документа."""

    page_index: int
    run_id: int
    glyph_start: int
    glyph_end: int
    new_text: str
    old_text: str = ""
    #: Продолжение правки, начатой в другом фрагменте. Текст, разорванный на
    #: несколько операторов показа, заменяется так: весь новый текст идёт в
    #: первый кусок, а остальные лишь вычищаются. Такие «хвостовые» правки
    #: нельзя принимать за самостоятельные замены — иначе синхронизация
    #: скрытых копий увидит в них пары вида «2» → «» и вычистит по этому
    #: правилу /ActualText, закладки и всё прочее.
    continuation: bool = False
    #: Номер группы. Правки одного разорванного фрагмента делят его и
    #: применяются только все вместе: если новый текст не удалось поместить в
    #: первый кусок, остальные тоже обязаны остаться нетронутыми. Иначе выйдет
    #: худшее из возможного — старый текст вычищен, новый не вставлен.
    #: Ноль — самостоятельная правка, ни с чем не связанная.
    group_id: int = 0

    def to_dict(self) -> dict:
        return {
            "page": self.page_index, "run": self.run_id,
            "glyph_start": self.glyph_start, "glyph_end": self.glyph_end,
            "new_text": self.new_text, "old_text": self.old_text,
            "continuation": self.continuation, "group": self.group_id,
        }

    @classmethod
    def from_dict(cls, blob: dict) -> "EditSpec":
        return cls(
            page_index=int(blob["page"]), run_id=int(blob["run"]),
            glyph_start=int(blob["glyph_start"]), glyph_end=int(blob["glyph_end"]),
            new_text=blob["new_text"], old_text=blob.get("old_text", ""),
            continuation=bool(blob.get("continuation", False)),
            group_id=int(blob.get("group", 0)),
        )


@dataclass
class Match:
    """Найденное вхождение искомого текста."""

    run: TextRun
    char_start: int
    char_end: int
    glyph_start: int
    glyph_end: int
    expanded: bool          # совпадение расширено до границ глифов (лигатура)
    #: результат регулярного выражения — нужен для ссылок на группы (\1, \g<имя>)
    regex_match: "re.Match | None" = None

    @property
    def text(self) -> str:
        return self.run.text[self.char_start : self.char_end]

    @property
    def page_index(self) -> int:
        return self.run.page_index

    def to_edit(self, new_text: str) -> EditSpec:
        return EditSpec(
            page_index=self.run.page_index, run_id=self.run.run_id,
            glyph_start=self.glyph_start, glyph_end=self.glyph_end,
            new_text=new_text, old_text=self.text,
        )


_group_counter = 0


def _next_group_id() -> int:
    """Выдаёт очередной номер группы связанных правок."""
    global _group_counter
    _group_counter += 1
    return _group_counter


@dataclass
class GroupMatch:
    """Совпадение, растянутое на несколько соседних фрагментов.

    Один и тот же на вид текст сплошь и рядом записан в потоке несколькими
    операторами показа подряд: так выходит у генераторов таблиц и бланков,
    где каждая цифра или каждое слово позиционируется своим ``Td``, и у любого
    документа, где посреди строки сменился шрифт или кегль. Разборщик честно
    делит такой текст на фрагменты — каждый со своим состоянием, — и поиск
    внутри одного фрагмента числа «1234567», записанного семью ``Tj``, не
    находит вовсе.

    Здесь совпадение хранится кусками: ``(фрагмент, начало, конец)``. Замена
    целиком кладётся в первый кусок, остальные вычищаются, — тогда текст на
    странице получается ровно тот, что задан, а не склеенный из огрызков.
    """

    pieces: list[tuple["TextRun", int, int]]
    text: str
    page_index: int
    regex_match: "re.Match | None" = None

    @property
    def run(self) -> "TextRun":
        """Первый фрагмент — к нему привязано положение совпадения."""
        return self.pieces[0][0]

    @property
    def split(self) -> bool:
        return len(self.pieces) > 1

    def to_edits(self, new_text: str) -> list[EditSpec]:
        """Правки для всех кусков: текст в первый, остальные — вычистить.

        Первая правка несёт **весь** заменяемый текст группы, а не только свою
        долю: по ней потом синхронизируются скрытые копии, и там должна стоять
        замена целиком («1234567» → «7654321»), а не по цифре.
        """
        whole = "".join(
            glyph.text
            for run, start, end in self.pieces
            for glyph in run.glyphs[start:end]
        )
        group = _next_group_id() if len(self.pieces) > 1 else 0
        edits: list[EditSpec] = []
        for index, (run, start, end) in enumerate(self.pieces):
            covered = "".join(glyph.text for glyph in run.glyphs[start:end])
            edits.append(EditSpec(
                page_index=run.page_index, run_id=run.run_id,
                glyph_start=start, glyph_end=end,
                new_text=new_text if index == 0 else "",
                old_text=whole if index == 0 else covered,
                continuation=index > 0,
                group_id=group,
            ))
        return edits


@dataclass
class ApplyReport:
    """Что именно было сделано при применении правок."""

    applied: list[EditSpec] = field(default_factory=list)
    skipped: list[tuple[EditSpec, str]] = field(default_factory=list)
    font_changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.skipped


def _apply_pairs(text: str, pairs: Sequence[tuple[str, str]]) -> str | None:
    """Применяет замены к строке. ``None``, если ничего не изменилось."""
    updated = text
    for old, new in pairs:
        if old in updated:
            updated = updated.replace(old, new)
    return updated if updated != text else None


def _replace_in_graph(
    root, keys: Sequence[str], pairs: Sequence[tuple[str, str]], depth_limit: int = 40
) -> int:
    """Заменяет текст в перечисленных ключах по всему поддереву объектов.

    Возвращает число изменённых значений. Обход идёт по ссылкам с защитой от
    петель: структурное дерево ссылается на страницы, страницы — обратно на
    структурные элементы, и без пометок обход не кончился бы никогда.

    Значение ключа бывает и потоком (``/RC`` с оформленным текстом заметки
    хранят и так, и так), поэтому потоки тоже просматриваются.
    """
    if root is None:
        return 0
    visited: set[tuple[int, int]] = set()
    changed = 0

    def walk(obj, depth: int) -> None:
        nonlocal changed
        if depth > depth_limit:
            return
        try:
            gen = obj.objgen
        except Exception:
            gen = None
        if gen is not None and gen != (0, 0):
            if gen in visited:
                return
            visited.add(gen)
        try:
            if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
                for key in list(obj.keys()):
                    value = obj[key]
                    if key in keys and isinstance(value, pikepdf.String):
                        updated = _apply_pairs(str(value), pairs)
                        if updated is not None:
                            obj[key] = pikepdf.String(updated)
                            changed += 1
                        continue
                    if key in keys and isinstance(value, pikepdf.Stream):
                        try:
                            raw = value.read_bytes().decode("utf-8")
                        except Exception:
                            walk(value, depth + 1)
                            continue
                        updated = _apply_pairs(raw, pairs)
                        if updated is not None:
                            value.write(updated.encode("utf-8"))
                            changed += 1
                        continue
                    walk(value, depth + 1)
            elif isinstance(obj, pikepdf.Array):
                for item in obj:
                    walk(item, depth + 1)
        except Exception:
            return

    walk(root, 0)
    return changed


# ----------------------------------------------------------------------
# Атомы содержимого оператора показа текста
# ----------------------------------------------------------------------

@dataclass
class _FontPlan:
    """Решение о том, как быть с нехваткой глифов в конкретном шрифте."""

    blocked: bool = False
    reason: str = ""
    #: подставной шрифт, которым будет набран изменённый фрагмент
    fallback_font: FontInfo | None = None
    fallback_dict: object | None = None
    #: имя ресурса подставного шрифта в каждом потоке содержимого
    resource_names: dict[str, str] = field(default_factory=dict)
    #: состояние шрифта до расширения — чтобы откатить неиспользованные глифы
    snapshot: object | None = None
    #: символы, глифы которых были добавлены в шрифт
    added_chars: str = ""


@dataclass
class _Atom:
    """Элементарная часть оператора показа: глиф, число или сырые байты."""

    kind: str                 # 'glyph' | 'num' | 'raw'
    value: object
    glyph_index: int = -1     # индекс глифа внутри оператора
    deleted: bool = False


class PdfEditor:
    """Редактор текста и метаданных PDF-документа."""

    def __init__(
        self,
        source: str | bytes,
        extra_font_dirs: Sequence[str] = (),
        use_document_fonts: bool = True,
        align_aware: bool = True,
        use_font_library: bool = True,
        allow_font_extension: bool = True,
        allow_fallback_font: bool = True,
        fit_mode: str = "auto",
        password: str = "",
        exact: bool = False,
        donor_pdf: str | None = None,
    ):
        self.extra_font_dirs = tuple(extra_font_dirs)
        self.use_document_fonts = use_document_fonts
        #: Сохранять ли выключку строк при изменении их ширины
        self.align_aware = align_aware
        self._page_lines_cache: dict[int, list] = {}
        self.use_font_library = use_font_library
        #: Каталог, куда выгружаются шрифты самого документа (создаётся по нужде)
        self._document_font_dir: str | None = None
        self.allow_font_extension = allow_font_extension
        self.allow_fallback_font = allow_fallback_font
        if fit_mode not in FIT_MODES:
            raise ValueError(
                f"неизвестный режим подгонки: {fit_mode}; допустимы: {', '.join(FIT_MODES)}"
            )
        self.fit_mode = fit_mode

        #: Точный режим: на странице оказывается ровно тот текст, который задан,
        #: и ни одной буквы сверх того. Отключается всё, что в обычном режиме
        #: помогает результату выглядеть аккуратно, но меняет заданное:
        #:
        #: * подгонка ширины (``Tz``, кернинг) — она сжимает или растягивает
        #:   глифы, чтобы новый текст занял место старого;
        #: * сдвиг строки ради выключки;
        #: * подбор шрифта на стороне: системные шрифты не просматриваются
        #:   вовсе, глифы берутся только из указанного донора.
        #:
        #: Чего не хватает — то не подменяется похожим, а честно приводит к
        #: отказу с перечислением недостающих символов.
        self.exact = exact
        self.donor_pdf = donor_pdf
        #: Каталог с извлечёнными шрифтами донорского документа
        self._donor_font_dir: str | None = None
        if exact:
            self.fit_mode = "natural"
            self.align_aware = False
            # Личная библиотека доноров — это пёстрый склад шрифтов из чужих
            # документов; брать оттуда «что подойдёт» и есть автоподбор,
            # которого точный режим не допускает
            self.use_font_library = False
            if donor_pdf is not None:
                # Донор задан явно — других источников не остаётся вовсе
                self.use_document_fonts = False

        if isinstance(source, bytes):
            self.original_bytes = source
        else:
            with open(source, "rb") as handle:
                self.original_bytes = handle.read()
        self.source_path = source if isinstance(source, str) else None
        #: пароль нужен и при сохранении: инкрементальный режим перечитывает
        #: готовый файл, чтобы сверить дописанное с тем, что было в памяти
        self._password = password

        #: предупреждения уровня документа — переживают повторный разбор
        self.document_warnings: list[str] = []
        try:
            self.pdf = pikepdf.open(io.BytesIO(self.original_bytes), password=password)
        except pikepdf.PasswordError as exc:
            raise PdfEditError(
                "неверный пароль документа" if password
                else "документ защищён паролем: укажите его ключом --password"
            ) from exc
        if self.pdf.is_encrypted:
            # При полной пересборке защита снимается, если её не попросили
            # воспроизвести; инкрементальный режим сохраняет её всегда
            self.document_warnings.append(
                "документ зашифрован: при обычном сохранении результат будет без "
                "защиты паролем (сохранить её: --incremental или --keep-encryption)"
            )

        #: отчёт последнего инкрементального сохранения (если оно было)
        self.last_incremental_report = None
        #: отчёт последней правки на месте (если она была)
        self.last_in_place_report = None
        #: заметки о воспроизведении защиты при последнем обычном сохранении
        self.last_encryption_notes: list[str] = []
        self.contexts: dict[str, StreamContext] = {}
        self.runs: list[TextRun] = []
        self.warnings: list[str] = list(self.document_warnings)
        self.parsed_pages: set[int] = set()
        self._parsed = False

    # ------------------------------------------------------------------
    def close(self) -> None:
        try:
            self.pdf.close()
        except Exception:
            pass
        # Выгруженные шрифты документа — временные файлы, за ними надо убрать
        if self._document_font_dir:
            import shutil

            shutil.rmtree(self._document_font_dir, ignore_errors=True)
            self._document_font_dir = ""

    def __enter__(self) -> "PdfEditor":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Разбор
    # ------------------------------------------------------------------
    def parse(self, pages: Iterable[int] | None = None) -> None:
        """Разбирает содержимое страниц в модель текстовых фрагментов."""
        self.runs = []
        self.contexts = {}
        self.warnings = list(self.document_warnings)
        indices = range(len(self.pdf.pages)) if pages is None else list(pages)
        self.parsed_pages = set(indices)
        for page_index in indices:
            page = self.pdf.pages[page_index]
            try:
                runs, self.contexts, warns = parse_page(
                    self.pdf, page, page_index, self.contexts
                )
            except Exception as exc:
                self.warnings.append(f"страница {page_index + 1}: не разобрана ({exc})")
                continue
            self.runs += runs
            self.warnings += [f"страница {page_index + 1}: {w}" for w in warns]
        self._parsed = True

    def ensure_parsed(self) -> None:
        if not self._parsed:
            self.parse()

    def page_runs(self, page_index: int) -> list[TextRun]:
        self.ensure_parsed()
        return [r for r in self.runs if r.page_index == page_index]

    def run_by_id(self, run_id: int) -> TextRun | None:
        self.ensure_parsed()
        for run in self.runs:
            if run.run_id == run_id:
                return run
        return None

    @property
    def page_count(self) -> int:
        return len(self.pdf.pages)

    def page_size(self, page_index: int) -> tuple[float, float]:
        page = self.pdf.pages[page_index]
        box = page.get("/MediaBox") or [0, 0, 612, 792]
        x0, y0, x1, y1 = (float(v) for v in list(box)[:4])
        return (abs(x1 - x0), abs(y1 - y0))

    # ------------------------------------------------------------------
    # Поиск
    # ------------------------------------------------------------------
    def find(
        self,
        pattern: str,
        regex: bool = False,
        ignore_case: bool = False,
        pages: Iterable[int] | None = None,
        whole_word: bool = False,
    ) -> list[Match]:
        """Ищет текст во всех фрагментах документа."""
        self.ensure_parsed()
        if not pattern:
            return []
        page_filter = set(pages) if pages is not None else None

        if regex:
            expression = pattern
        else:
            normalized_pattern, _ = normalize_for_search(pattern)
            expression = re.escape(normalized_pattern)
            if whole_word:
                expression = rf"(?<!\w){expression}(?!\w)"
        flags = re.IGNORECASE if ignore_case else 0
        try:
            compiled = re.compile(expression, flags)
        except re.error as exc:
            raise PdfEditError(f"некорректное регулярное выражение: {exc}") from exc

        matches: list[Match] = []
        for run in self.runs:
            if page_filter is not None and run.page_index not in page_filter:
                continue
            if not run.editable:
                continue
            norm, index_map = normalize_for_search(run.text)
            for found in compiled.finditer(norm):
                if found.start() == found.end():
                    continue
                char_start = index_map[found.start()]
                char_end = index_map[found.end() - 1] + 1
                g0, g1, expanded = run.glyph_span_for_chars(char_start, char_end)
                if g1 <= g0:
                    continue
                matches.append(
                    Match(run, char_start, char_end, g0, g1, expanded,
                          regex_match=found if regex else None)
                )
        return matches

    #: Зазор между фрагментами, ниже которого они читаются как сплошной текст
    #: (в долях кегля). Больше — но меньше :data:`GROUP_BREAK_GAP` — считается
    #: пробелом, ещё больше разрывает группу
    GROUP_SPACE_GAP = 0.19
    GROUP_BREAK_GAP = 2.5
    #: Насколько могут разойтись базовые линии, чтобы текст всё ещё считался
    #: одной строкой (в долях кегля): у надстрочных знаков и смены кегля
    #: базовая линия чуть плавает
    GROUP_BASELINE_TOLERANCE = 0.25

    def visual_groups(self, page_index: int | None = None) -> list[list["TextRun"]]:
        """Собирает фрагменты в визуальные строки: то, что читается слитно.

        Признак один и тот же, что и внутри фрагмента: общая базовая линия и
        небольшой горизонтальный зазор. Разница лишь в том, что здесь
        объединяются куски, которые разборщик разделил по причинам, к виду
        отношения не имеющим, — ``BT``/``ET``, смена шрифта, кегля, цвета.
        """
        self.ensure_parsed()
        by_line: dict[tuple[int, float], list[TextRun]] = {}
        for run in self.runs:
            if not run.editable:
                continue
            if page_index is not None and run.page_index != page_index:
                continue
            size = run.size or 1.0
            # Базовая линия округляется с допуском, пропорциональным кеглю
            step = max(size * self.GROUP_BASELINE_TOLERANCE, 0.01)
            # Поток обязан быть один и тот же. Внешний вид поля формы и текст
            # страницы вполне могут оказаться на одной базовой линии, но это
            # разные объекты: склеив их, мы бы искали текст, которого читатель
            # как одного целого не видит, и правили бы чужой поток
            key = (run.page_index, run.stream_id, round(run.bbox[1] / step))
            by_line.setdefault(key, []).append(run)

        groups: list[list[TextRun]] = []
        for line in by_line.values():
            line.sort(key=lambda run: run.bbox[0])
            current: list[TextRun] = []
            for run in line:
                if current:
                    previous = current[-1]
                    size = max(previous.size, run.size) or 1.0
                    gap = run.bbox[0] - previous.bbox[2]
                    if gap / size > self.GROUP_BREAK_GAP or gap / size < -0.5:
                        groups.append(current)
                        current = []
                current.append(run)
            if current:
                groups.append(current)
        return groups

    def _group_text(self, group: list["TextRun"]) -> tuple[str, list[tuple["TextRun", int]]]:
        """Текст группы и карта «символ → (фрагмент, индекс символа в нём)».

        Пробел между кусками вставляется по тому же правилу, что и внутри
        фрагмента: по величине зазора. Для числа, разбитого на цифры, зазор
        нулевой — и текст получается сплошным, каким его и видит читатель.
        """
        text_parts: list[str] = []
        index: list[tuple[TextRun, int]] = []
        for position, run in enumerate(group):
            if position:
                previous = group[position - 1]
                size = max(previous.size, run.size) or 1.0
                gap = run.bbox[0] - previous.bbox[2]
                if gap / size >= self.GROUP_SPACE_GAP and not text_parts[-1].endswith(" "):
                    text_parts.append(" ")
                    index.append((run, -1))
            text_parts.append(run.text)
            index.extend((run, char) for char in range(len(run.text)))
        return "".join(text_parts), index

    def find_grouped(
        self,
        pattern: str,
        regex: bool = False,
        ignore_case: bool = False,
        pages: Iterable[int] | None = None,
        whole_word: bool = False,
    ) -> list[GroupMatch]:
        """Ищет текст по визуальным строкам, а не по отдельным фрагментам.

        Находит и то, что лежит внутри одного фрагмента, и то, что разорвано
        на несколько: для вызывающей стороны разницы нет.
        """
        self.ensure_parsed()
        if not pattern:
            return []
        page_filter = set(pages) if pages is not None else None

        if regex:
            expression = pattern
        else:
            normalized_pattern, _ = normalize_for_search(pattern)
            expression = re.escape(normalized_pattern)
            if whole_word:
                expression = rf"(?<!\w){expression}(?!\w)"
        try:
            compiled = re.compile(expression, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise PdfEditError(f"некорректное регулярное выражение: {exc}") from exc

        found: list[GroupMatch] = []
        for group in self.visual_groups():
            if page_filter is not None and group[0].page_index not in page_filter:
                continue
            raw, index = self._group_text(group)
            norm, index_map = normalize_for_search(raw)
            for hit in compiled.finditer(norm):
                if hit.start() == hit.end():
                    continue
                pieces = self._pieces_for(index, index_map[hit.start()],
                                          index_map[hit.end() - 1] + 1)
                if pieces:
                    found.append(GroupMatch(
                        pieces=pieces, text=raw[index_map[hit.start()]:
                                                index_map[hit.end() - 1] + 1],
                        page_index=group[0].page_index,
                        regex_match=hit if regex else None,
                    ))
        return found

    @staticmethod
    def _pieces_for(
        index: list[tuple["TextRun", int]], char_start: int, char_end: int
    ) -> list[tuple["TextRun", int, int]]:
        """Переводит границы в тексте группы в границы глифов по фрагментам."""
        spans: dict[int, tuple[TextRun, int, int]] = {}
        order: list[int] = []
        for position in range(char_start, min(char_end, len(index))):
            run, char = index[position]
            if char < 0:
                continue  # пробел, вставленный по зазору: глифа за ним нет
            start, end, _expanded = run.glyph_span_for_chars(char, char + 1)
            if end <= start:
                continue
            key = run.run_id
            if key not in spans:
                spans[key] = (run, start, end)
                order.append(key)
            else:
                known_run, known_start, known_end = spans[key]
                spans[key] = (known_run, min(known_start, start), max(known_end, end))
        return [spans[key] for key in order]

    def page_text(self, page_index: int) -> str:
        """Собирает текст страницы (для диагностики и поиска по документу)."""
        runs = sorted(
            self.page_runs(page_index),
            key=lambda r: (-round(r.bbox[3], 1), round(r.bbox[0], 1)),
        )
        return "\n".join(run.text for run in runs)

    # ------------------------------------------------------------------
    # Применение правок
    # ------------------------------------------------------------------
    def apply_edits(self, edits: Sequence[EditSpec]) -> ApplyReport:
        """Применяет список правок к содержимому документа."""
        self.ensure_parsed()
        report = ApplyReport()
        if not edits:
            return report

        runs_by_id = {run.run_id: run for run in self.runs}
        resolved: list[tuple[EditSpec, TextRun]] = []
        for edit in edits:
            run = runs_by_id.get(edit.run_id)
            if run is None:
                report.skipped.append((edit, "фрагмент не найден (документ изменился?)"))
                continue
            if not run.editable:
                report.skipped.append((edit, "фрагмент нередактируем (шрифт Type3?)"))
                continue
            if not (0 <= edit.glyph_start < edit.glyph_end <= len(run.glyphs)):
                report.skipped.append((edit, "диапазон глифов вне фрагмента"))
                continue
            resolved.append((edit, run))

        # 1. Планирование шрифтов: собираем недостающие символы по каждому шрифту
        plans = self._plan_fonts(resolved, report)

        # 2. Пересборка операторов, поток за потоком
        by_stream: dict[str, list[tuple[EditSpec, TextRun]]] = {}
        refused: dict[int, str] = {}
        for edit, run in resolved:
            plan = plans.get(id(run.font))
            if plan is not None and plan.blocked and plan.fallback_font is None:
                missing = run.font.missing_chars(edit.new_text)
                if missing:
                    reason = (
                        f"в шрифте нет глифов для символов: {missing!r} ({plan.reason})"
                    )
                    report.skipped.append((edit, reason))
                    if edit.group_id:
                        refused[edit.group_id] = reason
                    continue
            by_stream.setdefault(run.stream_id, []).append((edit, run))

        # Правки одной группы неделимы. Если новый текст не удалось поместить
        # в первый кусок, вычищать остальные нельзя: вышло бы худшее из
        # возможного — старого текста уже нет, нового ещё нет
        if refused:
            for stream_id, items in list(by_stream.items()):
                kept = []
                for edit, run in items:
                    if edit.group_id in refused:
                        report.skipped.append((
                            edit,
                            f"правка разорванного фрагмента отменена целиком: "
                            f"{refused[edit.group_id]}",
                        ))
                        continue
                    kept.append((edit, run))
                if kept:
                    by_stream[stream_id] = kept
                else:
                    del by_stream[stream_id]

        for stream_id, items in by_stream.items():
            context = self.contexts.get(stream_id)
            if context is None:
                for edit, _ in items:
                    report.skipped.append((edit, "поток содержимого не найден"))
                continue
            try:
                applied = self._apply_to_stream(context, items, report, plans)
            except Exception as exc:
                for edit, _ in items:
                    report.skipped.append((edit, f"ошибка пересборки потока: {exc}"))
                continue
            report.applied += applied

        # 3. Записываем изменённые потоки обратно в документ
        for context in self.contexts.values():
            if context.dirty:
                self._write_back(context)

        self._drop_unused_glyphs(resolved, plans, report)
        self._sync_tounicode(report)
        self._sync_structure_text(report)
        self._parsed = False  # модель устарела, при следующем обращении разберём заново
        return report

    def _drop_unused_glyphs(
        self,
        resolved: Sequence[tuple[EditSpec, "TextRun"]],
        plans: dict[int, _FontPlan],
        report: "ApplyReport",
    ) -> None:
        """Убирает из шрифтов глифы, которые в итоге никому не понадобились.

        Глифы добываются заранее — до того, как правка применена, — потому что
        без них неизвестно, выполнима ли она вообще. Но применяется не всякая
        задуманная правка: фрагмент мог оказаться разорван на два оператора
        показа, поток содержимого — не найтись, пересборка — не удаться. Тогда
        добытые глифы остаются в шрифте, и на них не ссылается ни один код.

        Такой глиф-сирота ничего не рисует, но лежит в файле и прямо
        показывает, что шрифт правили, — и даже какие буквы для этого
        понадобились. Здесь шрифт возвращается к состоянию до расширения.
        """
        if not plans:
            return
        applied = {id(edit) for edit in report.applied}
        alive: set[int] = set()
        for edit, run in resolved:
            if id(edit) in applied:
                alive.add(id(run.font))

        for font_key, plan in plans.items():
            snapshot = plan.snapshot
            if snapshot is None or font_key in alive:
                continue
            if getattr(snapshot, "restore", None) is None or not snapshot.restore():
                continue
            report.warnings.append(
                f"глифы {plan.added_chars!r} убраны из шрифта: правки, ради "
                f"которых они добавлялись, не применились"
            )

    #: Ключи, в которых тегированный PDF хранит текстовые дубликаты содержимого
    STRUCTURE_TEXT_KEYS = ("/ActualText", "/Alt", "/E")
    #: Текстовые поля аннотации: заметка целиком и её оформленный вариант
    ANNOTATION_TEXT_KEYS = ("/Contents", "/RC", "/Subj")
    #: Поля формы: значение, значение по умолчанию, подсказка. Имя поля (``/T``)
    #: сюда не входит намеренно — по нему форму находят программы
    FIELD_TEXT_KEYS = ("/V", "/DV", "/TU", "/RV")
    #: Закладки и оглавление
    OUTLINE_TEXT_KEYS = ("/Title",)

    def _sync_structure_text(self, report: "ApplyReport") -> None:
        """Обновляет все скрытые копии текста, а не только видимую на странице.

        Один и тот же текст лежит в файле в нескольких местах сразу, и правка
        содержимого страницы меняет лишь одно из них:

        * **структурное дерево** тегированного PDF — ``/ActualText``, ``/Alt``,
          ``/E`` рядом со структурными элементами: тот же текст словами, для
          экранных дикторов и извлечения. Часть программ (например, PyMuPDF)
          предпочитает ``/ActualText`` содержимому страницы и показывает старый
          текст, хотя на экране давно новый;
        * **разметка внутри самого потока** — ``/Span <</ActualText …>> BDC``.
          Эту копию не видно при обходе объектов: она лежит операндом
          инструкции;
        * **поля форм** — ``/V`` и ``/DV`` хранят значение отдельно от того,
          что нарисовано в потоке внешнего вида ``/AP``. Расходятся они молча;
        * **аннотации** — ``/Contents`` заметки, всплывающие подсказки ``/TU``;
        * **закладки** — ``/Title`` в оглавлении.

        Любая из этих копий, оставшаяся со старым текстом, — прямая улика:
        исходная формулировка остаётся в файле открытым текстом. Здесь те же
        замены применяются ко всем перечисленным местам.
        """
        if not report.applied:
            return

        # Замены применяются от длинных к коротким: иначе короткая строка
        # могла бы попасть внутрь уже вставленной длинной
        # Хвостовые куски разорванной правки пропускаем: они не самостоятельные
        # замены, а лишь вычистка остатков, и пара вида «2» → «» вычистила бы
        # по этому правилу все двойки в структурном дереве и закладках
        pairs = sorted(
            {(edit.old_text, edit.new_text) for edit in report.applied
             if edit.old_text and edit.old_text != edit.new_text
             and not edit.continuation},
            key=lambda pair: -len(pair[0]),
        )
        if not pairs:
            return

        changed: dict[str, int] = {}

        def note(where: str, count: int) -> None:
            if count:
                changed[where] = changed.get(where, 0) + count

        root = self.pdf.Root
        try:
            note("структурное дерево",
                 _replace_in_graph(root.get("/StructTreeRoot"),
                                   self.STRUCTURE_TEXT_KEYS, pairs))
            note("поля форм",
                 _replace_in_graph(root.get("/AcroForm"),
                                   self.FIELD_TEXT_KEYS + self.STRUCTURE_TEXT_KEYS, pairs))
            note("закладки",
                 _replace_in_graph(root.get("/Outlines"), self.OUTLINE_TEXT_KEYS, pairs))

            annotation_keys = (
                self.ANNOTATION_TEXT_KEYS + self.FIELD_TEXT_KEYS + self.STRUCTURE_TEXT_KEYS
            )
            total = 0
            for page in self.pdf.pages:
                total += _replace_in_graph(page.obj.get("/Annots"), annotation_keys, pairs)
            note("аннотации и поля на страницах", total)

            note("разметка внутри потоков", self._replace_inline_actual_text(pairs))
        except Exception as exc:
            report.warnings.append(f"не удалось обновить скрытые копии текста: {exc}")
            return

        for where, count in changed.items():
            report.warnings.append(f"обновлены текстовые копии ({where}): {count}")

    def _replace_inline_actual_text(self, pairs: list[tuple[str, str]]) -> int:
        """Заменяет текст в словарях размеченного содержимого (``BDC``).

        Тегированный PDF помечает куски содержимого прямо в потоке:
        ``/Span <</ActualText (Иванов)>> BDC … EMC``. Такая копия текста не
        встречается при обходе объектов документа — она лежит операндом
        инструкции, внутри потока, и потому переживает правку незамеченной.
        """
        changed = 0
        for context in self.contexts.values():
            touched = False
            for index, instruction in enumerate(context.instructions):
                try:
                    operator = str(instruction.operator)
                    operands = list(instruction.operands)
                except Exception:
                    continue
                if operator not in ("BDC", "DP") or len(operands) < 2:
                    continue
                properties = operands[1]
                if not isinstance(properties, pikepdf.Dictionary):
                    continue
                for key in self.STRUCTURE_TEXT_KEYS:
                    value = properties.get(key)
                    if not isinstance(value, pikepdf.String):
                        continue
                    updated = _apply_pairs(str(value), pairs)
                    if updated is None:
                        continue
                    properties[key] = pikepdf.String(updated)
                    changed += 1
                    touched = True
                if touched:
                    context.instructions[index] = pikepdf.ContentStreamInstruction(
                        operands, instruction.operator
                    )
            if touched:
                context.dirty = True
                self._write_back(context)
        return changed

    def _sync_tounicode(self, report: "ApplyReport") -> None:
        """Приводит ``/ToUnicode`` в соответствие с тем, что записано на страницах.

        Код глифа для нового текста подбирается в том числе по внутренней
        таблице внедрённого шрифта, где глиф может найтись, даже если в
        ``/ToUnicode`` документа записи о нём нет. Тогда страница выглядит
        правильно, а копирование и поиск дают другой текст — расхождение,
        по которому правку и обнаруживают. Здесь оно устраняется.
        """
        seen: set[int] = set()
        for context in self.contexts.values():
            for font in context.fonts.values():
                if id(font) in seen or not font.tounicode_dirty:
                    continue
                seen.add(id(font))
                try:
                    fontops.rewrite_tounicode(self.pdf, font)
                    font.clear_tounicode_dirty()
                except Exception as exc:
                    report.warnings.append(
                        f"не удалось обновить таблицу /ToUnicode шрифта "
                        f"{font.resource_name}: {exc}"
                    )

    @property
    def only_donor_glyphs(self) -> bool:
        """Запрещено ли брать глифы откуда-либо, кроме донорских каталогов."""
        return self.exact

    def _ensure_donor_fonts(self) -> str | None:
        """Выгружает шрифты донорского документа во временный каталог."""
        if self._donor_font_dir is not None:
            return self._donor_font_dir or None
        if not self.donor_pdf:
            self._donor_font_dir = ""
            return None
        try:
            import tempfile

            from .donors import extract_embedded_fonts

            target = tempfile.mkdtemp(prefix="pdfedit-donor-")
            with pikepdf.open(self.donor_pdf) as donor:
                found = extract_embedded_fonts(donor, target)
            if not any(font.usable for font in found):
                import shutil

                shutil.rmtree(target, ignore_errors=True)
                self._donor_font_dir = ""
                raise PdfEditError(
                    f"в донорском документе {self.donor_pdf} не нашлось ни одного "
                    f"пригодного внедрённого шрифта"
                )
            self._donor_font_dir = target
            return target
        except PdfEditError:
            raise
        except Exception as exc:
            self._donor_font_dir = ""
            raise PdfEditError(
                f"не удалось прочитать шрифты донора {self.donor_pdf}: {exc}"
            ) from exc

    @property
    def donor_priority_dirs(self) -> tuple[str, ...]:
        """Каталоги, которые просматриваются раньше системных шрифтов.

        Сначала — программы шрифтов самого документа: если нужные буквы уже
        есть в файле, пусть и в другом шрифте, лучше взять их, а не искать
        замену на стороне. Затем — личная библиотека доноров, собранная
        пользователем из других PDF.

        В точном режиме с указанным донором список сводится к нему одному:
        глифам больше неоткуда взяться.
        """
        dirs: list[str] = []
        donor = self._ensure_donor_fonts()
        if donor:
            dirs.append(donor)
        if self.use_document_fonts:
            own = self._ensure_document_fonts()
            if own:
                dirs.append(own)
        if self.use_font_library:
            from .donors import library_dir

            library = library_dir()
            if library.is_dir():
                dirs.append(str(library))
        return tuple(dirs)

    def _ensure_document_fonts(self) -> str | None:
        """Выгружает шрифты документа во временный каталог (один раз)."""
        if self._document_font_dir is not None:
            return self._document_font_dir
        try:
            import tempfile

            from .donors import extract_embedded_fonts

            target = tempfile.mkdtemp(prefix="pdfedit-fonts-")
            found = extract_embedded_fonts(self.pdf, target)
            if not any(f.usable for f in found):
                # Пустой каталог только замедлил бы поиск
                import shutil

                shutil.rmtree(target, ignore_errors=True)
                self._document_font_dir = ""
                return None
            self._document_font_dir = target
            return target
        except Exception as exc:
            self.document_warnings.append(
                f"шрифты документа не удалось использовать как доноры: {exc}"
            )
            self._document_font_dir = ""
            return None

    def _codes_in_use(self, font: FontInfo) -> set[int]:
        """Коды, которыми в документе действительно набран текст этим шрифтом."""
        codes: set[int] = set()
        for run in self.runs:
            if run.font is font:
                codes.update(glyph.code for glyph in run.glyphs)
        return codes

    def _plan_fonts(
        self, resolved: Sequence[tuple[EditSpec, TextRun]], report: ApplyReport
    ) -> dict[int, _FontPlan]:
        """Определяет, каким шрифтам каких глифов не хватает, и добывает их."""
        needed: dict[int, tuple[FontInfo, set[str], set[str]]] = {}
        for edit, run in resolved:
            missing = run.font.missing_chars(edit.new_text)
            if missing:
                entry = needed.setdefault(id(run.font), (run.font, set(), set()))
                entry[1].update(missing)
                # Для подставного шрифта понадобится весь текст правки целиком
                entry[2].update(edit.new_text)

        plans: dict[int, _FontPlan] = {}
        for font_key, (font, chars, full_text) in needed.items():
            ordered = "".join(sorted(chars))
            plan = self._extend_or_fallback(font, ordered, "".join(sorted(full_text)), report)
            plans[font_key] = plan
        return plans

    def _extend_or_fallback(
        self, font: FontInfo, missing: str, full_text: str, report: ApplyReport
    ) -> _FontPlan:
        """Пытается расширить шрифт, иначе — подобрать и внедрить запасной."""
        if not self.allow_font_extension:
            return self._make_fallback(
                font, full_text, "правка шрифтов запрещена ключом", report
            )
        if not font.is_embedded:
            # Шрифт не внедрён: глифы берутся из системы читателя, дописать
            # в документ нечего — остаётся внедрить запасной шрифт.
            report.warnings.append(
                f"шрифт {font.base_font} не внедрён в документ, "
                f"символов {missing!r} в нём может не быть"
            )
            return self._make_fallback(
                font, full_text, f"шрифт {font.base_font} не внедрён", report
            )
        # Снимок до расширения: если добавленные глифы окажутся никому не
        # нужны, шрифт вернётся к прежнему виду и сирот в нём не останется
        snapshot = fontops.FontSnapshot(font)
        try:
            result = fontops.extend_font(
                self.pdf, font, missing, self.extra_font_dirs, self._codes_in_use(font),
                priority_dirs=self.donor_priority_dirs,
                only_priority=self.only_donor_glyphs,
            )
        except Exception as exc:
            snapshot.restore()
            return self._make_fallback(font, full_text, str(exc), report)

        added = "".join(sorted(result.added))
        if result.failed:
            # Часть символов донор не дал, и весь фрагмент пойдёт запасным
            # шрифтом — значит добавленные глифы не понадобятся ни одному коду
            if snapshot.restore():
                report.warnings.append(
                    f"{font.base_font}: добавленные глифы {added!r} убраны — "
                    f"фрагмент всё равно набирается запасным шрифтом"
                )
            elif added:
                report.font_changes.append(
                    f"{font.base_font}: добавлены глифы {added!r} из «{result.donor}»"
                )
            report.warnings += result.notes
            return self._make_fallback(
                font, full_text, f"не удалось добавить символы {result.failed!r}", report
            )

        if added:
            report.font_changes.append(
                f"{font.base_font}: добавлены глифы {added!r} из «{result.donor}»"
            )
        report.warnings += result.notes
        return _FontPlan(blocked=False, snapshot=snapshot, added_chars=added)

    def _make_fallback(
        self, font: FontInfo, full_text: str, reason: str, report: ApplyReport
    ) -> _FontPlan:
        """Внедряет в документ запасной шрифт для изменённого фрагмента."""
        if self.exact and not self.donor_priority_dirs:
            # Точный режим без донора: подставлять нечего и незачем
            return _FontPlan(
                blocked=True,
                reason=f"{reason}; в точном режиме глифы берутся только из донора, "
                       f"а он не задан (ключ --donor)",
            )
        if not self.allow_fallback_font:
            return _FontPlan(blocked=True, reason=reason)
        chars = "".join(sorted(set(full_text)))
        if not chars:
            return _FontPlan(blocked=True, reason=reason)

        # Кандидаты по убыванию желательности. Приоритетные каталоги — шрифты
        # документа и библиотеки — идут первыми, но за ними обязательно стоят
        # системные: в приоритетных лежат подмножества, вырезанные из чужих
        # PDF, и целиком внедрить такой шрифт удаётся не всегда (в нём может
        # не быть полных таблиц метрик). Для копирования отдельных глифов они
        # годятся, для внедрения нового шрифта — не обязательно.
        candidates: list = []
        # В точном режиме второй заход (по системным шрифтам) не делается
        # вовсе: подставлять чужой шрифт там запрещено
        rounds = (self.donor_priority_dirs,) if self.only_donor_glyphs \
            else (self.donor_priority_dirs, ())
        for priority in rounds:
            for found in (
                find_system_font(
                    font.base_font, font.style, required_chars=chars,
                    extra_dirs=self.extra_font_dirs, priority_dirs=priority,
                    only_priority=self.only_donor_glyphs,
                ),
                find_fallback_font(
                    chars, style=font.style, serif=font.is_serif,
                    extra_dirs=self.extra_font_dirs, priority_dirs=priority,
                    only_priority=self.only_donor_glyphs,
                ),
            ):
                if found is not None and not any(
                    found.path == other.path and found.index == other.index
                    for other in candidates
                ):
                    candidates.append(found)

        if not candidates:
            return _FontPlan(
                blocked=True,
                reason=f"{reason}; " + (
                    f"среди донорских шрифтов нет символов {chars!r}"
                    if self.only_donor_glyphs
                    else f"в системе нет шрифта с символами {chars!r}"
                ),
            )

        system = None
        font_dict = None
        last_error = ""
        for candidate in candidates:
            try:
                font_dict, _char_to_gid, _widths = fontops.embed_type0_font(
                    self.pdf, candidate, chars
                )
                system = candidate
                break
            except Exception as exc:
                last_error = str(exc)
        if system is None:
            return _FontPlan(
                blocked=True,
                reason=f"{reason}; запасной шрифт не внедрён: {last_error}",
            )

        fallback = FontInfo("/PEF", font_dict)
        report.font_changes.append(
            f"{font.base_font}: изменённый текст набран внедрённым запасным шрифтом "
            f"«{system.family}» ({reason})"
        )
        return _FontPlan(blocked=True, reason=reason,
                         fallback_font=fallback, fallback_dict=font_dict)

    # ------------------------------------------------------------------
    def _apply_to_stream(
        self,
        context: StreamContext,
        items: Sequence[tuple[EditSpec, TextRun]],
        report: ApplyReport,
        plans: dict[int, _FontPlan] | None = None,
    ) -> list[EditSpec]:
        """Пересобирает операторы показа текста одного потока содержимого."""
        # Правка → набор частей, по одной на каждый затронутый оператор
        per_op: dict[int, list[dict]] = {}
        applied: list[EditSpec] = []
        plans = plans or {}
        #: изменение ширины по строкам (ключ — оператор начала строки)
        line_deltas: dict[int, float] = {}
        line_runs: dict[int, TextRun] = {}

        #: группы, у которых не вышло поместить текст в первый кусок
        refused: set[int] = set()
        # Сначала «ведущие» правки, потом их продолжения: только так отказ
        # ведущей успевает отменить вычистку остальных кусков группы
        for edit, run in sorted(items, key=lambda pair: pair[0].continuation):
            if edit.group_id and edit.group_id in refused:
                report.skipped.append((
                    edit,
                    "правка разорванного фрагмента отменена целиком: "
                    "новый текст не удалось поместить в первый кусок",
                ))
                continue
            glyphs = run.glyphs[edit.glyph_start : edit.glyph_end]
            if not glyphs:
                continue
            font = run.font
            font_switch: str | None = None

            plan = plans.get(id(run.font))
            if plan is not None and plan.fallback_font is not None:
                if run.font.missing_chars(edit.new_text):
                    # Своими глифами текст не набрать — переключаемся на
                    # подставной шрифт только на время этого фрагмента
                    font = plan.fallback_font
                    font_switch = plan.resource_names.get(context.stream_id)
                    if font_switch is None:
                        font_switch = fontops.add_font_resource(
                            context.resources, plan.fallback_dict
                        )
                        plan.resource_names[context.stream_id] = font_switch
                        context.fonts[font_switch] = font

            new_bytes, new_codes, missing = font.encode(edit.new_text)
            if missing:
                report.skipped.append(
                    (edit, f"в шрифте нет глифов для символов: {missing!r}")
                )
                if edit.group_id:
                    refused.add(edit.group_id)
                continue

            # Продвижение пера для нового текста (в текстовых единицах до Tz)
            advance_new = 0.0
            for code in new_codes:
                is_space = font.code_size == 1 and code == 32
                advance_new += (
                    font.width(code) / 1000.0 * run.size
                    + run.char_spacing
                    + (run.word_spacing if is_space else 0.0)
                )

            # Насколько строка станет шире (или уже). Это понадобится, чтобы
            # сохранить выключку: у текста по правому краю или по центру
            # неизменным должно остаться не начало строки, а её край.
            #
            # Считать надо ОСТАТОЧНОЕ изменение — то, которое не убрано
            # подгонкой ширины. Режимы «preserve» и «squeeze» компенсируют
            # разницу на месте (кернингом в массиве TJ или горизонтальным
            # сжатием Tz), и строка после них той же ширины, что была. Если
            # брать сырую разницу, к уже скомпенсированной строке добавится
            # ещё и сдвиг начала `[число] TJ` — лишняя инструкция, которой в
            # потоке ничто не объясняет, и текст уедет вдвое.
            advance_old = sum(g.advance for g in glyphs)
            if self._effective_fit_mode(advance_old, advance_new) == "natural":
                line_deltas[run.line_start_op] = (
                    line_deltas.get(run.line_start_op, 0.0) + advance_new - advance_old
                )
            line_runs.setdefault(run.line_start_op, run)

            # Группируем глифы правки по операторам показа
            by_op: dict[int, list] = {}
            for glyph in glyphs:
                by_op.setdefault(glyph.op_index, []).append(glyph)
            ordered_ops = sorted(by_op)
            if len(ordered_ops) > 1:
                report.warnings.append(
                    f"фрагмент «{edit.old_text[:30]}» разорван на "
                    f"{len(ordered_ops)} оператора показа: новый текст помещён в первый"
                )

            for position, op_index in enumerate(ordered_ops):
                op_glyphs = by_op[op_index]
                part = {
                    "edit": edit,
                    "run": run,
                    "first_glyph": op_glyphs[0],
                    "last_glyph": op_glyphs[-1],
                    "insert_bytes": new_bytes if position == 0 else b"",
                    "advance_new": advance_new if position == 0 else 0.0,
                    "font_switch": font_switch if position == 0 else None,
                }
                per_op.setdefault(op_index, []).append(part)
            applied.append(edit)

        if not per_op:
            return applied

        # Строим индекс глифов по операторам: нужен, чтобы разобрать операнды
        glyphs_by_op: dict[int, list] = {}
        for run in self.runs:
            if run.stream_id != context.stream_id:
                continue
            for glyph in run.glyphs:
                glyphs_by_op.setdefault(glyph.op_index, []).append((glyph, run))
        for op_index in glyphs_by_op:
            glyphs_by_op[op_index].sort(key=lambda pair: (pair[0].elem_index, pair[0].byte_start))

        shifts = self._alignment_shifts(context, line_deltas, line_runs, report)

        # Пересборка идёт с конца, чтобы вставка новых инструкций не сдвигала
        # индексы ещё не обработанных операторов
        for op_index in sorted(set(per_op) | set(shifts), reverse=True):
            if op_index in per_op:
                instruction = context.instructions[op_index]
                replacement = self._rebuild_show_op(
                    instruction, glyphs_by_op.get(op_index, []), per_op[op_index]
                )
                context.instructions[op_index : op_index + 1] = replacement
                context.dirty = True
            if op_index in shifts:
                context.instructions.insert(op_index, shifts[op_index])
                context.dirty = True

        return applied

    def _effective_fit_mode(self, advance_old: float, advance_new: float) -> str:
        """Каким способом будет подогнана ширина заменяемого куска.

        Решение принимается дважды — при подсчёте сдвига выключки и при
        пересборке оператора показа, — и оба раза обязано выходить одинаковым,
        иначе строка получит и компенсацию ширины, и сдвиг начала сразу.
        Поэтому оно живёт здесь, а не в двух местах порознь.
        """
        mode = self.fit_mode
        if advance_new <= 0 and mode == "auto":
            # Вставлять нечего — это чистое удаление. Горизонтальным сжатием
            # тут компенсировать нечего, строка честно становится короче, и
            # выключку надо восстанавливать сдвигом
            return "natural"
        if mode != "auto":
            return mode
        # Небольшую разницу ширин прячем горизонтальным сжатием — это
        # незаметно и не двигает соседний текст. Заметную разницу сжатием
        # не спрятать, поэтому строка просто переверстывается.
        ratio = advance_old / advance_new
        return "squeeze" if abs(ratio - 1.0) <= AUTO_SQUEEZE_TOLERANCE else "natural"

    def _alignment_shifts(
        self,
        context: StreamContext,
        line_deltas: dict[int, float],
        line_runs: dict[int, "TextRun"],
        report: ApplyReport,
    ) -> dict[int, object]:
        """Готовит сдвиги начала строк, сохраняющие выключку.

        Сдвиг вставляется отдельной инструкцией ``[число] TJ`` перед первым
        оператором показа строки: она перемещает перо, ничего не рисуя, и
        действует до ближайшего оператора позиционирования — то есть ровно до
        конца этой строки. Изменять сам оператор ``Td`` нельзя: он задаёт
        положение относительно предыдущей строки, и правка сместила бы все
        последующие строки абзаца.
        """
        shifts: dict[int, object] = {}
        if not line_deltas or not self.align_aware:
            return shifts

        sample = next(iter(line_runs.values()), None)
        lines = self._page_lines(sample.page_index if sample else -1)
        if not lines:
            return shifts

        for start_op, delta in line_deltas.items():
            if start_op < 0 or abs(delta) < 0.01:
                continue
            run = line_runs.get(start_op)
            if run is None or not run.size:
                continue
            line = layout.find_line(run.bbox, lines)
            if line is None:
                continue
            alignment = layout.alignment_of(line, lines)
            shift = layout.shift_for_alignment(alignment, delta)
            if abs(shift) < 0.01:
                continue

            # Величина в массиве TJ задаётся в тысячных долях кегля, причём
            # положительное число сдвигает текст влево
            hscale = run.hscale or 1.0
            amount = shift * 1000.0 / (run.size * hscale)
            shifts[start_op] = pikepdf.ContentStreamInstruction(
                [pikepdf.Array([round(amount, 3)])], pikepdf.Operator("TJ")
            )
            report.warnings.append(
                f"строка «{run.text[:24]}»: выключка {alignment}, "
                f"начало сдвинуто на {shift:.1f} пт"
            )
        return shifts

    def _page_lines(self, page_index: int) -> list:
        """Строки страницы с их положением — для определения выключки."""
        if page_index < 0:
            return []
        cached = self._page_lines_cache.get(page_index)
        if cached is not None:
            return cached
        try:
            from .mupdf import fitz

            doc = fitz.open(stream=self.original_bytes, filetype="pdf")
            lines = layout.page_lines(doc[page_index])
            doc.close()
        except Exception:
            lines = []
        self._page_lines_cache[page_index] = lines
        return lines

    def _rebuild_show_op(
        self, instruction, glyph_pairs: list, parts: list[dict]
    ) -> list:
        """Собирает новый оператор показа текста вместо исходного."""
        operator = str(instruction.operator)
        operands = list(instruction.operands)

        # 1. Приводим операнд к списку элементов массива TJ
        prefix_instructions: list = []
        if operator == "TJ":
            elements = list(operands[0]) if operands and isinstance(operands[0], pikepdf.Array) else []
        elif operator == "Tj":
            elements = [operands[0]] if operands else []
        elif operator == "'":
            # Перевод строки выносим в отдельный оператор T*
            prefix_instructions.append(
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("T*"))
            )
            elements = [operands[0]] if operands else []
        elif operator == '"':
            if len(operands) >= 3:
                prefix_instructions += [
                    pikepdf.ContentStreamInstruction([operands[0]], pikepdf.Operator("Tw")),
                    pikepdf.ContentStreamInstruction([operands[1]], pikepdf.Operator("Tc")),
                ]
            prefix_instructions.append(
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("T*"))
            )
            elements = [operands[2]] if len(operands) >= 3 else []
        else:
            return [instruction]

        # 2. Раскладываем операнды на атомы: числа и отдельные глифы
        glyphs_of_op = [glyph for glyph, _ in glyph_pairs]
        by_element: dict[int, list] = {}
        for index, glyph in enumerate(glyphs_of_op):
            by_element.setdefault(glyph.elem_index, []).append((index, glyph))

        atoms: list[_Atom] = []
        for elem_index, element in enumerate(elements):
            if isinstance(element, (int, float)):
                atoms.append(_Atom("num", float(element)))
                continue
            if not isinstance(element, pikepdf.String):
                atoms.append(_Atom("raw", bytes(str(element), "latin-1")))
                continue
            raw = bytes(element)
            cursor = 0
            for glyph_index, glyph in by_element.get(elem_index, []):
                if glyph.byte_start > cursor:
                    atoms.append(_Atom("raw", raw[cursor : glyph.byte_start]))
                atoms.append(
                    _Atom(
                        "glyph",
                        raw[glyph.byte_start : glyph.byte_start + glyph.byte_len],
                        glyph_index,
                    )
                )
                cursor = glyph.byte_start + glyph.byte_len
            if cursor < len(raw):
                atoms.append(_Atom("raw", raw[cursor:]))

        position_of_glyph = {
            atom.glyph_index: index for index, atom in enumerate(atoms) if atom.kind == "glyph"
        }
        glyph_number = {id(glyph): index for index, glyph in enumerate(glyphs_of_op)}

        # 3. Помечаем удаляемое и планируем вставки
        insertions: dict[int, bytes] = {}
        compensations: dict[int, float] = {}
        scales: dict[int, float] = {}
        font_switches: dict[int, tuple[str, float, str]] = {}
        for part in parts:
            first_index = glyph_number.get(id(part["first_glyph"]))
            last_index = glyph_number.get(id(part["last_glyph"]))
            if first_index is None or last_index is None:
                continue
            start_atom = position_of_glyph.get(first_index)
            end_atom = position_of_glyph.get(last_index)
            if start_atom is None or end_atom is None:
                continue

            advance_deleted = 0.0
            run = part["run"]
            for index in range(start_atom, end_atom + 1):
                atom = atoms[index]
                if atom.deleted:
                    continue
                if atom.kind == "glyph":
                    advance_deleted += glyphs_of_op[atom.glyph_index].advance
                    atom.deleted = True
                elif atom.kind == "num":
                    # Кернинг внутри заменяемого куска исчезает вместе с ним
                    advance_deleted += -float(atom.value) / 1000.0 * run.size
                    atom.deleted = True

            if part["insert_bytes"]:
                insertions[start_atom] = part["insert_bytes"]
                if part.get("font_switch"):
                    font_switches[start_atom] = (
                        part["font_switch"], run.size, run.font_res
                    )
            delta = part["advance_new"] - advance_deleted
            mode = self._effective_fit_mode(advance_deleted, part["advance_new"])

            if mode == "preserve" and run.size:
                # Число в TJ сдвигает перо на -n/1000 * кегль: подбираем n так,
                # чтобы суммарное продвижение осталось прежним
                number = delta * 1000.0 / run.size
                if abs(number) > 0.01:
                    compensations[end_atom] = compensations.get(end_atom, 0.0) + number
            elif mode == "squeeze" and part["insert_bytes"] and part["advance_new"] > 0:
                # Новый текст сжимается или растягивается по горизонтали так,
                # чтобы занять ровно ширину заменённого — соседний текст не
                # сдвинется и не перекроется
                scale = run.hscale * advance_deleted / part["advance_new"]
                if abs(scale - run.hscale) > 1e-4:
                    scales[start_atom] = scale

        # 4. Собираем новые элементы, разбивая их на сегменты.
        #    Обычно сегмент один; в режиме «squeeze» вставленный текст выносится
        #    в отдельный сегмент, обрамлённый операторами Tz.
        segments: list[tuple[float | None, tuple | None, list]] = []
        new_elements: list = []
        pending = bytearray()

        def flush_bytes() -> None:
            if pending:
                new_elements.append(pikepdf.String(bytes(pending)))
                pending.clear()

        def flush_segment(scale: float | None = None, switch: tuple | None = None) -> None:
            nonlocal new_elements
            flush_bytes()
            if new_elements:
                segments.append((scale, switch, new_elements))
            new_elements = []

        for index, atom in enumerate(atoms):
            if index in insertions:
                scale = scales.get(index)
                switch = font_switches.get(index)
                if scale is not None or switch is not None:
                    # Вставку выносим в отдельный сегмент: вокруг него встанут
                    # операторы Tz и/или Tf, действующие только на неё
                    flush_segment()
                    pending += insertions[index]
                    flush_segment(scale, switch)
                else:
                    pending += insertions[index]
            if not atom.deleted:
                if atom.kind == "num":
                    flush_bytes()
                    new_elements.append(_clean_number(float(atom.value)))
                else:
                    pending += atom.value  # type: ignore[operator]
            if index in compensations:
                flush_bytes()
                new_elements.append(_clean_number(compensations[index]))
        flush_segment()

        # Пустой оператор показа лучше убрать целиком
        if not segments:
            return prefix_instructions

        # 5. Превращаем сегменты в инструкции
        result = list(prefix_instructions)
        base_hscale = parts[0]["run"].hscale if parts else 1.0
        for scale, switch, elements in segments:
            if switch is not None:
                resource_name, size, _original = switch
                result.append(
                    pikepdf.ContentStreamInstruction(
                        [pikepdf.Name(resource_name), _clean_number(size)],
                        pikepdf.Operator("Tf"),
                    )
                )
            if scale is not None:
                result.append(
                    pikepdf.ContentStreamInstruction(
                        [_clean_number(scale * 100.0)], pikepdf.Operator("Tz")
                    )
                )
            # Одна строка без чисел записывается компактным Tj — так поток
            # ближе всего к исходному
            if len(elements) == 1 and isinstance(elements[0], pikepdf.String):
                result.append(
                    pikepdf.ContentStreamInstruction([elements[0]], pikepdf.Operator("Tj"))
                )
            else:
                result.append(
                    pikepdf.ContentStreamInstruction(
                        [pikepdf.Array(elements)], pikepdf.Operator("TJ")
                    )
                )
            if scale is not None:
                result.append(
                    pikepdf.ContentStreamInstruction(
                        [_clean_number(base_hscale * 100.0)], pikepdf.Operator("Tz")
                    )
                )
            if switch is not None:
                # Возвращаем исходный шрифт, иначе им окажется набран
                # весь последующий текст того же текстового объекта
                _resource_name, size, original = switch
                result.append(
                    pikepdf.ContentStreamInstruction(
                        [pikepdf.Name(original), _clean_number(size)],
                        pikepdf.Operator("Tf"),
                    )
                )
        return result

    # ------------------------------------------------------------------
    def _write_back(self, context: StreamContext) -> None:
        """Записывает пересобранные инструкции обратно в поток содержимого.

        Сначала пробуется точечная замена (:mod:`pdfedit.streampatch`): она
        оставляет нетронутые инструкции ровно теми байтами, какими они были, а
        новые пишет в манере тех, которые они заменяют. Полная пересборка
        отдала бы форматирование библиотеке — та печатает строки, числа и
        пробелы по-своему, и место правки становится видно в распакованном
        потоке невооружённым глазом. Пересборка остаётся запасным путём: если
        точечная замена в чём-то не уверена, она отказывается работать.
        """
        data = None
        if context.original_data:
            from .streampatch import patch_content

            try:
                data = patch_content(
                    context.original_data,
                    context.original_instructions,
                    context.instructions,
                )
            except Exception:
                data = None
        owner = context.owner
        if context.is_page:
            contents = owner.get("/Contents")
            if isinstance(contents, pikepdf.Array):
                if self._write_back_split(context, contents):
                    context.dirty = False
                    return
                # Разложить правку по своим потокам не вышло: сворачиваем их в
                # один. Это меняет словарь страницы, и правка на месте станет
                # невозможной — но содержимое будет верным
                if data is None:
                    data = pikepdf.unparse_content_stream(context.instructions)
                first = contents[0]
                first.write(data)
                if len(contents) > 1:
                    owner["/Contents"] = pikepdf.Array([first])
                context.dirty = False
                return
            if data is None:
                data = pikepdf.unparse_content_stream(context.instructions)
            if isinstance(contents, pikepdf.Stream):
                contents.write(data)
            else:
                owner["/Contents"] = self.pdf.make_stream(data)
        else:
            if data is None:
                data = pikepdf.unparse_content_stream(context.instructions)
            owner.write(data)
        context.dirty = False

    def _write_back_split(self, context: StreamContext, contents) -> bool:
        """Записывает правку обратно в те же потоки массива ``/Contents``.

        Страница часто хранит содержимое не одним потоком, а массивом: так
        делают PyMuPDF, многие отчётные генераторы и любой документ, к
        которому что-то дописывали. Разбирается такой массив как одно целое,
        и раньше обратно он и записывался одним целым — всё в первый поток,
        остальные отцеплялись. Содержимое от этого верное, но страница
        меняется куда сильнее, чем требовала правка: у неё меняется словарь,
        часть объектов остаётся без ссылок, и правка на месте становится
        невозможной в принципе.

        Здесь инструкции раскладываются обратно по своим потокам, и трогается
        только тот из них, в котором действительно что-то изменилось.
        Возвращает ``False``, если разложить не удалось — тогда остаётся
        прежний путь со сворачиванием.
        """
        from .streampatch import patch_content

        streams = [item for item in contents if isinstance(item, pikepdf.Stream)]
        if len(streams) != len(contents):
            return False
        # Число инструкций должно совпадать: правка, добавившая или убравшая
        # инструкцию, сдвинула бы границы между потоками, и разложить их по
        # прежним местам было бы уже нельзя
        if len(context.instructions) != len(context.original_instructions):
            return False

        counts: list[int] = []
        pieces: list[bytes] = []
        try:
            for stream in streams:
                pieces.append(stream.read_bytes())
                counts.append(len(list(pikepdf.parse_content_stream(stream))))
        except Exception:
            return False
        if sum(counts) != len(context.original_instructions):
            return False

        written: list[tuple[pikepdf.Stream, bytes]] = []
        start = 0
        for stream, raw, count in zip(streams, pieces, counts):
            end = start + count
            before = context.original_instructions[start:end]
            after = context.instructions[start:end]
            start = end
            if all(left is right for left, right in zip(before, after)):
                continue  # этот поток правка не тронула
            patched = patch_content(raw, before, after)
            if patched is None:
                return False
            written.append((stream, patched))

        for stream, patched in written:
            stream.write(patched)
        return True

    # ------------------------------------------------------------------
    # Высокоуровневые операции
    # ------------------------------------------------------------------
    def replace(
        self,
        old: str,
        new: str,
        count: int = 0,
        regex: bool = False,
        ignore_case: bool = False,
        pages: Iterable[int] | None = None,
        whole_word: bool = False,
    ) -> ApplyReport:
        """Находит текст и заменяет его; ``count=0`` — заменить все вхождения.

        Поиск идёт по визуальным строкам, а не по отдельным фрагментам потока:
        текст, разбитый на несколько операторов показа, для читателя выглядит
        сплошным и должен так же и заменяться (см. :meth:`find_grouped`).
        """
        matches = self.find_grouped(old, regex=regex, ignore_case=ignore_case,
                                    pages=pages, whole_word=whole_word)
        if not matches:
            hint = self._not_found_hint(old, ignore_case)
            raise TextNotFoundError(f"текст {old!r} не найден{hint}")
        if count > 0:
            matches = matches[:count]

        edits: list[EditSpec] = []
        for match in matches:
            replacement = new
            if regex and match.regex_match is not None:
                # В режиме регулярных выражений допускаем ссылки на группы:
                # подстановка берётся из того самого совпадения, а не из первого
                try:
                    replacement = match.regex_match.expand(new)
                except (re.error, IndexError):
                    replacement = new
            covered = "".join(
                glyph.text
                for run, start, end in match.pieces
                for glyph in run.glyphs[start:end]
            )
            if covered != match.text and match.text in covered:
                # Совпадение задело лигатуру: дополняем новый текст её остатком
                head = covered[: covered.find(match.text)]
                tail = covered[len(head) + len(match.text):]
                replacement = head + replacement + tail
            edits += match.to_edits(replacement)
        return self.apply_edits(edits)

    def _not_found_hint(self, needle: str, ignore_case: bool) -> str:
        """Подсказывает, почему текст не найден, если он есть на странице."""
        normalized, _ = normalize_for_search(needle)
        probe = normalized.lower() if ignore_case else normalized
        for page_index in range(self.page_count):
            text, _ = normalize_for_search(self.page_text(page_index))
            haystack = text.lower() if ignore_case else text
            if probe in haystack.replace("\n", " "):
                return (
                    f" в пределах одного фрагмента, но встречается на странице "
                    f"{page_index + 1}, разорванный на несколько фрагментов "
                    f"(разные строки или шрифты). Замените части по отдельности "
                    f"или воспользуйтесь графическим режимом."
                )
        return ""

    # ------------------------------------------------------------------
    def save(
        self, path: str, incremental: bool = False, in_place: bool = False, **kwargs
    ) -> None:
        """Сохраняет документ.

        ``incremental=False`` — обычный путь: файл собирается заново, следов
        правки в структуре не остаётся (:mod:`pdfedit.saving`).

        ``incremental=True`` — исходные байты остаются нетронутыми, а новые
        редакции изменённых объектов дописываются в конец файла
        (:mod:`pdfedit.incremental`). Так гарантированно сохраняются номера
        объектов, все неизменённые потоки, шифрование и предыдущие редакции
        документа. Отчёт о дописанном остаётся в :attr:`last_incremental_report`.

        ``in_place=True`` — изменённые потоки записываются поверх старых, на то
        же место и той же длины (:mod:`pdfedit.inplace`). Файл тогда не меняется
        нигде, кроме самих этих байтов: ни смещения, ни таблица ссылок, ни
        трейлер. Что не помещается в исходную длину, дописывается слоем.
        """
        if in_place:
            from .inplace import save_in_place

            report, layer = save_in_place(
                self.pdf, path, self.original_bytes, password=self._password
            )
            self.last_in_place_report = report
            self.last_incremental_report = layer
            return

        if incremental:
            from .incremental import save_incremental

            self.last_incremental_report = save_incremental(
                self.pdf,
                path,
                self.original_bytes,
                compress=kwargs.get("compress", True),
                password=self._password,
            )
            return

        from .saving import save_clean

        profile = save_clean(self.pdf, path, self.original_bytes, **kwargs)
        #: что пришлось разменять при воспроизведении защиты документа
        self.last_encryption_notes = list(profile.encryption_notes)

    def to_bytes(self, incremental: bool = False, in_place: bool = False, **kwargs) -> bytes:
        if in_place:
            from .inplace import build_in_place

            data, report, layer = build_in_place(
                self.pdf, self.original_bytes, password=self._password
            )
            self.last_in_place_report = report
            self.last_incremental_report = layer
            return data

        if incremental:
            from .incremental import incremental_bytes

            return incremental_bytes(
                self.pdf,
                self.original_bytes,
                compress=kwargs.get("compress", True),
                password=self._password,
            )

        from .saving import save_clean_to_bytes

        return save_clean_to_bytes(self.pdf, self.original_bytes, **kwargs)


def minimal_edit(old_text: str, new_text: str) -> tuple[int, int, str] | None:
    """Находит минимальный изменившийся кусок между двумя строками.

    Возвращает ``(начало, конец, замена)`` в координатах символов ``old_text``
    либо ``None``, если строки совпадают. Позволяет при правке целой строки
    переписать в потоке только действительно изменившиеся байты, сохранив
    кернинг остальной части.
    """
    if old_text == new_text:
        return None
    prefix = 0
    limit = min(len(old_text), len(new_text))
    while prefix < limit and old_text[prefix] == new_text[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < limit - prefix
        and old_text[len(old_text) - 1 - suffix] == new_text[len(new_text) - 1 - suffix]
    ):
        suffix += 1
    start = prefix
    end = len(old_text) - suffix
    replacement = new_text[prefix : len(new_text) - suffix]

    if start == end:
        # Чистая вставка: захватываем соседний символ, чтобы диапазон глифов
        # не оказался пустым — заменять «ничто» в потоке не на что
        if start > 0:
            start -= 1
            replacement = old_text[start] + replacement
        elif end < len(old_text):
            end += 1
            replacement = replacement + old_text[end - 1]
        else:
            return None
    return (start, end, replacement)


def _clean_number(value: float):
    """Округляет число для записи в поток, убирая лишние знаки."""
    rounded = round(value, 3)
    if abs(rounded - round(rounded)) < 1e-9:
        return int(round(rounded))
    return rounded
