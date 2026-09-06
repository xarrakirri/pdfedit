"""pdfedit — редактирование текста и метаданных PDF на уровне объектов.

Пакет заменяет текст прямо в потоках содержимого (операторы ``Tj``/``TJ``),
сохраняя внедрённые шрифты, форматирование, структуру документа и метаданные.

Основные точки входа::

    from pdfedit import PdfEditor

    with PdfEditor("договор.pdf") as editor:
        editor.replace("Иванов", "Петров")
        editor.save("договор-исправленный.pdf")
"""

from .editor import ApplyReport, EditSpec, Match, PdfEditor
from .errors import (
    FontError,
    GlyphsMissingError,
    PdfEditError,
    TextNotFoundError,
    UnsupportedFontProgramError,
)
from .incremental import (
    IncrementalReport,
    IncrementalUpdateError,
    incremental_bytes,
    save_incremental,
)
from .metadata import MetadataSnapshot, apply_metadata, read_metadata
from .saving import VerifyReport, verify
from .validate import ComparisonReport, StructureReport, check_file, compare_files

__version__ = "1.1.0"

__all__ = [
    "PdfEditor", "EditSpec", "Match", "ApplyReport",
    "PdfEditError", "FontError", "GlyphsMissingError",
    "TextNotFoundError", "UnsupportedFontProgramError",
    "read_metadata", "apply_metadata", "MetadataSnapshot",
    "verify", "VerifyReport",
    "save_incremental", "incremental_bytes",
    "IncrementalReport", "IncrementalUpdateError",
    "check_file", "compare_files", "StructureReport", "ComparisonReport",
    "__version__",
]
