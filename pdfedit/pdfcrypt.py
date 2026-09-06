"""Шифрование объектов, дописываемых к защищённому документу.

Нужен ровно одному режиму — инкрементальному обновлению
(:mod:`pdfedit.incremental`). Если в документе есть словарь ``/Encrypt``, то
всё, что лежит в файле, зашифровано: и данные потоков, и строки внутри
словарей. Дописать туда объект открытым текстом нельзя — ридер попытается его
расшифровать и получит мусор. Значит, новые редакции объектов надо зашифровать
тем же ключом и тем же способом, что и остальной файл.

Ключ шифрования файла считать не приходится: qpdf уже проделал это при
открытии документа и отдаёт его через ``Pdf.encryption.encryption_key``.
Здесь остаётся вторая половина работы (ISO 32000-1, 7.6.2):

* ключ конкретного объекта — MD5 от ключа файла с номером и поколением
  объекта (для AES — плюс метка ``sAlT``); в AES-256 (R5/R6) ключ объекта
  равен ключу файла, номер в него не подмешивается;
* сам шифр — RC4 или AES-CBC со случайным вектором инициализации в начале
  данных и дополнением по PKCS#5.

AES написан здесь же на чистом Python: в стандартной библиотеке его нет, а
тянуть ради него ``cryptography`` в программу, которой шифрование нужно для
пары килобайт правок, несоразмерно. Реализация табличная (T-таблицы), только
зашифрование — расшифровывать нам нечего. Правильность проверяется
контрольными примерами из FIPS-197 в tests/test_incremental.py, а сверх того
каждый записанный объект перечитывается из готового файла и сверяется с тем,
что было в памяти.
"""

from __future__ import annotations

import hashlib
import os
import struct

import pikepdf

# ----------------------------------------------------------------------
# AES: таблицы
# ----------------------------------------------------------------------

def _build_tables() -> tuple[bytes, list[list[int]]]:
    """Строит S-блок и четыре T-таблицы зашифрования (FIPS-197)."""
    # Таблицы логарифмов в поле GF(2^8) по образующей 3 — через них считается
    # обратный элемент, на котором стоит S-блок
    exp = [0] * 512
    log = [0] * 256
    value = 1
    for i in range(255):
        exp[i] = value
        log[value] = i
        value ^= (value << 1) ^ (0x1B if value & 0x80 else 0)
        value &= 0xFF
    for i in range(255, 512):
        exp[i] = exp[i - 255]

    sbox = [0] * 256
    for i in range(256):
        inverse = 0 if i == 0 else exp[255 - log[i]]
        result = inverse
        for _ in range(4):
            inverse = ((inverse << 1) | (inverse >> 7)) & 0xFF
            result ^= inverse
        sbox[i] = result ^ 0x63

    def xtime(a: int, b: int) -> int:
        if a == 0 or b == 0:
            return 0
        return exp[log[a] + log[b]]

    tables: list[list[int]] = [[0] * 256 for _ in range(4)]
    for i in range(256):
        s = sbox[i]
        word = (xtime(s, 2) << 24) | (s << 16) | (s << 8) | xtime(s, 3)
        for shift in range(4):
            tables[shift][i] = ((word >> (8 * shift)) | (word << (32 - 8 * shift))) & 0xFFFFFFFF
    return bytes(sbox), tables


_SBOX, _T = _build_tables()
_RCON = [0x01000000, 0x02000000, 0x04000000, 0x08000000, 0x10000000,
         0x20000000, 0x40000000, 0x80000000, 0x1B000000, 0x36000000,
         0x6C000000, 0xD8000000, 0xAB000000, 0x4D000000]


def _sub_word(word: int) -> int:
    return (
        (_SBOX[(word >> 24) & 0xFF] << 24)
        | (_SBOX[(word >> 16) & 0xFF] << 16)
        | (_SBOX[(word >> 8) & 0xFF] << 8)
        | _SBOX[word & 0xFF]
    )


