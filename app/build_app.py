"""Сборка приложения pdfedit.app для macOS.

Собирается обычная связка (bundle) — каталог с расширением ``.app``, который
система показывает как одну программу: с собственным значком, именем в строке
меню, местом в Dock и связью с файлами PDF.

Два режима сборки:

``--mode standalone`` (по умолчанию)
    В связку кладётся всё: интерпретатор Python, стандартная библиотека,
    зависимости и сам пакет ``pdfedit``. Приложение ни от чего не зависит —
    его можно перенести в «Программы», отдать на другой компьютер и
    пользоваться им, даже если удалить и каталог с исходным кодом, и Python.

``--mode thin``
    Связка только запускает код из каталога разработки. Собирается мгновенно,
    занимает считаные килобайты, но перестанет работать, если каталог
    переместить. Удобно, пока программа дорабатывается.

Как приложение получает собственное имя. На macOS интерпретатор из фреймворка
передаёт управление вложенной связке ``Python.app``: только программа-связка
получает доступ к оконной системе. Имя в строке меню, в Dock и в списке
процессов система берёт именно у неё, поэтому при сборке её паспорт
переписывается, а файл интерпретатора переименовывается в ``pdfedit``. Без
этого пользователь видел бы программу под именем «Python».

Запуск::

    python app/build_app.py                 # обычная сборка
    python app/build_app.py --mode thin     # для разработки
    python app/build_app.py --install       # сразу положить в ~/Applications
"""

from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pdfedit import __version__  # noqa: E402

BUNDLE_ID = "local.pdfedit"
APP_NAME = "pdfedit"

#: Как называется библиотека внутри фреймворка Python у разных сборок
FRAMEWORK_LIBRARY_NAMES = ("Python3", "Python")

#: Каталоги site-packages, которые в связку не нужны
SKIP_PACKAGES = {
    "pip", "setuptools", "wheel", "pkg_resources", "_distutils_hack",
    "pyflakes", "__pycache__",
}

#: Части стандартной библиотеки, без которых приложение обходится
SKIP_STDLIB = {
    "test", "tests", "idlelib", "ensurepip", "lib2to3", "pydoc_data",
    "turtledemo", "site-packages", "__pycache__", "config-3.9-darwin",
}

LAUNCHER = r"""#!/bin/bash
# Запускающий сценарий связки pdfedit.app.
# Его задача — найти интерпретатор, задать пути и показать внятное сообщение,
# если что-то пойдёт не так: вывод программы, запущенной из Finder, иначе
# просто пропал бы.
set -u

RESOURCES="$(cd "$(dirname "$0")/../Resources" && pwd)"
LOG="${{HOME}}/Library/Logs/pdfedit.log"
mkdir -p "$(dirname "$LOG")"

{python_setup}

# macOS может передать служебный аргумент вида -psn_0_12345 — он не нужен
ARGS=()
for arg in "$@"; do
  case "$arg" in
    -psn_*) ;;
    *) ARGS+=("$arg") ;;
  esac
done

if [ ! -x "$PYTHON" ]; then
  osascript -e 'display alert "pdfedit не может запуститься" message "Не найден интерпретатор Python 3. Установите инструменты командной строки: xcode-select --install" as critical' >/dev/null 2>&1
  exit 1
fi

# Системный python3 — универсальная программа (x86_64 + arm64), и при запуске
# из Finder система вполне может выбрать x86_64. Расширения же собраны под
# архитектуру этого компьютера, поэтому её надо задать явно, иначе загрузка
# библиотек оборвётся на несовпадении архитектур.
NATIVE_ARCH="x86_64"
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
  NATIVE_ARCH="arm64"
fi
LAUNCH=("$PYTHON")
if [ -x /usr/bin/arch ]; then
  LAUNCH=(/usr/bin/arch "-$NATIVE_ARCH" "$PYTHON")
fi

{env_setup}

echo "--- $(date) запуск pdfedit {version} ($NATIVE_ARCH) ---" >>"$LOG"
"${{LAUNCH[@]}}" -m pdfedit gui "${{ARGS[@]+"${{ARGS[@]}}"}}" >>"$LOG" 2>&1
STATUS=$?

# Об ошибке сообщаем только если она действительно была. Код возврата 128 и
# выше означает завершение по сигналу — так программу закрывают извне
# (команда kill, перезагрузка, принудительное завершение), и пугать
# пользователя окном в этом случае не за что.
if [ $STATUS -ge 128 ]; then
  echo "--- завершено по сигналу (код $STATUS) ---" >>"$LOG"
elif [ $STATUS -ne 0 ]; then
  TAIL=$(tail -n 15 "$LOG" | sed 's/"/\\"/g')
  osascript -e "display alert \"pdfedit завершился с ошибкой\" message \"Подробности в ~/Library/Logs/pdfedit.log\n\n$TAIL\" as critical" >/dev/null 2>&1
fi
exit $STATUS
"""

