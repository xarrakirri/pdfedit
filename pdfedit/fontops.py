"""Модификация шрифтов документа: добавление глифов и внедрение запасных.

Если новый текст содержит символы, которых нет во внедрённом подмножестве
шрифта (частый случай: документ набран латиницей, а вставляется кириллица),
есть три пути:

1. **Расширить подмножество** — взять контуры недостающих глифов из системного
   шрифта того же семейства и дописать их во внедрённую программу. Внешне
   результат неотличим от исходного набора: те же контуры, те же метрики.
2. **Внедрить запасной шрифт** — если донора того же семейства нет или формат
   программы не поддаётся правке (CFF/Type1), добавить в ресурсы новый шрифт
   и использовать его только для изменённого фрагмента.
3. **Отказаться от замены** — если пользователь запретил трогать шрифты.

Здесь реализованы первые два пути.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from io import BytesIO
from typing import Iterable

import pikepdf

# fontTools подробно рассказывает о таблицах, которые не умеет обрабатывать
# («morx NOT subset… dropped»). Для нас это штатное поведение, а в выводе
# программы такие сообщения только мешают.
for _noisy in ("fontTools", "fontTools.subset", "fontTools.ttLib"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

from .cmap import build_tounicode_cmap
from .encodings_tables import glyph_name_for
from .errors import UnsupportedFontProgramError
from .fonts import FontInfo, SystemFont, find_fallback_font, find_system_font


@dataclass
class ExtensionResult:
    """Итог расширения шрифта."""

    added: dict[str, int] = field(default_factory=dict)   # символ → код
    donor: str = ""
    failed: str = ""                                      # символы, что не вышло добавить
    notes: list[str] = field(default_factory=list)


def _no_donor_message(chars: str, only_priority: bool) -> str:
    """Объясняет, где именно искали глифы и не нашли.

    В точном режиме системные шрифты не просматриваются вовсе, и жалоба «в
    системе нет такого шрифта» сбивала бы с толку: искать там никто и не
    пробовал.
    """
    if only_priority:
        return (f"среди донорских шрифтов нет символов: {chars!r} "
                f"(точный режим: другие источники запрещены)")
    return f"в системе нет шрифта с символами: {chars!r}"


def _donor_note(font: FontInfo, donor: SystemFont) -> str:
    """Составляет понятное объяснение, какой донор взят и чем он отличается."""
    wanted, got = font.style, donor.style
    if wanted.bold == got.bold and wanted.italic == got.italic:
        return (f"донор того же семейства не найден, взят {donor.family} "
                f"— начертание сохранено")
    lost = []
    if wanted.bold and not got.bold:
        lost.append("жирность")
    if wanted.italic and not got.italic:
        lost.append("курсив")
    if lost:
        return (f"донор того же семейства не найден, взят {donor.family}: "
                f"в системе нет подходящего шрифта, где есть и нужные символы, "
                f"и {' и '.join(lost)} — новые символы будут отличаться от соседних")
    return f"донор того же семейства не найден, взят {donor.family}"


def _load_donor(path: str, index: int, target_upem: int):
    """Открывает системный шрифт-донор, приводя его к нужному кегельному кубу."""
    from fontTools.ttLib import TTFont

    # Донора мы не сохраняем, поэтому пересчёт габаритных рамок ему не вредит,
    # а после scale_upem он прямо необходим: масштабированные контуры со
    # старыми рамками отрисовываются обрезанными
    donor = TTFont(path, fontNumber=index, recalcTimestamp=False)
    donor_upem = donor["head"].unitsPerEm
    if donor_upem != target_upem:
        # Контуры нужно масштабировать, иначе новые глифы будут другого размера
        from fontTools.ttLib.scaleUpem import scale_upem

        scale_upem(donor, target_upem)
    return donor


def physical_table_order(program: bytes) -> list[str]:
    """Порядок таблиц в файле шрифта — по возрастанию смещения.

    Именно физический порядок, а не алфавитный: каталог таблиц отсортирован по
    тегам всегда (этого требует формат), а вот сами таблицы лежат так, как их
    уложил тот, кто шрифт собирал. Порядок у каждого сборщика свой и меняется
    редко — поэтому по нему и опознают, что шрифт пересобирали.
    """
    from fontTools.ttLib import TTFont

    try:
        font = TTFont(BytesIO(program), lazy=True)
    except Exception:
        return []
    try:
        entries = font.reader.tables
        return sorted(entries.keys(), key=lambda tag: entries[tag].offset)
    except Exception:
        return []
    finally:
        font.close()


def save_font_preserving_layout(font, order: list[str]) -> bytes:
    """Сохраняет шрифт, восстанавливая исходный физический порядок таблиц.

    ``TTFont.save`` раскладывает таблицы в своём каноническом порядке
    (``sortedTagList``), а не в том, в каком они лежали. На Arial это, к
    примеру, переносит ``cmap`` с двадцать второго места на восьмое: содержимое
    то же, файл другой. Для нас это чистый след правки, поэтому порядок
    возвращается на место.
    """
    from fontTools.ttLib import reorderFontTables

    raw = BytesIO()
    font.save(raw)
    if not order:
        return raw.getvalue()

    # Таблицы, которых в оригинале не было, дописываем в конец: выкинуть их
    # нельзя, а вставлять в середину неоткуда
    try:
        present = physical_table_order(raw.getvalue())
    except Exception:
        return raw.getvalue()
    wanted = [tag for tag in order if tag in present]
    wanted += [tag for tag in present if tag not in wanted]
    if wanted == present:
        return raw.getvalue()

    out = BytesIO()
    try:
        raw.seek(0)
        reorderFontTables(raw, out, tableOrder=wanted)
    except Exception:
        # Переупорядочивание — улучшение, а не условие работоспособности
        return raw.getvalue()
    return out.getvalue()


def _copy_glyph(donor, donor_glyph_name: str, add_helper=None):
    """Копирует глиф из донора **дословно**, вместе с хинтингом.

    Дословность здесь не педантизм. Перерисовка пером даёт те же контуры, но
    теряет инструкции хинтинга, и глиф, побайтово совпадавший с донорским,
    перестаёт с ним совпадать. Для точного режима это и есть главное
    требование: в документе оказывается ровно донорский глиф, а не похожий.

    Составной глиф (буква из основы и надстрочного знака) ссылается на другие
    глифы по имени, и они тоже переносятся — ``add_helper`` дописывает их во
    внедрённый шрифт и возвращает новое имя. Меняются только эти ссылки:
    контуры и хинтинг и самого глифа, и его составляющих остаются донорскими.
    """
    import copy

    glyph = copy.deepcopy(donor["glyf"][donor_glyph_name])
    if not glyph.isComposite() or add_helper is None:
        return glyph
    for component in glyph.components:
        component.glyphName = add_helper(component.glyphName)
    return glyph


def glyph_is_verbatim(donor, donor_glyph_name: str) -> bool:
    """Копируется ли глиф совсем без изменений (простой, без ссылок)."""
    try:
        return not donor["glyf"][donor_glyph_name].isComposite()
    except Exception:
        return False


def _unique_glyph_name(existing: set[str], char: str) -> str:
    base = glyph_name_for(char)
    if base not in existing:
        return base
    counter = 1
    while f"{base}.alt{counter}" in existing:
        counter += 1
    return f"{base}.alt{counter}"


def copy_glyphs_from_donor(program: bytes, donor_font, chars: str):
    """Дописывает глифы из донорского шрифта во внедрённую программу TrueType.

    Возвращает ``(новые байты шрифта, {символ: (gid, ширина в единицах 1000)})``.

    Копирование сделано так, чтобы результат отличался от исходной программы
    ровно на добавленные глифы и ни на что больше:

    * ``recalcTimestamp=False`` — иначе fontTools запишет в ``head.modified``
      текущее время, и шрифт, созданный в 2002 году, окажется «изменён»
      сегодня. Это первое, на что смотрят при разборе файла;
    * ``recalcBBoxes=False`` — иначе пересчитываются габаритные рамки **всех**
      глифов, включая нетронутые. У аккуратно собранного шрифта рамки и так
      верны и не изменятся, но у собранного небрежно (а таких немало) правка
      разойдётся по всей таблице ``glyf``, и «изменённым» окажется весь шрифт;
    * физический порядок таблиц восстанавливается
      (:func:`save_font_preserving_layout`) — fontTools укладывает их по-своему;
    * хинтинг (``fpgm``, ``prep``, ``cvt``) не трогается вовсе: новые глифы
      идут без инструкций, а чужие остаются как были;
    * ширина нового глифа кладётся и в ``hmtx``, и — вызывающей стороной — в
      ``/W`` или ``/Widths``, причём из одного источника, чтобы значения не
      разошлись (см. :func:`width_to_pdf`).
    """
    from fontTools.ttLib import TTFont

    original_order = physical_table_order(program)
    emb = TTFont(BytesIO(program), recalcTimestamp=False, recalcBBoxes=False)
    if "glyf" not in emb:
        raise UnsupportedFontProgramError(
            "внедрённая программа не содержит таблицу glyf (вероятно, CFF)"
        )
    upem = emb["head"].unitsPerEm
    donor = _load_donor(donor_font.path, donor_font.index, upem)
    donor_cmap = donor.getBestCmap()

    glyf = emb["glyf"]
    hmtx = emb["hmtx"]
    order = list(emb.getGlyphOrder())
    existing = set(order)
    added: dict[str, tuple[int, float]] = {}

    #: донорское имя глифа → имя, под которым он лёг во внедрённый шрифт
    transferred: dict[str, str] = {}

    def transfer(donor_name: str, char: str | None = None) -> str:
        """Переносит глиф (и его составляющие) в внедрённый шрифт."""
        known = transferred.get(donor_name)
        if known is not None:
            return known
        # Имя резервируется до копирования: составной глиф может ссылаться
        # сам на себя через цепочку, и без метки обход бы зациклился
        new_name = _unique_glyph_name(
            existing, char if char is not None else donor_name
        )
        transferred[donor_name] = new_name
        existing.add(new_name)
        glyph = _copy_glyph(donor, donor_name, add_helper=transfer)
        order.append(new_name)
        glyf.glyphs[new_name] = glyph
        advance, lsb = donor["hmtx"][donor_name]
        # Ширина в hmtx — целое число единиц шрифта; в PDF она пойдёт
        # пересчитанной в тысячные доли кегля из этого же значения
        hmtx.metrics[new_name] = (int(advance), int(lsb))
        return new_name

    for char in chars:
        donor_name = donor_cmap.get(ord(char))
        if donor_name is None:
            continue
        try:
            new_name = transfer(donor_name, char)
        except Exception:
            continue
        gid = order.index(new_name)
        advance = donor["hmtx"][donor_name][0]
        added[char] = (gid, width_to_pdf(int(advance), upem))

    if not added:
        donor.close()
        raise UnsupportedFontProgramError("донор не содержит ни одного нужного глифа")

    emb.setGlyphOrder(order)
    glyf.glyphOrder = order
    emb["maxp"].numGlyphs = len(order)
    # Таблица cmap внедрённого шрифта для CID-шрифтов не используется, но для
    # простых шрифтов новый код обязан через неё разрешаться.
    _update_embedded_cmap(emb, {ch: order[gid] for ch, (gid, _) in added.items()})

    data = save_font_preserving_layout(emb, original_order)
    donor.close()
    emb.close()
    return data, added


#: Прежнее имя функции: код внутри программы звал её так
_append_glyphs_to_truetype = copy_glyphs_from_donor


def width_to_pdf(advance: int, upem: int) -> float:
    """Ширина глифа в тысячных долях кегля — так, как её пишут в PDF.

    Единственное место, где делается этот пересчёт. Ширина глифа хранится
    дважды: в таблице ``hmtx`` самого шрифта и в ``/W`` (или ``/Widths``)
    словаря PDF. Расхождение между ними — не косметика: просмотрщик
    расставляет буквы по ``/W``, а печатающее устройство может взять ``hmtx``,
    и текст «поплывёт». Для проверяющего же это прямое указание, что шрифт и
    словарь правились порознь.
    """
    if not upem:
        return 0.0
    return round(advance * 1000.0 / upem, 2)


def _update_embedded_cmap(font, char_to_glyph_name: dict[str, str]) -> None:
    """Добавляет соответствия Unicode → глиф в cmap внедрённого шрифта."""
    cmap_table = font.get("cmap")
    if cmap_table is None:
        return
    for sub in cmap_table.tables:
        try:
            if sub.platformID == 3 and sub.platEncID == 0:
                # Символьная подтаблица: коды живут в диапазоне F000–F0FF
                for char, gname in char_to_glyph_name.items():
                    if ord(char) <= 0xFF:
                        sub.cmap[0xF000 + ord(char)] = gname
            elif sub.platformID == 3 and sub.platEncID in (1, 10):
                for char, gname in char_to_glyph_name.items():
                    sub.cmap[ord(char)] = gname
            elif sub.platformID == 0:
                for char, gname in char_to_glyph_name.items():
                    sub.cmap[ord(char)] = gname
        except Exception:
            continue


# ----------------------------------------------------------------------
# Расширение составного (CID) шрифта
# ----------------------------------------------------------------------

def extend_cid_font(
    pdf: pikepdf.Pdf, font: FontInfo, chars: str,
    extra_font_dirs: Iterable[str] = (), priority_dirs: Iterable[str] = (),
    only_priority: bool = False,
) -> ExtensionResult:
    """Добавляет глифы в составной шрифт Type0/CIDFontType2.

    Работает только при ``/CIDToGIDMap`` = Identity — тогда CID нового глифа
    совпадает с его номером в программе шрифта, и достаточно дописать глиф в
    конец, расширив ``/W`` и ``/ToUnicode``.
    """
    result = ExtensionResult()
    if font.program_kind != "truetype":
        raise UnsupportedFontProgramError(
            f"расширение поддержано только для TrueType, а здесь {font.program_kind}"
        )
    cid2gid = font.metrics_dict.get("/CIDToGIDMap")
    if isinstance(cid2gid, pikepdf.Stream):
        raise UnsupportedFontProgramError(
            "явная таблица /CIDToGIDMap не поддержана при расширении"
        )

    donor = find_system_font(
        font.base_font, font.style, required_chars=chars,
        extra_dirs=extra_font_dirs, priority_dirs=priority_dirs,
        only_priority=only_priority,
    )
    if donor is None:
        donor = find_fallback_font(
            chars, style=font.style, serif=font.is_serif,
            extra_dirs=extra_font_dirs, priority_dirs=priority_dirs,
            only_priority=only_priority,
        )
        if donor is None:
            raise UnsupportedFontProgramError(
                _no_donor_message(chars, only_priority)
            )
        result.notes.append(_donor_note(font, donor))
    result.donor = f"{donor.family} ({donor.path})"

    new_program, added = _append_glyphs_to_truetype(font.font_program, donor, chars)

    key = font.font_file_key
    stream = font.descriptor[key]
    stream.write(new_program)
    stream["/Length1"] = len(new_program)

    # /W: дописываем ширины новых CID (CID == GID)
    widths = font.metrics_dict.get("/W")
    if widths is None:
        widths = pikepdf.Array()
        font.metrics_dict["/W"] = widths
    for char, (gid, width) in sorted(added.items(), key=lambda kv: kv[1][0]):
        widths.append(gid)
        widths.append(pikepdf.Array([round(width, 2)]))
        font.register_glyph(gid, char, width)
        result.added[char] = gid

    _extend_cid_set(font, [gid for gid, _ in added.values()])
    rewrite_tounicode(pdf, font)

    missing = "".join(ch for ch in chars if ch not in added)
    result.failed = missing
    return result


def _extend_cid_set(font: FontInfo, gids: list[int]) -> None:
    """Обновляет ``/CIDSet`` дескриптора — битовую карту используемых CID."""
    if font.descriptor is None or "/CIDSet" not in font.descriptor:
        return
    stream = font.descriptor["/CIDSet"]
    data = bytearray(stream.read_bytes())
    for gid in gids:
        byte_index = gid // 8
        if byte_index >= len(data):
            data.extend(b"\x00" * (byte_index + 1 - len(data)))
        data[byte_index] |= 0x80 >> (gid % 8)
    stream.write(bytes(data))


def rewrite_tounicode(pdf: pikepdf.Pdf, font: FontInfo) -> None:
    """Перезаписывает ``/ToUnicode`` актуальной таблицей шрифта.

    Без этого шага текст в изменённом фрагменте перестанет копироваться и
    искаться в просмотрщике — заметный след правки.
    """
    mapping = font.to_unicode_map
    if not mapping:
        return
    data = build_tounicode_cmap(mapping, code_bytes=font.code_size)
    existing = font.dict.get("/ToUnicode")
    if isinstance(existing, pikepdf.Stream):
        existing.write(data)
    else:
        font.dict["/ToUnicode"] = pdf.make_stream(data)


# ----------------------------------------------------------------------
# Расширение простого шрифта
# ----------------------------------------------------------------------

def extend_simple_font(
    pdf: pikepdf.Pdf,
    font: FontInfo,
    chars: str,
    extra_font_dirs: Iterable[str] = (),
    codes_in_use: set[int] | None = None,
    priority_dirs: Iterable[str] = (),
    only_priority: bool = False,
) -> ExtensionResult:
    """Добавляет глифы в простой шрифт TrueType, занимая свободные коды 0–255.

    «Свободный» здесь — код, которым в документе не написан ни один символ.
    Переопределить его через ``/Differences`` безопасно: на вид документа это
    не влияет, потому что такой код нигде не встречается.
    """
    result = ExtensionResult()
    if font.program_kind != "truetype":
        raise UnsupportedFontProgramError(
            f"расширение простого шрифта поддержано только для TrueType, здесь {font.program_kind}"
        )

    used = set(codes_in_use) if codes_in_use is not None else set(font.to_unicode_map)
    free_codes = [c for c in range(32, 256) if c not in used]
    if len(free_codes) < len(chars):
        raise UnsupportedFontProgramError(
            f"в однобайтовой кодировке шрифта свободно всего {len(free_codes)} кодов, "
            f"а нужно {len(chars)}"
        )

    donor = find_system_font(
        font.base_font, font.style, required_chars=chars,
        extra_dirs=extra_font_dirs, priority_dirs=priority_dirs,
        only_priority=only_priority,
    )
    if donor is None:
        donor = find_fallback_font(
            chars, style=font.style, serif=font.is_serif,
            extra_dirs=extra_font_dirs, priority_dirs=priority_dirs,
            only_priority=only_priority,
        )
        if donor is None:
            raise UnsupportedFontProgramError(_no_donor_message(chars, only_priority))
        result.notes.append(_donor_note(font, donor))
    result.donor = f"{donor.family} ({donor.path})"

    new_program, added = _append_glyphs_to_truetype(font.font_program, donor, chars)

    stream = font.descriptor[font.font_file_key]
    stream.write(new_program)
    stream["/Length1"] = len(new_program)

    # Кодам сопоставляем имена глифов через /Differences
    encoding = font.dict.get("/Encoding")
    if not isinstance(encoding, pikepdf.Dictionary):
        base = encoding if isinstance(encoding, pikepdf.Name) else pikepdf.Name("/StandardEncoding")
        encoding = pikepdf.Dictionary(Type=pikepdf.Name("/Encoding"), BaseEncoding=base)
        font.dict["/Encoding"] = encoding
    differences = encoding.get("/Differences")
    if differences is None:
        differences = pikepdf.Array()
        encoding["/Differences"] = differences

    widths_map = dict(font._widths)  # noqa: SLF001 — обновляем метрики того же объекта
    for char, code in zip(chars, free_codes):
        if char not in added:
            continue
        _gid, width = added[char]
        differences.append(code)
        differences.append(pikepdf.Name("/" + glyph_name_for(char)))
        widths_map[code] = width
        font.register_glyph(code, char, width)
        result.added[char] = code

    _rewrite_widths_array(font, widths_map)
    rewrite_tounicode(pdf, font)
    result.failed = "".join(ch for ch in chars if ch not in result.added)
    return result


def _rewrite_widths_array(font: FontInfo, widths: dict[int, float]) -> None:
    """Пересобирает ``/Widths``, ``/FirstChar`` и ``/LastChar`` простого шрифта."""
    if not widths:
        return
    first, last = min(widths), max(widths)
    missing = 0.0
    if font.descriptor is not None and "/MissingWidth" in font.descriptor:
        missing = float(font.descriptor.MissingWidth)
    values = [round(widths.get(code, missing), 2) for code in range(first, last + 1)]
    font.dict["/FirstChar"] = first
    font.dict["/LastChar"] = last
    font.dict["/Widths"] = pikepdf.Array(values)


class FontSnapshot:
    """Состояние шрифта до расширения — чтобы можно было вернуть всё как было.

    Нужен из-за глифов-сирот. Глифы добавляются в шрифт **до** того, как
    правка применена: сначала выясняется, каких символов не хватает, и они
    добываются у донора, и только потом переписывается содержимое страниц. Но
    правка может и не состояться — фрагмент оказался разорван на два оператора
    показа, поток не нашёлся, донор дал не все символы и текст пошёл запасным
    шрифтом. Тогда в шрифте остаются глифы, на которые не ссылается ни один
    код: они ничего не рисуют, но лежат в файле и прямо показывают, что шрифт
    правили — и даже какие буквы понадобились.

    Возврат к снимку решает это надёжнее выборочного удаления: программа
    шрифта, ширины и таблицы восстанавливаются теми же байтами, что были,
    и сверка с оригиналом не находит в шрифте ни одного изменения.
    """

    __slots__ = ("font", "program", "widths", "first", "last", "warr", "cidset",
                 "tounicode", "encoding", "registry")

    def __init__(self, font: FontInfo):
        self.font = font
        self.program = None
        self.warr = self.widths = self.first = self.last = None
        self.cidset = self.tounicode = self.encoding = None
        self.registry = None
        try:
            if font.descriptor is not None and font.font_file_key:
                stream = font.descriptor.get(font.font_file_key)
                if isinstance(stream, pikepdf.Stream):
                    self.program = stream.read_bytes()
            metrics = font.metrics_dict
            if metrics is not None and "/W" in metrics:
                self.warr = metrics["/W"].unparse()
            for key, slot in (("/Widths", "widths"), ("/FirstChar", "first"),
                              ("/LastChar", "last"), ("/Encoding", "encoding")):
                if key in font.dict:
                    setattr(self, slot, font.dict[key].unparse())
            if font.descriptor is not None and "/CIDSet" in font.descriptor:
                target = font.descriptor["/CIDSet"]
                if isinstance(target, pikepdf.Stream):
                    self.cidset = target.read_bytes()
            existing = font.dict.get("/ToUnicode")
            if isinstance(existing, pikepdf.Stream):
                self.tounicode = existing.read_bytes()
            self.registry = font.metrics_state()
        except Exception:
            # Снимок — страховка: не вышло снять, значит откатывать не будем
            self.program = None

    @property
    def usable(self) -> bool:
        return self.program is not None

    def restore(self) -> bool:
        """Возвращает шрифт к снятому состоянию. ``True``, если получилось."""
        if not self.usable:
            return False
        font = self.font
        try:
            stream = font.descriptor[font.font_file_key]
            stream.write(self.program)
            stream["/Length1"] = len(self.program)
            metrics = font.metrics_dict
            if metrics is not None:
                if self.warr is not None:
                    metrics["/W"] = pikepdf.Object.parse(self.warr)
                elif "/W" in metrics:
                    del metrics["/W"]
            for key, value in (("/Widths", self.widths), ("/FirstChar", self.first),
                               ("/LastChar", self.last), ("/Encoding", self.encoding)):
                if value is not None:
                    font.dict[key] = pikepdf.Object.parse(value)
                elif key in font.dict:
                    del font.dict[key]
            if self.cidset is not None and font.descriptor is not None:
                target = font.descriptor.get("/CIDSet")
                if isinstance(target, pikepdf.Stream):
                    target.write(self.cidset)
            if self.tounicode is not None:
                existing = font.dict.get("/ToUnicode")
                if isinstance(existing, pikepdf.Stream):
                    existing.write(self.tounicode)
            if self.registry is not None:
                font.restore_metrics_state(self.registry)
        except Exception:
            return False
        return True


def extend_font(
    pdf: pikepdf.Pdf,
    font: FontInfo,
    chars: str,
    extra_font_dirs: Iterable[str] = (),
    codes_in_use: set[int] | None = None,
    priority_dirs: Iterable[str] = (),
    only_priority: bool = False,
) -> ExtensionResult:
    """Добавляет недостающие глифы в шрифт документа."""
    if not font.is_embedded:
        raise UnsupportedFontProgramError("шрифт не внедрён — расширять нечего")
    if font.is_composite:
        return extend_cid_font(
            pdf, font, chars, extra_font_dirs, priority_dirs, only_priority
        )
    return extend_simple_font(
        pdf, font, chars, extra_font_dirs, codes_in_use, priority_dirs, only_priority
    )


# ----------------------------------------------------------------------
# Внедрение нового (запасного) шрифта
# ----------------------------------------------------------------------

def _subset_for_chars(path: str, index: int, chars: str) -> tuple[bytes, dict[str, int], int]:
    """Создаёт подмножество системного шрифта, сохраняя исходные номера глифов.

    ``retain_gids`` важен: при ``/CIDToGIDMap /Identity`` код символа в PDF
    равен номеру глифа, поэтому номера должны остаться прежними.
    """
    from fontTools import subset
    from fontTools.ttLib import TTFont

    # recalcTimestamp=False: иначе fontTools проставит в таблицу head дату
    # сборки, и во внедрённом шрифте останется след того, когда правили файл.
    # recalcBBoxes=False: пересчёт габаритных рамок задевает нетронутые глифы
    font = TTFont(path, fontNumber=index, recalcTimestamp=False, recalcBBoxes=False)
    cmap = font.getBestCmap()
    char_to_gid: dict[str, int] = {}
    glyph_order = font.getGlyphOrder()
    name_to_gid = {name: gid for gid, name in enumerate(glyph_order)}
    wanted = set()
    for char in chars:
        gname = cmap.get(ord(char))
        if gname is None:
            continue
        wanted.add(ord(char))
        char_to_gid[char] = name_to_gid[gname]

    options = subset.Options()
    options.retain_gids = True
    options.glyph_names = True
    options.notdef_outline = True
    options.recalc_bounds = False
    options.drop_tables += ["FFTM"]
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(unicodes=wanted)
    subsetter.subset(font)

    upem = font["head"].unitsPerEm
    out = BytesIO()
    font.save(out)
    font.close()
    return out.getvalue(), char_to_gid, upem


def embed_type0_font(
    pdf: pikepdf.Pdf,
    system_font: SystemFont,
    chars: str,
    resource_prefix: str = "PEF",
) -> tuple[pikepdf.Object, dict[str, int], dict[int, float]]:
    """Создаёт в документе новый шрифт Type0/Identity-H из системного файла.

    Возвращает ``(словарь шрифта, {символ: код}, {код: ширина})``.
    """
    from fontTools.ttLib import TTFont

    program, char_to_gid, upem = _subset_for_chars(
        system_font.path, system_font.index, chars
    )
    probe = TTFont(BytesIO(program), lazy=True)
    hmtx = probe["hmtx"]
    glyph_order = probe.getGlyphOrder()
    head, hhea, os2 = probe["head"], probe["hhea"], probe.get("OS/2")
    post = probe.get("post")

    widths: dict[int, float] = {}
    for char, gid in char_to_gid.items():
        advance = hmtx[glyph_order[gid]][0]
        widths[gid] = advance * 1000.0 / upem

    scale = 1000.0 / upem
    flags = 4 if (os2 is not None and os2.usWeightClass and False) else 32  # 32 = Nonsymbolic
    descriptor = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name("/FontDescriptor"),
            FontName=pikepdf.Name("/" + system_font.ps_name.replace(" ", "")),
            Flags=flags,
            FontBBox=pikepdf.Array([
                round(head.xMin * scale), round(head.yMin * scale),
                round(head.xMax * scale), round(head.yMax * scale),
            ]),
            ItalicAngle=round(float(post.italicAngle) if post is not None else 0.0, 2),
            Ascent=round(hhea.ascent * scale),
            Descent=round(hhea.descent * scale),
            CapHeight=round(getattr(os2, "sCapHeight", hhea.ascent) * scale),
            StemV=80,
        )
    )
    font_file = pdf.make_stream(program)
    font_file["/Length1"] = len(program)
    descriptor["/FontFile2"] = font_file

    cid_font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name("/Font"),
            Subtype=pikepdf.Name("/CIDFontType2"),
            BaseFont=pikepdf.Name("/" + system_font.ps_name.replace(" ", "")),
            CIDSystemInfo=pikepdf.Dictionary(
                Registry=pikepdf.String("Adobe"),
                Ordering=pikepdf.String("Identity"),
                Supplement=0,
            ),
            FontDescriptor=descriptor,
            DW=1000,
            CIDToGIDMap=pikepdf.Name("/Identity"),
        )
    )
    warr = pikepdf.Array()
    for gid in sorted(widths):
        warr.append(gid)
        warr.append(pikepdf.Array([round(widths[gid], 2)]))
    cid_font["/W"] = warr

    to_unicode = {gid: char for char, gid in char_to_gid.items()}
    font_dict = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name("/Font"),
            Subtype=pikepdf.Name("/Type0"),
            BaseFont=pikepdf.Name("/" + system_font.ps_name.replace(" ", "")),
            Encoding=pikepdf.Name("/Identity-H"),
            DescendantFonts=pikepdf.Array([cid_font]),
            ToUnicode=pdf.make_stream(build_tounicode_cmap(to_unicode, 2)),
        )
    )
    probe.close()
    return font_dict, char_to_gid, widths


def add_font_resource(resources: pikepdf.Object, font_dict: pikepdf.Object, prefix: str = "PEF") -> str:
    """Регистрирует шрифт в ресурсах, подбирая свободное имя."""
    if "/Font" not in resources:
        resources["/Font"] = pikepdf.Dictionary()
    table = resources["/Font"]
    index = 1
    while f"/{prefix}{index}" in table:
        index += 1
    name = f"/{prefix}{index}"
    table[name] = font_dict
    return name
