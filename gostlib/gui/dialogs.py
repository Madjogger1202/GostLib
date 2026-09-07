"""Диалоги: импорт из KiCad, импорт по LCSC, настройки."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QMessageBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QDoubleSpinBox, QProgressBar, QPushButton, QSpinBox,
                               QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import config

MM = 0.0254          # 1 mil в миллиметрах


class LcscDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Импорт из EasyEDA / LCSC")
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("C2040, C25804, ... (можно несколько через пробел)")
        f = QFormLayout()
        f.addRow("Код LCSC:", self.edit)
        hint = QLabel("Берётся символ, посадочное место и 3D-модель.\n"
                      "Символ перерисовывается по ГОСТ, посадка переносится как есть.")
        hint.setStyleSheet("color:#888;")
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(f)
        lay.addWidget(hint)
        lay.addWidget(bb)

    def codes(self):
        return [c.strip().upper() for c in self.edit.text().replace(",", " ").split()
                if c.strip()]


class IndexWorker(QThread):
    """Индексация библиотек KiCad в фоне -- иначе окно замирает."""
    tick = Signal(int, int, str)
    msg = Signal(str)
    done = Signal(dict, str)

    def __init__(self, svc):
        super().__init__()
        self.svc = svc

    def run(self):
        old = self.svc.log
        # трогать виджеты из фонового потока нельзя -- логи идут сигналом
        self.svc.log = self.msg.emit
        try:
            st = self.svc.index_kicad(
                lambda d, t, txt: self.tick.emit(d, t, txt))
            self.done.emit(st, "")
        except Exception as e:
            self.done.emit({}, str(e))
        finally:
            self.svc.log = old


def _make_hit_tree() -> QTreeWidget:
    """
    Список найденного: слева «библиотека:имя», справа путь к файлу.

    Путь нужен, когда на машине несколько версий KiCad: одно и то же имя
    находится по разу на версию, и различить их можно только путём.
    """
    t = QTreeWidget()
    t.setColumnCount(2)
    t.setHeaderLabels(["Библиотека и имя", "Файл"])
    t.setRootIsDecorated(False)
    t.setUniformRowHeights(True)
    t.setAlternatingRowColors(True)
    t.setTextElideMode(Qt.ElideLeft)          # у пути важен хвост
    t.header().setStretchLastSection(True)
    t.setColumnWidth(0, 300)
    return t


class KicadDialog(QDialog):
    """
    Поиск символа и посадки в установленных библиотеках KiCad.
    Поиск идёт по индексу в базе, поэтому мгновенный; сам индекс строится
    отдельной кнопкой в фоновом потоке.
    """

    def __init__(self, svc, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.worker = None
        self.setWindowTitle("Импорт из KiCad")

        self.sym_edit = QLineEdit()
        self.sym_edit.setPlaceholderText("RP2040  или  MCU_RaspberryPi:RP2040")
        self.fp_edit = QLineEdit()
        self.fp_edit.setPlaceholderText("QFN-56  или  Package_DFN_QFN:QFN-56...")
        self.sym_list = _make_hit_tree()
        self.fp_list = _make_hit_tree()
        self.sym_edit.textChanged.connect(self.find_sym)
        self.fp_edit.textChanged.connect(self.find_fp)
        self.sym_list.itemSelectionChanged.connect(self._sym_picked)
        self.fp_list.itemSelectionChanged.connect(self._fp_picked)
        self.sym_list.itemDoubleClicked.connect(lambda *_: self.accept())

        self.info = QLabel()
        self.info.setStyleSheet("color:#888;")
        self.reindex = QPushButton("Обновить индекс KiCad")
        self.reindex.clicked.connect(self.do_index)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setTextVisible(True)

        lay = QVBoxLayout(self)
        hi = QHBoxLayout()
        hi.addWidget(self.info, 1)
        hi.addWidget(self.reindex)
        lay.addLayout(hi)
        lay.addWidget(self.progress)

        g1 = QGroupBox("Схемный символ")
        v1 = QVBoxLayout(g1)
        v1.addWidget(self.sym_edit)
        v1.addWidget(self.sym_list)
        g2 = QGroupBox("Посадочное место")
        v2 = QVBoxLayout(g2)
        v2.addWidget(self.fp_edit)
        v2.addWidget(self.fp_list)
        lay.addWidget(g1)
        lay.addWidget(g2)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(820, 660)

        self._pick_sym = None
        self._pick_fp = None
        self._update_info()
        # первый запуск: индекса ещё нет -- строим сразу, не заставляя жать кнопку
        if not any(self.svc.kicad_stats().values()):
            QTimer.singleShot(150, self.do_index)

    # ------------------------------------------------------------ индекс ----
    def _update_info(self):
        st = self.svc.kicad_stats()
        if st["symbols"] or st["footprints"]:
            self.info.setText(f"В индексе: символов {st['symbols']}, "
                              f"посадок {st['footprints']} "
                              f"(библиотек {st['files']})")
        else:
            self.info.setText("Индекс пуст — нажмите «Обновить индекс KiCad»")

    def do_index(self):
        if self.worker and self.worker.isRunning():
            return
        self.reindex.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.worker = IndexWorker(self.svc)
        self.worker.tick.connect(self._tick)
        self.worker.msg.connect(lambda t: self.info.setText(t))
        self.worker.done.connect(self._indexed)
        self.worker.start()

    def _tick(self, done, total, text):
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)
        self.progress.setFormat(f"{text}  ({done}/{total})")

    def _indexed(self, st, err):
        self.progress.setVisible(False)
        self.reindex.setEnabled(True)
        if err:
            self.info.setText("Ошибка индексации: " + err)
            return
        self._update_info()
        if not any(st.values()):
            self.info.setText(
                "Библиотеки KiCad не найдены. Проверьте, что KiCad установлен "
                "в Program Files, либо задайте переменную окружения "
                "KICAD10_SYMBOL_DIR на папку symbols.")
        self.find_sym()
        self.find_fp()

    # ------------------------------------------------------------- поиск ----
    def find_sym(self):
        self.sym_list.clear()
        self._pick_sym = None
        q = self.sym_edit.text().strip()
        if not q:
            return
        for r in self.svc.kicad_find("sym", q, 300):
            it = QTreeWidgetItem([f"{r['nickname']}:{r['name']}", r["path"]])
            it.setData(0, Qt.UserRole, (r["path"], r["name"]))
            it.setToolTip(1, r["path"])
            self.sym_list.addTopLevelItem(it)

    def find_fp(self):
        self.fp_list.clear()
        self._pick_fp = None
        q = self.fp_edit.text().strip()
        if not q:
            return
        for r in self.svc.kicad_find("fp", q, 300):
            p = r["path"]
            if os.path.isdir(p):
                p = os.path.join(p, r["name"] + ".kicad_mod")
            it = QTreeWidgetItem([f"{r['nickname']}:{r['name']}", p])
            it.setData(0, Qt.UserRole, p)
            it.setToolTip(1, p)
            self.fp_list.addTopLevelItem(it)

    def _sym_picked(self):
        it = self.sym_list.currentItem()
        self._pick_sym = it.data(0, Qt.UserRole) if it else None
        # если у символа прописана посадка -- сразу подставим её в поиск
        if self._pick_sym and not self.fp_edit.text().strip():
            try:
                from ..sources import kicad as kc
                fp = kc.symbol_footprint(self._pick_sym[0], self._pick_sym[1])
                if fp:
                    self.fp_edit.setText(fp)
            except Exception:
                pass

    def _fp_picked(self):
        it = self.fp_list.currentItem()
        self._pick_fp = it.data(0, Qt.UserRole) if it else None

    def values(self) -> dict:
        sym_path, sym_name = self._pick_sym or ("", "")
        return {
            "sym_ref": self.sym_edit.text().strip() if not sym_path else "",
            "fp_ref": self.fp_edit.text().strip() if not self._pick_fp else "",
            "sym_path": sym_path,
            "sym_name": sym_name,
            "fp_path": self._pick_fp or "",
        }


class ProjectsDialog(QDialog):
    """
    Проекты и их библиотеки.

    Каталог компонентов один на всё; проект -- это список ссылок на него
    плюс папка, куда собирать .SchLib/.PcbLib. Поэтому один и тот же
    резистор может стоять в пяти проектах, а правится в одном месте.
    """

    def __init__(self, svc, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.setWindowTitle("Проекты и их библиотеки")
        self.resize(860, 480)

        self.list = QTreeWidget()
        self.list.setColumnCount(4)
        self.list.setHeaderLabels(["Проект", "Компонентов", "Библиотека",
                                   "Папка проекта"])
        self.list.setRootIsDecorated(False)
        self.list.setColumnWidth(0, 180)
        self.list.setColumnWidth(1, 90)
        self.list.setColumnWidth(2, 160)
        self.list.itemSelectionChanged.connect(self._pick)

        self.name = QLineEdit()
        self.path = QLineEdit()
        self.path.setPlaceholderText("папка проекта Altium (где .PrjPcb)")
        br = QPushButton("…")
        br.setFixedWidth(30)
        br.clicked.connect(self._browse)
        self.libdir = QLineEdit()
        self.libdir.setPlaceholderText("пусто — <папка проекта>\\Libraries")
        brl = QPushButton("…")
        brl.setFixedWidth(30)
        brl.clicked.connect(self._browse_lib)
        self.libname = QLineEdit()
        self.libname.setPlaceholderText("пусто — по имени проекта")
        self.note = QLineEdit()

        f = QFormLayout()
        f.addRow("Имя проекта:", self.name)
        h1 = QHBoxLayout(); h1.addWidget(self.path, 1); h1.addWidget(br)
        f.addRow("Папка проекта:", self._wrap(h1))
        h2 = QHBoxLayout(); h2.addWidget(self.libdir, 1); h2.addWidget(brl)
        f.addRow("Папка библиотеки:", self._wrap(h2))
        f.addRow("Имя библиотеки:", self.libname)
        f.addRow("Примечание:", self.note)

        self.preview = QLabel("")
        self.preview.setStyleSheet("color:#888;")
        self.preview.setWordWrap(True)
        for w in (self.name, self.path, self.libdir, self.libname):
            w.textChanged.connect(self._preview)

        row = QHBoxLayout()
        b_new = QPushButton("Создать / сохранить")
        b_new.clicked.connect(self._save)
        b_del = QPushButton("Удалить проект")
        b_del.setToolTip("Компоненты останутся в общем каталоге")
        b_del.clicked.connect(self._delete)
        row.addWidget(b_new)
        row.addWidget(b_del)
        row.addStretch(1)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(self.list, 1)
        lay.addLayout(f)
        lay.addWidget(self.preview)
        lay.addLayout(row)
        lay.addWidget(bb)
        self._reload()

    @staticmethod
    def _wrap(layout):
        w = QWidget()
        w.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)
        return w

    def _reload(self):
        self.list.clear()
        for r in self.svc.db.projects():
            lib_dir, lib_name, _ = self.svc.project_library(r)
            it = QTreeWidgetItem([r["name"], str(r["n"]), lib_name + ".SchLib",
                                  r["path"] or "(не задана)"])
            it.setData(0, Qt.UserRole, int(r["id"]))
            it.setToolTip(2, os.path.join(lib_dir, lib_name + ".SchLib"))
            self.list.addTopLevelItem(it)
        self._preview()

    def _pick(self):
        it = self.list.currentItem()
        if not it:
            return
        r = self.svc.db.project(int(it.data(0, Qt.UserRole)))
        if not r:
            return
        self.name.setText(r["name"])
        self.path.setText(r["path"])
        self.libdir.setText(r["lib_dir"])
        self.libname.setText(r["lib_name"])
        self.note.setText(r["note"])
        self._preview()

    def _preview(self):
        """Показать, куда именно лягут файлы — до того, как нажата кнопка."""
        name = self.name.text().strip()
        if not name:
            self.preview.setText("")
            return
        fake = {"name": name, "path": self.path.text().strip(),
                "lib_dir": self.libdir.text().strip(),
                "lib_name": self.libname.text().strip()}
        lib_dir, lib_name, _ = self.svc.project_library(fake)
        self.preview.setText(
            f"Библиотека проекта ляжет сюда:\n"
            f"    {os.path.join(lib_dir, lib_name + '.SchLib')}\n"
            f"    {os.path.join(lib_dir, lib_name + '.PcbLib')}")

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Папка проекта Altium",
                                             self.path.text())
        if d:
            self.path.setText(d)

    def _browse_lib(self):
        d = QFileDialog.getExistingDirectory(self, "Папка библиотеки проекта",
                                             self.libdir.text()
                                             or self.path.text())
        if d:
            self.libdir.setText(d)

    def _save(self):
        name = self.name.text().strip()
        if not name:
            QMessageBox.information(self, "GostLib", "Задайте имя проекта.")
            return
        self.svc.db.add_project(name, self.path.text().strip(),
                                self.libdir.text().strip(),
                                self.libname.text().strip(),
                                self.note.text().strip())
        self._reload()

    def _delete(self):
        it = self.list.currentItem()
        if not it:
            return
        pid = int(it.data(0, Qt.UserRole))
        r = self.svc.db.project(pid)
        if not r:
            return
        if QMessageBox.question(
                self, "GostLib",
                f"Удалить проект «{r['name']}»?\n\n"
                f"Компоненты останутся в общем каталоге, "
                f"собранные файлы библиотеки не трогаются.") \
                != QMessageBox.Yes:
            return
        self.svc.db.delete_project(pid)
        if int(getattr(self.svc.cfg, "active_project", 0) or 0) == pid:
            self.svc.set_active_project(0)
        self._reload()


class SettingsDialog(QDialog):
    def __init__(self, cfg: config.Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Настройки GostLib")

        self.lib_name = QLineEdit(cfg.library_name)
        self.out_dir = QLineEdit(cfg.lib_dir)
        browse = QPushButton("…")
        browse.setFixedWidth(30)
        browse.clicked.connect(self._browse)

        self.font = QLineEdit(cfg.font)
        self.s_pin = QSpinBox(); self.s_pin.setRange(4, 40); self.s_pin.setValue(cfg.size_pin)
        self.s_num = QSpinBox(); self.s_num.setRange(4, 40); self.s_num.setValue(cfg.size_pin_num)
        self.s_des = QSpinBox(); self.s_des.setRange(4, 40); self.s_des.setValue(cfg.size_desig)
        self.s_typ = QSpinBox(); self.s_typ.setRange(4, 40); self.s_typ.setValue(cfg.size_type)
        self.s_tab = QSpinBox(); self.s_tab.setRange(4, 40); self.s_tab.setValue(cfg.size_table)
        # Геометрия задаётся в миллиметрах: сетка ЕСКД -- 2.54 мм, и глазами
        # проще думать про 2.54, чем про 100 mil. Внутри всё по-прежнему
        # в милах, пересчёт тут же.
        self.pin_len = QDoubleSpinBox()
        self.pin_len.setRange(2.54, 25.4)
        self.pin_len.setDecimals(2)
        self.pin_len.setSingleStep(1.27)
        self.pin_len.setSuffix(" мм")
        self.pin_len.setValue(round(cfg.pin_length * MM, 2))
        self.pitch = QDoubleSpinBox()
        self.pitch.setRange(1.27, 10.16)
        self.pitch.setDecimals(2)
        self.pitch.setSingleStep(1.27)
        self.pitch.setSuffix(" мм")
        self.pitch.setValue(round(cfg.pin_pitch * MM, 2))
        self.pitch.setToolTip("Шаг сетки ЕСКД — 2.54 мм.")
        self.auto_pitch = QCheckBox("поднимать шаг под кегль текста")
        self.auto_pitch.setToolTip(
            "Иначе при крупном шрифте подписи наезжают друг на друга. "
            "Снимите, если хотите держать шаг вручную.")
        self.auto_pitch.setChecked(getattr(cfg, "auto_pitch", True))
        self.num_off = QDoubleSpinBox()
        self.num_off.setRange(0.0, 2.0)
        self.num_off.setDecimals(2)
        self.num_off.setSingleStep(0.05)
        self.num_off.setSuffix(" мм")
        self.num_off.setValue(round(getattr(cfg, "num_offset", 25) * MM, 2))
        self.num_off.setToolTip("Насколько номер вывода поднят над его линией")

        self.tbl_src = QComboBox()
        self.tbl_src.addItems(["имя вывода", "номер контакта"])
        self.tbl_src.setCurrentIndex(0 if cfg.table_cell_source == "name" else 1)
        self.tbl_sort = QComboBox()
        self.tbl_sort.addItems(["как в источнике", "по номеру контакта"])
        self.tbl_sort.setCurrentIndex(0 if cfg.table_sort == "source" else 1)
        self.tbl_lines = QCheckBox("линии между строками таблицы")
        self.tbl_lines.setChecked(cfg.table_row_lines)

        self.num_text = QCheckBox("номера выводов рисовать текстом "
                                  "(гарантирует шрифт ГОСТ)")
        self.num_text.setChecked(cfg.pin_numbers_as_text)
        self.pow_marks = QCheckBox("обозначать мощность внутри резистора")
        self.pow_marks.setChecked(cfg.show_power_marks)
        self.fields = QCheckBox("дополнительные поля у микросхем")
        self.fields.setChecked(cfg.ic_fields)
        self.pin_nums = QCheckBox("показывать номера выводов")
        self.pin_nums.setChecked(getattr(cfg, "show_pin_numbers", True))
        self.black = QCheckBox("вся графика и текст чёрные")
        self.black.setToolTip("Иначе Altium раскрасит по-своему: корпус "
                              "бордовым, текст синим")
        self.black.setChecked(getattr(cfg, "color_graphic", 0) == 0
                              and getattr(cfg, "color_text", 0) == 0)
        self.gap_rows = QSpinBox()
        self.gap_rows.setRange(0, 4)
        self.gap_rows.setValue(getattr(cfg, "group_gap_rows", 1))
        self.gap_rows.setSuffix(" строк")
        self.pscale = QSpinBox()
        self.pscale.setRange(20, 300)
        self.pscale.setSingleStep(5)
        self.pscale.setSuffix(" %")
        self.pscale.setValue(int(round(getattr(cfg, "passive_scale", 1.0) * 100)))
        self.pscale.setToolTip("Размер графики резисторов, конденсаторов, "
                               "диодов и прочих двухвыводных элементов")
        self.autoinst = QCheckBox("подключать библиотеку к Altium автоматически")
        self.autoinst.setChecked(cfg.auto_install_library)
        self.intlib = QCheckBox("собирать .IntLib — одна запись в панели "
                                "Components вместо пары «символы + посадки»")
        self.intlib.setToolTip(
            "Altium скомпилирует .LibPkg в интегрированную библиотеку. "
            "Файловые .SchLib/.PcbLib при этом отключаются, чтобы не было "
            "дублей. Если компиляция не пройдёт, останутся обычные файловые "
            "библиотеки — ничего не сломается.")
        self.intlib.setChecked(getattr(cfg, "build_intlib", False))
        # Какую установку KiCad слушать. Когда версий несколько, один и тот
        # же символ находится по разу на версию -- отсюда тройные строки.
        self.kicad_root = QComboBox()
        self.kicad_root.setEditable(True)
        self.kicad_root.addItem("все установки", "")
        try:
            from ..sources import kicad as _kc
            for label, path in _kc.installed_versions():
                self.kicad_root.addItem(f"{label} — {path}", path)
        except Exception:
            pass
        cur = getattr(cfg, "kicad_root", "") or ""
        idx = self.kicad_root.findData(cur)
        if idx >= 0:
            self.kicad_root.setCurrentIndex(idx)
        elif cur:
            self.kicad_root.setEditText(cur)
        self.kicad_root.setToolTip(
            "Папка share/kicad нужной версии. После смены нажмите "
            "«Обновить индекс KiCad» в окне импорта.")

        self.altium = QLineEdit(cfg.altium_exe)
        alt_b = QPushButton("…")
        alt_b.setFixedWidth(30)
        alt_b.clicked.connect(self._browse_altium)

        tabs = QTabWidget()

        w1 = QWidget(); f1 = QFormLayout(w1)
        h = QHBoxLayout(); h.addWidget(self.out_dir, 1); h.addWidget(browse)
        f1.addRow("Имя библиотеки:", self.lib_name)
        f1.addRow("Папка библиотеки:", self._wrap(h))
        ha = QHBoxLayout(); ha.addWidget(self.altium, 1); ha.addWidget(alt_b)
        f1.addRow("Altium (X2.EXE):", self._wrap(ha))
        f1.addRow("", self.autoinst)
        f1.addRow("", self.intlib)
        f1.addRow("Библиотеки KiCad:", self.kicad_root)
        tabs.addTab(w1, "Библиотека")

        w2 = QWidget(); f2 = QFormLayout(w2)
        f2.addRow("Шрифт:", self.font)
        f2.addRow("Имена выводов, пт:", self.s_pin)
        f2.addRow("Номера выводов, пт:", self.s_num)
        f2.addRow("Позиционное обозначение, пт:", self.s_des)
        f2.addRow("Тип в основном поле, пт:", self.s_typ)
        f2.addRow("Текст таблицы разъёма, пт:", self.s_tab)
        f2.addRow("Длина вывода:", self.pin_len)
        f2.addRow("Шаг выводов:", self.pitch)
        f2.addRow("", self.auto_pitch)
        f2.addRow("Номер над выводом:", self.num_off)
        f2.addRow("", self.num_text)
        f2.addRow("", self.pow_marks)
        f2.addRow("", self.fields)
        f2.addRow("", self.pin_nums)
        f2.addRow("", self.black)
        f2.addRow("Зазор между группами выводов:", self.gap_rows)
        f2.addRow("Размер двухвыводных элементов:", self.pscale)
        tabs.addTab(w2, "ГОСТ / шрифты")

        w3 = QWidget(); f3 = QFormLayout(w3)
        f3.addRow("В колонке «Конт.»:", self.tbl_src)
        f3.addRow("Порядок строк:", self.tbl_sort)
        f3.addRow("", self.tbl_lines)
        tabs.addTab(w3, "Разъёмы")

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(bb)
        self.resize(560, 520)

    @staticmethod
    def _wrap(layout):
        w = QWidget()
        w.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)
        return w

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Папка библиотеки",
                                             self.out_dir.text())
        if d:
            self.out_dir.setText(d)

    def _browse_altium(self):
        f, _ = QFileDialog.getOpenFileName(self, "Altium Designer",
                                           self.altium.text(), "X2.EXE (X2.EXE);;*.exe")
        if f:
            self.altium.setText(f)

    def apply(self) -> config.Config:
        c = self.cfg
        c.library_name = self.lib_name.text().strip() or "GOST_Lib"
        c.out_dir = self.out_dir.text().strip()
        c.font = self.font.text().strip() or "GOST type B"
        c.size_pin = self.s_pin.value()
        c.size_pin_num = self.s_num.value()
        c.size_desig = self.s_des.value()
        c.size_type = self.s_typ.value()
        c.size_table = self.s_tab.value()
        c.pin_length = int(round(self.pin_len.value() / MM))
        c.pin_pitch = int(round(self.pitch.value() / MM))
        c.auto_pitch = self.auto_pitch.isChecked()
        c.num_offset = int(round(self.num_off.value() / MM))
        c.table_cell_source = "name" if self.tbl_src.currentIndex() == 0 else "number"
        c.table_sort = "source" if self.tbl_sort.currentIndex() == 0 else "number"
        c.table_row_lines = self.tbl_lines.isChecked()
        c.pin_numbers_as_text = self.num_text.isChecked()
        c.show_power_marks = self.pow_marks.isChecked()
        c.ic_fields = self.fields.isChecked()
        c.show_pin_numbers = self.pin_nums.isChecked()
        # список даёт путь в данных пункта; вручную вписанный текст берём
        # как есть -- это тоже путь
        txt = self.kicad_root.currentText().strip()
        known = self.kicad_root.findText(txt)
        c.kicad_root = (self.kicad_root.itemData(known) or "") if known >= 0 \
            else txt
        c.group_gap_rows = self.gap_rows.value()
        c.passive_scale = self.pscale.value() / 100.0
        black = 0 if self.black.isChecked() else None
        if black is not None:
            c.color_graphic = c.color_text = c.color_pin_num = c.color_pin = 0
        else:
            # цвета по умолчанию Altium: корпус бордовый, текст синий
            c.color_graphic = c.color_pin = 128
            c.color_text = c.color_pin_num = 8388608
        c.auto_install_library = self.autoinst.isChecked()
        c.build_intlib = self.intlib.isChecked()
        c.altium_exe = self.altium.text().strip()
        c.save()
        return c


class PickComponentsDialog(QDialog):
    """
    Взять компоненты из общего каталога в текущий проект.

    Нужен ровно потому, что таблица показывает состав проекта: то, чего в
    проекте ещё нет, в ней и не видно, а значит и выбрать нечего. Здесь
    список полный, с поиском, и сразу помечено, что уже входит в проект.
    """

    def __init__(self, svc, pid: int, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.pid = int(pid)
        pr = svc.db.project(self.pid)
        self.pname = pr["name"] if pr else ""
        self.setWindowTitle(f"Добавить в проект «{self.pname}»")
        self.resize(900, 560)

        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "поиск по имени, парт-номеру, производителю, корпусу…")
        self.search.textChanged.connect(self._refill)
        self.cb_type = QComboBox()
        self.cb_type.addItem("все типы", "")
        for ct, n in svc.db.types():
            from .. import classify
            self.cb_type.addItem(
                f"{classify.CTYPE_NAME.get(ct, ct)} ({n})", ct)
        self.cb_type.currentIndexChanged.connect(self._refill)
        self.cb_hide = QCheckBox("скрыть то, что уже в проекте")
        self.cb_hide.setChecked(True)
        self.cb_hide.stateChanged.connect(self._refill)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Имя", "Тип", "Обозн.", "Парт-номер",
                                   "Корпус"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 240)
        self.tree.setColumnWidth(1, 150)
        self.tree.setColumnWidth(2, 70)
        self.tree.setColumnWidth(3, 160)
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.itemDoubleClicked.connect(self._toggle)

        self.lb = QLabel("")

        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.cb_type)
        top.addWidget(self.cb_hide)

        b_all = QPushButton("Отметить все показанные")
        b_all.clicked.connect(lambda: self._mark(True))
        b_none = QPushButton("Снять отметки")
        b_none.clicked.connect(lambda: self._mark(False))
        row = QHBoxLayout()
        row.addWidget(b_all)
        row.addWidget(b_none)
        row.addStretch(1)
        row.addWidget(self.lb)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Добавить в проект")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.tree, 1)
        lay.addLayout(row)
        lay.addWidget(bb)

        self._refill()

    # ------------------------------------------------------------ данные ---
    def _refill(self):
        from .. import classify
        keep = set(self.svc.db.project_uids(self.pid))
        rows = self.svc.db.search(self.search.text().strip(),
                                  self.cb_type.currentData() or "",
                                  False, None, "name ASC")
        self.tree.clear()
        shown = 0
        for r in rows:
            inside = r["uid"] in keep
            if inside and self.cb_hide.isChecked():
                continue
            it = QTreeWidgetItem([
                r["name"] or "",
                classify.CTYPE_NAME.get(r["ctype"], r["ctype"]),
                r["designator"] or "",
                r["mpn"] or "",
                r["package"] or "",
            ])
            it.setData(0, Qt.UserRole, r["uid"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Unchecked)
            if inside:
                # уже в проекте: показываем, но повторно не добавляем
                it.setDisabled(True)
                it.setText(1, it.text(1) + "  · уже в проекте")
            self.tree.addTopLevelItem(it)
            shown += 1
        self.lb.setText(f"показано {shown} из {len(rows)}")

    def _toggle(self, it, _col=0):
        if it.isDisabled():
            return
        it.setCheckState(0, Qt.Unchecked
                         if it.checkState(0) == Qt.Checked else Qt.Checked)

    def _mark(self, on: bool):
        state = Qt.Checked if on else Qt.Unchecked
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if not it.isDisabled():
                it.setCheckState(0, state)

    def chosen(self):
        """uid отмеченных, а если не отмечено ничего -- выделенных мышью."""
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if not it.isDisabled() and it.checkState(0) == Qt.Checked:
                out.append(it.data(0, Qt.UserRole))
        if out:
            return out
        return [it.data(0, Qt.UserRole) for it in self.tree.selectedItems()
                if not it.isDisabled()]