PYTHON_SETUP_STANDALONE = """\
# Запускается интерпретатор из вложенной связки Python.app — переименованный
# и с переписанным паспортом. Именно от него система берёт имя программы для
# строки меню, Dock и списка процессов; запуск любого другого файла показал бы
# пользователю «Python».
CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$CONTENTS/Frameworks/python3.9/Resources/Python.app/Contents/MacOS/pdfedit\""""

PYTHON_SETUP_THIN = """\
# Режим разработки: код берётся из каталога проекта, а запускается копия
# вспомогательной связки Python.app с переписанным паспортом — иначе система
# показала бы программу под именем «Python»
CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$CONTENTS/Resources/Python.app/Contents/MacOS/pdfedit"
if [ ! -x "$PYTHON" ]; then
  PYTHON="{venv_python}"
fi
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || true)"
fi"""

ENV_SETUP_STANDALONE = """\
export PYTHONHOME="$CONTENTS/Frameworks/python3.9"
export PYTHONPATH="$RESOURCES/lib"
export PYTHONDONTWRITEBYTECODE=1"""

ENV_SETUP_THIN = """\
export PYTHONHOME="{python_home}"
export PYTHONPATH="{project_root}:{site_packages}"
export PYTHONDONTWRITEBYTECODE=1"""


def detect_tk_version(venv_root: Path) -> tuple[int, int]:
    """Определяет версию Tk, с которой соберётся приложение."""
    probe = venv_root / "bin" / "python3"
    if not probe.exists():
        return (0, 0)
    try:
        result = subprocess.run(
            [str(probe), "-c",
             "import tkinter; r=tkinter.Tk(); r.withdraw(); "
             "print(r.tk.call('info','patchlevel')); r.destroy()"],
            capture_output=True, text=True, timeout=30,
        )
        parts = result.stdout.strip().split(".")
        return (int(parts[0]), int(parts[1]))
    except Exception:
        return (0, 0)


def build_info_plist(icon_name: str, tk_version: tuple[int, int] = (0, 0)) -> dict:
    """Составляет Info.plist — паспорт приложения для системы.

    Ключ ``NSHighResolutionCapable`` означает «программа сама умеет рисовать
    в разрешении экрана Retina». Tk 8.5 этого не умеет: он работает в
    масштабе 1:1 и о плотности экрана не знает. Если пообещать системе
    обратное, она отдаст программе буфер двойного разрешения, в который Tk
    нарисует вчетверо меньшую картинку — на экране не появится ничего.
    Поэтому при старом Tk честнее сказать «нет»: система сама увеличит
    изображение — чуть мягче, зато видно.
    """
    hidpi = tk_version >= (8, 6)
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIconFile": icon_name,
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": __version__,
        "CFBundleShortVersionString": __version__,
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "10.13",
        "NSHighResolutionCapable": hidpi,
        # Tk 8.5 из инструментов командной строки Apple выпущен в 2010 году и
        # о тёмном оформлении не знает: система даёт окну тёмный фон, а Tk
        # рисует содержимое по правилам светлой темы — и на новых версиях
        # macOS не рисует вовсе, окно остаётся чёрным. Этот ключ просит
        # систему показывать программу в светлом оформлении.
        "NSRequiresAquaSystemAppearance": True,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHumanReadableCopyright": (
            "Редактирование текста и метаданных PDF на уровне объектов документа"
        ),
        # Приложение умеет открывать PDF, но не претендует на роль основного
        # средства просмотра: ранг Alternate оставляет эту роль системному
        # «Просмотру», а pdfedit появляется в меню «Открыть в программе».
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "PDF-документ",
                "CFBundleTypeRole": "Editor",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["com.adobe.pdf"],
                "CFBundleTypeExtensions": ["pdf"],
            }
        ],
    }


