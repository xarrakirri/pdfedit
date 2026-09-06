"""Точка входа для запуска пакета: ``python -m pdfedit``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
