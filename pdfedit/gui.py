"""Графический редактор: правка текста прямо на изображении страницы.

Окно показывает настоящий отрисованный лист документа. Текстовые фрагменты,
которые программа умеет редактировать, обведены рамкой; щелчок по фрагменту
открывает поле ввода ровно на его месте. После правки страница
перерисовывается из уже изменённого документа — то есть видно не «предпросмотр
намерения», а фактический результат.

Подсветка:

* синяя рамка — фрагмент можно редактировать;
* серая рамка — фрагмент нередактируем (например, шрифт Type3);
* зелёная заливка — фрагмент изменён;
* красная рамка — правку применить не удалось (причина в журнале).
"""

from __future__ import annotations

import json
import os
import sys
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .editor import FIT_MODES, EditSpec, PdfEditor, minimal_edit
from .metadata import INFO_FIELDS, apply_metadata, read_metadata
from .saving import verify

IS_MACOS = sys.platform == "darwin"
#: Сочетание клавиш-модификатора: на macOS — Command, в остальных системах — Ctrl
ACCEL = "Command" if IS_MACOS else "Control"

# Цвета подсветки
COLOR_EDITABLE = "#4a7fd0"
COLOR_LOCKED = "#9a9a9a"
COLOR_CHANGED = "#2e9b57"
COLOR_CHANGED_FILL = "#c9f0d8"
COLOR_FAILED = "#cc3333"
COLOR_HOVER = "#f0a000"

ZOOM_STEPS = [0.5, 0.65, 0.8, 1.0, 1.25, 1.5, 2.0, 3.0]

#: Запас в точках вокруг видимой области. С ним небольшая прокрутка обходится
#: без новой отрисовки, а расход памяти остаётся ограниченным.
RENDER_MARGIN = 250


@dataclass
class RunBox:
    """Прямоугольник фрагмента на холсте."""

    run_id: int
    item_id: int
    x0: float
    y0: float
    x1: float
    y1: float
    editable: bool


