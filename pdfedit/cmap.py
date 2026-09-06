"""Разбор и построение CMap-таблиц PDF.

В PDF используются два вида CMap:

* **ToUnicode CMap** — отображает *коды символов* (то, что реально лежит в
  строках операторов ``Tj``/``TJ``) в Unicode. Нужен нам, чтобы понять, какой
  текст изображён на странице, и чтобы обратным преобразованием получить коды
  для нового текста.
* **Encoding CMap** (``/Encoding`` у составных шрифтов Type0) — задаёт разбиение
  байтовой строки на коды и отображение код → CID. Чаще всего это
  ``/Identity-H`` (два байта на код, CID = код), но встречаются и встроенные
  CMap-потоки.

Обе таблицы записываются на подмножестве PostScript, поэтому здесь есть
небольшой токенизатор и разборщик нужных нам конструкций.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator, Sequence

# Токены CMap: hex-строка <..>, литеральная строка (..), имя /Name, число,
# скобки массива и ключевое слово.
_TOKEN_RE = re.compile(
    rb"""
      (?P<hex>   <[0-9A-Fa-f\s]*>        )
    | (?P<name>  /[^\s/\[\]<>(){}%]*     )
    | (?P<num>   [+-]?\d+\.?\d*          )
    | (?P<open>  \[                      )
    | (?P<close> \]                      )
    | (?P<dictopen>  <<                  )
    | (?P<dictclose> >>                  )
    | (?P<comment> %[^\r\n]*             )
    | (?P<kw>    [A-Za-z][A-Za-z0-9_*']* )
    """,
    re.VERBOSE,
)


@dataclass
class Token:
    kind: str
    value: object


def tokenize(data: bytes) -> Iterator[Token]:
    """Разбивает содержимое CMap на токены.

    Литеральные строки в CMap встречаются редко (только в ``/CIDSystemInfo``),
    поэтому обрабатываются упрощённо — до закрывающей скобки.
    """
    pos = 0
    length = len(data)
    while pos < length:
        ch = data[pos : pos + 1]
        if ch.isspace():
            pos += 1
            continue
        if ch == b"(":  # литеральная строка — пропускаем с учётом вложенности
            depth, pos = 1, pos + 1
            start = pos
            while pos < length and depth:
                c = data[pos : pos + 1]
                if c == b"\\":
                    pos += 2
                    continue
                if c == b"(":
                    depth += 1
                elif c == b")":
                    depth -= 1
                pos += 1
            yield Token("string", data[start : pos - 1])
            continue
        # `<<` должен проверяться раньше, чем hex-строка `<...>`
        if data[pos : pos + 2] == b"<<":
            yield Token("dictopen", None)
            pos += 2
            continue
        if data[pos : pos + 2] == b">>":
            yield Token("dictclose", None)
            pos += 2
            continue
        m = _TOKEN_RE.match(data, pos)
        if not m:
            pos += 1
            continue
        pos = m.end()
        kind = m.lastgroup
        raw = m.group()
        if kind == "comment":
            continue
        if kind == "hex":
            body = bytes(raw[1:-1])
            body = b"".join(body.split())
            if len(body) % 2:  # нечётное число цифр дополняется нулём
                body += b"0"
            yield Token("hex", bytes.fromhex(body.decode("ascii")))
        elif kind == "name":
            yield Token("name", raw[1:].decode("latin-1"))
        elif kind == "num":
            text = raw.decode("ascii")
            yield Token("num", float(text) if "." in text else int(text))
        elif kind == "open":
            yield Token("open", None)
        elif kind == "close":
            yield Token("close", None)
        elif kind in ("dictopen", "dictclose"):
            yield Token(kind, None)
        else:
            yield Token("kw", raw.decode("latin-1"))


def _hex_to_int(raw: bytes) -> int:
    return int.from_bytes(raw, "big") if raw else 0


def _utf16be_to_str(raw: bytes) -> str:
    """Декодирует значение bfchar/bfrange (UTF-16BE) в строку Python."""
    if len(raw) % 2:
        raw += b"\x00"
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        # Битые суррогаты — декодируем по кодовым единицам, отбрасывая мусор
        return raw.decode("utf-16-be", errors="replace")


@dataclass
class CodespaceRange:
    """Диапазон кодов фиксированной длины: определяет, сколько байт в коде."""

    low: bytes
    high: bytes

    @property
    def nbytes(self) -> int:
        return len(self.low)

    def contains(self, code_bytes: bytes) -> bool:
        if len(code_bytes) != self.nbytes:
            return False
        return all(
            self.low[i] <= code_bytes[i] <= self.high[i] for i in range(self.nbytes)
        )


@dataclass
class ToUnicodeCMap:
    """Разобранная ToUnicode-таблица: код → строка Unicode."""

    mapping: dict[int, str] = field(default_factory=dict)
    codespaces: list[CodespaceRange] = field(default_factory=list)

    @property
    def code_lengths(self) -> list[int]:
        """Длины кодов (в байтах), встречающиеся в codespace-диапазонах."""
        lengths = sorted({cs.nbytes for cs in self.codespaces})
        return lengths or [1]


def parse_tounicode(data: bytes) -> ToUnicodeCMap:
    """Разбирает поток ``/ToUnicode`` в отображение код → Unicode."""
    result = ToUnicodeCMap()
    tokens = list(tokenize(data))
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.kind != "kw":
            i += 1
            continue

        if tok.value == "begincodespacerange":
            i += 1
            buf: list[bytes] = []
            while i < n and not (tokens[i].kind == "kw" and tokens[i].value == "endcodespacerange"):
                if tokens[i].kind == "hex":
                    buf.append(tokens[i].value)
                i += 1
            for j in range(0, len(buf) - 1, 2):
                result.codespaces.append(CodespaceRange(buf[j], buf[j + 1]))

        elif tok.value == "beginbfchar":
            i += 1
            pending: list[Token] = []
            while i < n and not (tokens[i].kind == "kw" and tokens[i].value == "endbfchar"):
                pending.append(tokens[i])
                i += 1
            for j in range(0, len(pending) - 1, 2):
                src, dst = pending[j], pending[j + 1]
                if src.kind != "hex":
                    continue
                code = _hex_to_int(src.value)
                if dst.kind == "hex":
                    result.mapping[code] = _utf16be_to_str(dst.value)
                elif dst.kind == "name":
                    from fontTools.agl import toUnicode  # локальный импорт: тяжёлый модуль

                    result.mapping[code] = toUnicode(dst.value)

        elif tok.value == "beginbfrange":
            i += 1
            pending = []
            depth = 0
            array: list[Token] = []
            entries: list[list] = []
            current: list = []
            while i < n and not (tokens[i].kind == "kw" and tokens[i].value == "endbfrange"):
                t = tokens[i]
                if t.kind == "open":
                    depth += 1
                    array = []
                elif t.kind == "close":
                    depth -= 1
                    current.append(("array", array))
                    if len(current) == 3:
                        entries.append(current)
                        current = []
                elif depth:
                    array.append(t)
                else:
                    current.append((t.kind, t.value))
                    if len(current) == 3:
                        entries.append(current)
                        current = []
                i += 1
            for entry in entries:
                (lo_kind, lo), (hi_kind, hi), (dst_kind, dst) = entry
                if lo_kind != "hex" or hi_kind != "hex":
                    continue
                lo_i, hi_i = _hex_to_int(lo), _hex_to_int(hi)
                if hi_i < lo_i or hi_i - lo_i > 0x10000:
                    continue  # защита от повреждённых таблиц
                if dst_kind == "array":
                    for offset, item in enumerate(dst):
                        if item.kind == "hex":
                            result.mapping[lo_i + offset] = _utf16be_to_str(item.value)
                elif dst_kind == "hex":
                    base = _utf16be_to_str(dst)
                    if not base:
                        continue
                    # Приращение применяется к последней кодовой единице UTF-16
                    prefix, last = base[:-1], ord(base[-1])
                    for offset in range(hi_i - lo_i + 1):
                        try:
                            result.mapping[lo_i + offset] = prefix + chr(last + offset)
                        except ValueError:
                            break
        i += 1
    return result


@dataclass
class EncodingCMap:
    """Encoding-CMap составного шрифта: разбиение на коды и код → CID."""

    codespaces: list[CodespaceRange] = field(default_factory=list)
    cid_map: dict[int, int] = field(default_factory=dict)
    identity: bool = False

    def split_codes(self, raw: bytes) -> list[int]:
        """Разбивает байтовую строку на коды согласно codespace-диапазонам."""
        if self.identity or not self.codespaces:
            # Identity-H/V: ровно два байта на код
            return [int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw) - 1, 2)]
        codes: list[int] = []
        pos = 0
        lengths = sorted({cs.nbytes for cs in self.codespaces})
        while pos < len(raw):
            for nb in lengths:
                chunk = raw[pos : pos + nb]
                if len(chunk) == nb and any(cs.contains(chunk) for cs in self.codespaces):
                    codes.append(int.from_bytes(chunk, "big"))
                    pos += nb
                    break
            else:
                # Код не попал ни в один диапазон — берём минимальную длину
                nb = lengths[0]
                codes.append(int.from_bytes(raw[pos : pos + nb], "big"))
                pos += nb
        return codes

    def to_cid(self, code: int) -> int:
        if self.identity:
            return code
        return self.cid_map.get(code, code)


def parse_encoding_cmap(data: bytes) -> EncodingCMap:
    """Разбирает встроенный поток ``/Encoding`` составного шрифта."""
    result = EncodingCMap()
    tokens = list(tokenize(data))
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.kind != "kw":
            i += 1
            continue
        if tok.value == "begincodespacerange":
            i += 1
            buf = []
            while i < n and not (tokens[i].kind == "kw" and tokens[i].value == "endcodespacerange"):
                if tokens[i].kind == "hex":
                    buf.append(tokens[i].value)
                i += 1
            for j in range(0, len(buf) - 1, 2):
                result.codespaces.append(CodespaceRange(buf[j], buf[j + 1]))
        elif tok.value == "begincidrange":
            i += 1
            buf = []
            while i < n and not (tokens[i].kind == "kw" and tokens[i].value == "endcidrange"):
                if tokens[i].kind in ("hex", "num"):
                    buf.append(tokens[i])
                i += 1
            for j in range(0, len(buf) - 2, 3):
                lo, hi, cid = buf[j], buf[j + 1], buf[j + 2]
                if lo.kind != "hex" or hi.kind != "hex" or cid.kind != "num":
                    continue
                lo_i, hi_i = _hex_to_int(lo.value), _hex_to_int(hi.value)
                if hi_i - lo_i > 0x10000:
                    continue
                for offset in range(hi_i - lo_i + 1):
                    result.cid_map[lo_i + offset] = int(cid.value) + offset
        elif tok.value == "begincidchar":
            i += 1
            buf = []
            while i < n and not (tokens[i].kind == "kw" and tokens[i].value == "endcidchar"):
                if tokens[i].kind in ("hex", "num"):
                    buf.append(tokens[i])
                i += 1
            for j in range(0, len(buf) - 1, 2):
                src, cid = buf[j], buf[j + 1]
                if src.kind == "hex" and cid.kind == "num":
                    result.cid_map[_hex_to_int(src.value)] = int(cid.value)
        i += 1
    if not result.codespaces:
        result.codespaces.append(CodespaceRange(b"\x00\x00", b"\xff\xff"))
    return result


def identity_encoding_cmap() -> EncodingCMap:
    """Готовая CMap для ``/Identity-H`` и ``/Identity-V``."""
    cmap = EncodingCMap(identity=True)
    cmap.codespaces.append(CodespaceRange(b"\x00\x00", b"\xff\xff"))
    return cmap


def _group_ranges(items: Sequence[tuple[int, str]]) -> list[tuple[int, int, str]]:
    """Схлопывает подряд идущие пары (код, символ) в bfrange-диапазоны."""
    ranges: list[tuple[int, int, str]] = []
    start = prev = None
    base = ""
    for code, text in items:
        contiguous = (
            prev is not None
            and code == prev + 1
            and len(text) == 1
            and len(base) == 1
            and ord(text) == ord(base) + (code - start)
        )
        if contiguous:
            prev = code
            continue
        if start is not None:
            ranges.append((start, prev, base))
        start = prev = code
        base = text
    if start is not None:
        ranges.append((start, prev, base))
    return ranges


def build_tounicode_cmap(mapping: dict[int, str], code_bytes: int = 2) -> bytes:
    """Собирает поток ``/ToUnicode`` из отображения код → Unicode.

    Используется после расширения подмножества шрифта: новым кодам нужно
    прописать соответствие Unicode, иначе текст перестанет извлекаться
    средствами просмотра — а это как раз тот «след», которого мы избегаем.
    """
    width = code_bytes * 2  # число hex-цифр в коде
    items = sorted((c, t) for c, t in mapping.items() if t)
    singles = [(c, t) for c, t in items if len(t) != 1]
    rangeable = [(c, t) for c, t in items if len(t) == 1]
    ranges = _group_ranges(rangeable)
    # Одиночные диапазоны выгоднее писать как bfchar
    singles += [(lo, base) for lo, hi, base in ranges if lo == hi]
    ranges = [r for r in ranges if r[0] != r[1]]
    singles.sort()

    def hexcode(value: int) -> str:
        return f"<{value:0{width}X}>"

    def hextext(text: str) -> str:
        return "<" + text.encode("utf-16-be").hex().upper() + ">"

    out: list[str] = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo <</Registry (Adobe) /Ordering (UCS) /Supplement 0>> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        f"<{'0' * width}> <{'F' * width}>",
        "endcodespacerange",
    ]
    for chunk_start in range(0, len(singles), 100):
        chunk = singles[chunk_start : chunk_start + 100]
        out.append(f"{len(chunk)} beginbfchar")
        out += [f"{hexcode(c)} {hextext(t)}" for c, t in chunk]
        out.append("endbfchar")
    for chunk_start in range(0, len(ranges), 100):
        chunk = ranges[chunk_start : chunk_start + 100]
        out.append(f"{len(chunk)} beginbfrange")
        out += [f"{hexcode(lo)} {hexcode(hi)} {hextext(base)}" for lo, hi, base in chunk]
        out.append("endbfrange")
    out += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return ("\n".join(out) + "\n").encode("latin-1")
