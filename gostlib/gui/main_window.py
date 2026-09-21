"""Главное окно GostLib."""
from __future__ import annotations

import os
import subprocess
import traceback
from typing import Dict, List, Optional

from PySide6.QtCore import QSortFilterProxyModel, Qt, QThread, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QStandardItem, \
    QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox,
                               QDockWidget, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow,
                               QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QDialog, QProgressBar,
                               QSplitter, QStatusBar, QTabWidget, QTableView,
                               QTableWidget, QTableWidgetItem, QToolBar,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget, QCheckBox, QSpinBox,
                               QSizePolicy)

from .. import classify, config
from ..ir import ETYPES, Component
from ..render import svg
from ..service import Service
from .dialogs import KicadDialog, LcscDialog, SettingsDialog
from .symedit import PinPlanDialog
from .view3d import Model3DPane
from .widgets import PreviewPane

COLS = ["Имя", "Тип", "Обозн.", "Парт-номер", "Производитель", "Корпус",
        "Выводы", "Посадка", "Источник", "В библиотеке", "uid"]
COL_UID = len(COLS) - 1

# Что можно править прямо в таблице. Тип, число выводов и посадка --
# производные величины, их правят на своих вкладках.
EDITABLE_COLS = {"Имя", "Обозн.", "Парт-номер", "Производитель"}

# Модель крупнее этого честно предупреждает, что разбор небыстрый.
# 69 МБ у CH375B из EasyEDA -- это десятки секунд даже в фоне.
BIG_MODEL = 12_000_000


def _mb(n: int) -> str:
    """Размер по-человечески: у 3D-моделей разброс от килобайт до сотен МБ."""
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} ГБ"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} МБ"
    if n >= 1024:
        return f"{n / 1024:.0f} КБ"
    return f"{n} Б"

DARK_QSS = """
QWidget { background:#1f2126; color:#dfe3ea; font-size:12px; }
QLineEdit,QComboBox,QPlainTextEdit,QTableWidget,QTableView,QTreeWidget {
  background:#282b32; border:1px solid #3a3f4a; border-radius:4px; padding:3px; }
QSpinBox,QDoubleSpinBox {
  background:#282b32; border:1px solid #3a3f4a; border-radius:4px;
  padding:2px 18px 2px 4px; min-height:20px; }
QSpinBox::up-button,QDoubleSpinBox::up-button {
  subcontrol-origin:border; subcontrol-position:top right;
  width:16px; height:11px; background:#3a4150; border-left:1px solid #4a5262; }
QSpinBox::down-button,QDoubleSpinBox::down-button {
  subcontrol-origin:border; subcontrol-position:bottom right;
  width:16px; height:11px; background:#3a4150; border-left:1px solid #4a5262; }
QSpinBox::up-button:hover,QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover,QDoubleSpinBox::down-button:hover {
  background:#4a5262; }
QSpinBox::up-arrow,QDoubleSpinBox::up-arrow {
  image:none; border-left:4px solid transparent; border-right:4px solid transparent;
  border-bottom:5px solid #cfd6e2; width:0; height:0; }
QSpinBox::down-arrow,QDoubleSpinBox::down-arrow {
  image:none; border-left:4px solid transparent; border-right:4px solid transparent;
  border-top:5px solid #cfd6e2; width:0; height:0; }
QHeaderView::section { background:#2c3038; border:0; padding:5px; }
QToolBar { background:#242730; border:0; spacing:4px; padding:4px; }
QPushButton { background:#333844; border:1px solid #444b58; border-radius:4px;
  padding:5px 12px; }
QPushButton:hover { background:#3d4451; }
QTabBar::tab { background:#282b32; padding:6px 12px; border:0; }
QTabBar::tab:selected { background:#39414f; }
QTableView::item:selected,QTreeWidget::item:selected { background:#3c5a8a; }
QStatusBar { background:#242730; }
"""

LIGHT_QSS = """
QWidget { background:#f4f5f8; color:#1b1e24; font-size:12px; }
QLineEdit,QComboBox,QPlainTextEdit,QTableWidget,QTableView,QTreeWidget {
  background:#ffffff; border:1px solid #c6cbd6; border-radius:4px; padding:3px; }
QSpinBox,QDoubleSpinBox {
  background:#ffffff; border:1px solid #c6cbd6; border-radius:4px;
  padding:2px 18px 2px 4px; min-height:20px; }
QSpinBox::up-button,QDoubleSpinBox::up-button {
  subcontrol-origin:border; subcontrol-position:top right;
  width:16px; height:11px; background:#e3e6ec; border-left:1px solid #c6cbd6; }
QSpinBox::down-button,QDoubleSpinBox::down-button {
  subcontrol-origin:border; subcontrol-position:bottom right;
  width:16px; height:11px; background:#e3e6ec; border-left:1px solid #c6cbd6; }
QSpinBox::up-arrow,QDoubleSpinBox::up-arrow {
  image:none; border-left:4px solid transparent; border-right:4px solid transparent;
  border-bottom:5px solid #3a4150; width:0; height:0; }
QSpinBox::down-arrow,QDoubleSpinBox::down-arrow {
  image:none; border-left:4px solid transparent; border-right:4px solid transparent;
  border-top:5px solid #3a4150; width:0; height:0; }
QHeaderView::section { background:#e6e9ef; border:0; padding:5px; }
QToolBar { background:#e9ecf2; border:0; spacing:4px; padding:4px; }
QPushButton { background:#e6e9ef; border:1px solid #c6cbd6; border-radius:4px;
  padding:5px 12px; }
QPushButton:hover { background:#dde1ea; }
QTabBar::tab { background:#e6e9ef; padding:6px 12px; border:0; }
QTabBar::tab:selected { background:#ffffff; }
QTableView::item:selected,QTreeWidget::item:selected {
  background:#cfe0ff; color:#12151a; }
QStatusBar { background:#e9ecf2; }
"""


def app_icon():
    """Иконка приложения: берём файл рядом с кодом, иначе рисуем на месте."""
    from PySide6.QtGui import QIcon, QPixmap, QPainter, QPen, QColor
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "gostlib.ico"),
                 os.path.join(os.path.dirname(here), "gostlib.ico")):
        if os.path.isfile(cand):
            ic = QIcon(cand)
            if not ic.isNull():
                return ic
    pm = QPixmap(64, 64)
    pm.fill(QColor("#f0f2f6"))
    p = QPainter(pm)
    p.setPen(QPen(QColor("#14161c"), 3))
    p.drawRect(14, 10, 36, 44)
    p.setPen(QPen(QColor("#14161c"), 2))
    for i in range(4):
        y = 18 + i * 10
        p.drawLine(4, y, 14, y)
        p.drawLine(50, y, 60, y)
    p.end()
    return QIcon(pm)


class ImportWorker(QThread):
    done = Signal(list, str)
    msg = Signal(str)

    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args

    def run(self):
        try:
            res = self.fn(*self.args)
            self.done.emit(list(res or []), "")
        except Exception as e:
            self.msg.emit(traceback.format_exc(limit=3))
            self.done.emit([], str(e))


class ModelWorker(QThread):
    """
    Разбор 3D-модели вне главного потока.

    STEP, собранный из OBJ, бывает на десятки мегабайт, и OpenCASCADE
    жуёт такой файл десятки секунд. В главном потоке это выглядит как
    намертво зависшее окно.
    """
    done = Signal(int, object, object, list)

    def __init__(self, path: str, fp, models_dir: str, token: int):
        super().__init__()
        self.path, self.fp, self.models_dir, self.token = (path, fp,
                                                           models_dir, token)
        self.stale = False

    def run(self):
        from .. import mesh3d
        extra: List[str] = []
        model = mesh = None
        # Сначала пробуем нормальную поверхность. Для моделей EasyEDA рядом
        # со STEP лежит исходный OBJ: он читается примерно за секунду, тогда
        # как повторный разбор фасетного STEP может занимать минуты.
        try:
            mesh = mesh3d.load(self.path,
                               os.path.join(self.models_dir, "_mesh_cache"))
            if mesh.ok:
                extra += mesh3d.describe(mesh)
                extra += mesh3d.compare_with_footprint(mesh, self.fp)
                b = mesh.bbox()
                if b:
                    extra.append(f"Точная высота модели: "
                                 f"{b[5] - b[2]:.2f} мм")
        except Exception as e:
            mesh = None
            extra.append(f"3D-поверхность не построилась: {e}")
        if mesh is not None and mesh.error:
            extra.append(mesh.error)

        # Старый STEP-парсер оставляем запасным путём для установок без
        # trimesh/cascadio и для необычных файлов, которые они не приняли.
        if not self.stale and not (mesh is not None and mesh.ok):
            try:
                from .. import step3d
                model = step3d.parse(self.path)
                extra += model.lines() + step3d.compare_with_footprint(
                    model, self.fp)
            except Exception as e:
                extra.append(f"3D-модель: не разобралась ({e})")
        self.done.emit(self.token, model, mesh, extra)


class BuildWatcher(QThread):
    """
    Ждёт отчёт скрипта. Ждать в главном потоке нельзя -- окно замрёт, а
    сборка большой библиотеки идёт десятки секунд.
    """
    done = Signal(object)

    def __init__(self, job_path: str, timeout: float = 240.0):
        super().__init__()
        self.job_path = job_path
        self.timeout = timeout

    def run(self):
        from ..emit import buildreport
        self.done.emit(buildreport.wait(self.job_path, self.timeout))