def copy_dependencies(target_lib: Path, venv_root: Path) -> list[str]:
    """Копирует пакет pdfedit и его зависимости внутрь связки."""
    target_lib.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []

    shutil.copytree(
        ROOT / "pdfedit", target_lib / "pdfedit",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    copied.append("pdfedit")

    site_packages = next(
        (p for p in (venv_root / "lib").glob("python*/site-packages") if p.is_dir()), None
    )
    if site_packages is None:
        raise SystemExit(
            f"не найден каталог site-packages в {venv_root}; "
            f"создайте окружение: python3 -m venv .venv && "
            f".venv/bin/pip install -r requirements.txt"
        )

    for item in sorted(site_packages.iterdir()):
        if item.name in SKIP_PACKAGES or item.name.startswith("~"):
            continue
        if item.suffix in (".pth", ".txt") or item.name.endswith(".dist-info"):
            continue
        destination = target_lib / item.name
        if item.is_dir():
            shutil.copytree(
                item, destination,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "test"),
            )
        else:
            shutil.copy2(item, destination)
        copied.append(item.name)
    return copied


def find_python_framework(venv_root: Path) -> tuple[Path, Path]:
    """Находит фреймворк Python и файл интерпретатора, на которых собрано окружение.

    Важно взять именно тот Python, которым установлены зависимости: их
    двоичные расширения собраны под конкретную версию и не заработают
    с другой.
    """
    probe = venv_root / "bin" / "python3"
    if not probe.exists():
        raise SystemExit(f"не найден интерпретатор окружения: {probe}")

    # Путь к фреймворку определяется по фактическому расположению файла
    # интерпретатора: sysconfig в этом вопросе ненадёжен и может указать
    # на сборочный каталог, которого на компьютере нет.
    real_executable = probe.resolve()
    prefix = None
    # Библиотека фреймворка называется по-разному: «Python3» у сборки Apple,
    # «Python» у сборок Homebrew и python.org
    for parent in real_executable.parents:
        if parent.parent.name == "Versions" and any(
            (parent / name).is_file() for name in FRAMEWORK_LIBRARY_NAMES
        ):
            prefix = parent
            break

    if prefix is None:
        result = subprocess.run(
            [str(probe), "-c", "import sys; print(sys.base_prefix)"],
            capture_output=True, text=True, check=True,
        )
        candidate = Path(result.stdout.strip())
        if any((candidate / name).is_file() for name in FRAMEWORK_LIBRARY_NAMES):
            prefix = candidate

    if prefix is None:
        raise SystemExit(
            f"интерпретатор {real_executable} не является частью фреймворка Python, "
            f"поэтому вложить его в связку нельзя. Самодостаточная сборка требует "
            f"Python, установленного как фреймворк (например, из инструментов "
            f"командной строки Xcode или с python.org). Используйте --mode thin."
        )
    return prefix, real_executable


def _rebrand_inner_app(inner_app: Path, icon_name: str,
                       hidpi: bool = False) -> None:
    """Переписывает паспорт вспомогательной связки Python внутри фреймворка.

    На macOS интерпретатор из фреймворка при запуске передаёт управление
    вложенной связке ``Python.app``: только программа-связка получает доступ
    к оконной системе. Именно её паспорт система и читает, показывая имя в
    строке меню, — поэтому «Python» надо заменить на «pdfedit» здесь, а не
    только во внешней связке.
    """
    plist_path = inner_app / "Contents" / "Info.plist"
    with open(plist_path, "rb") as handle:
        plist = plistlib.load(handle)

    # Файл интерпретатора переименовывается: его имя видно и в списке
    # процессов, и в «Мониторинге системы»
    macos_dir = inner_app / "Contents" / "MacOS"
    original = macos_dir / plist.get("CFBundleExecutable", "Python")
    renamed = macos_dir / APP_NAME
    if original.exists() and original != renamed:
        original.rename(renamed)
        renamed.chmod(0o755)

    plist.update({
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleIconFile": icon_name,
        "CFBundleVersion": __version__,
        "CFBundleShortVersionString": __version__,
        # Оформление и способ отрисовки система берёт у той связки, чей
        # исполняемый файл запущен, поэтому ключи нужны и здесь
        "NSHighResolutionCapable": hidpi,
        "NSRequiresAquaSystemAppearance": True,
        "CFBundleGetInfoString": f"pdfedit {__version__}",
    })
    # Справка и типы файлов достались от Python — они здесь ни к чему
    for key in ("CFBundleHelpBookFolder", "CFBundleHelpBookName",
                "CFBundleHelpTOCFile", "CFBundleDocumentTypes"):
        plist.pop(key, None)

    with open(plist_path, "wb") as handle:
        plistlib.dump(plist, handle)

    # Прежняя подпись после правки паспорта недействительна
    signature = inner_app / "Contents" / "_CodeSignature"
    if signature.exists():
        shutil.rmtree(signature)