class PdfEditApp:
    """Главное окно редактора."""

    def __init__(self, root: tk.Tk, path: str | None = None):
        self.root = root
        self.root.title("pdfedit — редактор PDF")
        self.root.geometry("1280x860")

        self.path: str | None = None
        self.original_bytes: bytes = b""
        self.base_editor: PdfEditor | None = None
        self.edits: list[EditSpec] = []
        self.failed_runs: set[int] = set()
        self.metadata_changes: dict[str, str] = {}

        self.page_index = 0
        self.zoom_index = ZOOM_STEPS.index(1.0)
        self.preview_bytes: bytes = b""
        self._preview_doc = None
        self._photo = None
        #: Отрисованный сейчас кусок листа (x0, y0, x1, y1) в координатах холста
        self._rendered_region = None
        #: Отложенный запрос на перерисовку после прокрутки
        self._rerender_job = None
        #: Размер листа в координатах холста (ширина, высота) при текущем увеличении
        self._full_size = None
        self._boxes: list[RunBox] = []
        self._editor_widget: tk.Entry | None = None
        self._editor_window: int | None = None
        self._editing_run: int | None = None
        self._transform = (1.0, 0.0, 0.0, -1.0, 0.0, 0.0)

        self.fit_mode = tk.StringVar(value="auto")
        self.preserve_id = tk.BooleanVar(value=True)
        #: Сохранять дописыванием слоя правок вместо пересборки файла
        self.incremental_save = tk.BooleanVar(value=False)
        self.show_boxes = tk.BooleanVar(value=True)
        #: Обновлять ли изображение страницы сразу после каждой правки
        self.live_preview = tk.BooleanVar(value=True)
        #: Источники недостающих глифов, помимо системных шрифтов
        self.use_document_fonts = tk.BooleanVar(value=True)
        self.use_font_library = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Откройте PDF-файл")

        self._apply_theme()
        self._build_ui()
        self._build_menu()
        self._register_system_handlers()
        # Перехват ошибок ставится последним, когда журнал уже готов принимать
        # сообщения
        self.root.report_callback_exception = self._on_callback_error
        if path:
            self.open_file(path)

    def _apply_theme(self) -> None:
        """Задаёт оформление, не полагаясь на системное.

        Штатная тема macOS («aqua») поручает отрисовку виджетов самой системе.
        Tk 8.5 — а именно он идёт с инструментами командной строки Apple —
        выпущен в 2010 году и о тёмном оформлении не знает: в тёмной теме
        система даёт окну тёмный фон, а виджеты остаются неотрисованными, и
        окно выглядит пустым.

        Поэтому берётся тема «clam»: она рисует всё собственными средствами,
        с явно заданными цветами, и от оформления системы не зависит.
        Оформление окна всегда светлое — вне зависимости от версии Tk и от
        настроек системы. Изображение самой страницы это не затрагивает:
        документ отрисовывается ровно в тех цветах, которые в нём заданы.
        """
        try:
            version = float(self.root.call("info", "patchlevel").rsplit(".", 1)[0])
        except Exception:
            version = 0.0
        self.legacy_tk = bool(IS_MACOS and version and version < 8.6)

        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")

        background, foreground, field = "#ececec", "#1a1a1a", "#ffffff"
        self.root.configure(background=background)
        # tk_setPalette задаёт цвета обычным (не ttk) виджетам разом
        try:
            self.root.tk_setPalette(
                background=background, foreground=foreground,
                activeBackground="#d6d6d6", activeForeground=foreground,
                selectBackground="#3875d7", selectForeground="#ffffff",
                highlightBackground=background, highlightColor="#8a8a8a",
                insertBackground=foreground,
            )
        except tk.TclError:
            pass

        for widget in ("TFrame", "TLabelframe", "TNotebook", "TPanedwindow"):
            style.configure(widget, background=background)
        for widget in ("TLabel", "TCheckbutton", "TRadiobutton", "TLabelframe.Label"):
            style.configure(widget, background=background, foreground=foreground)
        style.configure("TButton", background="#dcdcdc", foreground=foreground)
        style.map("TButton", background=[("active", "#c8c8c8")])
        style.configure("TNotebook.Tab", background="#d8d8d8", foreground=foreground)
        style.map("TNotebook.Tab", background=[("selected", background)])
        for widget in ("TEntry", "TCombobox", "Treeview"):
            style.configure(widget, fieldbackground=field, background=field,
                            foreground=foreground)
        style.configure("Treeview", background=field)
        style.map("Treeview", background=[("selected", "#3875d7")],
                  foreground=[("selected", "#ffffff")])

    def _on_callback_error(self, exc_type, value, tb) -> None:
        """Показывает ошибку вместо того, чтобы дать программе тихо умереть.

        Без этого исключение внутри обработчика Tkinter уходит в поток ошибок
        и, если программа запущена не из терминала, пропадает бесследно:
        пользователь видит лишь то, что окно перестало отзываться.
        """
        import traceback

        text = "".join(traceback.format_exception(exc_type, value, tb))
        self.log("СБОЙ: " + text.strip())
        self.set_status(f"Ошибка: {value}")
        messagebox.showerror(
            "Внутренняя ошибка",
            f"{exc_type.__name__}: {value}\n\n"
            "Программа продолжит работу. Подробности — в журнале справа "
            "и в файле ~/Library/Logs/pdfedit.log",
        )

    # ------------------------------------------------------------------
    # Меню и связь с операционной системой
    # ------------------------------------------------------------------
    def _build_menu(self) -> None:
        """Собирает строку меню приложения."""
        menubar = tk.Menu(self.root)

        if IS_MACOS:
            # Меню с именем "apple" macOS показывает как меню приложения —
            # в него попадают пункты «О программе» и «Завершить»
            app_menu = tk.Menu(menubar, name="apple")
            menubar.add_cascade(menu=app_menu)
            app_menu.add_command(label="О программе pdfedit", command=self.show_about)
            app_menu.add_separator()

        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Файл", menu=file_menu)
        file_menu.add_command(label="Открыть…", accelerator=f"{ACCEL}+O",
                              command=self.on_open)
        file_menu.add_command(label="Сохранить как…", accelerator=f"{ACCEL}+S",
                              command=self.on_save)
        file_menu.add_checkbutton(
            label="Сохранять дописыванием (не пересобирать файл)",
            variable=self.incremental_save,
        )
        file_menu.add_separator()
        file_menu.add_command(label="Экспорт списка правок…", command=self.on_export_edits)
        file_menu.add_command(label="Импорт списка правок…", command=self.on_import_edits)
        if not IS_MACOS:
            file_menu.add_separator()
            file_menu.add_command(label="Выход", command=self.on_quit)

        edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Правка", menu=edit_menu)
        edit_menu.add_command(label="Убрать выбранную правку", command=self.on_remove_edit)
        edit_menu.add_command(label="Убрать все правки", command=self.on_clear_edits)
        edit_menu.add_separator()
        edit_menu.add_command(label="Вернуть исходные метаданные",
                              command=self.load_metadata_fields)

        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Вид", menu=view_menu)
        view_menu.add_command(label="Следующая страница", accelerator="PgDn",
                              command=lambda: self.change_page(1))
        view_menu.add_command(label="Предыдущая страница", accelerator="PgUp",
                              command=lambda: self.change_page(-1))
        view_menu.add_separator()
        view_menu.add_command(label="Крупнее", accelerator=f"{ACCEL}++",
                              command=lambda: self.change_zoom(1))
        view_menu.add_command(label="Мельче", accelerator=f"{ACCEL}+-",
                              command=lambda: self.change_zoom(-1))
        view_menu.add_separator()
        view_menu.add_checkbutton(label="Показывать рамки фрагментов",
                                  variable=self.show_boxes, command=self.refresh_page)
        view_menu.add_checkbutton(label="Обновлять вид сразу после правки",
                                  variable=self.live_preview,
                                  command=lambda: self.rebuild_preview(force=True))
        view_menu.add_command(label="Обновить вид",
                              command=lambda: self.rebuild_preview(force=True))

        fonts_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Шрифты", menu=fonts_menu)
        fonts_menu.add_command(label="Добавить шрифты из PDF…",
                               command=self.on_add_donor_pdf)
        fonts_menu.add_command(label="Библиотека шрифтов…", command=self.show_font_library)
        fonts_menu.add_separator()
        fonts_menu.add_checkbutton(label="Брать глифы из этого же документа",
                                   variable=self.use_document_fonts,
                                   command=self.on_font_sources_changed)
        fonts_menu.add_checkbutton(label="Использовать библиотеку шрифтов",
                                   variable=self.use_font_library,
                                   command=self.on_font_sources_changed)

        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Справка", menu=help_menu)
        help_menu.add_command(label="Как пользоваться", command=self.show_help)
        if not IS_MACOS:
            help_menu.add_command(label="О программе", command=self.show_about)

        self.root.config(menu=menubar)

    def _register_system_handlers(self) -> None:
        """Подписывается на события операционной системы."""
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)
        if not IS_MACOS:
            return
        # Открытие файла двойным щелчком в Finder и перетаскиванием на значок
        try:
            self.root.createcommand("::tk::mac::OpenDocument", self.on_open_documents)
            self.root.createcommand("::tk::mac::Quit", self.on_quit)
            self.root.createcommand("tkAboutDialog", self.show_about)
            # Щелчок по значку в Dock должен возвращать окно на экран
            self.root.createcommand("::tk::mac::ReopenApplication", self._reopen)
        except tk.TclError:
            pass

    def _reopen(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_open_documents(self, *paths: str) -> None:
        """Обрабатывает файлы, открытые средствами системы."""
        for path in paths:
            if path.lower().endswith(".pdf"):
                self.open_file(path)
                self._reopen()
                break

    @property
    def has_unsaved_changes(self) -> bool:
        return bool(self.edits) or bool(self.collect_metadata_changes())

    def on_quit(self) -> None:
        """Завершает работу, предупреждая о несохранённых правках."""
        if self.has_unsaved_changes:
            answer = messagebox.askyesnocancel(
                "Есть несохранённые изменения",
                f"Внесённых правок: {len(self.edits)}. Сохранить их в новый файл?",
            )
            if answer is None:
                return
            if answer:
                self.on_save()
                if self.has_unsaved_changes:
                    return  # сохранение отменили — выход тоже отменяем
        if self.base_editor is not None:
            self.base_editor.close()
        self.root.destroy()

    # ------------------------------------------------------------------
    # Источники глифов
    # ------------------------------------------------------------------
    def on_add_donor_pdf(self) -> None:
        """Пополняет библиотеку шрифтами из указанных пользователем PDF."""
        paths = filedialog.askopenfilenames(
            title="Выберите PDF, откуда взять шрифты",
            filetypes=[("PDF", "*.pdf"), ("Все файлы", "*.*")],
        )
        if not paths:
            return
        from .donors import add_pdf_to_library, library_dir

        added = 0
        for path in paths:
            try:
                fonts = add_pdf_to_library(path)
            except Exception as exc:
                self.log(f"! {os.path.basename(path)}: не разобран ({exc})")
                continue
            self.log(f"Шрифты из {os.path.basename(path)}:")
            for font in fonts:
                mark = "+" if font.usable else "-"
                self.log(f"   {mark} {font.name}: {font.note}")
                added += int(font.usable)

        self._reset_font_index()
        messagebox.showinfo(
            "Шрифты добавлены",
            f"В библиотеку добавлено пригодных шрифтов: {added}.\n\n"
            f"Они будут использоваться как доноры недостающих букв — раньше "
            f"системных шрифтов.\n\nБиблиотека: {library_dir()}",
        )

    def show_font_library(self) -> None:
        """Показывает, что лежит в библиотеке доноров."""
        from .donors import clear_library, library_dir, library_fonts

        fonts = library_fonts()
        if not fonts:
            messagebox.showinfo(
                "Библиотека шрифтов пуста",
                "Пополнить её можно через «Шрифты → Добавить шрифты из PDF…».\n\n"
                "Пригодятся документы, где есть нужные буквы: программа возьмёт "
                "из них начертания, когда во внедрённом шрифте символов не хватит."
                f"\n\nКаталог: {library_dir()}",
            )
            return

        lines = []
        for font in fonts:
            style = font.style
            marks = " ".join(filter(None, [
                "жирный" if style.bold else "", "курсив" if style.italic else "",
            ])) or "обычный"
            lines.append(f"  {font.family} — {marks}")
        listing = "\n".join(lines[:40])
        if len(lines) > 40:
            listing += f"\n  … и ещё {len(lines) - 40}"

        if messagebox.askyesno(
            "Библиотека шрифтов",
            f"Шрифтов в библиотеке: {len(fonts)}\n\n{listing}\n\n"
            f"Каталог: {library_dir()}\n\nОчистить библиотеку?",
            default="no",
        ):
            removed = clear_library()
            self._reset_font_index()
            self.log(f"Библиотека шрифтов очищена, удалено файлов: {removed}")

    def on_font_sources_changed(self) -> None:
        """Пересобирает вид после смены набора источников глифов."""
        self._reset_font_index()
        if self.base_editor is not None:
            self.base_editor.use_document_fonts = self.use_document_fonts.get()
            self.base_editor.use_font_library = self.use_font_library.get()
        self.rebuild_preview(force=True)

    def _reset_font_index(self) -> None:
        """Сбрасывает кэш индекса шрифтов — состав библиотеки изменился."""
        from .fonts import fonts_in_dirs

        try:
            fonts_in_dirs.cache_clear()
        except Exception:
            pass

    def show_about(self) -> None:
        messagebox.showinfo(
            "О программе pdfedit",
            f"pdfedit {__version__}\n\n"
            "Редактирование текста и метаданных PDF на уровне объектов документа.\n"
            "Текст заменяется прямо в потоках содержимого, внедрённые шрифты, "
            "форматирование и структура сохраняются.\n\n"
            "Программа предназначена для законного использования: исправления "
            "опечаток в официальных документах, работы с архивами и проверки "
            "систем обработки PDF.",
        )

    def show_help(self) -> None:
        messagebox.showinfo(
            "Как пользоваться",
            "1. Откройте PDF (Файл → Открыть).\n"
            "2. Текст, который можно править, обведён синей рамкой.\n"
            "3. Щёлкните по нему — поле ввода откроется прямо на странице.\n"
            "4. Enter применяет правку, Esc отменяет.\n"
            "5. Страница сразу перерисовывается из уже изменённого документа: "
            "видно фактический результат.\n\n"
            "Изменённые фрагменты подсвечены зелёным, неудавшиеся — красным "
            "(причина в журнале справа).\n\n"
            "«Ширина текста» задаёт, что делать с разницей ширин:\n"
            "  auto — сжать, если разница мала, иначе переверстать строку;\n"
            "  natural — переверстать строку;\n"
            "  preserve — оставить соседний текст на месте;\n"
            "  squeeze — вписать в исходную ширину.\n\n"
            "Исходный файл никогда не перезаписывается: результат сохраняется "
            "в новый файл.",
        )

    def on_import_edits(self) -> None:
        """Загружает список правок, сохранённый ранее."""
        if self.base_editor is None:
            messagebox.showinfo("Сначала откройте документ",
                                "Список правок применяется к открытому файлу.")
            return
        path = filedialog.askopenfilename(
            title="Открыть список правок", filetypes=[("JSON", "*.json")]
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as handle:
                specs = [EditSpec.from_dict(item) for item in json.load(handle)]
        except Exception as exc:
            messagebox.showerror("Не удалось прочитать список правок", str(exc))
            return
        self.edits = specs
        self.refresh_edit_list()
        self.rebuild_preview()
        self.log(f"Загружен список правок: {path} (записей {len(specs)})")

    # ------------------------------------------------------------------
    # Построение интерфейса
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(6, 4))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(toolbar, text="Открыть…", command=self.on_open).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Сохранить как…", command=self.on_save).pack(
            side=tk.LEFT, padx=(4, 12)
        )

        ttk.Button(toolbar, text="◀", width=3, command=lambda: self.change_page(-1)).pack(
            side=tk.LEFT
        )
        self.page_label = ttk.Label(toolbar, text="— / —", width=10, anchor="center")
        self.page_label.pack(side=tk.LEFT)
        ttk.Button(toolbar, text="▶", width=3, command=lambda: self.change_page(1)).pack(
            side=tk.LEFT, padx=(0, 12)
        )

        ttk.Button(toolbar, text="−", width=3, command=lambda: self.change_zoom(-1)).pack(
            side=tk.LEFT
        )
        self.zoom_label = ttk.Label(toolbar, text="100%", width=6, anchor="center")
        self.zoom_label.pack(side=tk.LEFT)
        ttk.Button(toolbar, text="+", width=3, command=lambda: self.change_zoom(1)).pack(
            side=tk.LEFT, padx=(0, 12)
        )

        ttk.Checkbutton(
            toolbar, text="Показывать рамки", variable=self.show_boxes,
            command=self.refresh_page,
        ).pack(side=tk.LEFT, padx=(0, 12))

        # Обновление вида после правки стоит доли секунды, поэтому включено по
        # умолчанию; выключение оставлено для очень больших документов и для
        # тех, кому мешает перерисовка при каждой правке
        ttk.Checkbutton(
            toolbar, text="Обновлять вид сразу", variable=self.live_preview,
            command=lambda: self.rebuild_preview(force=True),
        ).pack(side=tk.LEFT)
        self.refresh_button = ttk.Button(
            toolbar, text="Обновить вид", width=13,
            command=lambda: self.rebuild_preview(force=True),
        )
        self.refresh_button.pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(toolbar, text="Ширина текста:").pack(side=tk.LEFT)
        fit_box = ttk.Combobox(
            toolbar, textvariable=self.fit_mode, values=list(FIT_MODES),
            width=9, state="readonly",
        )
        fit_box.pack(side=tk.LEFT, padx=(4, 0))
        fit_box.bind("<<ComboboxSelected>>", lambda _e: self.rebuild_preview())

        # --- основная область ---
        panes = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True)

        canvas_frame = ttk.Frame(panes)
        panes.add(canvas_frame, weight=3)

        self.canvas = tk.Canvas(canvas_frame, background="#6b6b6b", highlightthickness=0)
        # Прокрутка идёт через свои обёртки: после смещения вида надо
        # дорисовать ту часть листа, которая только что стала видимой
        vbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL,
                             command=self._scroll_y)
        hbar = ttk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL,
                             command=self._scroll_x)
        self.canvas.configure(
            yscrollcommand=lambda *a: (vbar.set(*a), self._on_view_changed()),
            xscrollcommand=lambda *a: (hbar.set(*a), self._on_view_changed()),
        )
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        hbar.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<Motion>", self.on_canvas_motion)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
        # Изменение размера окна тоже меняет видимую область
        self.canvas.bind("<Configure>", lambda e: self._on_view_changed())

        side = ttk.Notebook(panes)
        panes.add(side, weight=1)

        # Вкладка правок
        edits_tab = ttk.Frame(side, padding=6)
        side.add(edits_tab, text="Правки")
        self.edits_list = tk.Listbox(edits_tab, activestyle="none")
        self.edits_list.pack(fill=tk.BOTH, expand=True)
        self.edits_list.bind("<Double-Button-1>", self.on_edit_double_click)
        buttons = ttk.Frame(edits_tab)
        buttons.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(buttons, text="Убрать правку", command=self.on_remove_edit).pack(
            side=tk.LEFT
        )
        ttk.Button(buttons, text="Убрать все", command=self.on_clear_edits).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(buttons, text="Экспорт…", command=self.on_export_edits).pack(side=tk.RIGHT)

        # Вкладка метаданных
        meta_tab = ttk.Frame(side, padding=6)
        side.add(meta_tab, text="Метаданные")
        self.meta_entries: dict[str, tk.Entry] = {}
        for row, field in enumerate(INFO_FIELDS):
            label = field.lstrip("/")
            ttk.Label(meta_tab, text=label).grid(row=row, column=0, sticky="w", pady=2)
            entry = ttk.Entry(meta_tab, width=28)
            entry.grid(row=row, column=1, sticky="ew", pady=2, padx=(6, 0))
            self.meta_entries[field] = entry
        meta_tab.columnconfigure(1, weight=1)
        ttk.Label(
            meta_tab,
            text=("Даты: «2021-03-05 12:00:00», «now»\n"
                  "или исходный вид «D:20210305120000+03'00'».\n"
                  "Пустое поле — оставить как в оригинале."),
            foreground="#555555", justify="left",
        ).grid(row=len(INFO_FIELDS), column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(meta_tab, text="Вернуть исходные", command=self.load_metadata_fields).grid(
            row=len(INFO_FIELDS) + 1, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        # Вкладка журнала
        log_tab = ttk.Frame(side, padding=6)
        side.add(log_tab, text="Журнал")
        # Поле журнала — белое: так текст читается лучше, чем на сером фоне окна
        self.log_text = tk.Text(log_tab, wrap="word", height=10, state="disabled",
                                background="#ffffff", foreground="#1a1a1a",
                                insertbackground="#1a1a1a")
        log_scroll = ttk.Scrollbar(log_tab, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        ttk.Label(self.root, textvariable=self.status, relief=tk.SUNKEN, anchor="w",
                  padding=(6, 3)).pack(side=tk.BOTTOM, fill=tk.X)

        for key, handler in (
            ("o", lambda _e: self.on_open()),
            ("s", lambda _e: self.on_save()),
            ("plus", lambda _e: self.change_zoom(1)),
            ("equal", lambda _e: self.change_zoom(1)),
            ("minus", lambda _e: self.change_zoom(-1)),
        ):
            self.root.bind(f"<{ACCEL}-{key}>", handler)
        self.root.bind("<Prior>", lambda _e: self.change_page(-1))
        self.root.bind("<Next>", lambda _e: self.change_page(1))

    # ------------------------------------------------------------------
    # Журнал и статус
    # ------------------------------------------------------------------
    def log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        # Дублируем в обычный вывод: у приложения, запущенного из Finder, он
        # попадает в ~/Library/Logs/pdfedit.log — единственный способ узнать,
        # что происходило, если окно уже закрыто. Отсутствие вывода (например,
        # при запуске без терминала) не должно ничего ломать.
        try:
            print(message, flush=True)
        except (OSError, ValueError):
            pass

    def set_status(self, message: str) -> None:
        self.status.set(message)
        self.root.update_idletasks()

    # ------------------------------------------------------------------
    # Открытие и разбор
    # ------------------------------------------------------------------
    def on_open(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите PDF", filetypes=[("PDF", "*.pdf"), ("Все файлы", "*.*")]
        )
        if path:
            self.open_file(path)

    def open_file(self, path: str) -> None:
        self.set_status(f"Разбор {os.path.basename(path)}…")
        try:
            with open(path, "rb") as handle:
                self.original_bytes = handle.read()
            editor = PdfEditor(
                self.original_bytes, fit_mode=self.fit_mode.get(),
                use_document_fonts=self.use_document_fonts.get(),
                use_font_library=self.use_font_library.get(),
            )
            editor.parse()
        except Exception as exc:
            messagebox.showerror("Не удалось открыть", str(exc))
            self.set_status("Ошибка открытия")
            return

        if self.base_editor is not None:
            self.base_editor.close()
        self.base_editor = editor
        self.path = path
        self.edits = []
        self.failed_runs = set()
        self.metadata_changes = {}
        self.page_index = 0
        self.preview_bytes = self.original_bytes
        self._open_preview_doc()

        self.root.title(f"pdfedit — {os.path.basename(path)}")
        self.log(f"Открыт {path}: страниц {editor.page_count}, "
                 f"текстовых фрагментов {len(editor.runs)}")
        for warning in editor.warnings[:20]:
            self.log(f"  ! {warning}")
        self.load_metadata_fields()
        self.refresh_edit_list()
        self.refresh_page()

        # У отсканированных документов текста нет вовсе: страница — это
        # картинка. Без пояснения пустой лист без рамок выглядит как поломка
        if not editor.runs:
            self.log("  ! Текстовых фрагментов не найдено — вероятно, документ "
                     "отсканирован. Править можно только метаданные.")
            messagebox.showinfo(
                "В документе нет текста",
                "Программа не нашла ни одного текстового фрагмента. Скорее "
                "всего, страницы представляют собой изображения (скан или "
                "фотографии), и редактировать в них нечего: текста как "
                "объектов PDF там нет.\n\n"
                "Метаданные такого файла править по-прежнему можно — "
                "на вкладке «Метаданные».",
            )

    def load_metadata_fields(self) -> None:
        """Заполняет поля метаданных значениями из документа."""
        if self.base_editor is None:
            return
        snapshot = read_metadata(self.base_editor.pdf)
        for field, entry in self.meta_entries.items():
            entry.delete(0, "end")
            value = snapshot.info.get(field)
            if value:
                entry.insert(0, value)
        self.metadata_changes = {}

    def collect_metadata_changes(self) -> dict[str, str]:
        """Собирает поля метаданных, которые пользователь изменил."""
        if self.base_editor is None:
            return {}
        snapshot = read_metadata(self.base_editor.pdf)
        changes: dict[str, str] = {}
        for field, entry in self.meta_entries.items():
            new_value = entry.get().strip()
            old_value = (snapshot.info.get(field) or "").strip()
            if new_value != old_value:
                changes[field] = new_value
        return changes

    # ------------------------------------------------------------------
    # Отрисовка страницы
    # ------------------------------------------------------------------
    def _open_preview_doc(self) -> None:
        from .mupdf import fitz

        if self._preview_doc is not None:
            self._preview_doc.close()
        self._preview_doc = fitz.open(stream=self.preview_bytes, filetype="pdf")

    @property
    def zoom(self) -> float:
        return ZOOM_STEPS[self.zoom_index]

    def refresh_page(self) -> None:
        """Перерисовывает текущую страницу и накладывает рамки фрагментов.

        Отрисовывается только та часть листа, которая сейчас видна в окне.
        Иначе расход памяти растёт как квадрат увеличения и площадь страницы:
        у отсканированных документов лист бывает 1900×2800 точек, и при
        трёхкратном увеличении один такой показ потребовал бы около полугигабайта
        — система в этот момент просто снимает программу, и со стороны это
        выглядит как внезапное закрытие.
        """
        if self.base_editor is None or self._preview_doc is None:
            return
        self._close_inline_editor()
        from .mupdf import fitz

        page = self._preview_doc[self.page_index]
        zoom = self.zoom
        full_width = page.rect.width * zoom
        full_height = page.rect.height * zoom

        # Область прокрутки задаётся по всему листу, чтобы полосы прокрутки
        # показывали настоящий размер страницы, а не размер отрисованного куска
        self.canvas.configure(scrollregion=(0, 0, full_width, full_height))
        self._full_size = (full_width, full_height)
        clip = self._visible_clip(page, zoom, full_width, full_height, fitz)

        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
        self._photo = tk.PhotoImage(data=pixmap.tobytes("ppm"))
        self._rendered_region = (
            pixmap.x, pixmap.y, pixmap.x + pixmap.width, pixmap.y + pixmap.height
        )

        self.canvas.delete("all")
        # Отрисованный кусок кладётся на своё место в координатах листа
        self.canvas.create_image(pixmap.x, pixmap.y, image=self._photo, anchor="nw")

        # Преобразование координат PDF → координаты изображения
        transform = page.transformation_matrix
        self._transform = (transform.a, transform.b, transform.c,
                           transform.d, transform.e, transform.f)

        self._boxes = []
        if self.show_boxes.get():
            self._draw_run_boxes(fitz)

        self.page_label.configure(
            text=f"{self.page_index + 1} / {self._preview_doc.page_count}"
        )
        self.zoom_label.configure(text=f"{int(self.zoom * 100)}%")
        edited = len({(e.page_index, e.run_id) for e in self.edits})
        self.set_status(
            f"Страница {self.page_index + 1}: фрагментов "
            f"{len(self.base_editor.page_runs(self.page_index))}, правок в документе {edited}"
        )

    def _visible_clip(self, page, zoom: float, full_width: float,
                      full_height: float, fitz):
        """Возвращает область листа, которую нужно отрисовать.

        Это видимая часть окна плюс запас по краям: с запасом небольшая
        прокрутка обходится без новой отрисовки. Размер запаса ограничен,
        поэтому расход памяти не зависит ни от увеличения, ни от размера листа.
        """
        view_width = self.canvas.winfo_width()
        view_height = self.canvas.winfo_height()
        # До первой раскладки окна размеры ещё не известны
        if view_width <= 1 or view_height <= 1:
            view_width, view_height = 1000, 800

        left = self.canvas.canvasx(0)
        top = self.canvas.canvasy(0)
        # Пока страница не показана, canvasx возвращает ноль — этого достаточно:
        # отрисуется верхний левый угол листа, остальное догрузится при прокрутке
        left = max(0.0, min(left, max(0.0, full_width - view_width)))
        top = max(0.0, min(top, max(0.0, full_height - view_height)))

        x0 = max(0.0, left - RENDER_MARGIN)
        y0 = max(0.0, top - RENDER_MARGIN)
        x1 = min(full_width, left + view_width + RENDER_MARGIN)
        y1 = min(full_height, top + view_height + RENDER_MARGIN)

        # Обратный переход к координатам страницы: get_pixmap ждёт область
        # в единицах листа, а не в точках изображения
        return fitz.Rect(x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom) & page.rect

    def _needs_rerender(self) -> bool:
        """Проверяет, вышло ли окно за пределы уже отрисованного куска.

        Требуемая область обязательно урезается по краям листа. Без этого
        страница, которая меньше окна (обычное дело при уменьшении), считалась
        бы отрисованной не полностью: за краем листа рисовать нечего, но
        проверка требовала бы этого снова и снова — и холст перерисовывался бы
        без остановки, восемь раз в секунду, показывая пустоту.
        """
        if self._rendered_region is None or self._full_size is None:
            return True
        full_width, full_height = self._full_size
        left = max(0.0, self.canvas.canvasx(0))
        top = max(0.0, self.canvas.canvasy(0))
        right = min(full_width, left + max(self.canvas.winfo_width(), 1))
        bottom = min(full_height, top + max(self.canvas.winfo_height(), 1))
        rx0, ry0, rx1, ry1 = self._rendered_region
        # Небольшой допуск, чтобы не перерисовывать из-за округлений
        return (left < rx0 - 1 or top < ry0 - 1
                or right > rx1 + 1 or bottom > ry1 + 1)

    def _on_view_changed(self, *_args) -> None:
        """Откладывает перерисовку до окончания прокрутки.

        Прокрутка порождает поток событий; отрисовывать на каждое — значит
        занять процессор целиком. Поэтому запрос откладывается и выполняется
        один раз, когда движение прекратилось.
        """
        if self._rerender_job is not None:
            self.root.after_cancel(self._rerender_job)
        self._rerender_job = self.root.after(120, self._rerender_if_needed)

    def _rerender_if_needed(self) -> None:
        self._rerender_job = None
        if self._preview_doc is not None and self._needs_rerender():
            self.refresh_page()

    def _find_edited_rect(self, edit: EditSpec, original, fitz):
        """Находит, где изменённый текст оказался на самом деле.

        Рамка изменённого фрагмента иначе рисуется по координатам исходного
        текста, а он после правки сдвигается: новый текст другой ширины, да
        ещё и выключка может сместить всю строку. Рамка тогда стоит не на
        месте и закрывает собой цифры.
        """
        text = (edit.new_text or "").strip()
        if not text or self._preview_doc is None:
            return None
        try:
            hits = self._preview_doc[self.page_index].search_for(text)
        except Exception:
            return None
        if not hits:
            return None
        # Совпадений может быть несколько — берём ближайшее по вертикали
        # к прежнему положению фрагмента
        center = (original.y0 + original.y1) / 2.0
        return min(hits, key=lambda r: (abs((r.y0 + r.y1) / 2.0 - center),
                                        abs(r.x0 - original.x0)))

    def _draw_run_boxes(self, fitz) -> None:
        edits_by_run = {e.run_id: e for e in self.edits}
        matrix = fitz.Matrix(*self._transform)
        for run in self.base_editor.page_runs(self.page_index):
            x0, y0, x1, y1 = run.bbox
            rect = fitz.Rect(x0, y0, x1, y1) * matrix
            edit = edits_by_run.get(run.run_id)
            if edit is not None:
                found = self._find_edited_rect(edit, rect, fitz)
                if found is not None:
                    rect = found
            cx0, cy0 = rect.x0 * self.zoom, rect.y0 * self.zoom
            cx1, cy1 = rect.x1 * self.zoom, rect.y1 * self.zoom
            if cy1 < cy0:
                cy0, cy1 = cy1, cy0

            if run.run_id in self.failed_runs:
                outline, fill, width = COLOR_FAILED, "", 2
            elif edit is not None:
                # Только рамка, без заливки: изменённый текст надо разглядеть,
                # а не закрасить
                outline, fill, width = COLOR_CHANGED, "", 2
            elif run.editable:
                outline, fill, width = COLOR_EDITABLE, "", 1
            else:
                outline, fill, width = COLOR_LOCKED, "", 1

            # Рамки рисуются после изображения страницы, поэтому лежат поверх
            # него; редкая заливка (stipple) подсвечивает, не пряча текст
            item = self.canvas.create_rectangle(
                cx0 - 1, cy0 - 1, cx1 + 1, cy1 + 1,
                outline=outline, width=width, fill=fill, stipple="gray12" if fill else "",
            )
            self._boxes.append(
                RunBox(run.run_id, item, cx0, cy0, cx1, cy1, run.editable)
            )

    def change_page(self, delta: int) -> None:
        if self._preview_doc is None:
            return
        target = self.page_index + delta
        if 0 <= target < self._preview_doc.page_count:
            self.page_index = target
            self.refresh_page()

    def change_zoom(self, delta: int) -> None:
        target = self.zoom_index + delta
        if 0 <= target < len(ZOOM_STEPS):
            self.zoom_index = target
            self.refresh_page()

    def _scroll_y(self, *args) -> None:
        self.canvas.yview(*args)
        self._on_view_changed()

    def _scroll_x(self, *args) -> None:
        self.canvas.xview(*args)
        self._on_view_changed()

    def _on_wheel(self, event) -> None:
        if event.state & 0x0004:  # Ctrl — масштабирование
            self.change_zoom(1 if event.delta > 0 else -1)
        else:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    # ------------------------------------------------------------------
    # Правка текста на странице
    # ------------------------------------------------------------------
    def _box_at(self, x: float, y: float) -> RunBox | None:
        for box in self._boxes:
            if box.x0 - 2 <= x <= box.x1 + 2 and box.y0 - 2 <= y <= box.y1 + 2:
                return box
        return None

    def on_canvas_motion(self, event) -> None:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        box = self._box_at(x, y)
        self.canvas.configure(cursor="xterm" if box and box.editable else "")
        if box is not None and self.base_editor is not None:
            run = self.base_editor.run_by_id(box.run_id)
            if run is not None:
                note = "" if run.editable else "  [нередактируем]"
                self.set_status(f"#{run.run_id} {run.font_res} {run.size:g}пт: {run.text!r}{note}")

    def on_canvas_click(self, event) -> None:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        box = self._box_at(x, y)
        self._close_inline_editor()
        if box is None:
            return
        if not box.editable:
            messagebox.showinfo(
                "Фрагмент нередактируем",
                "Этот текст нарисован шрифтом, который программа не умеет "
                "изменять (например Type3), либо в нём нет распознанных глифов.",
            )
            return
        self._open_inline_editor(box)

    def _open_inline_editor(self, box: RunBox) -> None:
        """Открывает поле ввода поверх фрагмента."""
        if self.base_editor is None:
            return
        run = self.base_editor.run_by_id(box.run_id)
        if run is None:
            return
        current = self._current_text_for_run(run)

        height = max(int(box.y1 - box.y0), 16)
        entry = tk.Entry(
            self.canvas,
            font=("Helvetica", max(8, int(run.size * self.zoom * 0.85))),
            relief="solid", borderwidth=1, background="#fffbe6",
        )
        entry.insert(0, current)
        entry.select_range(0, "end")
        width = max(int(box.x1 - box.x0) + 60, 140)
        window = self.canvas.create_window(
            box.x0 - 2, box.y0 - 2, window=entry, anchor="nw",
            width=width, height=height + 6,
        )
        entry.focus_set()
        entry.bind("<Return>", lambda _e: self._commit_inline_editor())
        entry.bind("<Escape>", lambda _e: self._close_inline_editor())
        entry.bind("<FocusOut>", lambda _e: self._commit_inline_editor())

        self._editor_widget = entry
        self._editor_window = window
        self._editing_run = box.run_id

    def _current_text_for_run(self, run) -> str:
        """Текущий текст фрагмента с учётом уже внесённых правок."""
        text = run.text
        applicable = [e for e in self.edits if e.run_id == run.run_id]
        if not applicable:
            return text
        # Правки хранятся в координатах глифов; применяем их к тексту фрагмента
        pieces: list[tuple[int, int, str]] = []
        for edit in applicable:
            char_start = next(
                (i for i, g in enumerate(run.char_to_glyph) if g == edit.glyph_start), 0
            )
            char_end = max(
                (i + 1 for i, g in enumerate(run.char_to_glyph) if 0 <= g < edit.glyph_end),
                default=char_start,
            )
            pieces.append((char_start, char_end, edit.new_text))
        for start, end, replacement in sorted(pieces, reverse=True):
            text = text[:start] + replacement + text[end:]
        return text

    def _commit_inline_editor(self) -> None:
        if self._editor_widget is None or self._editing_run is None:
            return
        new_text = self._editor_widget.get()
        run_id = self._editing_run
        self._close_inline_editor()

        if self.base_editor is None:
            return
        run = self.base_editor.run_by_id(run_id)
        if run is None:
            return
        if new_text == self._current_text_for_run(run):
            return

        # Убираем прежние правки этого фрагмента: новый текст задаёт его целиком
        self.edits = [e for e in self.edits if e.run_id != run_id]
        change = minimal_edit(run.text, new_text)
        if change is None:
            self.rebuild_preview()
            return
        char_start, char_end, replacement = change
        glyph_start, glyph_end, expanded = run.glyph_span_for_chars(char_start, char_end)
        if glyph_end <= glyph_start:
            messagebox.showwarning(
                "Не удалось определить правку",
                "Изменение не удалось сопоставить с глифами фрагмента.",
            )
            return
        if expanded:
            # Правка задела лигатуру: расширяем замену до её границ
            covered = "".join(run.glyphs[i].text for i in range(glyph_start, glyph_end))
            original = run.text[char_start:char_end]
            head = covered[: covered.find(original)] if original in covered else ""
            tail = covered[len(head) + len(original):]
            replacement = head + replacement + tail

        self.edits.append(
            EditSpec(
                page_index=run.page_index, run_id=run.run_id,
                glyph_start=glyph_start, glyph_end=glyph_end,
                new_text=replacement, old_text=run.text[char_start:char_end],
            )
        )
        self.refresh_edit_list()
        self.rebuild_preview()

    def _close_inline_editor(self) -> None:
        widget, window = self._editor_widget, self._editor_window
        self._editor_widget = None
        self._editor_window = None
        self._editing_run = None
        if window is not None:
            try:
                self.canvas.delete(window)
            except tk.TclError:
                pass
        if widget is not None:
            try:
                widget.destroy()
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    # Предпросмотр результата
    # ------------------------------------------------------------------
    def rebuild_preview(self, force: bool = False) -> None:
        """Применяет все правки к копии документа и перерисовывает страницу.

        При выключенном «Обновлять вид сразу» пересборка откладывается:
        правки накапливаются, изменённые фрагменты помечаются на странице
        зелёным, но текст под ними остаётся прежним до нажатия «Обновить вид»
        или до сохранения. Сохранение в любом случае собирает документ заново
        из исходных байтов, поэтому на результат режим не влияет.
        """
        if self.base_editor is None:
            return
        if self.edits and not force and not self.live_preview.get():
            self.refresh_page()
            self.set_status(
                f"Правок накоплено: {len(self.edits)}. Вид не обновлён — "
                f"нажмите «Обновить вид»"
            )
            return
        if not self.edits:
            self.preview_bytes = self.original_bytes
            self.failed_runs = set()
            self._open_preview_doc()
            self.refresh_page()
            return

        self.set_status("Применение правок…")
        try:
            editor = PdfEditor(
                self.original_bytes, fit_mode=self.fit_mode.get(),
                use_document_fonts=self.use_document_fonts.get(),
                use_font_library=self.use_font_library.get(),
            )
            # Разбираются только страницы, которых касаются правки. Полный
            # разбор занимает почти всё время пересборки — на документе в 279
            # страниц это 5,8 секунды из 5,9, притом что правка затрагивает
            # одну страницу. Номера фрагментов от объёма разбора не зависят,
            # поэтому частичный разбор безопасен.
            touched = sorted({spec.page_index for spec in self.edits})
            editor.parse(touched)
            report = editor.apply_edits(self.edits)
            data = editor.to_bytes(preserve_id=self.preserve_id.get())
            editor.close()
        except Exception as exc:
            messagebox.showerror("Ошибка применения правок", str(exc))
            self.set_status("Ошибка применения правок")
            return

        self.failed_runs = {spec.run_id for spec, _ in report.skipped}
        for change in report.font_changes:
            self.log(f"шрифт: {change}")
        for warning in report.warnings:
            self.log(f"! {warning}")
        for spec, reason in report.skipped:
            self.log(f"[не применено] «{spec.old_text}» → «{spec.new_text}»: {reason}")

        self.preview_bytes = data
        self._open_preview_doc()
        self.refresh_page()

    # ------------------------------------------------------------------
    # Список правок
    # ------------------------------------------------------------------
    def refresh_edit_list(self) -> None:
        self.edits_list.delete(0, "end")
        for edit in self.edits:
            mark = "✗ " if edit.run_id in self.failed_runs else ""
            self.edits_list.insert(
                "end", f"{mark}с.{edit.page_index + 1}: «{edit.old_text}» → «{edit.new_text}»"
            )

    def on_edit_double_click(self, _event) -> None:
        selection = self.edits_list.curselection()
        if not selection:
            return
        edit = self.edits[selection[0]]
        self.page_index = edit.page_index
        self.refresh_page()

    def on_remove_edit(self) -> None:
        selection = self.edits_list.curselection()
        if not selection:
            return
        del self.edits[selection[0]]
        self.refresh_edit_list()
        self.rebuild_preview()

    def on_clear_edits(self) -> None:
        if not self.edits:
            return
        if messagebox.askyesno("Убрать все правки", "Отменить все внесённые правки?"):
            self.edits = []
            self.failed_runs = set()
            self.refresh_edit_list()
            self.rebuild_preview()

    def on_export_edits(self) -> None:
        if not self.edits:
            messagebox.showinfo("Нечего экспортировать", "Список правок пуст.")
            return
        path = filedialog.asksaveasfilename(
            title="Сохранить список правок", defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as handle:
            json.dump([e.to_dict() for e in self.edits], handle, ensure_ascii=False, indent=2)
        self.log(f"Список правок сохранён: {path} "
                 f"(применить: pdfedit replace … --edits {os.path.basename(path)})")

    # ------------------------------------------------------------------
    # Сохранение
    # ------------------------------------------------------------------
    def on_save(self) -> None:
        if self.base_editor is None or self.path is None:
            return
        meta_changes = self.collect_metadata_changes()
        if not self.edits and not meta_changes:
            messagebox.showinfo("Нет изменений", "Ни текст, ни метаданные не менялись.")
            return

        suggested = os.path.splitext(os.path.basename(self.path))[0] + "-изменённый.pdf"
        target = filedialog.asksaveasfilename(
            title="Сохранить как", defaultextension=".pdf", initialfile=suggested,
            filetypes=[("PDF", "*.pdf")],
        )
        if not target:
            return
        if os.path.abspath(target) == os.path.abspath(self.path):
            messagebox.showerror(
                "Недопустимый файл",
                "Результат нужно сохранить в новый файл, а не поверх исходного.",
            )
            return

        self.set_status("Сохранение…")
        try:
            editor = PdfEditor(self.original_bytes, fit_mode=self.fit_mode.get())
            # Как и при пересборке вида, разбираются только затронутые
            # страницы: остальные попадают в результат нетронутыми объектами
            editor.parse(sorted({spec.page_index for spec in self.edits}))
            report = editor.apply_edits(self.edits)
            if meta_changes:
                for note in apply_metadata(editor.pdf, meta_changes):
                    self.log(f"метаданные: {note}")
            if self.incremental_save.get():
                editor.save(target, incremental=True)
                if editor.last_incremental_report is not None:
                    self.log(editor.last_incremental_report.describe())
            else:
                editor.save(target, preserve_id=self.preserve_id.get())
            editor.close()
        except Exception as exc:
            messagebox.showerror("Ошибка сохранения", str(exc))
            self.set_status("Ошибка сохранения")
            return

        check = verify(self.path, target, set(meta_changes))
        self.log(f"\nСохранено: {target}")
        self.log(check.describe())
        summary = (
            f"Сохранено правок: {len(report.applied)}"
            + (f", не применено: {len(report.skipped)}" if report.skipped else "")
        )
        self.set_status(summary)
        messagebox.showinfo(
            "Готово",
            f"{summary}\n\nФайл: {target}\n\n"
            + ("Посторонних изменений метаданных и структуры не обнаружено."
               if check.clean else "Внимание: есть расхождения, подробности в журнале."),
        )


def run_gui(path: str | None = None) -> int:
    """Запускает графический редактор."""
    # Аварийное завершение самого интерпретатора (нехватка памяти, сбой в
    # библиотеке) иначе не оставляет никаких следов — с faulthandler в журнал
    # попадёт хотя бы место, на котором всё оборвалось
    try:
        import faulthandler

        faulthandler.enable()
    except Exception:
        pass

    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.4)
    except tk.TclError:
        pass

    app = PdfEditApp(root, path)

    if IS_MACOS:
        # Приложение, запущенное из связки .app, должно выйти на передний план
        # само — иначе окно откроется позади уже работающих программ
        try:
            root.createcommand("::tk::mac::ShowHelp", app.show_help)
        except tk.TclError:
            pass
        root.after(50, app._reopen)

    root.mainloop()
    return 0
