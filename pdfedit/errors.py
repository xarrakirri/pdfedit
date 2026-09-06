"""Иерархия исключений пакета pdfedit."""


class PdfEditError(Exception):
    """Базовое исключение пакета."""


class FontError(PdfEditError):
    """Проблема со шрифтом: не удалось разобрать кодировку, метрики и т. п."""


class GlyphsMissingError(FontError):
    """В шрифте нет глифов для части символов нового текста.

    Хранит список отсутствующих символов, чтобы вызывающий код мог решить,
    расширять ли подмножество шрифта или подставлять запасной шрифт.
    """

    def __init__(self, message: str, missing: str, font_name: str = ""):
        super().__init__(message)
        self.missing = missing
        self.font_name = font_name


class TextNotFoundError(PdfEditError):
    """Искомый текст не найден в документе."""


class UnsupportedFontProgramError(FontError):
    """Формат встроенной программы шрифта не поддерживается для расширения."""