class AES:
    """Зашифрование по AES для ключей длиной 16 или 32 байта."""

    def __init__(self, key: bytes):
        if len(key) not in (16, 24, 32):
            raise ValueError(f"длина ключа AES должна быть 16, 24 или 32 байта, дано {len(key)}")
        nk = len(key) // 4
        self.rounds = nk + 6
        words = list(struct.unpack(f">{nk}I", key))
        for i in range(nk, 4 * (self.rounds + 1)):
            temp = words[i - 1]
            if i % nk == 0:
                temp = _sub_word(((temp << 8) | (temp >> 24)) & 0xFFFFFFFF) ^ _RCON[i // nk - 1]
            elif nk > 6 and i % nk == 4:
                temp = _sub_word(temp)
            words.append(words[i - nk] ^ temp)
        self.schedule = words

    def encrypt_block(self, block: bytes) -> bytes:
        """Шифрует ровно 16 байт."""
        w = self.schedule
        s0, s1, s2, s3 = struct.unpack(">4I", block)
        s0 ^= w[0]; s1 ^= w[1]; s2 ^= w[2]; s3 ^= w[3]

        t0 = t1 = t2 = t3 = 0
        for r in range(1, self.rounds):
            k = 4 * r
            t0 = (_T[0][(s0 >> 24) & 0xFF] ^ _T[1][(s1 >> 16) & 0xFF]
                  ^ _T[2][(s2 >> 8) & 0xFF] ^ _T[3][s3 & 0xFF] ^ w[k])
            t1 = (_T[0][(s1 >> 24) & 0xFF] ^ _T[1][(s2 >> 16) & 0xFF]
                  ^ _T[2][(s3 >> 8) & 0xFF] ^ _T[3][s0 & 0xFF] ^ w[k + 1])
            t2 = (_T[0][(s2 >> 24) & 0xFF] ^ _T[1][(s3 >> 16) & 0xFF]
                  ^ _T[2][(s0 >> 8) & 0xFF] ^ _T[3][s1 & 0xFF] ^ w[k + 2])
            t3 = (_T[0][(s3 >> 24) & 0xFF] ^ _T[1][(s0 >> 16) & 0xFF]
                  ^ _T[2][(s1 >> 8) & 0xFF] ^ _T[3][s2 & 0xFF] ^ w[k + 3])
            s0, s1, s2, s3 = t0, t1, t2, t3

        # Последний раунд идёт без перемешивания столбцов
        k = 4 * self.rounds
        out = []
        for column, key_word in zip(
            ((s0, s1, s2, s3), (s1, s2, s3, s0), (s2, s3, s0, s1), (s3, s0, s1, s2)),
            w[k:k + 4],
        ):
            a, b, c, d = column
            value = (
                (_SBOX[(a >> 24) & 0xFF] << 24)
                | (_SBOX[(b >> 16) & 0xFF] << 16)
                | (_SBOX[(c >> 8) & 0xFF] << 8)
                | _SBOX[d & 0xFF]
            ) ^ key_word
            out.append(value & 0xFFFFFFFF)
        return struct.pack(">4I", *out)


def aes_cbc_encrypt(key: bytes, data: bytes, iv: bytes | None = None) -> bytes:
    """AES-CBC с дополнением PKCS#5; вектор инициализации идёт первым блоком.

    Именно такой формат предписан для ``/AESV2`` и ``/AESV3`` (ISO 32000-1,
    7.6.2): 16 байт вектора, затем шифртекст, дополненный до кратности блоку.
    Дополнение добавляется всегда, даже когда длина уже кратна 16.
    """
    if iv is None:
        iv = os.urandom(16)
    padding = 16 - (len(data) % 16)
    data = data + bytes([padding]) * padding

    cipher = AES(key)
    out = bytearray(iv)
    previous = iv
    for start in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[start:start + 16], previous))
        previous = cipher.encrypt_block(block)
        out += previous
    return bytes(out)


def rc4(key: bytes, data: bytes) -> bytes:
    """RC4 — тот же алгоритм и для зашифрования, и для расшифрования."""
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]
    out = bytearray(len(data))
    i = j = 0
    for position, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        out[position] = byte ^ state[(state[i] + state[j]) & 0xFF]
    return bytes(out)


# ----------------------------------------------------------------------
# Шифрование объектов документа
# ----------------------------------------------------------------------

class UnsupportedEncryption(Exception):
    """Схема защиты документа не распознана."""