def copy_python_runtime(contents: Path, prefix: Path, executable: Path) -> int:
    """Вкладывает в связку интерпретатор и стандартную библиотеку."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    # Каталог намеренно не называется «...framework»: связку с таким суффиксом
    # macOS проверяет по правилам фреймворков и отказывается подписывать, если
    # в ней нет положенных ссылок Versions/Current. Здесь это лишнее — файлы
    # находят друг друга по ссылке Contents/Python3.
    framework = contents / "Frameworks" / f"python{version}"
    framework.mkdir(parents=True)

    shutil.copy2(prefix / "Python3", framework / "Python3")

    stdlib_source = prefix / "lib" / f"python{version}"
    stdlib_target = framework / "lib" / f"python{version}"
    stdlib_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        stdlib_source, stdlib_target,
        ignore=shutil.ignore_patterns(*SKIP_STDLIB, "*.pyc"),
    )

    # Вспомогательная связка Python.app: интерпретатор передаёт ей управление,
    # чтобы получить доступ к оконной системе. Её расположение менять нельзя —
    # библиотеку она ищет по пути, отсчитанному от собственного файла.
    inner_source = prefix / "Resources" / "Python.app"
    if inner_source.is_dir():
        inner_target = framework / "Resources" / "Python.app"
        inner_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(inner_source, inner_target, symlinks=True)

    # Интерпретатор ищет библиотеку по пути @executable_path/../Python3,
    # то есть в Contents/Python3 — кладём туда относительную ссылку
    link = contents / "Python3"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(Path("Frameworks") / f"python{version}" / "Python3")

    return sum(f.stat().st_size for f in framework.rglob("*") if f.is_file())


def build(mode: str, output: Path, venv_root: Path, sign: bool) -> Path:
    """Собирает pdfedit.app."""
    app = output / f"{APP_NAME}.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    macos_dir = contents / "MacOS"
    resources = contents / "Resources"
    macos_dir.mkdir(parents=True)
    resources.mkdir(parents=True)

    # Значок
    from make_icon import build_icns

    icon_path = resources / f"{APP_NAME}.icns"
    build_icns(icon_path)
    print(f"  значок: {icon_path.name}")

    # Info.plist
    tk_version = detect_tk_version(venv_root)
    with open(contents / "Info.plist", "wb") as handle:
        plistlib.dump(build_info_plist(icon_path.name, tk_version), handle)
    print(f"  Info.plist: готов (Tk {tk_version[0]}.{tk_version[1]}, "
          f"поддержка Retina: {'да' if tk_version >= (8, 6) else 'нет — рисует система'})")

    # Код
    if mode == "standalone":
        prefix, executable = find_python_framework(venv_root)
        runtime_size = copy_python_runtime(contents, prefix, executable)
        inner_app = (
            contents / "Frameworks"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "Resources" / "Python.app"
        )
        if inner_app.is_dir():
            shutil.copy2(icon_path, inner_app / "Contents" / "Resources" / icon_path.name)
            _rebrand_inner_app(inner_app, icon_path.name, hidpi=tk_version >= (8, 6))
            print("  имя программы: pdfedit (паспорт вложенной связки переписан)")
        print(f"  интерпретатор: вложен ({runtime_size / 1024 / 1024:.0f} МБ)")
        copied = copy_dependencies(resources / "lib", venv_root)
        print(f"  вложено пакетов: {len(copied)} ({', '.join(copied[:6])}…)")
        python_setup = PYTHON_SETUP_STANDALONE
        env_setup = ENV_SETUP_STANDALONE
    else:
        # Даже в режиме разработки приложение должно называться своим именем,
        # поэтому вспомогательная связка Python.app копируется внутрь и
        # переименовывается. Она невелика (около 150 КБ) и находит библиотеку
        # интерпретатора по записанному в ней пути, так что копия работает.
        prefix, _executable = find_python_framework(venv_root)
        site_packages = next(
            (p for p in (venv_root / "lib").glob("python*/site-packages") if p.is_dir()),
            venv_root,
        )
        inner_source = prefix / "Resources" / "Python.app"
        if inner_source.is_dir():
            inner_target = resources / "Python.app"
            shutil.copytree(inner_source, inner_target, symlinks=True)
            shutil.copy2(icon_path, inner_target / "Contents" / "Resources" / icon_path.name)
            _rebrand_inner_app(inner_target, icon_path.name,
                               hidpi=tk_version >= (8, 6))
            print("  имя программы: pdfedit")
        python_setup = PYTHON_SETUP_THIN.format(venv_python=venv_root / "bin" / "python3")
        env_setup = ENV_SETUP_THIN.format(
            project_root=ROOT, python_home=prefix, site_packages=site_packages
        )
        print(f"  режим разработки: код берётся из {ROOT}")

    launcher = macos_dir / APP_NAME
    launcher.write_text(
        LAUNCHER.format(python_setup=python_setup, env_setup=env_setup, version=__version__)
    )
    launcher.chmod(0o755)

    # Подпись «для себя»: без неё macOS ругается на программу при переносе,
    # а с ней связка считается целостной на этом компьютере
    if sign:
        result = subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(app)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print("  подпись: выполнена (ad-hoc)")
        else:
            print(f"  подпись: пропущена ({result.stderr.strip().splitlines()[-1:]})")

    # Сообщаем системе о новой программе, чтобы значок и связь с PDF
    # заработали без перезапуска Finder. Прежняя запись сначала снимается:
    # после нескольких пересборок подряд служба запуска начинает считать
    # программу уже работающей и молча ничего не открывает.
    register_with_system(app)

    return app


def register_with_system(app: Path) -> None:
    """Обновляет запись о программе в службе запуска macOS."""
    lsregister = Path(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks"
        "/LaunchServices.framework/Support/lsregister"
    )
    if not lsregister.exists():
        return
    subprocess.run([str(lsregister), "-u", str(app)], capture_output=True)
    subprocess.run([str(lsregister), "-f", str(app)], capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Собирает приложение pdfedit.app для macOS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode", choices=("standalone", "thin"), default="standalone",
        help="standalone — самодостаточная связка со своим Python (по умолчанию); "
             "thin — тонкая обёртка над каталогом разработки",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "dist",
                        help="куда положить готовое приложение")
    parser.add_argument("--venv", type=Path, default=ROOT / ".venv",
                        help="окружение, откуда брать зависимости")
    parser.add_argument("--install", action="store_true",
                        help="скопировать готовое приложение в ~/Applications")
    parser.add_argument("--no-sign", action="store_true",
                        help="не подписывать связку")
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("Связка .app собирается только в macOS.", file=sys.stderr)
        print("В других системах запускайте программу как  python -m pdfedit gui",
              file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)
    print(f"Сборка pdfedit.app ({args.mode})…")
    app = build(args.mode, args.output, args.venv, sign=not args.no_sign)

    size = sum(f.stat().st_size for f in app.rglob("*") if f.is_file())
    print(f"\nГотово: {app}  ({size / 1024 / 1024:.0f} МБ)")

    if args.install:
        destination = Path.home() / "Applications" / app.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(app, destination, symlinks=True)
        register_with_system(destination)
        print(f"Установлено: {destination}")
        app = destination

    print(f"\nЗапуск:  open '{app}'")
    print("Открыть в нём файл:  open -a "
          f"'{app}' документ.pdf")
    return 0


if __name__ == "__main__":
    sys.exit(main())