class BuildDialog(QDialog):
    """
    Окно сборки: что собирается, куда и чем закончилось.

    Прежнее окно показывало текст «нажмите кнопку GostLib в Altium» даже
    когда скрипт уже был запущен автоматически -- и человек шёл нажимать
    второй раз. Здесь состояние честное: ждём, готово, не сошлось.
    """

    def __init__(self, svc, res: dict, autorun: bool, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.res = res
        self.job = res.get("job", "")
        self.watcher = None
        self.setWindowTitle("Сборка библиотеки")
        self.resize(680, 460)

        where = res.get("target", "библиотека")
        head = QLabel(f"<b>{where}</b>")
        head.setTextFormat(Qt.RichText)

        self.state = QLabel("")
        self.state.setWordWrap(True)
        f = self.state.font()
        # У шрифта, заданного в пикселях, pointSize() возвращает -1, и
        # прибавка к нему даёт 0 -- Qt ругается «Point size <= 0».
        if f.pointSize() > 0:
            f.setPointSize(f.pointSize() + 1)
        elif f.pixelSize() > 0:
            f.setPixelSize(f.pixelSize() + 2)
        self.state.setFont(f)

        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setFont(QFont("Consolas", 9))

        info = [
            f"Символов в задании: {res.get('components', '?')}",
            f"Посадок: {res.get('footprints', '?')}    "
            f"3D-моделей: {res.get('models', '?')}",
            "",
            f"Схемная: {res.get('schlib', '')}",
            f"Посадки: {res.get('pcblib', '')}",
        ]
        # Крупная 3D-модель -- главная причина, по которой сборка идёт
        # минутами: Altium разбирает фасетный STEP дольше, чем создаёт
        # сотни площадок. Предупреждаем ДО запуска, а не после.
        if res.get("heavy3d"):
            info += ["", "Крупные 3D-модели — сборка будет долгой:"]
            for item in res["heavy3d"].split(";")[:5]:
                nm, _, mb = item.rpartition(":")
                info.append(f"  {nm}: {mb} МБ")
            info.append("  «Настройки → Сервис → Пережать 3D-модели» ускорит "
                        "сборку в разы")
        if res.get("problems"):
            info += ["", "Замечания по заданию:"] + [
                "  " + x for x in res["problems"].splitlines()[:15]]
        self.body.setPlainText("\n".join(info))

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setVisible(False)

        self.b_run = QPushButton("Запустить в Altium")
        self.b_run.clicked.connect(self._run)
        self.b_log = QPushButton("Журнал скрипта")
        self.b_log.clicked.connect(self._show_log)
        self.b_dir = QPushButton("Открыть папку")
        self.b_dir.clicked.connect(
            lambda: self.parent()._open(os.path.dirname(res.get("schlib", ""))))
        self.b_close = QPushButton("Закрыть")
        self.b_close.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addWidget(self.b_run)
        row.addWidget(self.b_log)
        row.addWidget(self.b_dir)
        row.addStretch(1)
        row.addWidget(self.b_close)

        lay = QVBoxLayout(self)
        lay.addWidget(head)
        lay.addWidget(self.state)
        lay.addWidget(self.bar)
        lay.addWidget(self.body, 1)
        lay.addLayout(row)

        if autorun:
            self._run()
        else:
            self.state.setText(
                "Задание готово. Нажмите «Запустить в Altium» — или кнопку "
                "GostLib на панели Altium, если запускаете вручную.")

    def _run(self):
        try:
            self.svc.run_in_altium()
        except FileNotFoundError as e:
            self.state.setText(str(e))
            return
        except Exception as e:
            self.state.setText(f"Altium не запустился: {e}")
            return
        self.b_run.setEnabled(False)
        self.bar.setVisible(True)
        self.state.setText("Altium строит библиотеку. Окно можно не "
                           "закрывать — результат появится здесь.")
        self.watcher = BuildWatcher(self.job)
        self.watcher.done.connect(self._ready)
        self.watcher.start()

    def _ready(self, rep):
        self.bar.setVisible(False)
        self.b_run.setEnabled(True)
        if rep is None:
            self.state.setText(
                "Altium не отчитался за отведённое время. Возможно, окно "
                "скрипта ждёт ответа — проверьте Altium.")
            return
        lines = rep.lines()
        self.body.setPlainText("\n".join(lines))
        if rep.ok:
            self.state.setText("Готово: всё, что просили, построилось.")
        else:
            self.state.setText("Собралось НЕ всё — подробности ниже.")
        p = self.parent()
        if p is not None:
            for ln in lines:
                p.log("  " + ln)

    def _show_log(self):
        p = self.parent()
        if p is not None:
            p.show_script_log()


class MainWindow(QMainWindow):
    def __init__(self, cfg: Optional[config.Config] = None):
        super().__init__()
        self.cfg = cfg or config.load()
        self.svc = Service(self.cfg, log=self.log)
        self.current: Optional[Component] = None
        self._worker: Optional[ImportWorker] = None
        self._model_loader: Optional[ModelWorker] = None
        self._model_loaders = set()  # держим старые QThread до finished
        self._queued_model = None    # нативный 3D-бэкенд запускаем по одному
        self._model_token = 0
        self._model_lines: List[str] = []
        self._model_fp = None
        self._pending_model = None
        # параметры, вынесенные в таблицу отдельными колонками
        self.extra_cols: List[str] = list(getattr(self.cfg, "table_params", []))

        # Версия в заголовке: без неё нельзя на глаз отличить свежую сборку
        # от установленной прошлой -- ровно та путаница, из-за которой
        # «установщик поставил старую версию» долго нечем было проверить.
        from .. import __version__ as _ver
        self.setWindowTitle(
            f"GostLib {_ver} — библиотека компонентов для Altium (ЕСКД)")
        # Не открываться шире монитора: на 1920 окно 1500x900 влезает, но
        # если человек унесёт настройки на ноутбук с 1366, Qt начнёт
        # обрезать геометрию сам и ругаться в консоль.
        try:
            from PySide6.QtGui import QGuiApplication
            av = QGuiApplication.primaryScreen().availableGeometry()
            self.resize(min(1500, av.width() - 40), min(900, av.height() - 60))
        except Exception:
            self.resize(1500, 900)
        self._build_ui()
        self._allow_narrow_window()
        self.refresh_projects()
        self.refresh_proj_tree()
        self.refresh_types()
        self.refresh_table()
        self._rebuild_cols_menu()
        self.log("GostLib готов. Рабочая папка: " + self.cfg.root)
        self.log("  Каталог, настройки и резервные копии лежат там, "
                 "отдельно от программы: обновление их не трогает.")
        self._check_font()

    def _check_font(self):
        """Предупредить, если шрифта ГОСТ нет в системе."""
        try:
            from PySide6.QtGui import QFontDatabase
            families = set(QFontDatabase.families())
        except Exception:
            return
        want = self.cfg.font
        if want in families:
            return
        near = [f for f in families if "gost" in f.lower()
                or "isocp" in f.lower()]
        msg = (f"Шрифт «{want}» в системе не найден — превью и Altium "
               f"нарисуют текст другим шрифтом.")
        if near:
            msg += " Похожие установленные: " + ", ".join(sorted(near)[:5])
        self.log(msg)

    # ------------------------------------------------------------- каркас ---
    def _build_ui(self):
        tb = QToolBar("Основное")
        tb.setMovable(False)
        self.addToolBar(tb)

        def act(text, slot, tip="", shortcut=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if tip:
                a.setToolTip(tip)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            tb.addAction(a)
            return a

        act("Импорт архива", self.do_import_archive,
            "ZIP или папка от Ultra Librarian / SnapEDA / KiCad", "Ctrl+O")
        act("Из KiCad", self.do_import_kicad,
            "Взять символ и посадку из установленного KiCad")
        act("По коду LCSC", self.do_import_lcsc,
            "Скачать компонент из EasyEDA по коду вида C2040")
        tb.addSeparator()
        # «Пересобрать» и «Редактировать УГО» живут на панели УГО, а не
        # на верхней панели -- здесь только то, чем пользуются постоянно
        self.act_rebuild = QAction("Пересобрать символ", self)
        self.act_rebuild.setShortcut(QKeySequence("F5"))
        self.act_rebuild.triggered.connect(self.do_rebuild)
        self.addAction(self.act_rebuild)
        self.act_undo = QAction("Отменить", self)
        self.act_undo.setShortcut(QKeySequence.Undo)
        self.act_undo.triggered.connect(self.do_undo)
        self.addAction(self.act_undo)
        self.act_redo = QAction("Вернуть", self)
        self.act_redo.setShortcut(QKeySequence.Redo)
        self.act_redo.triggered.connect(self.do_redo)
        self.addAction(self.act_redo)
        self.act_rename = QAction("Переименовать", self)
        self.act_rename.setShortcut(QKeySequence("F2"))
        self.act_rename.triggered.connect(self.do_rename)
        self.addAction(self.act_rename)
        self.act_ugo = QAction("Редактировать УГО", self)
        self.act_ugo.setShortcut(QKeySequence("F4"))
        self.act_ugo.triggered.connect(self.do_edit_layout)
        self.addAction(self.act_ugo)

        # Выбор проекта. Каталог один на всё, а библиотек столько, сколько
        # проектов: компонент лежит в каталоге один раз и входит в проекты
        # ссылкой, поэтому правка резистора доезжает во все сразу.
        tb.addWidget(QLabel(" Проект: "))
        self.proj_pick = QComboBox()
        self.proj_pick.setMinimumWidth(190)
        self.proj_pick.setToolTip(
            "Библиотека собирается в папку выбранного проекта и только из "
            "его состава. «Общая библиотека» — всё, что помечено «в "
            "библиотеке».")
        self.proj_pick.currentIndexChanged.connect(self._project_changed)
        tb.addWidget(self.proj_pick)
        b_proj = QPushButton("Проекты…")
        b_proj.clicked.connect(self.do_projects)
        tb.addWidget(b_proj)

        # Взять готовое из общего каталога. Без этой кнопки состав проекта
        # можно было пополнить только тем, что уже видно в таблице, -- а
        # видно в ней как раз только то, что в проекте уже есть.
        self.b_pick = QPushButton("Взять из каталога…")
        self.b_pick.setToolTip(
            "Добавить в текущий проект компоненты, которые уже есть в общем "
            "каталоге. Компонент остаётся и в каталоге: проект хранит "
            "ссылку, а не копию.")
        self.b_pick.clicked.connect(self.do_pick_from_catalog)
        tb.addWidget(self.b_pick)

        # «Импорт -> в проект» -- настройка, а не кнопка: её выставляют один
        # раз. Живёт в «Настройки -> Работа».
        tb.addSeparator()

        # Общая библиотека на отдел живёт не в SQLite, а в текстовой папке,
        # которую ведут в git. Кнопки собраны в одно меню: это не то, чем
        # пользуются каждую минуту.
        git_btn = QPushButton("Репозиторий ▾")
        git_btn.setToolTip(
            "Текстовая копия библиотеки для git: её можно коммитить, "
            "смотреть в pull request и синхронизировать между машинами")
        self.git_menu = QMenu(self)
        self.git_menu.addAction("Выложить библиотеку в папку",
                                self.do_git_export)
        self.git_menu.addAction("Что изменилось", self.do_git_status)
        self.git_menu.addAction("Забрать новое из папки",
                                lambda: self.do_git_import(False))
        self.git_menu.addAction("Взять версию из репозитория (перезаписать)",
                                lambda: self.do_git_import(True))
        self.git_menu.addSeparator()
        self.git_menu.addAction("Синхронизировать", self.do_git_sync)
        self.git_menu.addSeparator()
        self.git_menu.addAction("Открыть папку",
                                lambda: self._open(self.svc.git_root()))
        git_btn.setMenu(self.git_menu)
        tb.addWidget(git_btn)
        tb.addSeparator()

        view_btn = QPushButton("Вид ▾")
        self.view_menu = QMenu(self)
        self.act_panel = self.view_menu.addAction("Панель просмотра")
        self.act_panel.setCheckable(True)
        self.act_panel.setChecked(True)
        self.act_panel.setShortcut(QKeySequence("F11"))
        self.act_panel.triggered.connect(self._toggle_panel)
        self.act_tree = self.view_menu.addAction("Дерево категорий")
        self.act_tree.setCheckable(True)
        self.act_tree.setChecked(True)
        self.act_tree.triggered.connect(self._toggle_tree)
        self.view_menu.addSeparator()
        self.cols_menu = self.view_menu.addMenu("Колонки таблицы")
        self.view_menu.addSeparator()
        self.view_menu.addAction("Светлая тема", lambda: self._set_theme("light"))
        self.view_menu.addAction("Тёмная тема", lambda: self._set_theme("dark"))
        view_btn.setMenu(self.view_menu)
        tb.addWidget(view_btn)
        tb.addSeparator()
        # Две главные кнопки выделены цветом: в остальном интерфейсе
        # ничего цветного нет, и глаз находит их сразу -- как «Собрать» и
        # «Собрать и прошить» в среде разработки.
        # Две главные кнопки -- обычными виджетами, а не действиями: только
        # так им можно задать цвет. В остальном интерфейсе цветного нет,
        # поэтому глаз находит их сразу.
        self.b_build = QPushButton("Собрать задание")
        self.b_build.setToolTip(
            "Подготовить задание. Собирается РОВНО то, что написано справа "
            "в строке состояния: состав текущего проекта либо общая "
            "библиотека. Выделение в таблице ни на что не влияет.\n"
            "Altium построит библиотеки своим API по кнопке GostLib.  F9")
        self.b_build.clicked.connect(lambda: self.do_convert("script"))
        # F9 -- действием окна, а не кнопки: у спрятанной кнопки клавиша
        # не срабатывает, а спрятана она по умолчанию.
        self.act_job = QAction("Собрать задание", self)
        self.act_job.setShortcut(QKeySequence("F9"))
        self.act_job.triggered.connect(lambda: self.do_convert("script"))
        self.addAction(self.act_job)
        self.b_build.setStyleSheet(
            "QPushButton{background:#3a4a63;border:1px solid #4d6180;"
            "padding:6px 14px;font-weight:bold;}"
            "QPushButton:hover{background:#46587a;}")
        self._act_build = tb.addWidget(self.b_build)
        self._act_build.setVisible(
            bool(getattr(self.cfg, "show_job_button", False)))

        self.b_run = QPushButton("▶  Собрать и запустить")
        self.b_run.setToolTip(
            "Собрать и сразу выполнить скрипт в Altium — без ручного "
            "Run Script. В библиотеку уйдёт РОВНО состав текущего проекта "
            "(или общая библиотека, если проект не выбран).\n"
            "Если Altium открыт, команда уйдёт в него.  Shift+F9")
        self.b_run.setShortcut(QKeySequence("Shift+F9"))
        self.b_run.clicked.connect(self.do_build_and_run)
        self.b_run.setStyleSheet(
            "QPushButton{background:#2e7d46;color:#f2fff5;"
            "border:1px solid #3c9c58;padding:6px 16px;font-weight:bold;}"
            "QPushButton:hover{background:#379a54;}")
        tb.addWidget(self.b_run)

        # Запасные пути и служебные действия -- в «Настройки -> Сервис»:
        # ими пользуются раз в месяц, а место на панели они занимали всегда.
        tb.addSeparator()
        act("Удалить", self.do_delete, "Удалить выбранное из каталога", "Del")
        act("Настройки", self.do_settings)

        # ---- дерево категорий
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Категория", "шт"])
        self.tree.setColumnWidth(0, 210)
        self.tree.itemSelectionChanged.connect(self.refresh_table)
        self.cb_only_proj = QCheckBox("показывать только состав проекта")
        self.cb_only_proj.setChecked(True)
        self.cb_only_proj.setToolTip(
            "Когда выбран проект, таблица показывает только его компоненты. "
            "Снимите, чтобы искать по всему каталогу и добавлять в проект.")
        self.cb_only_proj.stateChanged.connect(self.refresh_table)
        self.only_lib = QCheckBox("только то, что уже в библиотеке")
        self.only_lib.stateChanged.connect(self.refresh_table)
        # ---- состав текущего проекта: категории и имена
        self.proj_tree = QTreeWidget()
        self.proj_tree.setHeaderLabels(["Состав проекта", "шт"])
        self.proj_tree.setColumnWidth(0, 200)
        self.proj_tree.setRootIsDecorated(True)
        self.proj_tree.itemDoubleClicked.connect(self._proj_tree_open)
        self.proj_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.proj_tree.customContextMenuRequested.connect(self._proj_tree_menu)
        self.proj_size = QLabel("")
        self.proj_size.setStyleSheet("color:#8aa;")
        self.proj_size.setWordWrap(True)
        b_size = QPushButton("Место на диске…")
        b_size.setToolTip("Сколько занимают общая библиотека и библиотеки "
                          "проектов")
        b_size.clicked.connect(self.do_disk_usage)

        # Порядок работы -- короткой подсказкой, а не в документации.
        # Утилиту берут в руки раз в месяц, и держать последовательность в
        # голове никто не обязан.
        self.steps_box = QWidget()
        sv = QVBoxLayout(self.steps_box)
        sv.setContentsMargins(6, 4, 6, 6)
        cap = QLabel("Порядок работы")
        cf = cap.font()
        cf.setBold(True)
        cap.setFont(cf)
        sv.addWidget(cap)
        self.step_labels = []
        for i, (txt, tip) in enumerate((
                ("1. Импортировать компоненты",
                 "Из KiCad, по коду LCSC или из архива"),
                ("2. Проверить и поправить",
                 "Тип, параметры, УГО, посадку и 3D — на вкладках справа"),
                ("3. Выбрать проект",
                 "Или оставить «Общая библиотека»"),
                ("4. Собрать и запустить",
                 "Shift+F9 — Altium построит библиотеки сам"))):
            lb = QLabel(txt)
            lb.setToolTip(tip)
            lb.setWordWrap(True)
            # перенос по словам сам по себе минимальную ширину не снимает
            lb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lb.setMinimumWidth(60)
            sv.addWidget(lb)
            self.step_labels.append(lb)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(4, 4, 4, 4)
        split_left = QSplitter(Qt.Vertical)
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.addWidget(self.tree, 1)
        tv.addWidget(self.cb_only_proj)
        tv.addWidget(self.only_lib)
        bot = QWidget()
        bv = QVBoxLayout(bot)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.addWidget(self.proj_tree, 1)
        bv.addWidget(self.proj_size)
        bv.addWidget(b_size)
        split_left.addWidget(top)
        split_left.addWidget(bot)
        split_left.setSizes([420, 380])
        lv.addWidget(split_left, 1)
        lv.addWidget(self.steps_box)
        dock = QDockWidget("Каталог и проект", self)
        dock.setWidget(left)
        dock.setFeatures(QDockWidget.NoDockWidgetFeatures)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        # ---- таблица
        self.search = QLineEdit()
        self.search.setPlaceholderText("поиск: имя, парт-номер, описание, корпус…")
        self.search.textChanged.connect(self.refresh_table)
        self.pfilter_key = QComboBox()
        self.pfilter_key.setEditable(True)
        self.pfilter_key.setMinimumWidth(140)
        self.pfilter_op = QComboBox()
        self.pfilter_op.addItems(["~", "=", ">", ">=", "<", "<="])
        self.pfilter_val = QLineEdit()
        self.pfilter_val.setPlaceholderText("значение параметра")
        self.pfilter_val.setMaximumWidth(180)
        self.pfilter_val.returnPressed.connect(self.refresh_table)
        pf_btn = QPushButton("Фильтр")
        pf_btn.clicked.connect(self.refresh_table)
        pf_clr = QPushButton("×")
        pf_clr.setMaximumWidth(30)
        pf_clr.clicked.connect(self._clear_filter)

        top = QHBoxLayout()
        top.addWidget(self.search, 3)
        top.addWidget(QLabel("параметр:"))
        top.addWidget(self.pfilter_key)
        top.addWidget(self.pfilter_op)
        top.addWidget(self.pfilter_val)
        top.addWidget(pf_btn)
        top.addWidget(pf_clr)

        self.model = QStandardItemModel(0, len(COLS))
        self._filling = False
        self.model.itemChanged.connect(self._table_edited)
        self.model.setHorizontalHeaderLabels(COLS)
        self.tree_dock = dock
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # Правка прямо в таблице -- по двойному щелчку. Так парт-номер и
        # производитель заполняются пачкой, не открывая карточку каждого.
        # Правятся только «безопасные» колонки (см. EDITABLE_COLS): имя,
        # обозначение, парт-номер, производитель и вынесенные параметры.
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked
                                   | QAbstractItemView.EditKeyPressed)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnHidden(COL_UID, True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        # колонки таскаются мышью: удобно поставить LCSC сразу после имени
        hh.setSectionsMovable(True)
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._header_menu)
        self.table.selectionModel().selectionChanged.connect(self.on_select)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._ctx_menu)

        center = QWidget()
        cv = QVBoxLayout(center)
        cv.setContentsMargins(4, 4, 4, 4)
        cv.addLayout(top)
        cv.addWidget(self.table, 1)

        # ---- правая панель
        self.sym_view = PreviewPane("символ", bg="#ffffff")
        self.fp_view = PreviewPane("посадочное место", bg="#101018")

        # ---- карточка посадочного места: габариты и 3D
        self.fp_pick = QComboBox()
        self.fp_pick.currentIndexChanged.connect(self._show_footprint)
        self.model_view = Model3DPane()
        self.model_view.transform_changed.connect(self._model_moved)
        self.model_view.color_changed.connect(self._model_color_changed)
        self.fp_info = QPlainTextEdit()
        self.fp_info.setReadOnly(True)
        self.fp_info.setMaximumHeight(210)
        self.fp_info.setFont(QFont("Consolas", 9))

        b3d = QPushButton("Подгрузить / заменить 3D…")
        b3d.setToolTip("STEP кладётся как есть; OBJ/STL конвертируются в STEP")
        b3d.clicked.connect(self.do_set_model)
        self.b3d_preview = QPushButton("Показать тяжёлую 3D")
        self.b3d_preview.setToolTip(
            "Разобрать большую STEP-модель вручную. Во время разбора "
            "интерфейс может работать медленнее.")
        self.b3d_preview.clicked.connect(self._load_pending_model)
        self.b3d_preview.setEnabled(False)
        b3dx = QPushButton("Убрать 3D")
        b3dx.clicked.connect(self.do_clear_model)
        bmod = QPushButton("Папка моделей")
        bmod.clicked.connect(lambda: self._open(self.cfg.models_dir))
        self.height_edit = QLineEdit()
        self.height_edit.setMaximumWidth(70)
        self.height_edit.setPlaceholderText("мм")
        bh = QPushButton("Задать высоту")
        bh.clicked.connect(self.do_set_height)
        self.tape_edit = QLineEdit()
        self.tape_edit.setMaximumWidth(60)
        self.tape_edit.setPlaceholderText("°")
        self.tape_edit.setToolTip(
            "Угол корпуса в ленте поставщика относительно посадочного места. "
            "Уходит в параметр «УголВЛенте» — чтобы в файле сборки вся партия "
            "не встала повёрнутой.")
        bt = QPushButton("Угол в ленте")
        bt.clicked.connect(self.do_set_tape)

        # Дозагрузка посадки. Часто в источнике приезжает только символ
        # (и иногда 3D), а посадки нет или подобралась не та. Раньше
        # лечилось повторным импортом -- с потерей всей ручной работы
        # над УГО; теперь посадка добирается отдельно.
        b_addfp = QPushButton("Дозагрузить посадку…")
        b_addfp.setToolTip(
            "Взять посадочное место из KiCad и добавить его этому "
            "компоненту.\nСимвол, раскладка выводов и параметры остаются "
            "как есть.")
        b_addfp.clicked.connect(lambda: self.do_attach_footprint(False))
        b_repfp = QPushButton("Заменить посадку…")
        b_repfp.setToolTip(
            "То же самое, но вместо текущей посадки.\nЗазоры, паста, "
            "высота и 3D-модель переносятся на новую.")
        b_repfp.clicked.connect(lambda: self.do_attach_footprint(True))

        fprow1 = QHBoxLayout()
        fprow1.addWidget(QLabel("Посадка:"))
        fprow1.addWidget(self.fp_pick, 1)
        fprow1.addWidget(b_addfp)
        fprow1.addWidget(b_repfp)
        fprow2 = QHBoxLayout()
        fprow2.addWidget(b3d)
        fprow2.addWidget(self.b3d_preview)
        fprow2.addWidget(b3dx)
        fprow2.addWidget(bmod)
        fprow2.addStretch(1)
        fprow2.addWidget(QLabel("Высота:"))
        fprow2.addWidget(self.height_edit)
        fprow2.addWidget(bh)
        fprow2.addWidget(self.tape_edit)
        fprow2.addWidget(bt)

        # Зазоры маски и пасты -- свойство ЭТОЙ посадки, а не библиотеки:
        # у BGA один зазор, у QFN с тепловым пятном другой. Пусто —
        # не задавать, Altium возьмёт правило проекта.
        fprow3 = QHBoxLayout()
        self.mask_edit = QLineEdit()
        self.mask_edit.setPlaceholderText("правило проекта")
        self.mask_edit.setFixedWidth(120)
        self.mask_edit.setToolTip(
            "Зазор паяльной маски на сторону, мм, для всех площадок этой "
            "посадки.\n"
            "Пусто — не задавать: Altium применит правило проекта.\n\n"
            "Задаётся через кеш площадки — тот же путь, что у Altium в "
            "свойствах Solder/Paste. В свойствах площадки после сборки "
            "будет «Manual» вместо «Rule Expansion».")
        self.paste_edit = QLineEdit()
        self.paste_edit.setPlaceholderText("правило проекта")
        self.paste_edit.setFixedWidth(120)
        self.paste_edit.setToolTip(
            "Зазор трафарета пасты на сторону, мм. Обычно отрицательный: "
            "окно меньше площадки.\n"
            "Пусто — правило проекта.")
        b_exp = QPushButton("Применить зазоры")
        b_exp.clicked.connect(self.do_set_expansion)
        # Паста -- свойство компонента целиком: трафарет режут на деталь,
        # а не на отдельную посадку. Разъём под запрессовку, тестовый
        # пятак, экран под ручную пайку -- пасты быть не должно вовсе.
        self.cb_paste = QCheckBox("наносить пасту")
        self.cb_paste.setChecked(True)
        self.cb_paste.setToolTip(
            "Снимите — и в трафарете не будет окон под этот компонент.\n"
            "В Altium это видно как большой отрицательный «Manual "
            "Expansion»\nу площадок: отдельного флага в скриптовом API нет, "
            "а такой\nзазор закрывает окно наверняка.")
        self.cb_paste.toggled.connect(self.do_toggle_paste)
        fprow3.addWidget(QLabel("Зазор маски, мм:"))
        fprow3.addWidget(self.mask_edit)
        fprow3.addWidget(QLabel("пасты, мм:"))
        fprow3.addWidget(self.paste_edit)
        fprow3.addWidget(b_exp)
        fprow3.addWidget(self.cb_paste)
        fprow3.addStretch(1)

        # Сетка окон пасты у крупных площадок. Сплошное окно на тепловом
        # пятаке QFN -- классическая причина всплывшей детали и коротышей
        # на соседних выводах; в производстве такое окно всегда разбивают.
        fprow4 = QHBoxLayout()
        self.paste_grid = QComboBox()
        self.paste_grid.addItem("сплошное окно", 0)
        for n in (2, 3, 4, 5):
            self.paste_grid.addItem(f"{n}×{n}", n)
        self.paste_grid.setToolTip(
            "Окно пасты у крупных площадок — сеткой квадратиков вместо\n"
            "сплошного. Мелких площадок не касается.")
        self.paste_over = QLineEdit("1")
        self.paste_over.setFixedWidth(60)
        self.paste_over.setToolTip("Сетка ставится площадкам крупнее этого, мм")
        self.paste_fill = QLineEdit("60")
        self.paste_fill.setFixedWidth(60)
        self.paste_fill.setToolTip("Сколько процентов площадки накрыть пастой")
        b_grid = QPushButton("Применить сетку")
        b_grid.clicked.connect(self.do_set_paste_grid)
        fprow4.addWidget(QLabel("Окно пасты:"))
        fprow4.addWidget(self.paste_grid)
        fprow4.addWidget(QLabel("у площадок от, мм:"))
        fprow4.addWidget(self.paste_over)
        fprow4.addWidget(QLabel("покрытие, %:"))
        fprow4.addWidget(self.paste_fill)
        fprow4.addWidget(b_grid)
        fprow4.addStretch(1)

        views = QSplitter(Qt.Horizontal)
        views.addWidget(self.fp_view)
        views.addWidget(self.model_view)
        views.setStretchFactor(0, 1)
        views.setStretchFactor(1, 1)

        fpw = QWidget()
        fpv = QVBoxLayout(fpw)
        fpv.setContentsMargins(4, 4, 4, 4)
        fpv.addLayout(fprow1)
        fpv.addWidget(views, 1)
        fpv.addWidget(self.fp_info)
        fpv.addLayout(fprow2)
        fpv.addLayout(fprow3)
        fpv.addLayout(fprow4)

        self.params = QTableWidget(0, 5)
        self.params.setHorizontalHeaderLabels(
            ["Параметр", "Значение", "Ед.", "Тип", "На схеме"])
        self.params.horizontalHeader().setStretchLastSection(False)
        self.params.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.params.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        pbtn = QPushButton("Сохранить параметры и пересобрать символ")
        pbtn.clicked.connect(self.save_params)
        # Свои параметры: у каждого компонента бывает что-то своё --
        # «ТокНагрузки», «ДатаЗакупки», ссылка на карточку у поставщика.
        b_padd = QPushButton("+ Свой параметр")
        b_padd.setToolTip("Добавить параметр с собственным именем, "
                          "единицей и типом")
        b_padd.clicked.connect(self.do_add_param)
        b_pdel = QPushButton("− Удалить строку")
        b_pdel.setToolTip("Убрать выделенный параметр")
        b_pdel.clicked.connect(self.do_del_param)
        self.type_box = QComboBox()
        for code, ru, pref in classify.CTYPES:
            self.type_box.addItem(f"{ru}  ({pref})", code)
        self.desig_edit = QLineEdit()
        self.desig_edit.setMaximumWidth(80)
        # обозначение едет за типом, пока пользователь не правил его руками
        self._desig_manual = False
        self.desig_edit.textEdited.connect(
            lambda *_: setattr(self, "_desig_manual", True))
        self.type_box.currentIndexChanged.connect(self._type_box_changed)
        prow = QHBoxLayout()
        prow.addWidget(QLabel("Тип:"))
        prow.addWidget(self.type_box, 1)
        prow.addWidget(QLabel("Обозн.:"))
        prow.addWidget(self.desig_edit)
        pw = QWidget()
        pv = QVBoxLayout(pw)
        pv.setContentsMargins(4, 4, 4, 4)
        pv.addLayout(prow)
        pv.addWidget(self.params, 1)
        prow2 = QHBoxLayout()
        prow2.addWidget(b_padd)
        prow2.addWidget(b_pdel)
        prow2.addStretch(1)
        prow2.addWidget(pbtn)
        pv.addLayout(prow2)

        self.pins = QTableWidget(0, 6)
        self.pins.setHorizontalHeaderLabels(
            ["Контакт", "Имя", "Тип", "Секция", "Сторона", "Группа"])
        self.pins.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        # Строки таскаются мышью: у разъёмов порядок выводов на схеме не
        # совпадает ни с номерами, ни с именами, и задать его можно только
        # руками. Порядок строк = порядок сверху вниз.
        self.pins.setSelectionBehavior(QAbstractItemView.SelectRows)
        # Строки таскаются за левый столбец. InternalMove здесь не годится:
        # он переносит только ячейки-элементы, а выпадающие списки типа и
        # стороны остаются на месте. Двигаем секции заголовка, а при
        # сохранении читаем ВИДИМЫЙ порядок -- иначе перетаскивание
        # выглядит рабочим, но в базу уходит прежний порядок.
        self.pins.verticalHeader().setSectionsMovable(True)
        self.pins.verticalHeader().setToolTip(
            "Тяните строки за этот столбец, чтобы задать порядок выводов")
        pinup = QPushButton("▲")
        pinup.setFixedWidth(34)
        pinup.setToolTip("Поднять выделенные выводы (Alt+↑)")
        pinup.clicked.connect(lambda: self._move_pin_rows(-1))
        pindn = QPushButton("▼")
        pindn.setFixedWidth(34)
        pindn.setToolTip("Опустить выделенные выводы (Alt+↓)")
        pindn.clicked.connect(lambda: self._move_pin_rows(1))
        pinsort = QPushButton("По номерам")
        pinsort.setToolTip("Вернуть порядок по номерам контактов")
        pinsort.clicked.connect(self._sort_pin_rows)
        pinai = QPushButton("ИИ-раскладка…")
        pinai.setToolTip(
            "Скопировать все выводы в текст для ИИ и принять готовую "
            "раскладку обратно")
        pinai.clicked.connect(self.ai_pin_plan)
        pintidy = QPushButton("Привести в порядок")
        pintidy.setToolTip(
            "Разнести питание и землю по своим секциям, выровнять стороны\n"
            "и разбить слишком длинные столбики. Работает и без ИИ — по\n"
            "именам выводов.")
        pintidy.clicked.connect(self.tidy_pins)
        pinbtn = QPushButton("Применить раскладку выводов")
        pinbtn.clicked.connect(self.save_pins)
        pinreset = QPushButton("Сбросить на автоматическую")
        pinreset.clicked.connect(self.reset_pins)
        hb = QHBoxLayout()
        hb.addWidget(pinup)
        hb.addWidget(pindn)
        hb.addWidget(pinsort)
        hb.addWidget(pinai)
        hb.addWidget(pintidy)
        hb.addWidget(pinbtn, 1)
        hb.addWidget(pinreset)
        pinw = QWidget()
        pinv = QVBoxLayout(pinw)
        pinv.setContentsMargins(4, 4, 4, 4)
        pinv.addWidget(self.pins, 1)
        pinv.addLayout(hb)

        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setFont(QFont("Consolas", 9))

        symw = QWidget()
        symv = QVBoxLayout(symw)
        symv.setContentsMargins(4, 4, 4, 4)
        symrow = QHBoxLayout()
        b_ugo = QPushButton("Редактировать УГО (F4)")
        b_ugo.clicked.connect(self.do_edit_layout)
        b_reb = QPushButton("Пересобрать по ГОСТ (F5)")
        b_reb.setToolTip("Вернуть автоматическую отрисовку по текущим "
                         "настройкам стиля")
        b_reb.clicked.connect(self.do_rebuild)
        symrow.addWidget(b_ugo)
        symrow.addWidget(b_reb)
        symrow.addSpacing(12)

        # Личные настройки ЭТОГО компонента. Общие трогать нельзя: одна
        # мелкая пассивка не должна ужимать всю библиотеку.
        self.cb_nums = QCheckBox("номера выводов")
        self.cb_nums.setTristate(False)
        self.cb_nums.setToolTip(
            "Показывать номера контактов у этого компонента. "
            "Общая настройка при этом не меняется.")
        self.cb_nums.clicked.connect(self._toggle_comp_numbers)
        symrow.addWidget(self.cb_nums)

        self.cb_compact = QCheckBox("компактно по сетке")
        self.cb_compact.setToolTip(
            "Подогнать автоматическое УГО под ширину текста в Altium и "
            "сделать одинаковые боковые поля минимально необходимой "
            "ширины. Настройка только этого компонента.")
        self.cb_compact.clicked.connect(self._toggle_compact)
        symrow.addWidget(self.cb_compact)

        symrow.addWidget(QLabel("масштаб УГО:"))
        self.sp_scale = QSpinBox()
        self.sp_scale.setRange(20, 300)
        self.sp_scale.setSingleStep(5)
        self.sp_scale.setSuffix(" %")
        self.sp_scale.setToolTip(
            "Размер обозначения у этого компонента. 100 % — как в общих "
            "настройках.")
        self.sp_scale.setKeyboardTracking(False)
        self.sp_scale.valueChanged.connect(self._set_comp_scale)
        symrow.addWidget(self.sp_scale)

        symrow.addWidget(QLabel("обозначение:"))
        self.cb_src = QComboBox()
        self.cb_src.addItem("рисовать по ГОСТ", "gost")
        self.cb_src.addItem("как в источнике (KiCad, EasyEDA)", "native")
        self.cb_src.setToolTip(
            "Пассивка и дискретная мелочь в KiCad и EasyEDA уже нарисованы "
            "по делу — перерисовывать их незачем. Микросхемы там безликие "
            "коробки, их рисуем сами.\n"
            "Родное обозначение бывает единственно верным: у ESD5471X это "
            "двунаправленный супрессор, а по списку выводов он неотличим от "
            "обычного диода.\n"
            "Если тип определился неверно, переключите здесь.")
        self.cb_src.currentIndexChanged.connect(self._set_symbol_source)
        symrow.addWidget(self.cb_src)


        # Секция для превью. Держим переключатель ОТДЕЛЬНОЙ строкой: после
        # добавления личных настроек первая строка стала шире панели и этот
        # комбобокс фактически уезжал за правый край. Из-за этого секции
        # можно было посмотреть только через «Редактировать УГО».
        self.prev_part_bar = QWidget()
        partrow = QHBoxLayout(self.prev_part_bar)
        partrow.setContentsMargins(0, 0, 0, 0)
        partrow.setSpacing(6)
        self.lb_prev_part = QLabel("Просмотр части УГО:")
        self.cb_prev_part = QComboBox()
        self.cb_prev_part.setMinimumWidth(130)
        self.cb_prev_part.setToolTip(
            "Какую секцию показывать в обычном превью — открывать "
            "редактор УГО не требуется")
        self.cb_prev_part.currentIndexChanged.connect(self._redraw_symbol)
        partrow.addWidget(self.lb_prev_part)
        partrow.addWidget(self.cb_prev_part)
        partrow.addStretch(1)
        self.prev_part_bar.hide()

        symrow.addStretch(1)
        symv.addLayout(symrow)
        symv.addWidget(self.prev_part_bar)
        symv.addWidget(self.sym_view, 1)

        # Правила группировки выводов. Живут рядом с параметрами: это
        # такое же свойство компонента, только текстом.
        rulesw = QWidget()
        rv = QVBoxLayout(rulesw)
        rv.setContentsMargins(4, 4, 4, 4)
        rrow = QHBoxLayout()
        self.cb_rules_own = QCheckBox("свои правила у этого компонента")
        self.cb_rules_own.setToolTip(
            "Снято — берутся общие правила. Поставьте, если у этой "
            "микросхемы своя логика группировки выводов.")
        self.cb_rules_own.clicked.connect(self._toggle_own_rules)
        rrow.addWidget(self.cb_rules_own)
        b_rules_save = QPushButton("Применить и пересобрать")
        b_rules_save.clicked.connect(self._save_rules)
        rrow.addWidget(b_rules_save)
        b_rules_def = QPushButton("Вернуть встроенные")
        b_rules_def.clicked.connect(self._default_rules)
        rrow.addWidget(b_rules_def)
        rrow.addStretch(1)
        self.rules_note = QLabel("")
        self.rules_note.setStyleSheet("color:#888;")
        rrow.addWidget(self.rules_note)
        rv.addLayout(rrow)
        self.rules_edit = QPlainTextEdit()
        self.rules_edit.setFont(QFont("Consolas", 10))
        self.rules_edit.setPlaceholderText(
            "имя группы | сторона | шаблоны через запятую  [+пара] [+низ]\n"
            "Питание | L | VDD*, VCC*, AVDD\n"
            "USB     | R | USB_D*, D+, D-   +пара\n"
            "Порт A  | A | PA[0-9]*")
        rsplit = QSplitter(Qt.Vertical)
        rsplit.addWidget(self.rules_edit)
        # Проверка правил на выбранном компоненте -- до применения: видно,
        # какой вывод в какую группу попал и что осталось автоматике.
        # Раньше это выяснялось только пересборкой и разглядыванием УГО.
        chk = QWidget()
        cv = QVBoxLayout(chk)
        cv.setContentsMargins(0, 0, 0, 0)
        crow = QHBoxLayout()
        b_check = QPushButton("Проверить на этом компоненте")
        b_check.setToolTip("Разложить выводы выбранного компонента по "
                           "тексту выше, ничего не сохраняя")
        b_check.clicked.connect(self._check_rules)
        crow.addWidget(b_check)
        self.rules_stat = QLabel("")
        self.rules_stat.setStyleSheet("color:#8a93a6;")
        crow.addWidget(self.rules_stat, 1)
        cv.addLayout(crow)
        self.rules_view = QPlainTextEdit()
        self.rules_view.setReadOnly(True)
        self.rules_view.setFont(QFont("Consolas", 9))
        cv.addWidget(self.rules_view, 1)
        rsplit.addWidget(chk)
        rsplit.setStretchFactor(0, 3)
        rsplit.setStretchFactor(1, 2)
        rv.addWidget(rsplit, 1)

        self.tabs = QTabWidget()
        self.tabs.addTab(symw, "УГО")
        self.tabs.addTab(fpw, "Посадка и 3D")
        self.tabs.addTab(pw, "Параметры")
        self.tabs.addTab(pinw, "Выводы")
        self.tabs.addTab(rulesw, "Правила выводов")
        self.tabs.addTab(self.logbox, "Журнал")

        split = QSplitter(Qt.Horizontal)
        split.addWidget(center)
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 4)
        self.setCentralWidget(split)
        self.setStatusBar(QStatusBar())
        # Куда пойдёт сборка -- всегда на виду, справа в строке состояния.
        # Иначе легко собрать в общую библиотеку, думая, что собираешь в
        # проект.
        self.lbl_target = QLabel("")
        self.lbl_target.setStyleSheet("color:#8aa;")
        # В подписи полный путь к библиотеке, и она тянула минимальную
        # ширину окна до 1970 px -- на мониторе 1920 окно переставало
        # помещаться, Qt ругался и обрезал геометрию. Пусть надпись
        # сжимается: полный путь всё равно есть в подсказке.
        self.lbl_target.setSizePolicy(QSizePolicy.Ignored,
                                      QSizePolicy.Preferred)
        self.lbl_target.setMinimumWidth(80)
        self.statusBar().addPermanentWidget(self.lbl_target)

    # ------------------------------------------------------------ данные ----
    def log(self, s: str):
        """Вызывается только из потока GUI (из фонового -- через сигнал)."""
        try:
            self.logbox.appendPlainText(str(s))
            self.statusBar().showMessage(str(s), 6000)
        except Exception:
            print(s)

    def refresh_types(self):
        cur = self.selected_type()
        self.tree.clear()
        stats = self.svc.db.stats()
        root = QTreeWidgetItem(["Все компоненты", str(stats["total"])])
        root.setData(0, Qt.UserRole, "")
        self.tree.addTopLevelItem(root)
        for code, n in self.svc.db.types():
            it = QTreeWidgetItem([classify.CTYPE_NAME.get(code, code), str(n)])
            it.setData(0, Qt.UserRole, code)
            root.addChild(it)
            if code == cur:
                self.tree.setCurrentItem(it)
        root.setExpanded(True)
        if not cur:
            self.tree.setCurrentItem(root)
        keys = self.svc.db.param_keys()
        cur_key = self.pfilter_key.currentText()
        self.pfilter_key.clear()
        self.pfilter_key.addItems(keys)
        self.pfilter_key.setCurrentText(cur_key)

    def selected_type(self) -> str:
        it = self.tree.currentItem()
        return it.data(0, Qt.UserRole) if it else ""

    def _clear_filter(self):
        self.pfilter_val.clear()
        self.refresh_table()

    def _table_edited(self, item):
        """
        Правка ячейки. Пишем в компонент и, если надо, пересобираем УГО.

        Важно: значения ложатся ПАРАМЕТРАМИ компонента, а не текстом на
        обозначении. На листе схемы они скрыты -- видно только Comment,
        который и так двигается мышью.
        """
        if getattr(self, "_filling", False):
            return
        row = item.row()
        uid_item = self.model.item(row, COL_UID)
        if uid_item is None:
            return
        uid = uid_item.text()
        c = self.svc.db.get(uid)
        if not c:
            return
        headers = [self.model.horizontalHeaderItem(i).text()
                   for i in range(self.model.columnCount())]
        col = headers[item.column()]
        val = item.text().strip()
        try:
            with self.svc.undo.step(f"Правка: {col}"):
                self.svc.undo.touch(uid)
                if col == "Имя":
                    self.svc.rename(uid, val)
                    self.refresh_proj_tree()
                    return
                if col == "Обозн.":
                    c.designator = val or c.designator
                elif col == "Парт-номер":
                    c.mpn = val
                    c.params["MPN"] = val
                elif col == "Производитель":
                    c.manufacturer = val
                    c.params["Manufacturer"] = val
                else:
                    # вынесенный в колонку параметр
                    if val:
                        c.params[col] = val
                    else:
                        c.params.pop(col, None)
                # подпись на схеме могла зависеть от парт-номера
                self.svc.rebuild_symbol(c)
                self.svc.db.upsert(c)
        except ValueError as e:
            QMessageBox.warning(self, "GostLib", str(e))
            self.refresh_table()
            return
        self.log(f"{c.name}: {col} = {val or '(пусто)'}  (Ctrl+Z вернёт)")
        if self.current and self.current.uid == uid:
            self.on_select()

    def refresh_table(self):
        pf = []
        key = self.pfilter_key.currentText().strip()
        val = self.pfilter_val.text().strip()
        if key and val:
            pf.append((key, self.pfilter_op.currentText(), val))
        rows = self.svc.db.search(self.search.text().strip(),
                                  self.selected_type(),
                                  self.only_lib.isChecked(), pf)
        # Когда выбран проект, таблица показывает его состав: иначе
        # приходится держать в голове, что из тысячи строк относится к
        # текущей плате. «Общая библиотека» -- показываем всё.
        if self.cb_only_proj.isChecked():
            pr = self.svc.active_project()
            if pr is not None:
                keep = set(self.svc.db.project_uids(int(pr["id"])))
                rows = [r for r in rows if r["uid"] in keep]
        headers = COLS[:-1] + list(self.extra_cols) + [COLS[-1]]
        uid_col = len(headers) - 1
        self.model.removeRows(0, self.model.rowCount())
        if self.model.columnCount() != len(headers):
            self.model.setColumnCount(len(headers))
        self.model.setHorizontalHeaderLabels(headers)
        globals()["COL_UID"] = uid_col
        self._filling = True
        for r in rows:
            items = [
                QStandardItem(r["name"] or ""),
                QStandardItem(classify.CTYPE_NAME.get(r["ctype"], r["ctype"])),
                QStandardItem(r["designator"] or ""),
                QStandardItem(r["mpn"] or ""),
                QStandardItem(r["manufacturer"] or ""),
                QStandardItem(r["package"] or ""),
                QStandardItem(str(r["pincount"] or 0)),
                QStandardItem(r["footprints"] or ""),
                QStandardItem(r["source"] or ""),
                QStandardItem("да" if r["in_library"] else ""),
            ]
            for key in self.extra_cols:
                val = ""
                c = self.svc.db.get(r["uid"])
                if c:
                    val = c.params.get(key, "")
                items.append(QStandardItem(str(val)))
            for i, it in enumerate(items):
                it.setEditable(COLS[i] in EDITABLE_COLS
                               if i < len(COLS) - 1 else False)
            for i in range(len(COLS) - 1, len(items)):
                items[i].setEditable(True)     # вынесенные параметры
            items.append(QStandardItem(r["uid"]))
            if r["in_library"]:
                items[9].setForeground(QColor("#6fbf73"))
            if not (r["footprints"] or ""):
                items[7].setForeground(QColor("#c9884a"))
                items[7].setText("нет")
            self.model.appendRow(items)
        self._filling = False
        self.table.setColumnHidden(uid_col, True)
        for i in range(uid_col):
            self.table.resizeColumnToContents(i)
        pr = self.svc.active_project()
        scope = ("весь каталог" if pr is None or not self.cb_only_proj.isChecked()
                 else f"проект «{pr['name']}»")
        self.statusBar().showMessage(f"Показано: {len(rows)}  ({scope})")

    def selected_uids(self) -> List[str]:
        out = []
        col = self.model.columnCount() - 1
        for idx in self.table.selectionModel().selectedRows():
            src = self.proxy.mapToSource(idx)
            it = self.model.item(src.row(), col)
            if it is not None:
                out.append(it.text())
        return out

    def on_select(self):
        uids = self.selected_uids()
        if len(uids) != 1:
            return
        c = self.svc.db.get(uids[0])
        self.current = c
        if not c:
            return
        self._fill_prev_parts(c)
        self._redraw_symbol()
        self._fill_footprints(c)
        self._fill_params(c)
        self._fill_pins(c)
        self._fill_own_style(c)
        self._fill_rules(c)

    def _allow_narrow_window(self):
        """
        Дать окну сжиматься до нормального монитора.

        Суммарная минимальная ширина панелей доходила до 1970 px: на
        мониторе 1920 окно уже не помещалось, Qt писал «Unable to set
        geometry» и обрезал его сам. Минимум окна складывается из
        минимумов вложенных раскладок, поэтому снимаем это ограничение у
        крупных панелей -- пусть содержимое ужимается, а не диктует
        размер окна.
        """
        from PySide6.QtWidgets import QLayout, QSplitter, QTabWidget

        def relax(w, mw=140, mh=100):
            """
            Разрешить панели быть узкой.

            Ключ тут -- политика Ignored: при ней Qt перестаёт считать
            минимумом подсказку раскладки и берёт явно заданный минимум.
            Одного setMinimumWidth мало -- подсказка всё равно побеждает.
            """
            if w is None:
                return
            lay = w.layout() if hasattr(w, "layout") else None
            if lay is not None:
                lay.setSizeConstraint(QLayout.SetNoConstraint)
            w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
            w.setMinimumSize(mw, mh)

        for tw in self.findChildren(QTabWidget):
            for i in range(tw.count()):
                relax(tw.widget(i))
            relax(tw, 200, 120)
        for sp in self.findChildren(QSplitter):
            sp.setChildrenCollapsible(True)
            for i in range(sp.count()):
                relax(sp.widget(i))
            relax(sp, 160, 100)
        for name in ("fp_view", "model_view", "sym_view"):
            relax(getattr(self, name, None), 160, 120)
        relax(self.centralWidget(), 320, 200)

    def _fill_prev_parts(self, c: Component):
        """Список секций над превью. У односекционного символа он скрыт."""
        sym = c.symbol
        n = max(int(getattr(sym, "part_count", 1) or 1),
                max([int(p.unit or 1) for p in sym.pins] or [1]))
        self.cb_prev_part.blockSignals(True)
        self.cb_prev_part.clear()
        for u in range(1, n + 1):
            k = sum(1 for p in sym.pins if int(p.unit or 1) == u)
            self.cb_prev_part.addItem(f"{u} ({k} выв.)", u)
        self.cb_prev_part.setCurrentIndex(0)
        self.cb_prev_part.blockSignals(False)
        show = n > 1
        self.prev_part_bar.setVisible(show)

    def _redraw_symbol(self):
        c = self.current
        if not c:
            return
        n = self.cb_prev_part.count()
        part = int(self.cb_prev_part.currentData() or 1) if n > 1 else 0
        try:
            title = f"{c.name} — выводов {len(c.symbol.pins)}"
            if part:
                k = sum(1 for p in c.symbol.pins if int(p.unit or 1) == part)
                title += f", секция {part}: {k}"
            self.sym_view.set_svg(
                svg.symbol_svg(c, self.svc.style(), part=part), title)
        except Exception as e:
            self.log(f"Превью символа: {e}")

    # ------------------------------------- личные настройки компонента ------
    def _fill_own_style(self, c: Component):
        """Показать личные настройки компонента, не трогая общие."""
        st = self.svc.style()
        over = getattr(c, "style_over", None) or {}
        for w in (self.cb_nums, self.cb_compact, self.sp_scale):
            w.blockSignals(True)
        self.cb_nums.setChecked(bool(over.get("show_pin_numbers",
                                              st.show_pin_numbers)))
        self.sp_scale.setValue(int(round(float(
            over.get("passive_scale", st.passive_scale)) * 100)))
        self.cb_compact.setChecked(bool(over.get("compact_symbols",
                                                 st.compact_symbols)))
        for w in (self.cb_nums, self.cb_compact, self.sp_scale):
            w.blockSignals(False)
        src = getattr(c, "symbol_source", "gost")
        self.cb_src.blockSignals(True)
        self.cb_src.setCurrentIndex(1 if src == "native" else 0)
        self.cb_src.setEnabled(bool(getattr(c, "native_prims", None))
                               or src == "native")
        self.cb_src.blockSignals(False)
        # масштаб есть смысл крутить только у двухвыводной графики
        shape = classify.SYMBOL_SHAPE.get(c.ctype, "box")
        self.sp_scale.setEnabled(shape not in ("box", "connector"))
        self.cb_compact.setEnabled(shape == "box")

    def _set_own(self, **kw):
        c = self.current
        if not c:
            return
        over = dict(getattr(c, "style_over", None) or {})
        over.update(kw)
        st = self.svc.style()
        # значение, совпавшее с общим, не храним -- иначе компонент
        # перестанет следовать настройкам библиотеки
        for k in list(over):
            if hasattr(st, k) and over[k] == getattr(st, k):
                del over[k]
        c.style_over = over
        self.svc.rebuild_symbol(c)
        self.svc.db.upsert(c)
        self.on_select()

    # ------------------------------------------------------- проекты -------
    def refresh_projects(self):
        """Перечитать список проектов и восстановить текущий."""
        cur = int(getattr(self.cfg, "active_project", 0) or 0)
        self.proj_pick.blockSignals(True)
        self.proj_pick.clear()
        self.proj_pick.addItem("Общая библиотека", 0)
        for r in self.svc.db.projects():
            self.proj_pick.addItem(f"{r['name']}  ({r['n']})", int(r["id"]))
        i = self.proj_pick.findData(cur)
        self.proj_pick.setCurrentIndex(i if i >= 0 else 0)
        self.proj_pick.blockSignals(False)
        self._show_target()
        self._sync_project_buttons()

    def _show_target(self):
        """Показать в строке состояния, куда пойдёт сборка."""
        pr = self.svc.active_project()
        schlib, _ = self.svc.library_paths(pr)
        n = len(self.svc.target_uids(pr))
        where = "общая библиотека" if pr is None else f"проект «{pr['name']}»"
        self.lbl_target.setText(f"{where}: {n} шт. → {schlib}")
        self.lbl_target.setToolTip(f"Сюда соберётся библиотека:\n{schlib}")

    def _project_changed(self):
        self.svc.set_active_project(self.proj_pick.currentData() or 0)
        self._show_target()
        self.refresh_table()
        self.refresh_proj_tree()
        self._sync_project_buttons()

    def _sync_project_buttons(self):
        """Кнопки, которым нужен выбранный проект, без него бессмысленны."""
        has = self.svc.active_project() is not None
        for w in (getattr(self, "b_pick", None),
                  getattr(self, "cb_imp_proj", None)):
            if w is not None:
                w.setEnabled(has)

    def refresh_proj_tree(self):
        """Состав текущего проекта: категории, внутри -- имена."""
        self.proj_tree.clear()
        pr = self.svc.active_project()
        if pr is None:
            self.proj_tree.setHeaderLabels(["Общая библиотека", "шт"])
            uids = self.svc.db.all_uids(only_lib=True)
        else:
            self.proj_tree.setHeaderLabels([f"Проект «{pr['name']}»", "шт"])
            uids = self.svc.db.project_uids(int(pr["id"]))
        by_cat: Dict[str, List] = {}
        for uid in uids:
            r = self.svc.db.get_row(uid)
            if not r:
                continue
            cat = classify.CTYPE_NAME.get(r["ctype"], r["ctype"])
            by_cat.setdefault(cat, []).append(r)
        for cat in sorted(by_cat):
            rows = sorted(by_cat[cat], key=lambda r: (r["name"] or "").lower())
            node = QTreeWidgetItem([cat, str(len(rows))])
            for r in rows:
                it = QTreeWidgetItem([r["name"], ""])
                it.setData(0, Qt.UserRole, r["uid"])
                sub = r["mpn"] or r["value"] or ""
                if sub and sub != r["name"]:
                    it.setText(1, "")
                    it.setToolTip(0, sub)
                node.addChild(it)
            self.proj_tree.addTopLevelItem(node)
        self.proj_tree.expandAll() if len(by_cat) <= 6 else None
        self._show_sizes()

    def _show_sizes(self):
        info = self.svc.disk_usage()
        pr = self.svc.active_project()
        cur = info["projects"].get(pr["name"], 0) if pr is not None \
            else info["common"]
        self.proj_size.setText(
            f"библиотека: {_mb(cur)} · модели: {_mb(info['models'])} · "
            f"всего: {_mb(info['total'])}")

    def _proj_tree_open(self, it, _col):
        uid = it.data(0, Qt.UserRole)
        if not uid:
            return
        self.search.setText("")
        self.refresh_table()
        self._select_uid(uid)

    def _select_uid(self, uid: str):
        for row in range(self.model.rowCount()):
            if self.model.item(row, COL_UID) \
                    and self.model.item(row, COL_UID).text() == uid:
                idx = self.proxy.mapFromSource(self.model.index(row, 0))
                if idx.isValid():
                    self.table.setCurrentIndex(idx)
                    self.table.scrollTo(idx)
                return

    def _proj_tree_menu(self, pos):
        it = self.proj_tree.itemAt(pos)
        if not it or not it.data(0, Qt.UserRole):
            return
        uid = it.data(0, Qt.UserRole)
        m = QMenu(self)
        m.addAction("Показать в таблице", lambda: self._proj_tree_open(it, 0))
        pr = self.svc.active_project()
        if pr is not None:
            m.addAction("Убрать из проекта", lambda: (
                self.svc.db.project_remove(int(pr["id"]), [uid]),
                self.refresh_projects(), self.refresh_proj_tree(),
                self.refresh_table()))
        m.exec(self.proj_tree.viewport().mapToGlobal(pos))

    def do_shrink_models(self):
        """
        Пережать крупные 3D-модели под текущую «подробность 3D».

        Именно они делают сборку многоминутной: разбор фасетного STEP --
        самая дорогая операция в Altium. Замер на живом проекте: посадка с
        моделью на 23 МБ строилась 337 секунд, соседняя посадка из 355
        площадок -- 8,5 секунды.
        """
        heavy = self.svc.heavy_models()
        budget = int(getattr(self.cfg, "model_faces", 6000))
        if not heavy:
            QMessageBox.information(
                self, "GostLib",
                "Крупных 3D-моделей нет — сборку они не тормозят.")
            return
        lines = [f"Крупных моделей: {len(heavy)}", ""]
        for n, size in heavy[:10]:
            lines.append(f"  {n} — {_mb(size)}")
        if len(heavy) > 10:
            lines.append(f"  … и ещё {len(heavy) - 10}")
        lines += ["",
                  f"Пережать под текущую подробность ({budget} треугольников)?",
                  "Исходные .obj останутся на месте, так что операция "
                  "обратима: поднимете подробность — пережмёте заново.",
                  "Модели без исходного .obj рядом пропускаются."]
        if QMessageBox.question(self, "Пережать 3D-модели",
                                "\n".join(lines)) != QMessageBox.Yes:
            return
        self.setEnabled(False)
        try:
            res = self.svc.shrink_models(budget=budget)
        except Exception as e:
            self.setEnabled(True)
            QMessageBox.warning(self, "GostLib", f"Не вышло: {e}")
            return
        self.setEnabled(True)
        done, skip = res["done"], res["skipped"]
        msg = [f"Пережато моделей: {len(done)}"]
        if done:
            msg.append(f"Было {_mb(res['before'])}, стало {_mb(res['after'])}")
            msg.append("")
            msg.append("Пересоберите библиотеку — сборка должна заметно "
                       "ускориться.")
        if skip:
            msg += ["", "Пропущены:"] + [f"  {s}" for s in skip[:8]]
        QMessageBox.information(self, "GostLib", "\n".join(msg))
        self.log("\n".join(msg))

    def do_reseat_models(self):
        """
        Пересчитать вертикальное положение 3D-тел.

        Altium считает Z = 0 плоскостью платы, а модели из EasyEDA
        приходят с началом координат где придётся: у FBGA-96 геометрия
        шла от -0.37 мм, и шарики сидели ВНУТРИ платы. Правим только
        смещение (standoff) -- сами файлы моделей не трогаем.
        """
        sel = self.selected_uids()
        scope = "выделенных" if sel else "всех"
        if QMessageBox.question(
                self, "Посадить 3D-модели",
                f"Пересчитать посадку 3D-тел у {scope} компонентов?\n\n"
                "Altium считает Z = 0 плоскостью платы, а модели приходят "
                "с началом координат где придётся — отсюда корпуса, "
                "утонувшие в плате.\n\n"
                "Меняется только смещение по высоте. Файлы моделей "
                "не трогаются: выбранные вручную остаются как есть.\n"
                "У выводных корпусов посадка не применяется — там ножки "
                "ниже платы, и это правильно.") != QMessageBox.Yes:
            return
        self.setEnabled(False)
        try:
            r = self.svc.reseat_models(sel or None)
        except Exception as e:
            self.setEnabled(True)
            QMessageBox.warning(self, "GostLib", f"Не вышло: {e}")
            return
        self.setEnabled(True)
        QMessageBox.information(
            self, "GostLib",
            f"Поправлено компонентов: {r['moved']}\n"
            f"Без изменений: {r['skipped']}\n\n"
            "Пересоберите библиотеку. Ctrl+Z вернёт как было.")
        self.on_select()

    def do_disk_usage(self):
        """Сколько занимает каждая библиотека -- чтобы не разрастались."""
        info = self.svc.disk_usage()
        lines = [f"Общая библиотека: {_mb(info['common'])}"]
        for name, n in sorted(info["projects"].items(),
                              key=lambda kv: -kv[1]):
            lines.append(f"Проект «{name}»: {_mb(n)}")
        lines += [
            "",
            f"3D-модели: {_mb(info['models'])}",
            f"Кеш сеток: {_mb(info['mesh_cache'])}",
            f"Задания: {_mb(info['jobs'])}",
            f"Кеш загрузок: {_mb(info.get('cache', 0))}",
            f"Резервные копии: {_mb(info['backups'])}",
            f"Каталог: {_mb(info['catalog'])}",
            "",
            f"Итого: {_mb(info['total'])}",
        ]
        if info.get("orphans"):
            lines += ["",
                      f"Ссылок на удалённые компоненты: {info['orphans']}"
                      " — их уберёт «Очистить»"]
        box = QMessageBox(self)
        box.setWindowTitle("Место на диске")
        box.setText("\n".join(lines))
        clean = box.addButton("Очистить кеш и задания",
                              QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is clean:
            freed = self.svc.clean_temp()
            QMessageBox.information(self, "GostLib",
                                    f"Освобождено: {_mb(freed)}")
            self._show_sizes()
            # счётчики у проектов могли измениться -- обновляем список
            self.refresh_projects()
            self.refresh_proj_tree()

    def do_projects(self):
        from .dialogs import ProjectsDialog
        d = ProjectsDialog(self.svc, self)
        d.exec()
        self.refresh_projects()
        self.refresh_table()

    # ------------------------------------------------ выгрузка наружу ------
    def do_export_kicad_project(self):
        """Дописать выделенные компоненты в библиотеки проекта KiCad."""
        uids = self.selected_uids()
        if not uids:
            return
        # Компонент, пришедший из KiCad, проще подключить из исходной
        # библиотеки. Предупреждаем и называем, откуда он: символ, посадку
        # и модель. Отключается в «Настройки -> Экспорт в KiCad».
        if getattr(self.cfg, "kicad_export_warn", True):
            orig = self.svc.kicad_origins(uids)
            if orig:
                lines = []
                for name, o in orig[:8]:
                    lines.append(f"• {name}\n"
                                 f"    символ: {o.get('symbol') or '—'}\n"
                                 f"    посадка: {o.get('footprint') or '—'}\n"
                                 f"    3D: {os.path.basename(o.get('model') or '') or '—'}")
                if len(orig) > 8:
                    lines.append(f"… и ещё {len(orig) - 8}")
                box = QMessageBox(self)
                box.setWindowTitle("Экспорт в проект KiCad")
                box.setIcon(QMessageBox.Information)
                box.setText(
                    ("Этот компонент и так из KiCad." if len(orig) == 1 else
                     f"{len(orig)} из выделенных и так из KiCad.") +
                    " Его можно взять из штатной библиотеки KiCad — "
                    "выгружать копию не обязательно.")
                box.setInformativeText("\n".join(lines))
                box.setDetailedText(
                    "В проект уйдёт символ, собранный в GostLib (по ГОСТ или "
                    "родной — как выбрано у компонента), а не исходный "
                    "символ KiCad. Если нужен именно исходный — подключите "
                    "библиотеку KiCad, названную выше.")
                go = box.addButton("Всё равно экспортировать",
                                   QMessageBox.AcceptRole)
                box.addButton("Отмена", QMessageBox.RejectRole)
                never = QCheckBox("больше не предупреждать")
                box.setCheckBox(never)
                box.exec()
                if never.isChecked():
                    self.cfg.kicad_export_warn = False
                    self.cfg.save()
                if box.clickedButton() is not go:
                    return
        from .dialogs import KicadExportDialog
        names = [c.name for c in self.svc.collect(uids)]
        dlg = KicadExportDialog(self.cfg, names, self)
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.values()
        try:
            res = self.svc.export_kicad_project(uids, **v)
        except Exception as e:
            QMessageBox.critical(self, "GostLib",
                                 f"Экспорт в KiCad не удался:\n{e}")
            self.log(traceback.format_exc())
            return
        added, replaced = res.get("added") or [], res.get("replaced") or []
        msg = [f"Библиотека: {res.get('lib')}",
               f"Символов добавлено: {len(added)}, заменено: {len(replaced)}",
               f"Посадок: {len(res.get('footprints') or [])}, "
               f"3D-моделей: {len(res.get('models') or [])}"]
        for kind, st in (res.get("tables") or {}).items():
            what = "символов" if kind == "sym" else "посадок"
            if st == "added":
                msg.append(f"Библиотека {what} подключена к проекту.")
            elif str(st).startswith("conflict:"):
                msg.append(f"В таблице {what} уже есть библиотека с этим "
                           f"именем, но с другим путём ({str(st)[9:]}) — "
                           f"таблицу не трогал.")
        msg.append("Если KiCad открыт — переоткройте проект.")
        QMessageBox.information(self, "Экспорт в проект KiCad",
                                "\n".join(msg))

    def do_save_model(self):
        """Сохранить 3D-модель выделенного компонента (или нескольких)."""
        from PySide6.QtWidgets import QFileDialog
        uids = self.selected_uids()
        models = self.svc.component_models(uids)
        if not models:
            QMessageBox.information(self, "GostLib",
                                    "У выделенного нет 3D-модели на диске.")
            return
        if len(models) == 1:
            name, path = models[0]
            ext = os.path.splitext(path)[1] or ".step"
            target, _ = QFileDialog.getSaveFileName(
                self, f"3D-модель: {name}",
                os.path.join(os.path.expanduser("~"),
                             os.path.basename(path)),
                f"3D-модель (*{ext});;Все файлы (*)")
        else:
            target = QFileDialog.getExistingDirectory(
                self, f"Куда сохранить {len(models)} 3D-моделей",
                os.path.expanduser("~"))
        if not target:
            return
        try:
            done = self.svc.save_models(uids, target)
        except Exception as e:
            QMessageBox.critical(self, "GostLib", f"Не сохранилось:\n{e}")
            return
        self.log(f"Сохранено 3D-моделей: {len(done)}")

    def service_actions(self):
        """
        Служебные действия для вкладки «Сервис» в настройках.

        Список -- здесь, рядом с функциями, которые он вызывает: окно
        настроек ничего не знает про главное окно и просто показывает
        кнопки.
        """
        return [
            ("Запасные пути сборки", [
                ("Через KiCad-импортёр (Import Wizard)",
                 lambda: self.do_convert("kicad"),
                 "Если скрипт в этом Altium не работает"),
                ("Через EAGLE (.lbr)", lambda: self.do_convert("eagle"), ""),
                ("Прямая двоичная запись .SchLib/.PcbLib",
                 lambda: self.do_convert("binary"),
                 "Без Altium; годится для простых компонентов"),
                ("Собрать задание без запуска (F9)",
                 lambda: self.do_convert("script"), ""),
            ]),
            ("Altium", [
                ("Открыть папку библиотеки",
                 lambda: self._open(self.cfg.lib_dir), ""),
                ("Показать журнал скрипта Altium", self.show_script_log, ""),
                ("Команда запуска скрипта…", self.do_show_command, ""),
            ]),
            ("Данные", [
                ("Резервная копия…", self.do_backup,
                 "Каталог, настройки, библиотеки, модели и задания — в .zip"),
                ("Открыть папку резервных копий",
                 lambda: self._open(self.svc.backup_dir()), ""),
                ("Открыть рабочую папку",
                 lambda: self._open(self.cfg.root), ""),
                ("Очистить библиотеку…", self.do_clear_library, ""),
            ]),
            ("3D-модели", [
                ("Доставить пакеты для просмотра 3D телом",
                 self.do_setup3d, ""),
                ("Пережать 3D-модели (ускорить сборку)…",
                 self.do_shrink_models, ""),
                ("Посадить 3D-модели на плату…", self.do_reseat_models, ""),
            ]),
        ]

    def do_pick_from_catalog(self):
        """Добрать в текущий проект то, что уже есть в общем каталоге."""
        pr = self.svc.active_project()
        if pr is None:
            QMessageBox.information(
                self, "GostLib",
                "Сейчас выбрана общая библиотека — добавлять некуда.\n"
                "Выберите проект в списке сверху.")
            return
        from .dialogs import PickComponentsDialog
        d = PickComponentsDialog(self.svc, int(pr["id"]), self)
        if d.exec() != QDialog.Accepted:
            return
        uids = d.chosen()
        if not uids:
            self.statusBar().showMessage("Ничего не отмечено", 3000)
            return
        n = self.svc.project_add(int(pr["id"]), uids)
        self.log(f"В проект «{pr['name']}» добавлено из каталога: {n}")
        self.refresh_projects()
        self.refresh_proj_tree()
        self.refresh_table()

    def _proj_add_sel(self):
        pr = self.svc.active_project()
        if pr is None:
            QMessageBox.information(self, "GostLib",
                                    "Сначала выберите проект в панели сверху.")
            return
        uids = self.selected_uids()
        n = self.svc.project_add(int(pr["id"]), uids)
        self.log(f"В проект «{pr['name']}» добавлено: {n}")
        self.refresh_projects()
        self.refresh_table()

    def _proj_del_sel(self):
        pr = self.svc.active_project()
        if pr is None:
            return
        uids = self.selected_uids()
        n = self.svc.project_remove(int(pr["id"]), uids)
        self.log(f"Из проекта «{pr['name']}» убрано: {n}")
        self.refresh_projects()
        self.refresh_table()

    # ------------------------------------------------------- отмена --------
    def do_undo(self):
        if not self.svc.undo.can_undo():
            self.statusBar().showMessage("Отменять нечего", 3000)
            return
        title = self.svc.undo.undo()
        self.statusBar().showMessage(f"Отменено: {title}", 4000)
        self._after_undo()

    def do_redo(self):
        if not self.svc.undo.can_redo():
            self.statusBar().showMessage("Возвращать нечего", 3000)
            return
        title = self.svc.undo.redo()
        self.statusBar().showMessage(f"Возвращено: {title}", 4000)
        self._after_undo()

    def _after_undo(self):
        uid = self.current.uid if self.current else ""
        self.refresh_types()
        self.refresh_table()
        self.refresh_projects()
        self.refresh_proj_tree()
        if uid:
            self._select_uid(uid)
        self.on_select()

    def do_rename(self):
        """Задать имя компонента вручную (оно же LibReference в Altium)."""
        uids = self.selected_uids()
        if len(uids) != 1:
            QMessageBox.information(self, "GostLib",
                                    "Переименовать можно один компонент.")
            return
        c = self.svc.db.get(uids[0])
        if not c:
            return
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "Переименовать компонент",
            f"Имя (LibReference в Altium).\n"
            f"Подсказка по типу и номиналу: {self.svc.suggest_name(c)}",
            text=c.name)
        if not ok:
            return
        try:
            with self.svc.undo.step("Переименовать"):
                self.svc.rename(c.uid, name)
        except ValueError as e:
            QMessageBox.warning(self, "GostLib", str(e))
            return
        self.refresh_table()
        self.refresh_proj_tree()
        self._select_uid(c.uid)
        self.on_select()

    def do_autoname(self):
        """Дать выделенным осмысленные имена: тип, номинал, корпус."""
        uids = self.selected_uids()
        if not uids:
            return
        n = 0
        stepper = self.svc.undo.step(f"Имена по типу: {len(uids)} шт.")
        stepper.__enter__()
        for uid in uids:
            c = self.svc.db.get(uid)
            if not c:
                continue
            want = self.svc.suggest_name(c)
            if want and want != c.name:
                try:
                    self.svc.rename(uid, want)
                    n += 1
                except ValueError as e:
                    self.log(f"  {c.name}: {e}")
        stepper.__exit__(None, None, None)
        self.log(f"Переименовано: {n}")
        self.refresh_table()
        self.refresh_proj_tree()
        self.on_select()

    def do_refresh_native(self):
        """Забрать родную графику из источника заново: .kicad_sym или LCSC."""
        uids = self.selected_uids()
        if not uids:
            return
        ok = bad = 0
        for uid in uids:
            try:
                self.svc.refresh_native(uid)
                ok += 1
            except Exception as e:
                bad += 1
                self.log(f"  {e}")
        self.log(f"Перечитано УГО: {ok}" + (f", не вышло: {bad}" if bad else ""))
        self.refresh_table()
        self.on_select()

    def _set_symbol_source(self):
        c = self.current
        if not c:
            return
        src = self.cb_src.currentData() or "gost"
        if src == getattr(c, "symbol_source", "gost"):
            return
        if src == "native" and not getattr(c, "native_prims", None):
            QMessageBox.information(
                self, "GostLib",
                "У этого компонента нет сохранённого обозначения из "
                "источника — его нечего показать. Возьмите «Перечитать УГО "
                "из источника» в контекстном меню или переимпортируйте "
                "компонент: родное УГО сохранится вместе с ним.")
            self._fill_own_style(c)
            return
        c.symbol_source = src
        c.symbol.manual_layout = False   # ручная раскладка была для другого УГО
        self.svc.rebuild_symbol(c)
        self.svc.db.upsert(c)
        self.log(f"{c.name}: обозначение — "
                 + ("как в источнике" if src == "native" else "по ГОСТ"))
        self.on_select()

    # ------------------------------------------- правила группировки -------
    def _fill_rules(self, c: Component):
        own = (getattr(c, "pin_rules", "") or "").strip()
        self.cb_rules_own.blockSignals(True)
        self.cb_rules_own.setChecked(bool(own))
        self.cb_rules_own.blockSignals(False)
        if own:
            self.rules_edit.setPlainText(own)
            self.rules_note.setText(f"правила этого компонента ({c.name})")
        else:
            from ..gost import pingroups
            # прежние встроенные правила, сохранённые как есть, считаются
            # встроенными -- показываем нынешние
            self.rules_edit.setPlainText(pingroups.effective_text(
                getattr(self.cfg, "pin_rules", "") or ""))
            self.rules_note.setText("общие правила для всей библиотеки")
        self._check_rules()

    def _toggle_own_rules(self):
        c = self.current
        if not c:
            return
        if self.cb_rules_own.isChecked():
            self.rules_note.setText(f"правила этого компонента ({c.name})")
        else:
            c.pin_rules = ""
            self.svc.db.upsert(c)
            self._fill_rules(c)
            self.svc.rebuild_symbol(c)
            self.svc.db.upsert(c)
            self.on_select()

    def _save_rules(self):
        from ..gost import pingroups
        text = self.rules_edit.toPlainText()
        problems = pingroups.check(text)
        if problems:
            QMessageBox.warning(
                self, "GostLib",
                "В правилах есть замечания:\n\n" + "\n".join(problems[:8]))
            return
        n = len(pingroups.parse(text))
        if self.cb_rules_own.isChecked() and self.current:
            self.current.pin_rules = text
            self.svc.db.upsert(self.current)
            self.svc.rebuild_symbol(self.current)
            self.svc.db.upsert(self.current)
            self.log(f"{self.current.name}: свои правила выводов ({n} групп)")
            self.on_select()
            return
        self.cfg.pin_rules = text
        self.cfg.save()
        self.log(f"Общие правила выводов сохранены ({n} групп). "
                 f"Пересобираю символы…")
        self._rebuild_all_rules()

    def _default_rules(self):
        from ..gost import pingroups
        self.rules_edit.setPlainText(pingroups.DEFAULT_RULES)
        self._check_rules()

    def _check_rules(self):
        """Показать, как текущий текст правил разложит выводы компонента."""
        from ..gost import pingroups
        view = getattr(self, "rules_view", None)
        if view is None:
            return
        c = self.current
        if not c:
            view.setPlainText("")
            self.rules_stat.setText("выберите компонент")
            return
        text = self.rules_edit.toPlainText()
        problems = pingroups.check(text)
        pins = list(c.raw_pins or c.symbol.pins)
        groups, rest = pingroups.explain(pins, pingroups.parse(text))
        side_name = {"L": "слева", "R": "справа", "A": "сам",
                     "T": "слева", "B": "справа"}
        lines = []
        for name, side, items in groups:
            shown = ", ".join(items[:24]) + (" …" if len(items) > 24 else "")
            lines.append(f"{name} ({side_name.get(side, side)}, "
                         f"{len(items)}): {shown}")
        if rest:
            lines.append("")
            lines.append(f"Не попали ни под одно правило ({len(rest)}) — их "
                         f"разложит автоматика по типу вывода:")
            lines.append(", ".join(sorted(set(rest))))
        if problems:
            lines.insert(0, "Замечания: " + "; ".join(problems[:4]))
            lines.insert(1, "")
        view.setPlainText("\n".join(lines))
        n = len(pins)
        hit = n - len(rest)
        self.rules_stat.setText(
            f"{c.name}: под правила попало {hit} из {n}"
            + (f" ({100 * hit // n}%)" if n else ""))

    def _rebuild_all_rules(self):
        """Правила общие — значит перерисовать надо все микросхемы."""
        n = 0
        for uid in self.svc.db.all_uids():
            c = self.svc.db.get(uid)
            if not c or getattr(c, "symbol_source", "gost") == "native":
                continue
            if c.symbol.manual_layout:
                continue          # ручную раскладку не трогаем
            self.svc.rebuild_symbol(c)
            self.svc.db.upsert(c)
            n += 1
        self.log(f"Пересобрано символов: {n}")
        self.refresh_table()
        self.on_select()

    def _toggle_comp_numbers(self):
        self._set_own(show_pin_numbers=self.cb_nums.isChecked())

    def _toggle_compact(self):
        self._set_own(compact_symbols=self.cb_compact.isChecked())

    def _set_comp_scale(self, v: int):
        self._set_own(passive_scale=round(v / 100.0, 2))

    def _reset_comp_style(self):
        c = self.current
        if not c:
            return
        c.style_over = {}
        self.svc.rebuild_symbol(c)
        self.svc.db.upsert(c)
        self.on_select()

    # -------------------------------------------- посадочное место и 3D ------
    def _fill_footprints(self, c: Component):
        self.fp_pick.blockSignals(True)
        self.fp_pick.clear()
        for i, fp in enumerate(c.footprints):
            mark = ""
            if fp.model and fp.model.path:
                mark = "  [3D]" if os.path.isfile(fp.model.path) else "  [3D?]"
            elif fp.is_external():
                mark = "  [вендорская]"
            self.fp_pick.addItem(f"{fp.name or '(без имени)'}{mark}", i)
        self.fp_pick.setEnabled(len(c.footprints) > 1)
        self.fp_pick.blockSignals(False)
        self._show_footprint()

    def current_fp_index(self) -> int:
        idx = self.fp_pick.currentData()
        return int(idx) if idx is not None else 0

    def _show_footprint(self):
        c = self.current
        self._pending_model = None
        self.b3d_preview.setEnabled(False)
        if not c or not c.footprints:
            self.fp_view.set_svg("", "посадочного места нет")
            self.fp_info.setPlainText(
                "У компонента нет посадочного места.\n"
                "Импортируйте его из KiCad или добавьте вендорскую .PcbLib.")
            self.height_edit.clear()
            self.mask_edit.clear()
            self.paste_edit.clear()
            return
        i = self.current_fp_index()
        if i >= len(c.footprints):
            i = 0
        fp = c.footprints[i]
        if fp.pads:
            self.fp_view.set_svg(svg.footprint_svg(fp),
                                 f"{fp.name} — площадок {len(fp.pads)}")
        elif fp.is_external():
            self.fp_view.set_svg("", f"{fp.name} — берётся из "
                                     f"{os.path.basename(fp.source_pcblib)}")
        else:
            self.fp_view.set_svg("", f"{fp.name} — без площадок")
        lines = []
        try:
            from .. import fpinfo
            lines += fpinfo.describe(fp).lines()
        except Exception as e:
            lines.append(f"Не удалось посчитать габариты: {e}")

        # ---- 3D: разбор модели уходит в фоновый поток
        # Модель, собранная из OBJ (EasyEDA), бывает на десятки мегабайт --
        # у CH375B получилось 69 МБ. Разбор такого файла в главном потоке
        # вешал всё окно намертво: снаружи это выглядело как «программа
        # больше не отвечает».
        self.model_view.set_model(None, fp, "3D-модели нет")
        self.fp_info.setPlainText("\n".join(lines))
        self.height_edit.setText(f"{fp.height:.2f}" if fp.height else "")
        # пусто -- «не задано»; ноль -- это именно ноль, а не пусто
        me = getattr(fp, "mask_expansion", None)
        pe = getattr(fp, "paste_expansion", None)
        self.mask_edit.setText("" if me is None else f"{me:g}")
        self.paste_edit.setText("" if pe is None else f"{pe:g}")
        self.cb_paste.blockSignals(True)
        self.cb_paste.setChecked(bool(getattr(fp, "paste", True)))
        self.cb_paste.blockSignals(False)
        gi = self.paste_grid.findData(int(getattr(fp, "paste_grid", 0) or 0))
        self.paste_grid.setCurrentIndex(gi if gi >= 0 else 0)
        self.paste_over.setText(f"{getattr(fp, 'paste_grid_over', 1.0):g}")
        self.paste_fill.setText(f"{getattr(fp, 'paste_grid_fill', 60.0):g}")
        tr = getattr(fp.model, "tape_rot", 0.0) if fp.model else 0.0
        self.tape_edit.setText(f"{tr:g}" if tr else "")
        path = fp.model.path if (fp.model and fp.model.path) else ""
        if path:
            self._start_model_load(path, fp, lines)

    # --------------------------------------------- загрузка 3D в фоне ------
    def _start_model_load(self, path: str, fp, lines, force: bool = False):
        # STEP из EasyEDA хранится для Altium, но превью берём из лежащего
        # рядом исходного OBJ. Ограничение размера относится именно к тому
        # файлу, который реально будет прочитан.
        try:
            from .. import mesh3d
            preview_path = mesh3d.preview_source(path)
        except Exception:
            preview_path = path
        big = 0
        try:
            big = os.path.getsize(preview_path)
        except OSError:
            pass
        self._model_lines = list(lines)
        self._model_fp = fp
        # предыдущий разбор мог ещё идти -- его результат нам уже не нужен
        self._model_token = getattr(self, "_model_token", 0) + 1
        for loader in tuple(self._model_loaders):
            if loader.isRunning():
                loader.stale = True

        name = os.path.basename(preview_path)
        if big > BIG_MODEL and not force:
            # STEP из EasyEDA может содержать десятки тысяч треугольников.
            # Даже в QThread чистый Python-парсер долго держит GIL, поэтому
            # автоматический старт при выборе строки всё равно подвешивает UI.
            self._pending_model = (path, fp, list(lines))
            self._queued_model = None
            self.b3d_preview.setEnabled(True)
            note = (f"{name} — {big / 1e6:.0f} МБ; "
                    "автопросмотр отключён")
            self.model_view.set_model(None, fp, note)
            self.fp_info.setPlainText("\n".join(lines + ["", (
                f"3D: {name} ({big / 1e6:.1f} МБ). Модель большая, поэтому "
                "не разбирается автоматически. Нажмите «Показать тяжёлую "
                "3D», если превью сейчас необходимо.")]))
            return

        self._pending_model = None
        self.b3d_preview.setEnabled(False)
        note = f"{name} — читаю…"
        if big > BIG_MODEL:
            note = (f"{name} — {big / 1e6:.0f} МБ, разбор займёт время; "
                    f"окно при этом работает")
        self.model_view.set_model(None, fp, note)
        self.fp_info.setPlainText("\n".join(
            lines + ["", f"3D: {name} ({big / 1e6:.1f} МБ) — разбираю…"]))

        # OCCT/trimesh не гарантируют безопасную параллельную загрузку в
        # двух Python QThread. При быстром выборе следующей строки ждём
        # окончания старой и запускаем только самый свежий запрос.
        if any(loader.isRunning() for loader in self._model_loaders):
            self._queued_model = (path, fp, list(lines), force)
            self.title_waiting_3d(name)
            return

        self._queued_model = None
        loader = ModelWorker(path, fp, self.cfg.models_dir,
                             self._model_token)
        self._model_loader = loader
        self._model_loaders.add(loader)
        loader.done.connect(self._model_ready)
        loader.finished.connect(self._model_loader_finished)
        loader.start()

    def title_waiting_3d(self, name: str):
        """Коротко показать, что новый просмотр стоит следующим в очереди."""
        self.model_view.title.setText(f"{name} — ожидает предыдущую 3D…")

    def _model_loader_finished(self):
        """Не отпускать QThread, пока он действительно не завершился."""
        loader = self.sender()
        self._model_loaders.discard(loader)
        if self._model_loader is loader:
            self._model_loader = None
        loader.deleteLater()
        if not self._model_loaders and self._queued_model:
            path, fp, lines, force = self._queued_model
            self._queued_model = None
            self._start_model_load(path, fp, lines, force=force)

    def _load_pending_model(self):
        """Явно запустить разбор большой модели, отложенный при выборе."""
        pending = self._pending_model
        if not pending:
            return
        path, fp, lines = pending
        self._start_model_load(path, fp, lines, force=True)

    def _model_ready(self, token, model, mesh, extra):
        if token != getattr(self, "_model_token", 0):
            return                      # пришёл ответ на устаревший запрос
        # Берём ЖИВОЕ посадочное место, а не то, что было в момент
        # запроса. Модель грузится в фоне, и если за это время человек
        # выбрал цвет, старый объект вернул бы прежний -- цвет на экране
        # откатывался, хотя в каталоге уже лежал новый.
        fp = self._model_fp
        cur = self.current
        if cur is not None and cur.footprints:
            i = self.current_fp_index()
            if 0 <= i < len(cur.footprints):
                fp = cur.footprints[i]
        lines = list(self._model_lines) + [""] + list(extra)
        if mesh is not None and getattr(mesh, "ok", False):
            b = mesh.bbox()
            size = (f"{b[3] - b[0]:.2f}x{b[4] - b[1]:.2f} мм"
                    if b else "готово")
            self.model_view.set_model(
                None, fp, f"{os.path.basename(fp.model.path)} — {size}")
            self.model_view.scene.set_mesh(mesh)
        elif model is not None and getattr(model, "ok", False):
            self.model_view.set_model(
                model, fp, f"{os.path.basename(model.path)} — "
                           f"{model.size()[0]:.2f}x{model.size()[1]:.2f} мм")
        else:
            self.model_view.set_model(
                None, fp, getattr(model, "error", "") or "модель не читается")
        self.fp_info.setPlainText("\n".join(lines))

    def do_show_command(self):
        """
        Показать готовую команду. Если автозапуск не срабатывает, её
        можно вставить в командную строку и увидеть настоящую ошибку
        Altium, а не гадать.
        """
        cmd = self.svc.altium_command()
        box = QMessageBox(self)
        box.setWindowTitle("Команда запуска скрипта в Altium")
        box.setText("Этой командой утилита просит Altium выполнить сборку.\n"
                    "Если Altium уже открыт, команда уходит в него.")
        box.setDetailedText(cmd)
        copy = box.addButton("Скопировать", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is copy:
            QApplication.clipboard().setText(cmd)
            self.log("Команда скопирована в буфер обмена")

    def do_build_and_run(self):
        """Собрать задание и сразу выполнить скрипт в Altium."""
        self._autorun = True
        try:
            self.do_convert("script")
        finally:
            self._autorun = False

    def do_backup(self):
        """Сложить каталог, настройки, библиотеки и модели в архив с датой."""
        self.setEnabled(False)
        try:
            path = self.svc.backup()
        except Exception as e:
            self.setEnabled(True)
            QMessageBox.warning(self, "GostLib",
                                f"Копия не сделана: {e}")
            return
        self.setEnabled(True)
        size = os.path.getsize(path) / 1e6
        if QMessageBox.information(
                self, "GostLib",
                f"Копия сохранена:\n{path}\n({size:.1f} МБ)\n\n"
                f"Открыть папку?",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._open(os.path.dirname(path))

    def do_clear_library(self):
        """
        Убрать из каталога всё. Копия делается сама, до удаления: платить
        за неё копейки, а восстановить иначе нечем.
        """
        n = self.svc.db.stats().get("total", 0)
        box = QMessageBox(self)
        box.setWindowTitle("Очистить библиотеку")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"Убрать из каталога все компоненты ({n} шт.)?")
        box.setInformativeText(
            "Сначала будет сделана резервная копия с датой — каталог, "
            "настройки, собранные библиотеки и 3D-модели.\n\n"
            "Индекс библиотек KiCad и файлы моделей останутся на месте.")
        cb = QCheckBox("удалить и собранные .SchLib/.PcbLib/.IntLib")
        box.setCheckBox(cb)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec() != QMessageBox.Yes:
            return
        self.setEnabled(False)
        try:
            zip_path = self.svc.backup("before-clear")
        except Exception as e:
            self.setEnabled(True)
            QMessageBox.critical(
                self, "GostLib",
                f"Копия не сделалась ({e}) — очистку отменяю, "
                f"чтобы не потерять данные.")
            return
        try:
            res = self.svc.clear_library(drop_files=cb.isChecked())
        finally:
            self.setEnabled(True)
        self.current = None
        self.refresh_types()
        self.refresh_table()
        QMessageBox.information(
            self, "GostLib",
            f"Убрано компонентов: {res['components']}.\n"
            f"Копия: {zip_path}")

    def do_setup3d(self):
        """
        Поставить cascadio и trimesh тем же интерпретатором, каким запущена
        программа. Руками легко попасть в системный Python вместо .venv --
        тогда пакеты стоят, а программа их не видит.
        """
        import subprocess
        import sys as _sys
        from .. import mesh3d
        if mesh3d.available():
            QMessageBox.information(
                self, "GostLib",
                "Пакеты уже стоят — просмотр телом работает.")
            return
        if QMessageBox.question(
                self, "GostLib",
                "Скачать и поставить cascadio и trimesh?\n\n"
                "Это около 18 МБ, ставится один раз, в среду программы:\n"
                f"{_sys.executable}") != QMessageBox.Yes:
            return
        self.setEnabled(False)
        try:
            r = subprocess.run(
                [_sys.executable, "-m", "pip", "install",
                 "cascadio", "trimesh"],
                capture_output=True, text=True)
        except Exception as e:
            self.setEnabled(True)
            QMessageBox.warning(self, "GostLib", f"Не запустился pip: {e}")
            return
        self.setEnabled(True)
        import importlib
        importlib.invalidate_caches()
        importlib.reload(mesh3d)
        if mesh3d.available():
            QMessageBox.information(
                self, "GostLib",
                "Готово. Выберите компонент заново — модель построится телом.")
            self.on_select()
        else:
            tail = (r.stderr or r.stdout or "")[-1200:]
            QMessageBox.warning(
                self, "GostLib",
                "Установка не удалась.\n\n" + tail)

    def do_set_model(self):
        if not self.current or not self.current.footprints:
            QMessageBox.information(self, "GostLib",
                                    "Сначала выберите компонент с посадочным местом")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "3D-модель корпуса", self.cfg.models_dir,
            "Модели (*.step *.stp *.obj *.stl *.wrl);;Все файлы (*.*)")
        if not path:
            return
        try:
            c = self.svc.set_model(self.current.uid, path,
                                   self.current_fp_index())
        except Exception as e:
            QMessageBox.critical(self, "3D-модель", str(e))
            self.log(f"ОШИБКА: {e}")
            return
        self.current = c
        self._fill_footprints(c)
        self.log("Модель привязана. Нажмите «Собрать в Altium» (F9), "
                 "чтобы она попала в .PcbLib.")

    def do_clear_model(self):
        if not self.current or not self.current.footprints:
            return
        c = self.svc.clear_model(self.current.uid, self.current_fp_index())
        if c:
            self.current = c
            self._fill_footprints(c)

    def do_set_expansion(self):
        """
        Зазоры маски и пасты у ТЕКУЩЕЙ посадки.

        Раньше это была общая настройка на всю библиотеку, и это было
        неверно: зазор -- свойство корпуса. Пустое поле означает «не
        задавать», и тогда Altium берёт правило проекта; ноль означает
        именно ноль.
        """
        if not self.current or not self.current.footprints:
            return

        def num(w, what):
            s = (w.text() or "").strip().replace(",", ".")
            if not s:
                return None, True
            try:
                return float(s), True
            except ValueError:
                QMessageBox.information(
                    self, "Зазоры",
                    f"{what} задаётся числом в миллиметрах "
                    f"(или пусто — правило проекта)")
                return None, False

        mask, ok1 = num(self.mask_edit, "Зазор маски")
        paste, ok2 = num(self.paste_edit, "Зазор пасты")
        if not (ok1 and ok2):
            return
        c = self.svc.set_expansion(self.current.uid, mask, paste,
                                   self.current_fp_index())
        if c:
            self.current = c
            self._show_footprint()
            if mask is not None or paste is not None:
                self.statusBar().showMessage(
                    "Зазоры сохранены. Пересоберите библиотеку, чтобы они "
                    "попали в посадочное место", 7000)

    # ------------------------------------------------ текстовая копия ------
    def _git_dir(self) -> str:
        """Папка зеркала; при первом обращении спрашиваем и запоминаем."""
        root = self.svc.git_root()
        if getattr(self.cfg, "git_dir", ""):
            return root
        d = QFileDialog.getExistingDirectory(
            self, "Папка для текстовой копии библиотеки (её и коммитить)",
            root)
        if not d:
            return ""
        self.cfg.git_dir = d
        self.cfg.save()
        return d

    def do_git_export(self):
        root = self._git_dir()
        if not root:
            return
        try:
            r = self.svc.git_export(root,
                                    with_models=bool(getattr(
                                        self.cfg, "git_models", False)))
        except Exception as e:                              # noqa: BLE001
            QMessageBox.warning(self, "Не выложилось", str(e))
            return
        QMessageBox.information(
            self, "Готово",
            f"Компонентов: {r['total']}\n"
            f"Изменено файлов: {len(r['changed'])}\n"
            f"Удалено: {len(r['removed'])}\n\n"
            f"Папка: {r['root']}\n\n"
            f"Дальше как обычно: git add -A, git commit, git push.")

    def do_git_status(self):
        root = self._git_dir()
        if not root:
            return
        try:
            st = self.svc.git_status(root)
        except Exception as e:                              # noqa: BLE001
            QMessageBox.warning(self, "Не посмотрелось", str(e))
            return

        def lst(rows):
            names = [n for _u, n in rows]
            return ("\n  " + "\n  ".join(names[:12])
                    + (f"\n  … и ещё {len(names) - 12}"
                       if len(names) > 12 else "")) if names else " —"

        QMessageBox.information(
            self, "Сравнение с папкой",
            f"В каталоге: {st['local_total']}, в папке: "
            f"{st['remote_total']}\n\n"
            f"Только у нас:{lst(st['ours'])}\n\n"
            f"Только в папке:{lst(st['theirs'])}\n\n"
            f"Расходятся:{lst(st['diff'])}")

    def do_git_import(self, overwrite: bool):
        root = self._git_dir()
        if not root:
            return
        if overwrite and QMessageBox.question(
                self, "Взять версию из репозитория",
                "Компоненты в каталоге будут заменены версиями из папки.\n"
                "Локальные правки этих компонентов пропадут "
                "(Ctrl+Z вернёт).\n\nПродолжить?") != QMessageBox.Yes:
            return
        try:
            r = self.svc.git_import(root, overwrite=overwrite)
        except Exception as e:                              # noqa: BLE001
            QMessageBox.warning(self, "Не забралось", str(e))
            return
        self.refresh_table()
        QMessageBox.information(
            self, "Готово",
            f"Добавлено: {len(r['added'])}\n"
            f"Обновлено: {len(r['updated'])}\n"
            f"Пропущено (уже есть): {len(r['skipped'])}")

    def do_git_sync(self):
        root = self._git_dir()
        if not root:
            return
        try:
            r = self.svc.git_sync(root,
                                  with_models=bool(getattr(
                                      self.cfg, "git_models", False)))
        except Exception as e:                              # noqa: BLE001
            QMessageBox.warning(self, "Не синхронизировалось", str(e))
            return
        self.refresh_table()
        msg = (f"Забрано из папки: {len(r['pulled'])}\n"
               f"Изменено файлов: {r['pushed']}\n")
        if r["conflicts"]:
            msg += ("\nРасходятся с репозиторием (выложена НАША версия):\n  "
                    + "\n  ".join(r["conflicts"][:10])
                    + "\n\nЧтобы взять чужую — «Взять версию из "
                      "репозитория».")
        QMessageBox.information(self, "Синхронизация", msg)

    def do_attach_footprint(self, replace: bool = False):
        """Дозагрузить (или заменить) посадочное место у компонента."""
        if not self.current:
            QMessageBox.information(self, "Посадка",
                                    "Сначала выберите компонент в списке")
            return
        from .dialogs import KicadDialog
        d = KicadDialog(self.svc, self)
        d.setWindowTitle("Заменить посадочное место" if replace
                         else "Дозагрузить посадочное место")
        # символ здесь не нужен -- берём только посадку
        d.sym_edit.setEnabled(False)
        d.sym_list.setEnabled(False)
        name = (self.current.params.get("KiCadFootprint", "")
                or self.current.name)
        d.fp_edit.setText(name)
        if d.exec() != QDialog.Accepted:
            return
        v = d.values()
        try:
            c = self.svc.attach_footprint(self.current.uid,
                                          fp_path=v.get("fp_path", ""),
                                          fp_ref=v.get("fp_ref", ""),
                                          replace=replace)
        except Exception as e:                              # noqa: BLE001
            QMessageBox.warning(self, "Посадка не добавлена", str(e))
            return
        if c:
            self.current = c
            self._fill_footprints(c)
            self.statusBar().showMessage(
                "Посадка заменена" if replace else "Посадка добавлена", 6000)

    def do_toggle_paste(self, on: bool):
        """Галочка «наносить пасту» — сразу у всего компонента."""
        if not self.current or not self.current.footprints:
            return
        c = self.svc.set_paste(self.current.uid, bool(on))
        if c:
            self.current = c
            self.statusBar().showMessage(
                "Паста будет наноситься. Пересоберите библиотеку" if on else
                "Окон пасты под этот компонент не будет. Пересоберите "
                "библиотеку", 7000)

    def do_set_paste_grid(self):
        """Сетка окон пасты у крупных площадок текущей посадки."""
        if not self.current or not self.current.footprints:
            return

        def num(w, what, default):
            s = (w.text() or "").strip().replace(",", ".")
            if not s:
                return default, True
            try:
                return float(s), True
            except ValueError:
                QMessageBox.information(self, "Окно пасты",
                                        f"{what} задаётся числом")
                return default, False

        over, ok1 = num(self.paste_over, "Порог", 1.0)
        fill, ok2 = num(self.paste_fill, "Покрытие", 60.0)
        if not (ok1 and ok2):
            return
        n = int(self.paste_grid.currentData() or 0)
        c = self.svc.set_paste_grid(self.current.uid, n, over, fill,
                                    self.current_fp_index())
        if c:
            self.current = c
            self._show_footprint()
            self.statusBar().showMessage(
                "Окно пасты сплошное" if n < 2 else
                f"Сетка {n}×{n} у площадок от {over:g} мм. "
                f"Пересоберите библиотеку", 7000)

    def do_set_height(self):
        if not self.current or not self.current.footprints:
            return
        txt = self.height_edit.text().strip().replace(",", ".")
        try:
            h = float(txt) if txt else 0.0
        except ValueError:
            QMessageBox.information(self, "Высота",
                                    "Высота задаётся числом в миллиметрах")
            return
        c = self.svc.set_height(self.current.uid, h, self.current_fp_index())
        if c:
            self.current = c
            self._show_footprint()
            self.log(f"{c.name}: высота корпуса {h:.2f} мм")

    def _model_moved(self, tr: dict):
        """Кнопки поворота и сдвига в 3D-виде сразу пишутся в компонент."""
        if not self.current or not self.current.footprints:
            return
        c = self.svc.set_model_transform(self.current.uid,
                                         self.current_fp_index(), **tr)
        if c:
            self.current = c

    def _model_color_changed(self, color: str):
        """Палитра 3D-вида сразу сохраняется у выбранной модели."""
        if not self.current or not self.current.footprints:
            return
        c = self.svc.set_model_color(self.current.uid, color,
                                     self.current_fp_index())
        if c:
            self.current = c

    def do_set_tape(self):
        if not self.current or not self.current.footprints:
            return
        txt = self.tape_edit.text().strip().replace(",", ".") or "0"
        try:
            a = float(txt)
        except ValueError:
            QMessageBox.information(self, "Угол в ленте",
                                    "Угол задаётся числом в градусах")
            return
        c = self.svc.set_tape_rotation(self.current.uid, a,
                                       self.current_fp_index())
        if c:
            self.current = c
            self._show_footprint()

    def show_script_log(self):
        """Показать журнал последнего запуска скрипта в Altium."""
        d = self.cfg.jobs_dir
        try:
            logs = sorted((os.path.join(d, f) for f in os.listdir(d)
                           if f.endswith(".log")), key=os.path.getmtime)
        except OSError:
            logs = []
        if not logs:
            self.log("Журналов пока нет — скрипт в Altium ещё не запускался.")
            self.tabs.setCurrentWidget(self.logbox)
            return
        t = self.svc.read_job_log(logs[-1])
        self.log(f"--- {os.path.basename(logs[-1])} ---")
        self.log(t or "(журнал пуст)")
        self.tabs.setCurrentWidget(self.logbox)

    def _fill_params(self, c: Component):
        schema = classify.params_for(c.ctype)
        keys = [p["key"] for p in schema]
        for k in c.params:
            if k not in keys:
                keys.append(k)
                schema.append({"key": k, "label": k, "unit": "", "type": "str",
                               "options": []})
        self.params.setRowCount(len(schema))
        for i, p in enumerate(schema):
            it = QTableWidgetItem(p["label"])
            it.setData(Qt.UserRole, p["key"])
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.params.setItem(i, 0, it)
            val = c.params.get(p["key"], "")
            if p["type"] in ("enum", "multi") and p["options"]:
                cb = QComboBox()
                cb.setEditable(True)
                cb.addItem("")
                cb.addItems(p["options"])
                cb.setCurrentText(str(val))
                self.params.setCellWidget(i, 1, cb)
            else:
                self.params.setCellWidget(i, 1, None)
                self.params.setItem(i, 1, QTableWidgetItem(str(val)))
            meta = (getattr(c, "param_meta", None) or {}).get(p["key"]) or {}
            # Единица у своих параметров правится, у справочных -- нет:
            # там она часть описания типа компонента.
            own = bool(meta) or p["key"] not in {q["key"] for q
                                                 in classify.params_for(c.ctype)}
            u = QTableWidgetItem(str(meta.get("unit", "") or p["unit"]))
            if not own:
                u.setFlags(u.flags() & ~Qt.ItemIsEditable)
            self.params.setItem(i, 2, u)
            tb = QComboBox()
            for code, ru in self.svc.PARAM_TYPES.items():
                tb.addItem(ru, code)
            ti = tb.findData(str(meta.get("type", "str") or "str"))
            tb.setCurrentIndex(ti if ti >= 0 else 0)
            tb.setToolTip("Тип нужен для проверки значения: число не "
                          "должно быть с запятой, ссылка — начинаться "
                          "с http:// или быть путём к файлу")
            self.params.setCellWidget(i, 3, tb)
            vb = QCheckBox()
            vb.setChecked(bool(meta.get("visible")))
            vb.setToolTip(
                "Показывать значение на листе схемы.\n"
                "По умолчанию выключено: видимый параметр садится в начало "
                "координат компонента\nи двигать его нечем — видимой должна "
                "быть одна подпись, Comment.")
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.addWidget(vb)
            hl.setAlignment(Qt.AlignCenter)
            holder.cb = vb
            self.params.setCellWidget(i, 4, holder)
        self._desig_manual = False
        self.type_box.blockSignals(True)
        idx = self.type_box.findData(c.ctype)
        if idx >= 0:
            self.type_box.setCurrentIndex(idx)
        self.type_box.blockSignals(False)
        self.desig_edit.setText(c.designator)

    def _fill_pins(self, c: Component):
        pins = c.raw_pins or c.symbol.pins
        cur = {p.number: p for p in c.symbol.pins}
        self.pins.setRowCount(len(pins))
        for i, p in enumerate(pins):
            self.pins.setItem(i, 0, QTableWidgetItem(p.number))
            self.pins.setItem(i, 1, QTableWidgetItem(p.name))
            cb = QComboBox()
            cb.addItems(ETYPES)
            cb.setCurrentText(p.etype)
            self.pins.setCellWidget(i, 2, cb)
            unit = QSpinBox()
            unit.setRange(1, 26)
            unit.setValue(max(1, int((cur.get(p.number) or p).unit or 1)))
            self.pins.setCellWidget(i, 3, unit)
            sb = QComboBox()
            sb.addItems(["авто", "L", "R", "T", "B"])
            live = cur.get(p.number)
            manual = bool(live and (live.group or "").startswith("!"))
            sb.setCurrentText(live.side if (manual and live) else "авто")
            self.pins.setCellWidget(i, 4, sb)
            grp = ""
            if manual and live:
                grp = (live.group or "")[1:]
            self.pins.setItem(i, 5, QTableWidgetItem(grp))
        self._reset_pin_visual_order()
        self.pins.resizeColumnsToContents()

    # ------------------------------------------------------------ действия --
    def _run(self, fn, *args):
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, "GostLib", "Дождитесь окончания импорта")
            return
        self._worker = ImportWorker(fn, *args)
        self._worker.msg.connect(self.log)
        self._worker.done.connect(self._import_done)
        # логи сервиса во время импорта идут через сигнал: трогать виджеты
        # из фонового потока нельзя
        self.svc.log = self._worker.msg.emit
        self.statusBar().showMessage("Импорт…")
        self._worker.start()

    def _import_done(self, comps, err):
        self.svc.log = self.log
        if err:
            self.log("Ошибка импорта: " + err)
            QMessageBox.warning(self, "Импорт", err)
        else:
            pr = self.svc.active_project()
            if pr is not None and getattr(self.cfg, "import_to_project", True):
                self.log(f"Импортировано: {len(comps)} "
                         f"→ в проект «{pr['name']}»")
            else:
                self.log(f"Импортировано: {len(comps)}")
        self.refresh_types()
        self.refresh_table()
        self.refresh_projects()
        self.refresh_proj_tree()

    def do_import_archive(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Архив или библиотека компонента", "",
            "Компоненты (*.zip *.SchLib *.PcbLib *.kicad_sym *.kicad_mod "
            "*.step *.stp);;Все файлы (*.*)")
        if not path:
            d = QFileDialog.getExistingDirectory(self, "…или выберите папку")
            if not d:
                return
            path = d
        self._run(self.svc.import_archive, path)

    def do_import_kicad(self):
        dlg = KicadDialog(self.svc, self)
        if dlg.exec():
            v = dlg.values()
            if not any(v.values()):
                return
            self._run(lambda: self.svc.import_kicad(**v))

    def do_import_lcsc(self):
        dlg = LcscDialog(self)
        if dlg.exec():
            codes = dlg.codes()
            if not codes:
                return

            def job():
                out = []
                for code in codes:
                    try:
                        out += self.svc.import_lcsc(code)
                    except Exception as e:
                        # job исполняется в QThread. Прямой вызов self.log
                        # трогал QTextEdit не из GUI-потока и иногда валил
                        # приложение после пакетного импорта.
                        self.svc.log(f"{code}: {e}")
                return out
            self._run(job)

    def do_edit_layout(self):
        if not self.current:
            QMessageBox.information(self, "GostLib",
                                    "Сначала выберите компонент в списке")
            return
        from .symedit import SymbolEditor
        dlg = SymbolEditor(self.current, self.svc.style(), self)
        if not dlg.exec():
            return
        manual, sym = dlg.result_symbol()
        c = (self.svc.apply_layout(self.current.uid, sym) if manual
             else self.svc.reset_layout(self.current.uid))
        if c:
            self.current = c
            self.refresh_table()
            self.on_select()

    def do_rebuild(self):
        uids = self.selected_uids() or self.svc.db.all_uids()
        n = 0
        for u in uids:
            c = self.svc.db.get(u)
            if not c:
                continue
            self.svc.rebuild_symbol(c)
            self.svc.db.upsert(c)
            n += 1
        self.log(f"Пересобрано символов: {n}")
        self.refresh_table()
        self.on_select()

    def do_delete(self):
        uids = self.selected_uids()
        if not uids:
            return
        if QMessageBox.question(self, "Удаление",
                                f"Удалить из каталога: {len(uids)}?") \
                != QMessageBox.Yes:
            return
        self.svc.delete(uids)
        self.statusBar().showMessage(
            f"Удалено: {len(uids)}. Ctrl+Z вернёт.", 6000)
        self.refresh_types()
        self.refresh_table()
        self.refresh_proj_tree()

    # --- три пути конвертации ------------------------------------------------
    # kicad / eagle: текстовый формат отдаём штатному мастеру Altium, и он сам
    # делает двоичные библиотеки. binary: пишем .SchLib/.PcbLib сами.
    def do_convert(self, kind: str, only: bool = False):
        # Основная сборка идёт по ЦЕЛИ (проект или общая библиотека), а не
        # по выделению: иначе в библиотеку проекта попадало то, что просто
        # оказалось выделенным, а при пустом выделении -- весь каталог.
        uids: List[str] = []
        if kind != "script" or only:
            uids = self.selected_uids() or (
                [] if only else self.svc.db.all_uids())
            if not uids:
                QMessageBox.information(
                    self, "GostLib",
                    "Ничего не выделено." if only else "Каталог пуст")
                return
        try:
            res = self.svc.build_variant(kind, uids, only=only)
        except Exception as e:
            QMessageBox.critical(self, "GostLib", str(e))
            self.log(f"ОШИБКА: {e}")
            return
        self.refresh_table()
        if kind == "script":
            self._script_report(res, autorun=bool(getattr(self, "_autorun",
                                                          False)))
            return
        if kind == "binary":
            self._binary_report(res)
            return
        title = self.svc.VARIANTS[kind]
        lines = [f"Готово: {title}", "",
                 f"Компонентов: {res.get('components','?')}   "
                 f"посадок: {res.get('footprints','?')}", "",
                 f"Файл: {res['path']}"]
        if res.get("pretty"):
            lines.append(f"Посадки: {res['pretty']}")
        lines += ["", res.get("hint", "")]
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText("\n".join(lines))
        openb = box.addButton("Открыть папку", QMessageBox.ActionRole)
        box.addButton("Закрыть", QMessageBox.AcceptRole)
        box.exec()
        self.log("\n".join(lines))
        if box.clickedButton() is openb:
            self._open(res["dir"])

    def do_build(self):
        self.do_convert("script")

    def do_build_selected(self):
        """Разовая сборка только выделенного, мимо состава проекта."""
        self.do_convert("script", only=True)

    def _script_report(self, res, autorun: bool = False):
        """Показать окно сборки. Оно же запускает Altium и ждёт отчёт."""
        d = BuildDialog(self.svc, res, autorun, self)
        d.exec()

    def _binary_report(self, res):
        lines = [f"Готово. Компонентов в библиотеке: {res['components']}",
                 "",
                 f"Символы:  {res['schlib']}"]
        if res.get("pcblib"):
            lines.append(f"Посадки:  {res['pcblib']}  ({res['footprints']} шт.)")
        if res.get("vendor"):
            lines.append(f"Вендорских библиотек посадок: "
                         f"{len(res['vendor'].split(';'))} (папка vendor)")
        lines.append("")
        if int(res.get("pending", "0")):
            lines.append(f"ВНИМАНИЕ: {res['pending']} файл(ов) занято Altium — "
                         "новые версии лежат рядом как .new.")
        lines += ["В Altium: DXP → Run Script… → GostLibBuilder → RunGostLib",
                  "Скрипт закроет открытые копии, подменит занятые файлы",
                  "и подключит библиотеки. Больше он ничего не делает."]
        box = QMessageBox(self)
        box.setWindowTitle("Собрать в Altium")
        box.setText("\n".join(lines))
        openb = box.addButton("Открыть папку библиотеки", QMessageBox.ActionRole)
        logb = box.addButton("Показать журнал Altium", QMessageBox.ActionRole)
        box.addButton("Закрыть", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is openb:
            self._open(os.path.dirname(res["schlib"]))
        elif box.clickedButton() is logb:
            t = self.svc.read_job_log(res.get("log", ""))
            self.log(t or "Скрипт ещё не запускался — журнала нет.")
            self.tabs.setCurrentWidget(self.logbox)

    def _open(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)          # noqa
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self.log(str(e))

    def do_settings(self):
        dlg = SettingsDialog(self.cfg, self, tools=self.service_actions())
        if dlg.exec():
            self.cfg = dlg.apply()
            self.svc.cfg = self.cfg
            act = getattr(self, "_act_build", None)
            if act is not None:
                act.setVisible(bool(getattr(self.cfg, "show_job_button",
                                            False)))
            if dlg.chosen is not None:
                # служебное действие из вкладки «Сервис»: окно настроек уже
                # закрыто, и действие может открывать свои окна
                dlg.chosen()
            else:
                self.log("Настройки сохранены. Пересоберите символы (F5), "
                         "чтобы применить стиль.")

    def _type_box_changed(self):
        if not self.current:
            return
        new = self.type_box.currentData()
        if not new:
            return
        if self._desig_manual and not self.svc.is_default_designator(
                self.desig_edit.text()):
            return
        want = classify.designator_for(new)
        if want != self.desig_edit.text():
            self.desig_edit.setText(want)

    def do_add_param(self):
        """Добавить строку под свой параметр."""
        if not self.current:
            return
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "Свой параметр",
            "Имя параметра (так он и будет называться в Altium):")
        name = (name or "").strip()
        if not ok or not name:
            return
        if any((self.params.item(i, 0).data(Qt.UserRole) or "").lower()
               == name.lower() for i in range(self.params.rowCount())):
            QMessageBox.information(self, "Свой параметр",
                                    "Такой параметр уже есть в таблице")
            return
        i = self.params.rowCount()
        self.params.insertRow(i)
        it = QTableWidgetItem(name)
        it.setData(Qt.UserRole, name)
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
        self.params.setItem(i, 0, it)
        self.params.setItem(i, 1, QTableWidgetItem(""))
        self.params.setItem(i, 2, QTableWidgetItem(""))
        tb = QComboBox()
        for code, ru in self.svc.PARAM_TYPES.items():
            tb.addItem(ru, code)
        self.params.setCellWidget(i, 3, tb)
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        cb = QCheckBox()
        hl.addWidget(cb)
        hl.setAlignment(Qt.AlignCenter)
        holder.cb = cb
        self.params.setCellWidget(i, 4, holder)
        self.params.setCurrentCell(i, 1)
        self.params.editItem(self.params.item(i, 1))

    def do_del_param(self):
        i = self.params.currentRow()
        if i < 0:
            return
        self.params.removeRow(i)

    def _param_rows(self):
        """Снять таблицу параметров: значения и их описание."""
        params: Dict[str, str] = {}
        meta: Dict[str, dict] = {}
        for i in range(self.params.rowCount()):
            key = self.params.item(i, 0).data(Qt.UserRole)
            w = self.params.cellWidget(i, 1)
            val = w.currentText() if isinstance(w, QComboBox) else (
                self.params.item(i, 1).text() if self.params.item(i, 1) else "")
            if not str(val).strip():
                continue
            params[key] = str(val).strip()
            tb = self.params.cellWidget(i, 3)
            holder = self.params.cellWidget(i, 4)
            unit = (self.params.item(i, 2).text().strip()
                    if self.params.item(i, 2) else "")
            kind = tb.currentData() if isinstance(tb, QComboBox) else "str"
            vis = bool(getattr(holder, "cb", None)
                       and holder.cb.isChecked())
            if unit or vis or (kind and kind != "str"):
                meta[key] = {"type": kind or "str", "unit": unit,
                             "visible": 1 if vis else 0}
        return params, meta

    def save_params(self):
        if not self.current:
            return
        params, meta = self._param_rows()
        bad = [f"{k}: {msg}" for k, v in params.items()
               if (msg := self.svc.check_param(
                   v, (meta.get(k) or {}).get("type", "str")))]
        if bad:
            if QMessageBox.question(
                    self, "Проверьте значения",
                    "Похоже на опечатки:\n\n" + "\n".join(bad[:8])
                    + "\n\nСохранить как есть?") != QMessageBox.Yes:
                return
        c = self.svc.apply_params(self.current.uid, params,
                                  self.type_box.currentData(),
                                  self.desig_edit.text().strip(),
                                  designator_manual=self._desig_manual,
                                  meta=meta)
        if c:
            self.current = c
            self.log(f"{c.name}: параметры сохранены")
            self.refresh_types()
            self.refresh_table()
            self.on_select()

    def _pin_row(self, i: int) -> dict:
        """Снять строку таблицы выводов целиком, вместе с виджетами."""
        return {
            "number": self.pins.item(i, 0).text() if self.pins.item(i, 0) else "",
            "name": self.pins.item(i, 1).text() if self.pins.item(i, 1) else "",
            "etype": self.pins.cellWidget(i, 2).currentText(),
            "unit": self.pins.cellWidget(i, 3).value(),
            "side": self.pins.cellWidget(i, 4).currentText(),
            "group": self.pins.item(i, 5).text() if self.pins.item(i, 5) else "",
        }

    def _all_pin_rows(self) -> List[dict]:
        """Строки в том порядке, в каком они видны -- с учётом перетаскивания."""
        vh = self.pins.verticalHeader()
        return [self._pin_row(vh.logicalIndex(v))
                for v in range(self.pins.rowCount())]

    def _reset_pin_visual_order(self):
        """
        Вернуть строкам порядок «визуальный = логический».

        Здесь была тонкая, но злая ошибка: `moveSection()` принимает
        ВИЗУАЛЬНЫЕ позиции, а ей передавался логический индекс. После
        перетаскивания строк отображение визуальных и логических номеров
        разъезжалось, `_all_pin_rows()` читала перестановку, не совпадающую
        с тем, что на экране, -- и часть выводов уезжала не туда. Со
        второго раза срабатывало потому, что таблица успевала
        перезаполниться и отображение снова становилось единичным.
        """
        vh = self.pins.verticalHeader()
        for logical in range(self.pins.rowCount()):
            v = vh.visualIndex(logical)
            if v != logical and v >= 0:
                vh.moveSection(v, logical)

    def _set_pin_rows(self, rows: List[dict], select: List[int] = ()):
        """Перезаполнить таблицу выводов из списка строк."""
        self.pins.setRowCount(len(rows))
        self._reset_pin_visual_order()
        for i, r in enumerate(rows):
            self.pins.setItem(i, 0, QTableWidgetItem(r["number"]))
            self.pins.setItem(i, 1, QTableWidgetItem(r["name"]))
            cb = QComboBox()
            cb.addItems(ETYPES)
            cb.setCurrentText(r["etype"])
            self.pins.setCellWidget(i, 2, cb)
            unit = QSpinBox()
            unit.setRange(1, 26)
            unit.setValue(max(1, int(r.get("unit", 1) or 1)))
            self.pins.setCellWidget(i, 3, unit)
            sb = QComboBox()
            sb.addItems(["авто", "L", "R", "T", "B"])
            sb.setCurrentText(r["side"] or "авто")
            self.pins.setCellWidget(i, 4, sb)
            self.pins.setItem(i, 5, QTableWidgetItem(r["group"]))
        self.pins.clearSelection()
        for i in select:
            if 0 <= i < len(rows):
                self.pins.selectRow(i)

    def _move_pin_rows(self, delta: int):
        """Сдвинуть выделенные выводы на строку вверх или вниз."""
        rows = self._all_pin_rows()
        # rows идут в ВИЗУАЛЬНОМ порядке, а selectedIndexes() отдаёт
        # логические номера строк -- сравнивать их напрямую нельзя.
        vh = self.pins.verticalHeader()
        sel = sorted({vh.visualIndex(ix.row())
                      for ix in self.pins.selectedIndexes()
                      if vh.visualIndex(ix.row()) >= 0})
        if not sel or not rows:
            return
        if delta < 0 and sel[0] == 0:
            return
        if delta > 0 and sel[-1] == len(rows) - 1:
            return
        for i in (sel if delta < 0 else reversed(sel)):
            rows[i], rows[i + delta] = rows[i + delta], rows[i]
        self._set_pin_rows(rows, [i + delta for i in sel])

    def _sort_pin_rows(self):
        def key(r):
            n = r["number"]
            m = __import__("re").match(r"^([A-Za-z]*)(\d+)$", n.strip())
            return (m.group(1), int(m.group(2))) if m else (n, 0)
        self._set_pin_rows(sorted(self._all_pin_rows(), key=key))

    def save_pins(self):
        if not self.current:
            return
        # Незакрытый редактор ячейки иначе теряет только что набранное:
        # значение ещё в виджете, а мы читаем модель.
        self.pins.setCurrentCell(-1, -1)
        rows = self._all_pin_rows()
        c = self.svc.apply_pins(self.current.uid, rows)
        if c:
            self.current = c
            order = ", ".join(r["number"] for r in rows[:12])
            self.log(f"{c.name}: раскладка применена — порядок {order}"
                     + (" …" if len(rows) > 12 else ""))
            self.on_select()

    def ai_pin_plan(self):
        """Открыть текстовый обмен раскладкой с внешней ИИ."""
        if not self.current:
            return
        dialog = PinPlanDialog(self.current, self)
        if dialog.exec() != QDialog.Accepted or dialog.rows is None:
            return
        c = self.svc.apply_pins(self.current.uid, dialog.rows,
                                reset_geometry=True)
        if c:
            self.current = c
            sections = max([int(r.get("unit", 1)) for r in dialog.rows] or [1])
            self.log(f"{c.name}: ИИ-раскладка применена — "
                     f"выводов {len(dialog.rows)}, секций {sections}")
            self.on_select()

    def tidy_pins(self):
        """
        Привести раскладку в порядок без всякого ИИ.

        Берём то, что сейчас в таблице выводов, и раскладываем по тем же
        правилам, что применяются после ответа модели: питание и земля --
        в свои секции, стороны выровнены, длинные столбики разбиты.
        """
        if not self.current:
            return
        from ..pinplan import tidy
        rows = self._all_pin_rows()
        if not rows:
            return
        rows, notes = tidy(rows)
        c = self.svc.apply_pins(self.current.uid, rows, reset_geometry=True)
        if not c:
            return
        self.current = c
        sections = max([int(r.get("unit", 1)) for r in rows] or [1])
        self.log(f"{c.name}: раскладка приведена в порядок — "
                 f"выводов {len(rows)}, секций {sections}")
        for n in notes:
            self.log(f"  {n}")
        self.on_select()
        QMessageBox.information(
            self, "Раскладка приведена в порядок",
            ("Что поправлено:\n\n  • " + "\n  • ".join(notes[:10]))
            if notes else "Всё и так было в порядке — менять нечего.")

    def reset_pins(self):
        if not self.current:
            return
        c = self.svc.reset_layout(self.current.uid)
        if c:
            self.current = c
            self.statusBar().showMessage(
                "Раскладка снова автоматическая. Ctrl+Z вернёт", 5000)
        self.on_select()

    def _set_type(self, code: str):
        uids = self.selected_uids()
        if not uids:
            return
        with self.svc.undo.step(f"Назначить тип: {len(uids)} шт."):
            n = self.svc.set_type(uids, code)
        self.log(f"Тип изменён у {n} шт. (Ctrl+Z вернёт)")
        self.refresh_types()
        self.refresh_table()
        self.on_select()

    # ------------------------------------------------------------- вид ------
    def _toggle_panel(self):
        """Спрятать правую панель: остаётся чистая база для поиска."""
        self.tabs.setVisible(self.act_panel.isChecked())

    def _toggle_tree(self):
        self.tree_dock.setVisible(self.act_tree.isChecked())

    def _set_theme(self, name: str):
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(LIGHT_QSS if name == "light" else DARK_QSS)
        self.cfg.theme = name
        try:
            self.cfg.save()
        except Exception:
            pass
        self.log(f"Тема: {'светлая' if name == 'light' else 'тёмная'}")

    def _rebuild_cols_menu(self):
        """Меню «Колонки таблицы»: базовые плюс параметры из каталога."""
        self.cols_menu.clear()
        extra = self.svc.db.param_keys()
        for i, name in enumerate(COLS[:-1]):
            a = self.cols_menu.addAction(name)
            a.setCheckable(True)
            a.setChecked(not self.table.isColumnHidden(i))
            a.triggered.connect(
                lambda checked=False, col=i: self.table.setColumnHidden(
                    col, not checked))
        if extra:
            self.cols_menu.addSeparator()
            sub = self.cols_menu.addMenu("Параметр как колонка")
            for key in extra[:60]:
                a = sub.addAction(key)
                a.setCheckable(True)
                a.setChecked(key in self.extra_cols)
                a.triggered.connect(
                    lambda checked=False, k=key: self._toggle_extra_col(k))

    def _toggle_extra_col(self, key: str):
        if key in self.extra_cols:
            self.extra_cols.remove(key)
        else:
            self.extra_cols.append(key)
        self.refresh_table()
        self._rebuild_cols_menu()
        self.cfg.table_params = list(self.extra_cols)
        try:
            self.cfg.save()
        except Exception:
            pass

    def _header_menu(self, pos):
        self._rebuild_cols_menu()
        self.cols_menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def _fix_designators(self):
        """Проставить позиционное обозначение по типу у выделенных."""
        n = 0
        for u in self.selected_uids():
            c = self.svc.db.get(u)
            if not c:
                continue
            want = classify.designator_for(c.ctype)
            if c.designator != want:
                c.designator = want
                self.svc.db.upsert(c)
                n += 1
        self.log(f"Обозначение приведено к типу: {n} шт.")
        self.refresh_table()
        self.on_select()

    def _ctx_menu(self, pos):
        m = QMenu(self)
        tm = m.addMenu("Назначить тип")
        groups = {
            "Пассивные": ["resistor", "resistor_var", "resistor_net", "capacitor",
                          "capacitor_pol", "inductor", "ferrite", "transformer",
                          "varistor", "fuse"],
            "Полупроводники": ["diode", "zener", "schottky", "tvs", "led",
                               "bridge", "bjt", "mosfet", "igbt", "thyristor",
                               "opto"],
            "Микросхемы": ["ic", "mcu", "memory", "logic", "opamp", "comparator",
                           "adc_dac", "ldo", "dcdc", "driver", "sensor", "module"],
            "Электромеханика": ["connector", "switch", "button", "relay",
                                "battery", "buzzer", "motor", "display"],
            "Прочее": ["crystal", "oscillator", "antenna", "rf", "testpoint",
                       "mount", "other"],
        }
        for title, codes in groups.items():
            sub = tm.addMenu(title)
            for code in codes:
                name = classify.CTYPE_NAME.get(code, code)
                pref = classify.CTYPE_PREFIX.get(code, "U")
                sub.addAction(f"{name}  ({pref})",
                              lambda checked=False, c=code: self._set_type(c))
        m.addSeparator()
        m.addAction("Пересобрать символ", self.do_rebuild)
        m.addAction("Расположение выводов (F4)", self.do_edit_layout)
        m.addAction("Привести обозначение к типу", self._fix_designators)
        m.addAction("Перечитать УГО из источника", self.do_refresh_native)
        m.addSeparator()
        m.addAction("Переименовать… (F2)", self.do_rename)
        m.addAction("Имя по типу и номиналу", self.do_autoname)
        m.addAction("Собрать в Altium (F9)", lambda: self.do_convert("script"))
        a_only = m.addAction("Собрать ТОЛЬКО выделенное", self.do_build_selected)
        a_only.setToolTip("Разовая сборка мимо состава проекта: в библиотеку "
                          "уйдут ровно выделенные компоненты")
        cm = m.addMenu("Запасные пути")
        cm.addAction("Через KiCad-импортёр", lambda: self.do_convert("kicad"))
        cm.addAction("Через EAGLE (.lbr)", lambda: self.do_convert("eagle"))
        cm.addAction("Прямая запись .SchLib/.PcbLib",
                     lambda: self.do_convert("binary"))
        m.addSeparator()
        m.addAction("Экспорт в проект KiCad…", self.do_export_kicad_project)
        m.addAction("Сохранить 3D-модель…", self.do_save_model)
        m.addSeparator()
        pm = m.addMenu("Проект")
        pm.addAction("Добавить в текущий проект", self._proj_add_sel)
        pm.addAction("Убрать из текущего проекта", self._proj_del_sel)
        pm.addSeparator()
        pm.addAction("Взять из общего каталога…", self.do_pick_from_catalog)
        m.addSeparator()
        m.addAction("Пометить как «не в библиотеке»",
                    lambda: (self.svc.db.set_in_library(self.selected_uids(), False),
                             self.refresh_table()))
        m.addAction("Удалить", self.do_delete)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def closeEvent(self, e):
        self._queued_model = None
        for loader in tuple(getattr(self, "_model_loaders", ())):
            loader.stale = True
            loader.requestInterruption()
        worker = getattr(self, "_worker", None)
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            worker.wait(3000)
        try:
            # Базу нельзя закрывать под ещё работающим импортом.
            if worker is None or not worker.isRunning():
                self.svc.close()
        except Exception:
            pass
        e.accept()