class DocumentCipher:
    """Шифрует данные так же, как они зашифрованы в исходном файле."""

    def __init__(
        self,
        key: bytes,
        version: int,
        stream_method: str,
        string_method: str,
        encrypt_metadata: bool = True,
    ):
        self.key = key
        self.version = version
        self.stream_method = stream_method   # "aes" | "rc4" | "none"
        self.string_method = string_method
        self.encrypt_metadata = encrypt_metadata

    # ------------------------------------------------------------------
    @classmethod
    def from_pdf(cls, pdf: pikepdf.Pdf) -> "DocumentCipher | None":
        """Собирает шифратор по словарю ``/Encrypt``; ``None`` — файл открытый."""
        if not pdf.is_encrypted:
            return None
        info = pdf.encryption
        key = bytes(info.encryption_key)
        if not key:
            raise UnsupportedEncryption(
                "qpdf не отдал ключ шифрования документа — дописывать к нему нельзя"
            )

        def method_name(method) -> str:
            name = str(method).rsplit(".", 1)[-1].lower()
            if name in ("aes", "aesv3"):
                return "aes"
            if name == "rc4":
                return "rc4"
            if name == "none":
                return "none"
            raise UnsupportedEncryption(f"неизвестный способ шифрования: {method}")

        encrypt_metadata = True
        raw = pdf.trailer.get("/Encrypt")
        if raw is not None and "/EncryptMetadata" in raw:
            encrypt_metadata = bool(raw["/EncryptMetadata"])

        return cls(
            key=key,
            version=int(info.V),
            stream_method=method_name(info.stream_method),
            string_method=method_name(info.string_method),
            encrypt_metadata=encrypt_metadata,
        )

    # ------------------------------------------------------------------
    def _object_key(self, objgen: tuple[int, int], aes: bool) -> bytes:
        """Ключ для конкретного объекта (алгоритм 1 из ISO 32000-1, 7.6.2).

        В AES-256 подмешивания номера объекта нет: там ключ файла используется
        напрямую — потому и ``/Encrypt`` версии 5 не боится перенумерации.
        """
        if self.version >= 5:
            return self.key
        number, generation = objgen
        digest = hashlib.md5()
        digest.update(self.key)
        digest.update(bytes([number & 0xFF, (number >> 8) & 0xFF, (number >> 16) & 0xFF]))
        digest.update(bytes([generation & 0xFF, (generation >> 8) & 0xFF]))
        if aes:
            digest.update(b"sAlT")  # 0x73 0x41 0x6C 0x54
        return digest.digest()[: min(len(self.key) + 5, 16)]

    def _apply(self, method: str, objgen: tuple[int, int], data: bytes) -> bytes:
        if method == "none" or not data:
            return data
        if method == "aes":
            return aes_cbc_encrypt(self._object_key(objgen, aes=True), data)
        return rc4(self._object_key(objgen, aes=False), data)

    def encrypt_stream(
        self, objgen: tuple[int, int], data: bytes, stream_type: str = ""
    ) -> bytes:
        """Шифрует данные потока (кроме тех, что по стандарту остаются открытыми)."""
        if stream_type == "/XRef":
            # Таблица ссылок читается до того, как ридер узнает ключ
            return data
        if stream_type == "/Metadata" and not self.encrypt_metadata:
            return data
        return self._apply(self.stream_method, objgen, data)

    def encrypt_string(self, objgen: tuple[int, int], data: bytes) -> bytes:
        return self._apply(self.string_method, objgen, data)

    def describe(self) -> str:
        names = {"aes": "AES", "rc4": "RC4", "none": "без шифрования"}
        return (
            f"/Encrypt версии {self.version}, потоки: {names[self.stream_method]}, "
            f"строки: {names[self.string_method]}, ключ {len(self.key) * 8} бит"
        )


def shield_strings(
    obj: pikepdf.Object, cipher: DocumentCipher, objgen: tuple[int, int]
) -> pikepdf.Object:
    """Возвращает копию объекта, в которой все строки зашифрованы.

    ``objgen`` — номер объекта, который сейчас записывается: ключ строки
    считается именно от него, а не от того, где строка лежит в дереве.

    Вложенные ссылки на другие объекты остаются ссылками — их содержимое
    зашифруется своим ключом, когда (и если) дописывается сам тот объект.
    Сам записываемый объект косвенный по определению, поэтому на верхнем
    уровне проверка косвенности не применяется: иначе шифрование не задело
    бы ровно те строки, ради которых всё и затевалось.
    """
    return _shield(obj, cipher, objgen, top=True)


def _shield(
    obj: pikepdf.Object, cipher: DocumentCipher, objgen: tuple[int, int], top: bool = False
) -> pikepdf.Object:
    if isinstance(obj, pikepdf.String):
        return pikepdf.String(cipher.encrypt_string(objgen, bytes(obj)))
    if not isinstance(obj, pikepdf.Object):
        return obj
    if not top:
        try:
            if obj.objgen != (0, 0):
                return obj  # косвенная ссылка — оставляем как есть
        except Exception:
            return obj
    if isinstance(obj, pikepdf.Dictionary):
        return pikepdf.Dictionary(
            {key: _shield(value, cipher, objgen) for key, value in obj.items()}
        )
    if isinstance(obj, pikepdf.Array):
        return pikepdf.Array([_shield(item, cipher, objgen) for item in obj])
    return obj
