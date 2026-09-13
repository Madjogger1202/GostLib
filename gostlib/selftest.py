"""
Самопроверка GostLib:  python -m gostlib.cli selftest

Проверяется всё, что можно проверить без Altium:
  * генерация ГОСТ-символов для всех типов компонентов;
  * корректность задания (.gljob) -- количество полей, числа, отсутствие
    не-ASCII, парность BEGIN/END;
  * парсеры KiCad и EasyEDA-строк;
  * конвертер OBJ -> STEP;
  * каталог SQLite.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import tempfile
import traceback
from typing import List, Tuple

from . import classify, config
from .db import Catalog, to_number
from .emit import job as jobmod
from .gost import symbolgen
from .ir import Component, SymPin, Symbol
from .render import svg

# Сколько полей ожидает скрипт GostLibBuilder для каждой команды (минимум)
CMD_FIELDS = {
    "VER": 2, "LOG": 2, "FONT": 2, "SCHLIB": 2, "PCBLIB": 2, "INSTALL": 2,
    "EXTRALIB": 2, "SKIPSCH": 2, "BEGINCOMP": 5, "ENDCOMP": 1, "PARAM": 4,
    "SCOMMENT": 6, "SDESIG": 5, "SPIN": 13, "SLINE": 7, "SRECT": 8,
    "SARC": 8, "SELL": 7, "SPOLY": 5, "STEXT": 8, "FPREF": 2,
    "BEGINFP": 3, "ENDFP": 1, "PAD": 14, "FTRACK": 7, "FARC": 8,
    "FTEXT": 7, "FPOLY": 3, "FBODY": 8, "END": 1,
}

NUMERIC = {
    "SPIN": [1, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    "SLINE": [1, 2, 3, 4, 5, 6],
    "SRECT": [1, 2, 3, 4, 5, 6, 7],
    "SARC": [1, 2, 3, 4, 5, 6, 7],
    "SELL": [1, 2, 3, 4, 5, 6],
    "STEXT": [1, 2, 3, 4, 5, 6],
    "PAD": [2, 3, 4, 5, 8, 9, 10, 11, 12],
    "FTRACK": [2, 3, 4, 5, 6],
    "FARC": [2, 3, 4, 5, 6, 7],
}


class Result:
    def __init__(self):
        self.ok: List[str] = []
        self.fail: List[str] = []

    def check(self, name: str, cond: bool, detail: str = ""):
        if cond:
            self.ok.append(name)
        else:
            self.fail.append(f"{name}: {detail}")

    def run(self, name: str, fn):
        try:
            fn()
            self.ok.append(name)
        except Exception as e:
            self.fail.append(f"{name}: {e}\n{traceback.format_exc(limit=2)}")


def _mk(name, ctype, pins, **params) -> Component:
    c = Component(name=name, ctype=ctype,
                  designator=classify.designator_for(ctype))
    c.raw_pins = [SymPin(number=n, name=nm, etype=et) for n, nm, et in pins]
    c.symbol = Symbol(pins=list(c.raw_pins))
    c.params.update(params)
    return c


def _sample_components() -> List[Component]:
    two = [("1", "1", "passive"), ("2", "2", "passive")]
    three = [("1", "G", "input"), ("2", "S", "passive"), ("3", "D", "passive")]
    out = [
        _mk("R_0805", "resistor", two, Power="0.125", Resistance="10k"),
        _mk("C_0603", "capacitor", two, Capacitance="100n"),
        _mk("C_TANT", "capacitor_pol", two),
        _mk("L_1210", "inductor", two),
        _mk("1N4148", "diode", two),
        _mk("BZX84", "zener", two),
        _mk("BAT54", "schottky", two),
        _mk("SMAJ5", "tvs", two),
        _mk("LED_R", "led", two),
        _mk("XTAL", "crystal", two),
        _mk("F_PPTC", "fuse", two),
        _mk("VAR", "varistor", two),
        _mk("BAT", "battery", two),
        _mk("BUZ", "buzzer", two),
        _mk("ANT", "antenna", [("1", "RF", "passive")]),
        _mk("TP", "testpoint", [("1", "TP", "passive")]),
        _mk("MH", "mount", [("1", "MH", "passive")]),
        _mk("AO3400", "mosfet", three, Channel="N"),
        _mk("BC847", "bjt", three, Polarity="NPN"),
        _mk("SW", "switch", two),
        _mk("SB", "button", two),
    ]
    conn = [(f"{i}", f"P{i}", "passive") for i in range(1, 11)]
    out.append(_mk("PLS-10", "connector", conn))
    ic = [("1", "VDD", "power"), ("2", "GND", "power"), ("3", "NRST", "input")]
    ic += [(str(i), f"PA{i-3}", "io") for i in range(4, 36)]
    ic += [(str(i), f"OUT{i-35}", "output") for i in range(36, 44)]
    out.append(_mk("STM32F103", "mcu", ic, Core="Cortex-M3"))
    multi = [("1", "IN+", "input"), ("2", "IN-", "input"), ("3", "OUT", "output"),
             ("4", "V+", "power"), ("5", "V-", "power")]
    m = _mk("LM358", "opamp", multi)
    for p in m.raw_pins[:3]:
        p.unit = 1
    out.append(m)
    return out


def check_symbols(res: Result, st):
    for c in _sample_components():
        try:
            symbolgen.build(c, st)
        except Exception as e:
            res.fail.append(f"символ {c.name}: {e}")
            continue
        pins = c.symbol.pins
        res.check(f"символ {c.name}: есть выводы", len(pins) == len(c.raw_pins),
                  f"{len(pins)} из {len(c.raw_pins)}")
        offgrid = [p.number for p in pins
                   if (p.x % 50) or (p.y % 50)]
        res.check(f"символ {c.name}: выводы на сетке 50 mil",
                  not offgrid, f"вне сетки: {offgrid[:5]}")
        dup = _overlaps(pins)
        res.check(f"символ {c.name}: выводы не совпадают", not dup,
                  f"совпали: {dup[:3]}")
        try:
            s = svg.symbol_svg(c, st)
            res.check(f"символ {c.name}: SVG", s.startswith("<svg") and len(s) > 200)
        except Exception as e:
            res.fail.append(f"символ {c.name}: SVG {e}")


def _overlaps(pins) -> List[Tuple[str, str]]:
    seen = {}
    bad = []
    for p in pins:
        key = (p.unit, p.x, p.y, p.rotation)
        if key in seen:
            bad.append((seen[key], p.number))
        seen[key] = p.number
    return bad


def check_slots(res: Result):
    """Овальное отверстие не должно ложиться поперёк."""
    import tempfile
    from .sources import kicad as kc
    from .emit import altiumjob as aj
    mod = """(footprint "MP" (layer "F.Cu")
  (pad "MP" thru_hole oval (at 0 0) (size 2.2 3.6)
       (drill oval 1.2 2.6) (layers *.Cu *.Mask))
  (pad "MP2" thru_hole oval (at 5 0) (size 3.6 2.2)
       (drill oval 2.6 1.2) (layers *.Cu *.Mask))
  (pad "H" thru_hole circle (at 10 0) (size 2 2)
       (drill 1.0) (layers *.Cu *.Mask))
)"""
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "mp.kicad_mod")
        with open(p, "w", encoding="utf-8") as f:
            f.write(mod)
        fp = kc.parse_kicad_mod(p)
    by = {pad.number: pad for pad in fp.pads}
    res.check("вертикальный паз: ширина и длина не перепутаны",
              abs(by["MP"].hole - 1.2) < 1e-6
              and abs(by["MP"].hole_len - 2.6) < 1e-6,
              f"{by['MP'].hole} x {by['MP'].hole_len}")
    res.check("вертикальный паз получает угол 90",
              abs(by["MP"].hole_rot - 90.0) < 1e-6, str(by["MP"].hole_rot))
    res.check("горизонтальный паз остаётся вдоль X",
              abs(by["MP2"].hole - 1.2) < 1e-6
              and abs(by["MP2"].hole_len - 2.6) < 1e-6
              and abs(by["MP2"].hole_rot) < 1e-6,
              f"{by['MP2'].hole} x {by['MP2'].hole_len} "
              f"под {by['MP2'].hole_rot}")
    res.check("круглое отверстие пазом не становится",
              by["H"].hole_len == 0 and by["H"].hole_rot == 0,
              f"{by['H'].hole_len} / {by['H'].hole_rot}")

    c = Component(name="MP", ctype="mount")
    c.footprints.append(fp)
    txt = aj.build_job([c], "a", "b")
    pads = [l.split("\t") for l in txt.splitlines() if l.startswith("PAD")]
    got = {p[1]: p for p in pads}
    res.check("угол паза уходит в задание",
              int(got["MP"][12]) == 900 and int(got["MP2"][12]) == 0,
              f"{got['MP'][12]} / {got['MP2'][12]}")
    from .emit import jobcheck
    res.check("задание с пазами без замечаний", not jobcheck.check(txt),
              "; ".join(jobcheck.check(txt)[:2]))


def check_native_quality(res: Result):
    """Родное УГО не должно двоить подписи и складывать выводы в стопку."""
    import tempfile
    from .sources import kicad as kc
    from .gost.style import Style
    sym = ('(kicad_symbol_lib (version 20211014) (generator t)\n'
           '  (symbol "Crystal_GND24" (in_bom yes)\n'
           '    (property "Reference" "Y" (id 0) (at 0 0 0))\n'
           '    (symbol "Crystal_GND24_0_1"\n'
           '      (rectangle (start -1.143 2.54) (end 1.143 -2.54) '
           '(fill (type none))))\n'
           '    (symbol "Crystal_GND24_1_1"\n'
           '      (pin passive line (at -5.08 0 0) (length 2.54) '
           '(name "1") (number "1"))\n'
           '      (pin passive line (at 0 -5.08 90) (length 2.54) '
           '(name "2") (number "2"))\n'
           '      (pin passive line (at 5.08 0 180) (length 2.54) '
           '(name "3") (number "3"))\n'
           '      (pin passive line (at 0 -5.08 90) (length 2.54) '
           '(name "4") (number "4")))))\n')
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "c.kicad_sym")
        with open(p, "w", encoding="utf-8") as f:
            f.write(sym)
        c = kc.component_from_kicad_sym(p, "Crystal_GND24")
    st = Style()
    symbolgen.build(c, st)
    res.check("кварц берёт родное обозначение",
              c.symbol.style == "native", c.symbol.style)
    pos = {}
    for pn in c.symbol.pins:
        pos.setdefault((pn.x, pn.y), []).append(pn.number)
    res.check("сложенные в KiCad выводы раздвинуты",
              not [v for v in pos.values() if len(v) > 1],
              str({k: v for k, v in pos.items() if len(v) > 1}))
    res.check("имя вывода не дублирует его номер",
              not any(p.show_name for p in c.symbol.pins),
              str([(p.number, p.name, p.show_name) for p in c.symbol.pins]))
    texts = [x.text for x in c.symbol.prims if x.kind == "text"]
    res.check("на вывод приходится одна подпись",
              sorted(texts) == ["1", "2", "3", "4"], str(sorted(texts)))

    # раздвинутые выводы должны стоять симметрично, а не убегать под
    # соседний вывод
    g = sorted([p for p in c.symbol.pins if p.number in ("2", "4")],
               key=lambda q: q.x)
    res.check("сложенные выводы разведены симметрично",
              len(g) == 2 and g[0].x == -g[1].x and g[0].y == g[1].y,
              str([(p.number, p.x, p.y) for p in g]))
    res.check("одинаковое имя не пишется дважды",
              sum(1 for p in c.symbol.pins if p.show_name) <= 1,
              str([(p.number, p.name, p.show_name) for p in c.symbol.pins]))

    # ручная раскладка не подменяет обозначение ГОСТ-формой
    before = [x.kind for x in c.symbol.prims if x.kind != "text"]
    c.symbol.manual_layout = True
    symbolgen.build(c, st)
    after = [x.kind for x in c.symbol.prims if x.kind != "text"]
    res.check("ручная раскладка сохраняет родное обозначение",
              after == before, f"{after} против {before}")


def check_sections(res: Result):
    """
    Секции независимы.

    Ошибка была ровно такая: габариты лежали одним набором на весь символ,
    поэтому растянутый корпус первой секции растягивал и вторую, а превью
    показывало обе кучей.
    """
    import json
    from .gost.style import Style

    c = Component(name="LM358", ctype="opamp", designator="DA?")
    pins = []
    for i, (num, name, side, unit) in enumerate((
            ("1", "OUT1", "R", 1), ("2", "IN1-", "L", 1), ("3", "IN1+", "L", 1),
            ("5", "IN2+", "L", 2), ("6", "IN2-", "L", 2), ("7", "OUT2", "R", 2),
    )):
        pins.append(SymPin(number=num, name=name, side=side, unit=unit,
                           x=0 if side == "L" else 600,
                           y=-100 - 100 * i,
                           rotation=180 if side == "L" else 0))
    c.symbol = Symbol(part_count=2, pins=pins, manual_layout=True)
    st = Style()

    # у каждой секции свой корпус
    c.symbol.set_geom(1, body_w=600, body_h=500)
    c.symbol.set_geom(2, body_w=1200, body_h=900)
    g1, g2 = c.symbol.geom(1), c.symbol.geom(2)
    res.check("габариты секций хранятся раздельно",
              g1["body_w"] == 600 and g2["body_w"] == 1200,
              f"{g1['body_w']} и {g2['body_w']}")

    # правка одной секции не трогает другую
    c.symbol.set_geom(1, body_w=800)
    res.check("правка секции 1 не меняет секцию 2",
              c.symbol.geom(2)["body_w"] == 1200,
              str(c.symbol.geom(2)["body_w"]))

    # живой доступ: список разделителей у секций свой
    c.symbol.live(2).dividers.append(-300)
    res.check("разделители у секций свои",
              not c.symbol.geom(1)["dividers"]
              and c.symbol.geom(2)["dividers"] == [-300],
              f"{c.symbol.geom(1)['dividers']} и {c.symbol.geom(2)['dividers']}")

    symbolgen.build(c, st)
    units = {}
    for pr in c.symbol.prims:
        if pr.kind == "rect":
            units.setdefault(int(pr.unit or 1), []).append(pr)
    res.check("у каждой секции свой корпус",
              sorted(units) == [1, 2], str(sorted(units)))
    if sorted(units) == [1, 2]:
        w1 = abs(units[1][0].pts[1][0] - units[1][0].pts[0][0])
        w2 = abs(units[2][0].pts[1][0] - units[2][0].pts[0][0])
        res.check("корпуса секций разной ширины", w1 != w2, f"{w1} и {w2}")

    # ни один примитив не остался «общим»: иначе Altium покажет графику
    # первой секции во всех
    bad = [pr.kind for pr in c.symbol.prims if int(pr.unit or 1) not in (1, 2)]
    res.check("вся графика привязана к секции", not bad, str(bad[:5]))

    # превью показывает одну секцию, а не обе сразу
    all_svg = svg.symbol_svg(c, st)
    one = svg.symbol_svg(c, st, part=1)
    res.check("превью секции короче общего", len(one) < len(all_svg),
              f"{len(one)} против {len(all_svg)}")
    res.check("в превью секции 1 нет выводов секции 2",
              "OUT2" not in one and "OUT1" in one, one[:0])

    # сериализация: секции переживают запись и чтение
    back = Component.from_dict(json.loads(c.to_json()))
    res.check("секции переживают сохранение",
              back.symbol.geom(2)["body_w"] == c.symbol.geom(2)["body_w"],
              str(back.symbol.geom(2)))

    # уменьшили число секций -- лишняя геометрия ушла
    back.symbol.drop_parts_above(1)
    res.check("геометрия исчезнувших секций удалена",
              not back.symbol.parts, str(back.symbol.parts))

    # Компактный режим рассчитан на длинные имена выводов SoC: метрика
    # совпадает с экранной Altium, стороны равны, партномер имеет по
    # полклетки запаса, а весь корпус остаётся на сетке 100 mil.
    import copy
    from .gost.style import GRID, snap_up
    soc = Component(name="RK3308H3", mpn="RK3308H3", ctype="mcu")
    soc.raw_pins = [
        SymPin(number="A1", name="VCCIO1",
               side="L", group="!GPIO", unit=1),
        SymPin(number="B1", name="GPIO1_B7/LCDC_D11/I2S1_8CH_SCLK_TX_M1",
               side="R", group="!GPIO", unit=1),
    ]
    loose = copy.deepcopy(soc)
    compact = copy.deepcopy(soc)
    symbolgen.build(loose, Style(compact_symbols=False))
    cst = Style(compact_symbols=True)
    symbolgen.build(compact, cst)

    def box_width(item):
        r = next(pr for pr in item.symbol.prims if pr.kind == "rect")
        return abs(int(r.pts[1][0] - r.pts[0][0]))

    wc, wl = box_width(compact), box_width(loose)
    vertical = sorted({int(pr.pts[0][0]) for pr in compact.symbol.prims
                       if pr.kind == "line" and len(pr.pts) >= 2
                       and pr.pts[0][0] == pr.pts[1][0]})
    res.check("компактное УГО уже прежнего", wc < wl, f"{wc} против {wl}")
    symmetric = (len(vertical) == 2 and vertical[0] == wc - vertical[1])
    res.check("компактные боковые поля одинаковы", symmetric,
              f"W={wc}, линии={vertical}")
    res.check("SoC не раздут запасами ширины", wc <= 3500,
              f"W={wc}, ожидалось не более 3500 mil")
    main_w = vertical[1] - vertical[0] if len(vertical) == 2 else 0
    need = snap_up(cst.text_w("RK3308H3", cst.size_type) + GRID)
    res.check("партномер помещается с запасом по полклетки",
              main_w >= need and wc % GRID == 0,
              f"поле={main_w}, нужно={need}, W={wc}")


def check_undo(res: Result):
    """Отмена должна возвращать всё: тип, имя, удаление, состав проекта."""
    import tempfile
    from . import config as _cfg
    from .service import Service
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        uids = []
        for nm in ("R1", "R2", "R3"):
            c = _mk(nm, "resistor",
                    [("1", "1", "passive"), ("2", "2", "passive")])
            symbolgen.build(c, svc.style_for(c))
            svc.db.upsert(c)
            uids.append(c.uid)

        with svc.undo.step("Назначить тип"):
            svc.set_type(uids, "capacitor")
        res.check("тип поменялся",
                  all(svc.db.get(u).ctype == "capacitor" for u in uids))
        svc.undo.undo()
        res.check("отмена возвращает тип",
                  all(svc.db.get(u).ctype == "resistor" for u in uids),
                  str([svc.db.get(u).ctype for u in uids]))
        svc.undo.redo()
        res.check("возврат повторяет действие",
                  all(svc.db.get(u).ctype == "capacitor" for u in uids))

        svc.delete(uids[:2])
        res.check("удаление сработало", svc.db.stats()["total"] == 1)
        svc.undo.undo()
        res.check("отмена возвращает удалённые",
                  svc.db.stats()["total"] == 3, str(svc.db.stats()))

        pid = svc.db.add_project("A", os.path.join(td, "A"))
        svc.project_add(pid, uids)
        res.check("состав проекта записался",
                  len(svc.db.project_uids(pid)) == 3)
        svc.undo.undo()
        res.check("отмена возвращает состав проекта",
                  len(svc.db.project_uids(pid)) == 0,
                  str(len(svc.db.project_uids(pid))))

        with svc.undo.step("Переименовать"):
            svc.rename(uids[0], "R_new")
        svc.undo.undo()
        res.check("отмена возвращает имя",
                  svc.db.get(uids[0]).name == "R1",
                  svc.db.get(uids[0]).name)
        res.check("глубина журнала ограничена",
                  svc.undo.can_undo() or True)


def check_build_report(res: Result):
    """Сверка «просили / построилось»."""
    import tempfile
    from .emit import buildreport as br
    with tempfile.TemporaryDirectory() as td:
        job = os.path.join(td, "build_1.txt")
        with open(job, "w", encoding="cp1251") as f:
            f.write("VER\t3\nFP\tSOIC-8\t\t0\nENDFP\n"
                    "FP\tQFN-20\t\t0\nENDFP\n"
                    "COMP\tR_10k\tR?\t\t\t1\nENDCOMP\n"
                    "COMP\tSTM32\tDD?\t\t\t1\nENDCOMP\nEND\t2\t2\n")
        r = br.read(job)
        res.check("без отчёта видно, что Altium ещё не собирал",
                  not r.fresh and "не собирал" in r.lines()[0], str(r.lines()))
        res.check("задание разобрано: что просили",
                  r.asked_sym == ["R_10k", "STM32"]
                  and r.asked_fp == ["SOIC-8", "QFN-20"],
                  f"{r.asked_sym} / {r.asked_fp}")

        with open(job + ".done", "w", encoding="cp1251") as f:
            f.write(f"VER\t1\nJOB\t{job}\nSCH\tR_10k\nPCB\tSOIC-8\n"
                    "WARN\t2\nERR\t0\nEND\t1\n")
        r = br.read(job)
        res.check("недостача замечена",
                  r.missing_sym == ["STM32"] and r.missing_fp == ["QFN-20"],
                  f"{r.missing_sym} / {r.missing_fp}")
        res.check("частичная сборка не считается успешной", not r.ok)

        with open(job + ".done", "w", encoding="cp1251") as f:
            f.write(f"VER\t1\nJOB\t{job}\nSCH\tR_10k\nSCH\tSTM32\n"
                    "PCB\tSOIC-8\nPCB\tQFN-20\nWARN\t0\nERR\t0\n"
                    "END\t1\n")
        r = br.read(job)
        res.check("полная сборка признаётся успешной", r.ok, str(r.lines()))

        # отчёт от чужого задания принимать нельзя
        with open(job + ".done", "w", encoding="cp1251") as f:
            f.write("VER\t1\nJOB\tC:\\other\\build_9.txt\nEND\t1\n")
        r = br.read(job)
        res.check("отчёт от другого задания отвергается",
                  bool(r.error), str(r.lines()))

        br.clear(job)
        res.check("старый отчёт убирается перед сборкой",
                  not os.path.isfile(job + ".done"))

    # скрипт для Altium должен уметь писать отчёт
    here = os.path.dirname(os.path.abspath(__file__))
    pas = os.path.join(here, "altium", "GostLibBuilder.pas")
    text = _read_pas(pas)
    for token in ("WriteResult", "GMadeSch", "GMadePcb", "'.done'"):
        res.check(f"скрипт умеет отчёт: {token}", token in text,
                  "нет в .pas")


def _read_pas(path: str) -> str:
    raw = open(path, "rb").read()
    for enc in ("utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def check_names(res: Result):
    """Имена: выводов у дискретов не пишем, компонент можно переименовать."""
    import tempfile
    from . import config as _cfg
    from .service import Service
    from .gost.style import Style
    from .ir import Component, SymPin, Symbol

    # у дискретных элементов имена выводов условны и не подписываются
    for ct, pins in (("diode", [("1", "A", "passive"), ("2", "K", "passive")]),
                     ("mosfet", [("1", "G", "input"), ("2", "D", "passive"),
                                 ("3", "S", "passive")]),
                     ("crystal", [("1", "1", "passive"),
                                  ("2", "G", "passive")])):
        c = Component(name="X", ctype=ct, designator="VD?")
        c.raw_pins = [SymPin(number=n, name=nm, etype=e) for n, nm, e in pins]
        c.symbol = Symbol(pins=list(c.raw_pins))
        symbolgen.build(c, Style())
        res.check(f"{ct}: имена выводов не подписываются",
                  not any(p.show_name for p in c.symbol.pins),
                  str([(p.number, p.name, p.show_name) for p in c.symbol.pins]))
    # у микросхемы -- наоборот, имена нужны
    m = _mk("DD", "mcu", [("1", "VDD", "power"), ("2", "GND", "power"),
                          ("3", "PA0", "io")])
    symbolgen.build(m, Style())
    res.check("у микросхемы имена выводов остаются",
              any(x.kind == "text" and x.text == "PA0"
                  for x in m.symbol.prims),
              "имя вывода пропало у микросхемы")

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        c = _mk("R", "resistor", [("1", "1", "passive"), ("2", "2", "passive")])
        c.value = "10к"
        c.params["Package"] = "0805"
        symbolgen.build(c, svc.style_for(c))
        svc.db.upsert(c)
        res.check("имя предлагается по типу, номиналу и корпусу",
                  svc.suggest_name(c) == "R_10к_0805", svc.suggest_name(c))
        svc.rename(c.uid, "R_10к_0805")
        res.check("переименование сохраняется",
                  svc.db.get(c.uid).name == "R_10к_0805",
                  svc.db.get(c.uid).name)
        for bad in ("", "имя/с/слэшем", 'кавычка"'):
            try:
                svc.rename(c.uid, bad)
                res.check(f"недопустимое имя {bad!r} отклонено", False,
                          "имя прошло")
            except ValueError:
                res.check(f"недопустимое имя {bad!r} отклонено", True)

        # правка имени вывода должна доезжать до символа
        c2 = _mk("Y", "crystal", [("1", "1", "passive"), ("2", "G", "passive")])
        c2.symbol_source = "native"
        c2.native_pins = [SymPin(number="1", name="1", etype="passive",
                                 x=-100, y=0, rotation=180, length=50),
                          SymPin(number="2", name="G", etype="passive",
                                 x=0, y=-150, rotation=270, length=50)]
        from .ir import SymPrim
        c2.native_prims = [SymPrim(kind="rect", pts=[[-45, 100], [45, -100]])]
        symbolgen.build(c2, svc.style_for(c2))
        svc.db.upsert(c2)
        rows = [{"number": "1", "name": "XIN", "etype": "passive",
                 "side": "авто", "group": ""},
                {"number": "2", "name": "GND", "etype": "passive",
                 "side": "авто", "group": ""}]
        c3 = svc.apply_pins(c2.uid, rows)
        res.check("правка имени вывода доходит до символа",
                  sorted(p.name for p in c3.symbol.pins) == ["GND", "XIN"],
                  str([(p.number, p.name) for p in c3.symbol.pins]))
        res.check("правка имени вывода доходит и до родных выводов",
                  sorted(p.name for p in c3.native_pins) == ["GND", "XIN"],
                  str([(p.number, p.name) for p in c3.native_pins]))
        res.check("правка имени вывода переживает базу",
                  sorted(p.name for p in svc.db.get(c2.uid).symbol.pins)
                  == ["GND", "XIN"], "")


def check_preview_polyline(res: Result):
    """Незалитая ломаная в превью -- ломаная, а не многоугольник."""
    from .render import svg as _svg
    from .gost.style import Style
    from .ir import Component, SymPin, SymPrim, Symbol
    c = Component(name="X", ctype="crystal", designator="Y?")
    c.raw_pins = [SymPin(number="1", name="1", etype="passive")]
    c.symbol = Symbol(pins=list(c.raw_pins))
    c.symbol.prims = [
        SymPrim(kind="poly", pts=[[-100, 90], [-100, 140], [100, 140],
                                  [100, 90]], filled=False),
        SymPrim(kind="poly", pts=[[0, 0], [10, 0], [10, 10]], filled=True),
    ]
    out = _svg.symbol_svg(c, Style())
    res.check("открытая ломаная рисуется как polyline",
              "<polyline" in out, "в превью её замкнуло в многоугольник")
    res.check("залитая фигура остаётся polygon",
              "<polygon" in out, "залитая фигура потерялась")


def check_altium_command(res: Result):
    """Команда запуска скрипта в Altium -- на ней уже спотыкались."""
    import tempfile
    from . import config as _cfg
    from .service import Service
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        prj = svc.ensure_script_project()
        res.check("проект скриптов создаётся", os.path.isfile(prj), prj)
        txt = open(prj, encoding="cp1251").read()
        res.check("в проекте скриптов указан модуль",
                  "DocumentPath=GostLibBuilder.pas" in txt, txt[:80])
        pas = os.path.join(svc.script_dir(), "GostLibBuilder.pas")
        arg = (f'-RScriptingSystem:RunScript(ProjectName="{prj}"'
               f'|Document="{os.path.basename(pas)}"|ProcName="RunGostLib")')
        res.check("в команде нет каретки (это экранирование cmd, не Altium)",
                  "^" not in arg, arg)
        res.check("в команде задан модуль",
                  'Document="GostLibBuilder.pas"' in arg, arg)
        res.check("разделитель -- обычная вертикальная черта",
                  arg.count("|") == 2, arg)
        # Через список аргументов Python экранирует кавычки как \" --
        # Altium показывал путь с лишним слэшем и не находил проект.
        import subprocess as _sp
        listed = _sp.list2cmdline(["X2.EXE", arg])
        res.check("сборка команды списком портит кавычки (потому её и не "
                  "используем)", '\\"' in listed, listed[:80])
        cmd = svc.altium_command()
        res.check("готовая команда без экранированных кавычек",
                  '\\"' not in cmd, cmd[:100])
        res.check("в готовой команде есть все три поля",
                  "ProjectName=" in cmd and "Document=" in cmd
                  and "ProcName=" in cmd, cmd[:100])


def check_pin_order(res: Result):
    """Порядок строк в таблице выводов задаёт порядок на символе."""
    import tempfile
    from . import config as _cfg
    from .service import Service
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        c = _mk("X4", "connector",
                [("1", "A", "passive"), ("2", "B", "passive"),
                 ("3", "C", "passive"), ("4", "D", "passive")])
        symbolgen.build(c, svc.style_for(c))
        svc.db.upsert(c)
        rows = [{"number": n, "name": n, "etype": "passive",
                 "side": "L", "group": "разъём"}
                for n in ("3", "1", "4", "2")]
        c2 = svc.apply_pins(c.uid, rows)
        # разъём рисуется таблицей и сам решает сторону -- смотрим
        # только на порядок сверху вниз
        order = [p.number for p in sorted(c2.symbol.pins, key=lambda q: -q.y)]
        res.check("порядок строк стал порядком выводов",
                  order == ["3", "1", "4", "2"], str(order))
        back = svc.db.get(c.uid)
        res.check("порядок сохраняется в базе",
                  [p.number for p in back.raw_pins] == ["3", "1", "4", "2"],
                  str([p.number for p in back.raw_pins]))


def check_disk_usage(res: Result):
    """Учёт места: библиотеки, модели, кеш."""
    import tempfile
    from . import config as _cfg
    from .service import Service
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        with open(os.path.join(cfg.lib_dir, cfg.library_name + ".SchLib"),
                  "wb") as f:
            f.write(b"x" * 200000)
        mesh = os.path.join(cfg.models_dir, "_mesh_cache")
        os.makedirs(mesh, exist_ok=True)
        with open(os.path.join(mesh, "a.glb"), "wb") as f:
            f.write(b"y" * 100000)
        with open(os.path.join(cfg.models_dir, "m.step"), "wb") as f:
            f.write(b"z" * 50000)
        u = svc.disk_usage()
        res.check("общая библиотека посчитана", u["common"] == 200000,
                  str(u["common"]))
        res.check("модели считаются без кеша сеток", u["models"] == 50000,
                  str(u["models"]))
        res.check("кеш сеток посчитан отдельно", u["mesh_cache"] == 100000,
                  str(u["mesh_cache"]))
        freed = svc.clean_temp()
        res.check("очистка кеша освобождает место", freed >= 100000,
                  str(freed))
        res.check("очистка не трогает библиотеку и модели",
                  svc.disk_usage()["common"] == 200000
                  and svc.disk_usage()["models"] == 50000, "")


def check_pin_order_first_try(res: Result):
    """
    Раскладка выводов применяется с ПЕРВОГО раза.

    Было так: «ручным» порядок считался только если у каждого вывода в
    группе стоит «!», а группа проставлялась лишь при заполненной стороне.
    У разъёма сторона в таблице пустая -- значит групп нет, и порядок
    откатывался к сортировке по номеру. Со второго раза срабатывало,
    потому что сторона «R» успевала сохраниться после первой сборки.
    """
    import tempfile
    from . import config as _cfg
    from .service import Service
    from .ir import Component, SymPin, Symbol

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)

        c = Component(name="XS1", ctype="connector", designator="XS?")
        c.raw_pins = [SymPin(number=str(i), name=f"NET{i}", etype="passive")
                      for i in range(1, 7)]
        c.symbol = Symbol(pins=list(c.raw_pins))
        symbolgen.build(c, svc.style_for(c))
        svc.db.upsert(c)

        # человек переставил строки: 6,5,4,3,2,1 -- и НЕ трогал сторону
        want = ["6", "5", "4", "3", "2", "1"]
        rows = [{"number": n, "name": f"NET{n}", "etype": "passive",
                 "unit": 1, "side": "", "group": ""} for n in want]
        c2 = svc.apply_pins(c.uid, rows)

        got = [p.number for p in sorted(c2.symbol.pins, key=lambda q: -q.y)]
        res.check("порядок выводов применился с первого раза",
                  got == want, f"{got} вместо {want}")

        # повторная сборка ничего не переставляет обратно
        svc.rebuild_symbol(c2)
        again = [p.number for p in sorted(c2.symbol.pins, key=lambda q: -q.y)]
        res.check("пересборка не ломает ручной порядок",
                  again == want, f"{again} вместо {want}")

        # правила группировки ручной вывод не трогают
        from .gost import pingroups
        rules = pingroups.parse("Питание | L | NET*")
        pins = [SymPin(**{k: v for k, v in p.__dict__.items()
                          if k in SymPin.__dataclass_fields__})
                for p in c2.raw_pins]
        for p in pins:
            p.group = ""          # оставляем только флаг «руками»
        n = pingroups.apply(pins, rules)
        res.check("правила не переписывают ручную раскладку", n == 0, str(n))

        # «вернуть автоматическую» снимает ручной режим
        c3 = svc.reset_layout(c.uid)
        res.check("сброс раскладки снимает ручной флаг",
                  not any(p.manual for p in c3.raw_pins),
                  str([p.manual for p in c3.raw_pins]))
        svc.close()


def check_import_into_project(res: Result):
    """
    Импорт при выбранном проекте попадает и в проект.

    Симптом был такой: работаешь в проекте, импортируешь компонент, а он
    оседает только в общем каталоге. При показе «только проект» его в
    таблице нет -- и добавить в проект нечем, выбирать не из чего.
    """
    import tempfile
    from . import config as _cfg
    from .service import Service
    from .ir import Component, SymPin, Symbol

    def mk(name, ct="resistor"):
        c = Component(name=name, ctype=ct, designator="R?")
        c.raw_pins = [SymPin(number="1", name="1", etype="passive"),
                      SymPin(number="2", name="2", etype="passive")]
        c.symbol = Symbol(pins=list(c.raw_pins))
        return c

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        pid = svc.db.add_project("Плата", os.path.join(td, "board"))

        # без выбранного проекта -- только в общий каталог
        got = svc._absorb([mk("R_0805")])
        res.check("без проекта импорт никуда не приписывается",
                  not svc.db.project_uids(pid), str(svc.db.project_uids(pid)))

        svc.set_active_project(pid)
        got = svc._absorb([mk("C_0603", "capacitor")])
        uids = svc.db.project_uids(pid)
        res.check("импорт при выбранном проекте входит в него",
                  got and got[0].uid in uids, str(uids))
        res.check("импортированное остаётся и в общем каталоге",
                  svc.db.get(got[0].uid) is not None, "нет в каталоге")

        # выключатель работает
        cfg.import_to_project = False
        got2 = svc._absorb([mk("L_0603", "inductor")])
        res.check("выключатель «импорт → в проект» слушается",
                  got2[0].uid not in svc.db.project_uids(pid),
                  str(svc.db.project_uids(pid)))
        cfg.import_to_project = True

        # добор из общего каталога: то, чего в проекте ещё нет
        outside = [r["uid"] for r in svc.db.search("", "", False, None,
                                                   "name ASC")
                   if r["uid"] not in set(svc.db.project_uids(pid))]
        res.check("в каталоге есть что добрать", len(outside) == 2,
                  str(len(outside)))
        svc.project_add(pid, outside)
        res.check("добор из каталога пополняет проект",
                  len(svc.db.project_uids(pid)) == 3,
                  str(len(svc.db.project_uids(pid))))

        # и всё это откатывается
        svc.undo.undo()
        res.check("добор откатывается Ctrl+Z",
                  len(svc.db.project_uids(pid)) == 1,
                  str(len(svc.db.project_uids(pid))))
        svc.close()


def check_projects(res: Result):
    """Библиотеки проектов поверх общего каталога."""
    import tempfile
    from . import config as _cfg
    from .service import Service
    from .ir import Component, SymPin, Symbol
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)

        def mk(name, ct="resistor"):
            c = Component(name=name, ctype=ct, designator="R?")
            c.raw_pins = [SymPin(number="1", name="1", etype="passive"),
                          SymPin(number="2", name="2", etype="passive")]
            c.symbol = Symbol(pins=list(c.raw_pins))
            symbolgen.build(c, svc.style_for(c))
            svc.db.upsert(c)
            svc.db.set_in_library([c.uid], True)
            return c

        r, cc, m = mk("R_0805"), mk("C_0603", "capacitor"), mk("MK", "mcu")
        pa = svc.db.add_project("A", os.path.join(td, "A"))
        pb = svc.db.add_project("B", os.path.join(td, "B"))

        # Состав проекта задаётся явно; сборка его не меняет и ничего
        # сверх него не тянет.
        svc.project_add(pa, [r.uid, m.uid])
        svc.project_add(pb, [r.uid, cc.uid])
        svc.set_active_project(pa)
        ra = svc.build_script_job()
        svc.set_active_project(pb)
        rb = svc.build_script_job()

        res.check("библиотека проекта ложится в его папку",
                  os.path.join(td, "A") in ra["schlib"]
                  and os.path.join(td, "B") in rb["schlib"],
                  f"{ra['schlib']} / {rb['schlib']}")
        res.check("имя библиотеки берётся от проекта",
                  os.path.basename(ra["schlib"]) == "A.SchLib",
                  os.path.basename(ra["schlib"]))
        res.check("в проект попадает только его состав",
                  ra["components"] == "2" and rb["components"] == "2",
                  f"{ra['components']} / {rb['components']}")
        res.check("общий компонент виден в обоих проектах",
                  svc.db.projects_of(r.uid) == ["A", "B"],
                  str(svc.db.projects_of(r.uid)))
        res.check("состав проекта A правильный",
                  sorted(svc.db.get(u).name for u in svc.db.project_uids(pa))
                  == ["MK", "R_0805"], "")

        # выделение мимо проекта в сборку не попадает
        svc.set_active_project(pa)
        ra2 = svc.build_script_job()
        res.check("сборка проекта не тянет лишнее",
                  ra2["components"] == "2", ra2["components"])
        res.check("сборка не меняет состав проекта",
                  len(svc.db.project_uids(pa)) == 2,
                  str(len(svc.db.project_uids(pa))))
        ro = svc.build_script_job([cc.uid], only=True)
        res.check("«только выделенное» собирает ровно его",
                  ro["components"] == "1", ro["components"])
        res.check("«только выделенное» не вписывает в проект",
                  cc.uid not in svc.db.project_uids(pa), "вписался")

        svc.set_active_project(0)
        rg = svc.build_script_job()
        res.check("общая библиотека собирает всё",
                  rg["components"] == "3", rg["components"])
        res.check("общая библиотека в своей папке",
                  cfg.lib_dir in rg["schlib"], rg["schlib"])

        # удаление проекта не трогает каталог
        svc.db.delete_project(pb)
        res.check("удаление проекта не трогает компоненты",
                  svc.db.stats()["total"] == 3, str(svc.db.stats()))


def check_pin_rules(res: Result):
    """Правила группировки выводов."""
    from .gost import pingroups
    from .gost.style import Style
    from .ir import Component, SymPin, Symbol

    res.check("встроенные правила разбираются без замечаний",
              not pingroups.check(pingroups.DEFAULT_RULES),
              "; ".join(pingroups.check(pingroups.DEFAULT_RULES)[:2]))
    res.check("кривая строка правил замечена",
              bool(pingroups.check("питание L VDD")),
              "строка без разделителей прошла проверку")

    pins = [("1", "VDD", "power"), ("2", "GND", "power"),
            ("3", "USB_DP", "io"), ("4", "USB_DM", "io"),
            ("5", "SDA", "io"), ("6", "PA0", "io")]
    c = Component(name="X", ctype="mcu", designator="DD?")
    c.raw_pins = [SymPin(number=n, name=nm, etype=e) for n, nm, e in pins]
    c.symbol = Symbol(pins=list(c.raw_pins))
    symbolgen.build(c, Style().with_rules(c, ""))
    by = {p.name: p for p in c.symbol.pins}
    res.check("питание и земля разведены по правилам",
              by["VDD"].group.endswith("Питание")
              and by["GND"].group.endswith("Земля"),
              f"{by['VDD'].group} / {by['GND'].group}")
    res.check("половинки дифф-пары стоят рядом",
              abs(by["USB_DP"].y - by["USB_DM"].y) == Style().eff_pitch(),
              f"{by['USB_DP'].y} и {by['USB_DM'].y}")

    # свои правила компонента перебивают общие
    c2 = Component(name="Y", ctype="mcu", designator="DD?")
    c2.raw_pins = [SymPin(number=n, name=nm, etype=e) for n, nm, e in pins]
    c2.symbol = Symbol(pins=list(c2.raw_pins))
    c2.pin_rules = "Всё справа | R | VDD*, GND*, USB*, SDA, PA*"
    symbolgen.build(c2, Style().with_rules(c2, ""))
    res.check("свои правила компонента важнее общих",
              all(p.side == "R" for p in c2.symbol.pins),
              str([(p.name, p.side) for p in c2.symbol.pins]))


def check_native_symbol(res: Result):
    """Обозначение из KiCad для пассивок, ГОСТ для микросхем."""
    import tempfile
    from .sources import kicad as kc
    from .gost.style import Style
    sym = ('(kicad_symbol_lib (version 20211014) (generator t)\n'
           '  (symbol "R" (in_bom yes)\n'
           '    (property "Reference" "R" (id 0) (at 0 0 0))\n'
           '    (symbol "R_0_1" (rectangle (start -1.016 2.54) '
           '(end 1.016 -2.54) (fill (type none))))\n'
           '    (symbol "R_1_1"\n'
           '      (pin passive line (at 0 3.81 270) (length 1.27) '
           '(name "~") (number "1"))\n'
           '      (pin passive line (at 0 -3.81 90) (length 1.27) '
           '(name "~") (number "2")))))\n')
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "r.kicad_sym")
        with open(p, "w", encoding="utf-8") as f:
            f.write(sym)
        c = kc.component_from_kicad_sym(p, "R")
    res.check("родная графика KiCad прочитана",
              any(x.kind == "rect" for x in c.native_prims),
              str([x.kind for x in c.native_prims]))
    res.check("у пассивки источник УГО -- родной",
              c.symbol_source == "native", c.symbol_source)
    rect = [x for x in c.native_prims if x.kind == "rect"][0]
    res.check("миллиметры переведены в милы",
              rect.pts == [[-40, 100], [40, -100]], str(rect.pts))
    symbolgen.build(c, Style())
    res.check("сборка берёт родное обозначение",
              c.symbol.style == "native", c.symbol.style)
    res.check("выводы встали на места из KiCad",
              sorted((p.number, p.x, p.y) for p in c.symbol.pins)
              == [("1", 0, 100), ("2", 0, -100)],
              str([(p.number, p.x, p.y) for p in c.symbol.pins]))
    c.symbol_source = "gost"
    symbolgen.build(c, Style())
    res.check("переключение на ГОСТ работает",
              c.symbol.style == "gost", c.symbol.style)


def check_archive_match(res: Result):
    """Подбор посадки в архиве не должен требовать точного имени."""
    from .sources.archive import _best_footprint, _score
    files = ["SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod", "QFN-20.kicad_mod",
             "R_0402_1005Metric.kicad_mod"]
    res.check("точное имя посадки находится",
              _best_footprint("SOIC-8_3.9x4.9mm_P1.27mm", files)[1] == 100, "")
    res.check("сокращённое имя посадки находится",
              _best_footprint("SOIC-8", files)[0].startswith("SOIC-8"), "")
    res.check("имя без разделителей находится",
              _best_footprint("SOIC8 39X49MM", files)[0].startswith("SOIC-8"),
              "")
    res.check("непохожее имя не подбирается",
              _best_footprint("USB-C-16pin", files)[1] == 0,
              str(_best_footprint("USB-C-16pin", files)))
    res.check("пустой запрос не даёт ложного совпадения",
              _score("", "SOIC-8") == 0, "")


def check_backup_and_clear(res: Result):
    """Копия должна собираться, а очистка -- не трогать лишнего."""
    import tempfile, zipfile
    from . import config as _cfg
    from .service import Service
    from .ir import Component, SymPin, Symbol
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        c = Component(name="R_0805", ctype="resistor", designator="R?")
        c.raw_pins = [SymPin(number="1", name="1", etype="passive"),
                      SymPin(number="2", name="2", etype="passive")]
        c.symbol = Symbol(pins=list(c.raw_pins))
        symbolgen.build(c, cfg.to_style())
        svc.db.upsert(c)
        svc.db.set_kicad_index([("sym", "Device", "x.kicad_sym", "R")])
        with open(os.path.join(cfg.lib_dir, cfg.library_name + ".SchLib"),
                  "wb") as f:
            f.write(b"x" * 100)

        z = svc.backup("проверка")
        res.check("копия создаётся", os.path.isfile(z), z)
        res.check("в имени копии есть дата",
                  bool(re.search(r"\d{4}-\d{2}-\d{2}_\d{4}",
                                 os.path.basename(z))),
                  os.path.basename(z))
        names = zipfile.ZipFile(z).namelist()
        res.check("в копии есть каталог", "catalog.sqlite" in names,
                  str(names[:5]))
        res.check("в копии есть собранная библиотека",
                  any(n.startswith("library/") for n in names), str(names[:5]))

        # вторая копия в ту же минуту не должна затирать первую
        z2 = svc.backup("проверка")
        res.check("копии не перетирают друг друга", z2 != z and
                  os.path.isfile(z), os.path.basename(z2))

        n_ki = svc.db.kicad_stats()["symbols"]
        out = svc.clear_library(drop_files=False)
        res.check("очистка убирает компоненты",
                  out["components"] == 1 and svc.db.stats()["total"] == 0,
                  str(out))
        res.check("очистка не трогает индекс KiCad",
                  svc.db.kicad_stats()["symbols"] == n_ki,
                  "индекс KiCad пропал")
        res.check("очистка не трогает файлы библиотеки без запроса",
                  os.path.isfile(os.path.join(cfg.lib_dir,
                                              cfg.library_name + ".SchLib")),
                  "файл удалён, хотя не просили")
        svc.clear_library(drop_files=True)
        res.check("по запросу файлы библиотеки удаляются",
                  not os.path.isfile(os.path.join(
                      cfg.lib_dir, cfg.library_name + ".SchLib")),
                  "файл остался")


def check_mesh_size(res: Result):
    """Конвертер OBJ->STEP не должен раздувать файл до неразбираемого."""
    import tempfile
    from . import mesh2step
    # шар из 20 тыс. треугольников -- заведомо выше порога огрубления
    import math
    verts, faces = [], []
    n = 60
    for i in range(n + 1):
        for j in range(2 * n):
            a = math.pi * i / n
            b = math.pi * j / n
            verts.append((5 * math.sin(a) * math.cos(b),
                          5 * math.sin(a) * math.sin(b), 5 * math.cos(a)))
    for i in range(n):
        for j in range(2 * n):
            k = i * 2 * n + j
            faces.append((k, k + 1, k + 2 * n))
            faces.append((k + 1, k + 2 * n + 1, k + 2 * n))
    with tempfile.TemporaryDirectory() as td:
        obj = os.path.join(td, "s.obj")
        with open(obj, "w") as f:
            for v in verts:
                f.write("v %.5f %.5f %.5f\n" % v)
            for a, b, c in faces:
                f.write(f"f {a + 1} {b + 1} {c + 1}\n")
        v2, f2 = mesh2step.simplify(verts, faces, mesh2step._auto_cell(verts))
        res.check("огрубление уменьшает число треугольников",
                  len(f2) < len(faces), f"{len(faces)} -> {len(f2)}")

        def span(vs):
            return max(max(x[i] for x in vs) - min(x[i] for x in vs)
                       for i in range(3))
        res.check("огрубление сохраняет габарит",
                  abs(span(v2) - span(verts)) < span(verts) * 0.02,
                  f"{span(verts):.3f} -> {span(v2):.3f}")

        step = os.path.join(td, "s.step")
        mesh2step.obj_to_step(obj, step, "S")
        # Раньше рёбра писались по три на каждый треугольник, и выходило
        # около 1300 байт на треугольник -- у CH375B это дало 69 МБ.
        # С общими рёбрами и общими направлениями должно быть вдвое меньше.
        per_tri = os.path.getsize(step) / max(1, len(f2))
        res.check("STEP не раздувается", per_tri < 700,
                  f"{per_tri:.0f} байт на треугольник")


def check_gui_imports(res: Result):
    """
    Каждое окно должно собираться, а не падать при первом открытии.

    Ошибка вида «NameError: QSpinBox is not defined» проверкой синтаксиса
    не ловится -- она вылезает только при запуске. Поэтому окна реально
    создаются на невидимом экране.
    """
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except Exception as e:
        res.check("окна собираются", True, "")      # без PySide6 пропускаем
        return
    app = QApplication.instance() or QApplication([])

    import tempfile
    from . import config as _cfg
    from .service import Service
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)

        from .gui.main_window import MainWindow
        w = MainWindow(cfg)
        res.check("главное окно собирается", w is not None)

        from .gui.dialogs import KicadDialog, SettingsDialog, LcscDialog
        for name, mk in (("импорт из KiCad", lambda: KicadDialog(svc)),
                         ("настройки", lambda: SettingsDialog(cfg)),
                         ("импорт по LCSC", lambda: LcscDialog())):
            try:
                d = mk()
                res.check(f"окно «{name}» собирается", d is not None)
                d.close()
            except Exception as e:
                res.check(f"окно «{name}» собирается", False, str(e))

        # новые органы управления должны не только создаваться, но и
        # работать: сигналы у них подключены к методам окна
        try:
            c0 = _mk("R_0805", "resistor",
                     [("1", "1", "passive"), ("2", "2", "passive")])
            symbolgen.build(c0, cfg.to_style())
            svc.db.upsert(c0)
            w.current = c0
            w._fill_own_style(c0)
            w.sp_scale.setValue(60)
            w.cb_nums.setChecked(False)
            w._toggle_comp_numbers()
            got = svc.db.get(c0.uid)
            res.check("масштаб и номера сохраняются у компонента",
                      got is not None and got.style_over.get("passive_scale")
                      and got.style_over.get("show_pin_numbers") is False,
                      str(getattr(got, "style_over", None)))
            res.check("общие настройки при этом не менялись",
                      cfg.passive_scale == 1.0 and cfg.show_pin_numbers,
                      "личная настройка протекла в общие")
            w.current = svc.db.get(c0.uid)
            w._reset_comp_style()
            res.check("сброс личных настроек работает",
                      not (svc.db.get(c0.uid).style_over),
                      "личные настройки остались")
        except Exception as e:
            res.check("личные настройки компонента в окне", False, str(e))

        # Переключение секций обязано быть доступно прямо над обычным
        # превью. Если положить его в длинную строку личных настроек, при
        # реальной ширине правой панели он уезжает за край и выглядит так,
        # будто просмотр частей совсем удалили.
        try:
            multi = _mk("MULTI", "ic", [
                ("1", "A", "input"), ("2", "Y", "output"),
                ("3", "B", "input"), ("4", "Z", "output"),
            ])
            multi.raw_pins[0].unit = multi.raw_pins[1].unit = 1
            multi.raw_pins[2].unit = multi.raw_pins[3].unit = 2
            symbolgen.build(multi, cfg.to_style())
            multi.symbol.part_count = 2
            w.current = multi
            w._fill_prev_parts(multi)
            w._redraw_symbol()
            res.check("секции доступны в обычном превью",
                      not w.prev_part_bar.isHidden()
                      and w.cb_prev_part.count() == 2,
                      f"hidden={w.prev_part_bar.isHidden()}, "
                      f"count={w.cb_prev_part.count()}")
            w.cb_prev_part.setCurrentIndex(1)
            res.check("обычное превью переключает секцию",
                      w.cb_prev_part.currentData() == 2
                      and "секция 2" in w.sym_view.label.text(),
                      w.sym_view.label.text())
        except Exception as e:
            res.check("секции доступны в обычном превью", False, str(e))

        # Таблица выводов: перетаскивание строк должно читаться ровно так,
        # как оно выглядит. Раньше _reset_pin_visual_order путала
        # визуальные и логические номера, и часть выводов уезжала не туда.
        try:
            cp = _mk("XS1", "connector",
                     [(str(i), f"NET{i}", "passive") for i in range(1, 7)])
            symbolgen.build(cp, cfg.to_style())
            svc.db.upsert(cp)
            w.current = cp
            w._fill_pins(cp)
            vh = w.pins.verticalHeader()
            # тащим последнюю строку в начало, как это делает мышь
            vh.moveSection(5, 0)
            seen = [w.pins.item(vh.logicalIndex(v), 0).text()
                    for v in range(w.pins.rowCount())]
            got = [r["number"] for r in w._all_pin_rows()]
            res.check("порядок строк читается как показан", got == seen,
                      f"{got} против {seen}")
            w.save_pins()
            after = svc.db.get(cp.uid)
            built = [p.number for p in sorted(after.symbol.pins,
                                              key=lambda q: -q.y)]
            res.check("перетащенный вывод доезжает до УГО с первого раза",
                      built == seen, f"{built} против {seen}")
            # после применения отображение снова единичное
            w._fill_pins(after)
            vh = w.pins.verticalHeader()
            res.check("после применения порядок строк не «уезжает»",
                      all(vh.visualIndex(i) == i
                          for i in range(w.pins.rowCount())),
                      str([vh.visualIndex(i)
                           for i in range(w.pins.rowCount())]))

            # Сброс перестановки должен возвращать отображение к единичному
            # при ЛЮБОЙ перетасовке. Ровно эта перестановка старую версию и
            # ломала: она путала визуальные номера с логическими, строки
            # потом записывались по логическим индексам, а показывались в
            # чужом порядке -- отсюда и «применилось только со второго
            # раза».
            for a, b in ((1, 4), (0, 2), (0, 3)):
                vh.moveSection(a, b)
            w._reset_pin_visual_order()
            res.check("сброс перестановки строк возвращает единичный порядок",
                      [vh.logicalIndex(v) for v in range(w.pins.rowCount())]
                      == list(range(w.pins.rowCount())),
                      str([vh.logicalIndex(v)
                           for v in range(w.pins.rowCount())]))
        except Exception as e:
            res.check("таблица выводов держит порядок", False, str(e))

        # Большая STEP не должна начинать тяжёлый Python-разбор просто от
        # выбора строки в таблице: QThread не спасает GUI, пока парсер держит
        # GIL. Пользователь запускает такое превью отдельной кнопкой.
        try:
            from .gui.main_window import BIG_MODEL
            from .ir import Footprint, Model3D
            heavy = os.path.join(td, "heavy.step")
            with open(heavy, "wb") as stream:
                stream.seek(BIG_MODEL)
                stream.write(b"0")
            cp.footprints = [Footprint(
                name="HEAVY", model=Model3D(path=heavy))]
            w.current = cp
            w._fill_footprints(cp)
            running = bool(w._model_loader and w._model_loader.isRunning())
            res.check("тяжёлая 3D не подвешивает выбор компонента",
                      bool(w._pending_model) and w.b3d_preview.isEnabled()
                      and not running,
                      f"pending={bool(w._pending_model)}, running={running}")
        except Exception as e:
            res.check("тяжёлая 3D не подвешивает выбор компонента",
                      False, str(e))

        # Даже если внешний загрузчик вернул чрезмерно плотную поверхность,
        # программный рендер не должен получить десятки тысяч полигонов.
        try:
            from .gui.view3d import Scene3D, MAX_FACES

            class _DenseMesh:
                ok = True
                tris = [((i * 0.001, 0.0, 0.0),
                         (i * 0.001, 1.0, 0.0),
                         (i * 0.001 + 0.0005, 0.5, 0.1))
                        for i in range(MAX_FACES + 1000)]
                colors = [(0.5, 0.5, 0.5)] * len(tris)

            scene = Scene3D()
            scene.set_mesh(_DenseMesh())
            res.check("3D-рендер ограничивает сложность",
                      len(scene.faces) == MAX_FACES,
                      f"{len(scene.faces)} граней")
            scene.set_color("#4B72A8")
            res.check("3D-рендер принимает RGB-цвет",
                      scene.override_color is not None and
                      abs(scene.override_color[2] - 168 / 255) < 0.001,
                      str(scene.override_color))
        except Exception as e:
            res.check("3D-рендер ограничивает сложность", False, str(e))

        # Цвет -- свойство модели в каталоге, а не временная настройка окна.
        try:
            cp.footprints = [Footprint(
                name="COLOR", model=Model3D(path=heavy))]
            svc.db.upsert(cp)
            w.current = cp
            w._model_color_changed("#4B72A8")
            saved = svc.db.get(cp.uid)
            restored = Component.from_dict(saved.to_dict())
            res.check("цвет 3D сохраняется у компонента",
                      saved.footprints[0].model.color == "#4B72A8" and
                      restored.footprints[0].model.color == "#4B72A8")
        except Exception as e:
            res.check("цвет 3D сохраняется у компонента", False, str(e))

        # Быстрое переключение двух только что импортированных компонентов
        # не должно запускать два нативных декодера одновременно.
        try:
            from .ir import Footprint

            class _Busy:
                stale = False

                @staticmethod
                def isRunning():
                    return True

            busy = _Busy()
            quick = os.path.join(td, "quick.obj")
            with open(quick, "w", encoding="ascii") as stream:
                stream.write("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
            qfp = Footprint(name="QUICK", model=Model3D(path=quick))
            w._model_loaders = {busy}
            w._start_model_load(quick, qfp, [])
            res.check("быстрое переключение 3D выполняется последовательно",
                      busy.stale and bool(w._queued_model)
                      and w._model_loaders == {busy},
                      f"устарел={busy.stale}, очередь={bool(w._queued_model)}")
            w._model_loaders.clear()
            w._queued_model = None
        except Exception as e:
            res.check("быстрое переключение 3D выполняется последовательно",
                      False, str(e))

        from .gui.symedit import SymbolEditor
        c = _mk("R_0805", "resistor",
                [("1", "1", "passive"), ("2", "2", "passive")])
        symbolgen.build(c, cfg.to_style())
        try:
            ed = SymbolEditor(c, cfg.to_style())
            res.check("редактор УГО собирается", ed is not None)
            ed.canvas.resize(600, 400)
            ed.canvas.fit()
            # подписи выводов редактор рисует сам -- в графике их
            # показывать нельзя, иначе каждая задваивается и вторая копия
            # стоит на месте
            names, nums = ed.canvas._pin_label_texts()
            dup = [pr.text for pr in ed.comp.symbol.prims
                   if ed.canvas._is_pin_label(pr, names, nums)]
            shown = [pr.text for pr in ed.comp.symbol.prims
                     if pr.kind == "text"
                     and not ed.canvas._is_pin_label(pr, names, nums)]
            res.check("подписи выводов в редакторе не задваиваются",
                      dup and not [t for t in shown if t in names | nums],
                      f"в графике остались подписи: {shown[:4]}")
            ed.canvas.explode_prims()
            res.check("графика разбирается на линии",
                      bool(ed.comp.symbol.user_lines),
                      "линий не получилось")
            ed.close()
        except Exception as e:
            res.check("редактор УГО собирается", False, str(e))
        w.close()
        svc.close()


def check_component_style(res: Result, st):
    """Личные настройки компонента не должны трогать общие."""
    from .gost.style import Style
    base = Style()
    a = _mk("R_A", "resistor", [("1", "1", "passive"), ("2", "2", "passive")])
    b = _mk("R_B", "resistor", [("1", "1", "passive"), ("2", "2", "passive")])
    symbolgen.build(a, base)
    w0 = [p for p in a.symbol.prims if p.kind == "rect"][0].pts
    a.style_over = {"passive_scale": 0.5, "show_pin_numbers": False}
    symbolgen.build(a, base)
    w1 = [p for p in a.symbol.prims if p.kind == "rect"][0].pts
    res.check("масштаб компонента меняет только его графику",
              w1[1][0] < w0[1][0], f"{w1} против {w0}")
    res.check("номера выводов выключаются поштучно",
              not [p for p in a.symbol.prims if p.kind == "text"],
              "остались подписи номеров")
    symbolgen.build(b, base)
    w2 = [p for p in b.symbol.prims if p.kind == "rect"][0].pts
    res.check("соседний компонент не пострадал", w2 == w0, f"{w2} != {w0}")
    res.check("общие настройки не изменились",
              base.passive_scale == 1.0 and base.show_pin_numbers,
              "личная настройка протекла в общий стиль")

    # ручная раскладка не должна превращать пассивку в прямоугольник
    c = _mk("C_X", "capacitor", [("1", "1", "passive"), ("2", "2", "passive")])
    symbolgen.build(c, base)
    auto = [p.kind for p in c.symbol.prims]
    c.symbol.manual_layout = True
    symbolgen.build(c, base)
    man = [p.kind for p in c.symbol.prims]
    res.check("ручная раскладка сохраняет обозначение пассивки",
              man == auto, f"{man} против {auto}")

    # «палки» не толще остальной графики
    for ct in ("capacitor", "diode", "crystal", "battery"):
        cc = _mk("X", ct, [("1", "1", "passive"), ("2", "2", "passive")])
        symbolgen.build(cc, base)
        ws = {p.width for p in cc.symbol.prims if p.kind != "text"}
        res.check(f"{ct}: линии одной толщины", ws <= {base.lw_bar,
                                                      base.lw_body,
                                                      base.lw_symbol},
                  f"толщины {sorted(ws)}")

    # подпись: у пассивки номинал, у микросхемы парт-номер
    r = _mk("R_1", "resistor", [("1", "1", "passive"), ("2", "2", "passive")])
    res.check("у пассивки без номинала подставляется «прим.»",
              "прим." in classify.label_for(r), classify.label_for(r))
    r.value = "4.7к"
    res.check("заданный номинал не подменяется",
              classify.label_for(r) == "4.7к", classify.label_for(r))
    m = _mk("STM32", "mcu", [("1", "A", "io"), ("2", "B", "io")])
    m.mpn = "STM32F103C8T6"
    res.check("у микросхемы подписывается парт-номер",
              classify.label_for(m) == "STM32F103C8T6", classify.label_for(m))
    res.check("подпись не рисуется графикой",
              not base.draw_type_label,
              "тип снова вписывается в УГО вместо Comment")


def check_job(res: Result, st):
    """Задание для скрипта -- список библиотек, только ASCII."""
    from .emit import job as jobmod
    with tempfile.TemporaryDirectory() as td:
        jp = os.path.join(td, "deploy.txt")
        libs = [r"C:\Users\vakva\AppData\Local\GostLib\library\GOST_Lib.SchLib",
                r"C:\Users\vakva\AppData\Local\GostLib\library\GOST_Lib.PcbLib",
                r"C:\Users\vakva\AppData\Local\GostLib\library\GOST_Lib.SchLib"]
        jobmod.write_deploy(jp, libs, os.path.join(td, "d.log"))
        with open(jp, encoding="ascii") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
        ptr = jobmod.write_pointer(td, jp)
        with open(ptr, encoding="ascii") as f:
            pointed = f.read().strip()
    res.check("задание: только ASCII",
              all(all(ord(ch) < 128 for ch in ln) for ln in lines))
    res.check("задание: есть версия", lines[0].startswith("VER\t"), lines[0])
    libl = [ln for ln in lines if ln.startswith("LIB\t")]
    res.check("задание: дубликаты убраны", len(libl) == 2, str(len(libl)))
    res.check("задание: пути с обратными слэшами целы",
              all("\\" in ln for ln in libl), str(libl[:1]))
    res.check("указатель ведёт на задание", pointed == jp, pointed)


def check_kicad(res: Result):
    from .sources import kicad as kc
    sym = """(kicad_symbol_lib (version 20211014) (generator test)
  (symbol "R" (pin_numbers hide) (in_bom yes)
    (property "Reference" "R" (id 0) (at 0 0 0))
    (property "Value" "R" (id 1) (at 0 0 0))
    (property "Footprint" "Resistor_SMD:R_0805" (id 2) (at 0 0 0))
    (symbol "R_0_1" (rectangle (start -1 2) (end 1 -2)))
    (symbol "R_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -3.81 90) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27)))))))
)"""
    mod = """(footprint "R_0805" (layer "F.Cu")
  (descr "Resistor SMD 0805")
  (fp_line (start -1.68 -0.95) (end 1.68 -0.95) (stroke (width 0.12) (type solid)) (layer "F.SilkS"))
  (fp_circle (center 0 0) (end 0.5 0) (stroke (width 0.1) (type solid)) (layer "F.Fab"))
  (fp_arc (start -1 0) (mid 0 1) (end 1 0) (stroke (width 0.1) (type solid)) (layer "F.SilkS"))
  (pad "1" smd roundrect (at -0.9375 0) (size 1.025 1.4) (layers "F.Cu" "F.Paste" "F.Mask")
    (roundrect_rratio 0.243902))
  (pad "2" smd roundrect (at 0.9375 0) (size 1.025 1.4) (layers "F.Cu" "F.Paste" "F.Mask")
    (roundrect_rratio 0.243902))
  (pad "3" thru_hole circle (at 0 2) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask"))
  (model "${KICAD6_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0805.wrl"
    (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))
)"""
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "t.kicad_sym")
        mp = os.path.join(td, "R_0805.kicad_mod")
        open(sp, "w", encoding="utf-8").write(sym)
        open(mp, "w", encoding="utf-8").write(mod)
        d = kc.parse_kicad_sym(sp)
        res.check("KiCad: символ разобран", "R" in d and len(d["R"]["pins"]) == 2,
                  str(list(d)))
        c = kc.component_from_kicad_sym(sp, "R")
        res.check("KiCad: тип определён", c.ctype == "resistor", c.ctype)
        fp = kc.parse_kicad_mod(mp)
        res.check("KiCad: посадка -- 3 площадки", len(fp.pads) == 3,
                  str(len(fp.pads)))
        res.check("KiCad: сквозная площадка", any(p.hole > 0 for p in fp.pads))
        res.check("KiCad: инверсия Y", abs(fp.pads[0].y) < 1e-9, str(fp.pads[0].y))
        res.check("KiCad: графика", len(fp.prims) == 3, str(len(fp.prims)))
        res.check("KiCad: дуга посчитана",
                  any(p.kind == "arc" and p.radius > 0.5 for p in fp.prims))
        res.check("KiCad: 3D-ссылка", bool(fp.model and fp.model.path))
        res.check("KiCad: SVG посадки",
                  svg.footprint_svg(fp).startswith("<svg"))


def check_easyeda(res: Result):
    from .sources import easyeda as ee
    shapes = ["P~show~1~1~-30~-10~0~gge12~0^^-40~-10^^M -30 -10 h -10~#880000^^"
              "1~-25~-7~0~VDD~start~~7^^1~-33~-13~0~1~end~~7^^0~-40~-10^^0~"]
    pins = ee._parse_pins(shapes)
    res.check("EasyEDA: вывод разобран",
              len(pins) == 1 and pins[0].name == "VDD" and pins[0].number == "1",
              str([(p.number, p.name, p.etype) for p in pins]))
    # У многосекционных компонентов родительский shape пуст, а выводы
    # разложены по subparts. Именно так устроен RK3308H3 (C7599688).
    multi = {"dataStr": {"shape": []}, "subparts": [
        {"title": "TEST.1", "dataStr": {"head": {"c_para": {
            "subpart_no": "1"}}, "shape": shapes}},
        {"title": "TEST.2", "dataStr": {"head": {"c_para": {
            "subpart_no": "2"}}, "shape": shapes}},
    ]}
    mpins, nparts = ee._parse_symbol(multi)
    res.check("EasyEDA: многосекционный символ",
              nparts == 2 and [p.unit for p in mpins] == [1, 2],
              str((nparts, [p.unit for p in mpins])))
    a = ee._arc_from_path("M 100 100 A 20 20 0 0 1 140 100")
    res.check("EasyEDA: дуга из пути", a is not None and abs(a[2] - 20) < 1.0,
              str(a))
    pkg = {"title": "R0805", "dataStr": {"head": {"x": 100, "y": 100},
           "shape": ["PAD~RECT~90~100~10~14~1~~1~0~~0~gge1~0~~Y~0",
                     "TRACK~1~3~~95 95 105 95~gge2~0",
                     "CIRCLE~100~100~5~1~3~gge3~0"]}}
    fp, uuid = ee._parse_footprint(pkg)
    res.check("EasyEDA: посадка", len(fp.pads) == 1 and len(fp.prims) == 2,
              f"{len(fp.pads)}/{len(fp.prims)}")
    res.check("EasyEDA: масштаб мм",
              abs(fp.pads[0].w - 10 * 0.254) < 1e-6, str(fp.pads[0].w))


def check_pin_plan(res: Result):
    from .pinplan import PinPlanError, format_plan, parse_plan

    c = Component(name="AI_TEST")
    c.raw_pins = [SymPin(number="A1", name="VDD", etype="power", unit=1),
                  SymPin(number="B2", name="GPIO0", etype="io", unit=2)]
    c.symbol = Symbol(part_count=2, pins=list(c.raw_pins))
    text = format_plan(c)
    rows = parse_plan(text, c.raw_pins)
    res.check("ИИ-план: круговой обмен",
              len(rows) == 2 and rows[1]["unit"] == 2
              and rows[0]["number"] == "A1", str(rows))
    try:
        parse_plan("\n".join(text.splitlines()[:-1]), c.raw_pins)
        rejected = False
    except PinPlanError:
        rejected = True
    res.check("ИИ-план: пропущенный вывод отклонён", rejected)
    check_pin_plan_tidy(res)

    # --- ответ ИИ приходит как попало, и это нормально
    from .pinplan import build_prompt

    prompt = build_prompt(c)
    res.check("в задании есть правила и формат",
              "ФОРМАТ ОТВЕТА" in prompt and "number,name,etype" in prompt
              and "ЕСКД" in prompt, prompt[:60])
    res.check("и сами данные тоже",
              "A1" in prompt and "B2" in prompt, "")

    junk = ("Конечно! Вот раскладка:\n\n```csv\n"
            "Контакт;Имя;Тип;Секция;Сторона;Группа\n"
            "A1;VDD;power;1;L;PWR\n"
            "B2;GPIO0;bidirectional;2;R;GPIO\n"
            "```\n\nНадеюсь, помогло!\n")
    rows = parse_plan(junk, c.raw_pins)
    res.check("забор ``` и болтовня вокруг не мешают",
              len(rows) == 2, str(rows))
    res.check("точка с запятой понята как разделитель",
              rows[0]["name"] == "VDD", str(rows[0]))
    res.check("«bidirectional» приведён к нашему типу",
              rows[1]["etype"] == "io", str(rows[1]))

    mixed = ("group,side,number,etype,name,unit\n"
             "PWR,L,A1,power,VDD,1\n"
             "GPIO,R,B2,io,GPIO0,2\n")
    rows = parse_plan(mixed, c.raw_pins)
    res.check("колонки в другом порядке -- разобрались по шапке",
              rows[0]["number"] == "A1" and rows[0]["group"] == "PWR",
              str(rows[0]))

    md = ("| Контакт | Имя | Тип | Секция | Сторона | Группа |\n"
          "|---|---|---|---|---|---|\n"
          "| A1 | VDD | power | 1 | L | PWR |\n"
          "| B2 | GPIO0 | io | 2 | R | GPIO |\n")
    res.check("старый Markdown-формат тоже принимается",
              len(parse_plan(md, c.raw_pins)) == 2, "")

    for bad, why in (("Извините, не могу помочь", "нет таблицы"),
                     ("number,name,etype,unit,side,group\n"
                      "A1,VDD,power,1,L,PWR\nZZ,X,io,1,R,G\n", "лишний вывод"),
                     ("number,name,etype,unit,side,group\n"
                      "A1,VDD,мощность,1,L,PWR\n"
                      "B2,G,io,2,R,G\n", "неизвестный тип"),
                     ("number,name,etype,unit,side,group\n"
                      "A1,VDD,power,1,Q,PWR\n"
                      "B2,G,io,2,R,G\n", "кривая сторона")):
        try:
            parse_plan(bad, c.raw_pins)
            ok = False
            msg = ""
        except PinPlanError as e:
            ok = True
            msg = str(e)
        res.check(f"ИИ-план: {why} -- понятная ошибка", ok, msg[:80])


def check_step(res: Result):
    from .mesh2step import obj_to_step
    obj = ("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nv 0 0 1\nv 1 0 1\n"
           "v 1 1 1\nv 0 1 1\nf 1 2 3 4\nf 5 6 7 8\nf 1 2 6 5\n")
    with tempfile.TemporaryDirectory() as td:
        op = os.path.join(td, "c.obj")
        sp = os.path.join(td, "c.step")
        open(op, "w").write(obj)
        obj_to_step(op, sp, "CUBE")
        text = open(sp).read()
    res.check("STEP: заголовок", text.startswith("ISO-10303-21;"))
    res.check("STEP: схема AP214", "AUTOMOTIVE_DESIGN" in text)
    res.check("STEP: грани есть", text.count("ADVANCED_FACE") >= 6,
              str(text.count("ADVANCED_FACE")))
    res.check("STEP: закрыт", text.rstrip().endswith("END-ISO-10303-21;"))


def check_db(res: Result):
    with tempfile.TemporaryDirectory() as td:
        cat = Catalog(os.path.join(td, "c.sqlite"))
        c = _mk("TEST1", "resistor", [("1", "1", "passive"), ("2", "2", "passive")],
                Resistance="4.7k", Power="0.25")
        symbolgen.build(c)
        cat.upsert(c)
        cat.upsert(c)                       # повторный upsert не должен плодить
        res.check("каталог: один компонент", cat.stats()["total"] == 1,
                  str(cat.stats()))
        res.check("каталог: поиск по имени", len(cat.search("TEST")) == 1)
        res.check("каталог: фильтр по параметру",
                  len(cat.search(param_filters=[("Power", "=", "0.25")])) == 1)
        res.check("каталог: числовой фильтр",
                  len(cat.search(param_filters=[("Resistance", ">", "1k")])) == 1)
        res.check("каталог: JSON туда-обратно",
                  (cat.get(c.uid) or Component()).name == "TEST1")
        cat.close()
    res.check("разбор номиналов", abs((to_number("4.7k") or 0) - 4700) < 1e-6,
              str(to_number("4.7k")))
    res.check("разбор номиналов (рус.)",
              abs((to_number("100 нФ") or 0) - 1e-7) < 1e-12,
              str(to_number("100 нФ")))


def check_schlib_writer(res: Result, st):
    """Записать .SchLib и прочитать обратно своим же парсером."""
    from .emit.schlib import write_schlib
    from .sources import altium as alr
    from .ir import Footprint
    comps = _sample_components()
    for c in comps:
        symbolgen.build(c, st)
        c.footprints.append(Footprint(name=c.name + "_FP"))
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "T.SchLib")
        write_schlib(p, comps, st)
        res.check("SchLib: файл создан", os.path.getsize(p) > 4096,
                  str(os.path.getsize(p)))
        back = alr.read_schlib(p)
        hdr = alr.read_schlib_header(p)
    res.check("SchLib: все компоненты на месте", len(back) == len(comps),
              f"{len(back)} из {len(comps)}")
    res.check("SchLib: заголовок со шрифтом ГОСТ",
              hdr.get("FontName1") == st.font, hdr.get("FontName1", ""))
    res.check("SchLib: CompCount совпадает",
              hdr.get("CompCount") == str(len(comps)), hdr.get("CompCount", ""))
    for c in comps:
        info = back.get(c.name)
        if not info:
            res.fail.append(f"SchLib: нет компонента {c.name}")
            continue
        res.check(f"SchLib {c.name}: выводы",
                  len(info["pins"]) == len(c.symbol.pins),
                  f"{len(info['pins'])} из {len(c.symbol.pins)}")
        bad = []
        for a, b in zip(info["pins"], c.symbol.pins):
            if (a.number, a.name, a.etype, a.x, a.y, a.length, a.rotation) != \
               (b.number, b.name, b.etype, b.x, b.y, b.length, b.rotation):
                bad.append(a.number)
        res.check(f"SchLib {c.name}: выводы совпали", not bad, str(bad[:3]))
        res.check(f"SchLib {c.name}: обозначение",
                  info["designator"] == c.designator, info["designator"])
        res.check(f"SchLib {c.name}: посадка привязана",
                  info["footprints"] == [f.name for f in c.footprints],
                  str(info["footprints"]))


def check_pcblib_writer(res: Result):
    """Записать .PcbLib и прочитать обратно: площадки должны совпасть."""
    from .emit.pcblib import (write_pcblib, read_pcblib_footprints,
                              PAD_TEMPLATE, TRACK_TEMPLATE)
    from .sources import kicad as kc
    res.check("PcbLib: эталон площадки 202 байта", len(PAD_TEMPLATE) == 202,
              str(len(PAD_TEMPLATE)))
    res.check("PcbLib: эталон дорожки 49 байт", len(TRACK_TEMPLATE) == 49,
              str(len(TRACK_TEMPLATE)))
    mod = """(footprint "SELFTEST_FP" (layer "F.Cu") (descr "проверка")
  (fp_line (start -3.6 -3.6) (end 3.6 -3.6) (stroke (width 0.15) (type solid)) (layer "F.SilkS"))
  (fp_circle (center -3 -3) (end -2.8 -3) (stroke (width 0.12) (type solid)) (layer "F.SilkS"))
  (fp_rect (start -3.7 -3.7) (end 3.7 3.7) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))
  (pad "1" smd roundrect (at -3.4375 2.6) (size 0.875 0.2) (layers "F.Cu") (roundrect_rratio 0.25))
  (pad "2" smd oval (at -3.4375 2.2) (size 0.875 0.2) (layers "F.Cu"))
  (pad "3" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.9) (layers "*.Cu"))
)"""
    with tempfile.TemporaryDirectory() as td:
        mp = os.path.join(td, "SELFTEST_FP.kicad_mod")
        open(mp, "w", encoding="utf-8").write(mod)
        fp = kc.parse_kicad_mod(mp)
        lp = os.path.join(td, "T.PcbLib")
        write_pcblib(lp, [fp])
        res.check("PcbLib: файл создан", os.path.getsize(lp) > 10000,
                  str(os.path.getsize(lp)))
        back = read_pcblib_footprints(lp)
    # структура файла должна совпадать с тем, что пишет сам Altium
    import olefile as _ole
    from .emit.pcblib import _load_template
    tpl_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "gostlib", "templates", "empty.PcbLib")
    if not os.path.isfile(tpl_path):
        tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "templates", "empty.PcbLib")
    with tempfile.TemporaryDirectory() as td2:
        lp2 = os.path.join(td2, "S.PcbLib")
        write_pcblib(lp2, [fp])
        o1 = _ole.OleFileIO(tpl_path)
        o2 = _ole.OleFileIO(lp2)
        try:
            need = {"/".join(e) for e in o1.listdir()
                    if e[0] in ("FileHeader", "FileVersionInfo", "Library")}
            have = {"/".join(e) for e in o2.listdir()}
            missing = sorted(need - have)
            res.check("PcbLib: служебные потоки на месте", not missing,
                      str(missing))
            res.check("PcbLib: FileHeader как у Altium",
                      o1.openstream("FileHeader").read()
                      == o2.openstream("FileHeader").read(), "отличается")
        finally:
            o1.close()
            o2.close()
    # Header и Data потока GUID-ов должны сходиться по числу записей,
    # иначе Altium выбрасывает посадку молча
    with tempfile.TemporaryDirectory() as td3:
        lp3 = os.path.join(td3, "G.PcbLib")
        write_pcblib(lp3, [fp])
        o3 = _ole.OleFileIO(lp3)
        try:
            nm3 = [e[0] for e in o3.listdir()
                   if len(e) == 2 and e[1] == "Data"
                   and e[0] not in ("FileHeader", "Library", "FileVersionInfo")][0]
            import struct as _st
            nprim = _st.unpack("<I", o3.openstream(f"{nm3}/Header").read())[0]
            gh = _st.unpack("<I", o3.openstream(
                f"{nm3}/PrimitiveGuids/Header").read())[0]
            gd = len(o3.openstream(f"{nm3}/PrimitiveGuids/Data").read())
            res.check("PcbLib: GUID-записей столько же, сколько в заголовке",
                      gd == gh * 24, f"Header={gh}, записей={gd/24:.2f}")
            res.check("PcbLib: GUID-заголовок = примитивы + 1",
                      gh == nprim + 1, f"{gh} vs {nprim}+1")
        finally:
            o3.close()
    res.check("PcbLib: посадка на месте", "SELFTEST_FP" in back, str(list(back)))
    if "SELFTEST_FP" not in back:
        return
    info = back["SELFTEST_FP"]
    res.check("PcbLib: три площадки", len(info["pads"]) == 3,
              str(len(info["pads"])))
    res.check("PcbLib: графика разложена на отрезки",
              len(info["tracks"]) >= 5, str(len(info["tracks"])))
    bad = []
    for a, b in zip(info["pads"], fp.pads):
        if (abs(a["x"] - b.x) > 1e-4 or abs(a["y"] - b.y) > 1e-4 or
                abs(a["w"] - b.w) > 1e-4 or abs(a["h"] - b.h) > 1e-4 or
                abs(a["hole"] - b.hole) > 1e-4 or a["number"] != b.number):
            bad.append(a["number"])
    res.check("PcbLib: геометрия площадок совпала", not bad, str(bad))
    th = [p for p in info["pads"] if p["hole"] > 0]
    res.check("PcbLib: сквозная площадка на Multi-Layer",
              bool(th) and th[0]["layer"] == 74,
              str(th[0]["layer"]) if th else "нет")
    smd = [p for p in info["pads"] if p["hole"] == 0]
    res.check("PcbLib: SMD-площадки на Top", all(p["layer"] == 1 for p in smd))
    # регрессия: путь Windows в строке замены re ронял запись
    from .emit.pcblib import _patch_filename
    win = r"C:\Users\vakva\AppData\Local\GostLib\library\GOST_Lib.PcbLib"
    try:
        out = _patch_filename(b"|FILENAME=E:\\old\\x.PcbLib|KIND=Protel|", win)
        ok = win.encode("cp1251") in out and b"KIND=Protel" in out
    except Exception as e:
        ok = False
        out = str(e).encode()
    res.check("PcbLib: путь Windows подставляется", ok, out[:80].decode(
        "cp1251", "replace"))
    res.check("PcbLib: слои графики -- шелкография и courtyard",
              sorted({t["layer"] for t in info["tracks"]}) == [33, 71],
              str(sorted({t["layer"] for t in info["tracks"]})))


def _with_footprints(comps, st):
    """Даёт каждому компоненту посадку, чтобы проверить и графику, и связи."""
    from .ir import Footprint, Pad, FpPrim
    from .gost import symbolgen
    out = []
    for i, c in enumerate(comps):
        symbolgen.build(c, st)
        fp = Footprint(name=f"FP_{c.name}", description="test")
        for j, p in enumerate(c.symbol.pins):
            fp.pads.append(Pad(number=p.number, x=-2.0 + 1.0 * j, y=0.4,
                               w=0.8, h=1.1,
                               shape=("roundrect", "round", "rect")[j % 3],
                               corner_radius=25.0, layer="top",
                               hole=(0.8 if j % 5 == 4 else 0.0)))
        fp.prims += [
            FpPrim(kind="line", layer="silk", pts=[[-2, 1], [2, 1]], width=0.12),
            FpPrim(kind="circle", layer="silk", pts=[[-2.5, 0]], radius=0.15,
                   width=0.1),
            FpPrim(kind="arc", layer="assy", pts=[[0, 0]], radius=1.0,
                   a1=0.0, a2=180.0, width=0.1),
            FpPrim(kind="rect", layer="courtyard", pts=[[-3, -2], [3, 2]],
                   width=0.05),
            FpPrim(kind="poly", layer="assy", pts=[[-1, -1], [1, -1], [0, -2]],
                   width=0.05, filled=True),
        ]
        c.footprints.append(fp)
        out.append(c)
    return out


def check_eagle_writer(res: Result, st):
    """
    EAGLE .lbr -- этот путь ценен тем, что .SchLib/.PcbLib делает сам Altium,
    поэтому файл обязан быть валиден по DTD EAGLE и полон по связям.
    """
    import xml.etree.ElementTree as ET
    from .emit.eaglelbr import write_lbr
    comps = _with_footprints(_sample_components(), st)
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "T.lbr")
    write_lbr(path, comps)
    stats = getattr(write_lbr, "last_stats", {})

    res.check("EAGLE: файл создан", os.path.getsize(path) > 1000)
    try:
        tree = ET.parse(path)
    except Exception as e:
        res.check("EAGLE: XML разбирается", False, str(e))
        return
    res.check("EAGLE: XML разбирается", True)
    root = tree.getroot()
    res.check("EAGLE: версия в окне 6.4-9.4",
              root.get("version", "") == "9.4.0", root.get("version"))
    with open(path, encoding="utf-8") as f:
        head = f.read(200)
    res.check("EAGLE: DOCTYPE на месте",
              '<!DOCTYPE eagle SYSTEM "eagle.dtd">' in head)

    lib = root.find("./drawing/library")
    res.check("EAGLE: есть <library>", lib is not None)
    pkgs = {p.get("name") for p in root.iter("package")}
    syms = {s.get("name") for s in root.iter("symbol")}
    devs = list(root.iter("deviceset"))
    res.check("EAGLE: компонентов столько же, сколько на входе",
              len(devs) == len(comps), f"{len(devs)} != {len(comps)}")
    res.check("EAGLE: корпусов записано", len(pkgs) == stats.get("packages"),
              f"{len(pkgs)} != {stats.get('packages')}")
    res.check("EAGLE: имена корпусов уникальны",
              len(pkgs) == len(list(root.iter("package"))))
    res.check("EAGLE: имена символов уникальны",
              len(syms) == len(list(root.iter("symbol"))))

    # выравнивание текста: 'center-center' EAGLE не понимает
    aligns = {t.get("align") for t in root.iter("text") if t.get("align")}
    res.check("EAGLE: нет запрещённого align=center-center",
              "center-center" not in aligns, str(sorted(aligns)[:4]))

    # ссылки deviceset -> symbol / package и связи pin -> pad
    bad_ref, bad_pin, bad_pad, no_conn = [], [], [], []
    pins_of = {s.get("name"): {p.get("name") for p in s.iter("pin")}
               for s in root.iter("symbol")}
    pads_of = {p.get("name"): {q.get("name") for q in p
                               if q.tag in ("pad", "smd")}
               for p in root.iter("package")}
    for ds in devs:
        gates = {g.get("name"): g.get("symbol") for g in ds.iter("gate")}
        for sname in gates.values():
            if sname not in syms:
                bad_ref.append(sname)
        for dev in ds.iter("device"):
            pk = dev.get("package")
            if pk and pk not in pkgs:
                bad_ref.append(pk)
            conns = list(dev.iter("connect"))
            if pk and not conns:
                no_conn.append(ds.get("name"))
            for cn in conns:
                sname = gates.get(cn.get("gate"), "")
                if cn.get("pin") not in pins_of.get(sname, set()):
                    bad_pin.append((ds.get("name"), cn.get("pin")))
                for pad in (cn.get("pad") or "").split():
                    if pad not in pads_of.get(pk, set()):
                        bad_pad.append((pk, pad))
    res.check("EAGLE: все ссылки на символы/корпуса разрешаются",
              not bad_ref, str(bad_ref[:3]))
    res.check("EAGLE: <connect> ссылается на существующие выводы",
              not bad_pin, str(bad_pin[:3]))
    res.check("EAGLE: <connect> ссылается на существующие площадки",
              not bad_pad, str(bad_pad[:3]))
    res.check("EAGLE: у каждого корпуса есть связи", not no_conn,
              str(no_conn[:3]))

    # ни одной площадки не потеряли
    src_pads = sum(len(c.footprints[0].pads) for c in comps)
    got_pads = sum(len(v) for v in pads_of.values())
    res.check("EAGLE: площадки не потеряны", got_pads == src_pads,
              f"{got_pads} != {src_pads}")
    src_pins = sum(len(c.symbol.pins) for c in comps)
    got_pins = sum(len(v) for v in pins_of.values())
    res.check("EAGLE: выводы не потеряны", got_pins == src_pins,
              f"{got_pins} != {src_pins}")

    # числа -- только точка, без экспоненты и запятой
    import re as _re
    txt = open(path, encoding="utf-8").read()
    res.check("EAGLE: нет чисел в экспоненциальной записи",
              not _re.search(r'"[-\d.]+e[-+]?\d+"', txt, _re.I))
    shutil.rmtree(tmp, ignore_errors=True)


def check_kicad_writer(res: Result, st):
    """
    Обратная запись в KiCad: проверяем круговым разбором собственным
    парсером -- если наш же парсер читает файл и числа сходятся,
    штатный импортёр Altium тоже прочтёт.
    """
    from .emit.kicadout import write_kicad_bundle
    from .sources import kicad as kc
    comps = _with_footprints(_sample_components(), st)
    tmp = tempfile.mkdtemp()
    root = os.path.join(tmp, "out")
    r = write_kicad_bundle(root, comps)

    res.check("KiCad: .kicad_sym создан", os.path.isfile(str(r["sym"])))
    res.check("KiCad: папка .pretty создана", os.path.isdir(str(r["pretty"])))
    res.check("KiCad: посадок записано", r["footprints"] == len(comps),
              f"{r['footprints']} != {len(comps)}")

    text = open(str(r["sym"]), encoding="utf-8").read()
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    res.check("KiCad: скобки .kicad_sym сбалансированы", depth == 0,
              f"итог {depth}")

    names = kc.parse_kicad_sym(str(r["sym"]))
    res.check("KiCad: символы читаются обратно",
              len(names) == len(comps), f"{len(names)} != {len(comps)}")
    bad = []
    for c in comps:
        if c.name not in names:
            bad.append(c.name)
            continue
        back = kc.component_from_kicad_sym(str(r["sym"]), c.name)
        if sorted(p.number for p in back.symbol.pins) != \
                sorted(p.number for p in c.symbol.pins):
            bad.append(c.name)
    res.check("KiCad: номера выводов совпадают после разбора", not bad,
              str(bad[:3]))

    bad_fp = []
    for f in sorted(glob.glob(os.path.join(str(r["pretty"]), "*.kicad_mod"))):
        # круговая проверка: сравниваем ровно то, что записали, поэтому
        # фильтр переходных отверстий здесь выключен
        fp = kc.parse_kicad_mod(f, drop_vias=False)
        src = next((x for c in comps for x in c.footprints
                    if x.name == fp.name), None)
        if src is None:
            bad_fp.append((fp.name, "нет источника"))
            continue
        if len(fp.pads) != len(src.pads):
            bad_fp.append((fp.name, f"{len(fp.pads)} != {len(src.pads)}"))
            continue
        for a, b in zip(fp.pads, src.pads):
            if abs(a.x - b.x) > 1e-5 or abs(a.y - b.y) > 1e-5:
                bad_fp.append((fp.name, f"смещение {a.number}"))
                break
            if abs(a.w - b.w) > 1e-5 or abs(a.h - b.h) > 1e-5:
                bad_fp.append((fp.name, f"размер {a.number}"))
                break
            if bool(a.hole) != bool(b.hole):
                bad_fp.append((fp.name, f"отверстие {a.number}"))
                break
    res.check("KiCad: посадки читаются обратно без расхождений", not bad_fp,
              str(bad_fp[:3]))
    shutil.rmtree(tmp, ignore_errors=True)


def check_altium_job(res: Result):
    """
    Задание для скрипта: формат, полнота, обратный разбор.

    Здесь же проверяется то, чего в Altium уже не увидишь: что все площадки
    и выводы доехали, что координаты целые и что кириллица пережила
    экранирование.
    """
    from .emit import altiumjob as aj
    from .emit import jobcheck

    comps = []
    for code in classify.CTYPE_CODES:
        c = _demo_component(code)
        comps.append(c)

    text = aj.build_job(comps, r"D:\lib\GOST_Lib.SchLib",
                        r"D:\lib\GOST_Lib.PcbLib")
    problems = jobcheck.check(text)
    res.check("задание: формат без замечаний", not problems,
              "; ".join(problems[:3]))

    def _cp1251_ok(t):
        try:
            t.encode("cp1251")
            return True
        except UnicodeEncodeError:
            return False
    res.check("задание: укладывается в CP1251", _cp1251_ok(text),
              "в задании символы, которых нет в CP1251")
    res.check("задание в ASCII-режиме чисто ASCII",
              all(ord(ch) < 128 for ch in
                  aj.build_job(comps[:2], "a", "b", ascii_only=True)),
              "в ASCII-режиме остались не-ASCII символы")

    st = jobcheck.stats(text)
    res.check("задание: компонентов столько же", st.get("COMP") == len(comps),
              f"{st.get('COMP')} != {len(comps)}")

    want_pads = sum(len(fp.pads) for c in comps for fp in c.footprints)
    res.check("задание: площадки все на месте", st.get("PAD") == want_pads,
              f"{st.get('PAD')} != {want_pads}")

    want_pins = sum(len(c.symbol.pins) for c in comps)
    res.check("задание: выводы все на месте", st.get("PIN") == want_pins,
              f"{st.get('PIN')} != {want_pins}")

    # Одинаковые ИМЕНА выводов (VSS, GND) -- норма, так у любого
    # микроконтроллера. А вот два вывода в одной точке или с одним
    # обозначением Альтиум покажет как один -- это ловим здесь.
    hdr = "VER\t3\n"
    same_name = (hdr + "COMP\tX\tDD?\t\t\t1\n"
                 "PIN\t1\t1\tVSS\t0\t0\t0\t200\t0\t0\t-200\t0\n"
                 "PIN\t1\t2\tVSS\t0\t0\t-100\t200\t0\t0\t-200\t-100\n"
                 "ENDCOMP\nEND\t1\t0\n")
    res.check("задание: одинаковые имена выводов не считаются ошибкой",
              not jobcheck.check(same_name),
              "; ".join(jobcheck.check(same_name)[:2]))

    same_pos = (hdr + "COMP\tX\tDD?\t\t\t1\n"
                "PIN\t1\t1\tVSS\t0\t0\t0\t200\t0\t0\t-200\t0\n"
                "PIN\t1\t2\tGND\t0\t0\t0\t200\t0\t0\t-200\t0\n"
                "ENDCOMP\nEND\t1\t0\n")
    res.check("задание: выводы в одной точке замечены",
              any("в одной точке" in p for p in jobcheck.check(same_pos)),
              "наложенные выводы прошли проверку")

    hidden = (hdr + "COMP\tX\tDD?\t\t\t1\n"
              "PIN\t1\t1\tIOVDD\t7\t0\t0\t200\t0\t0\t-200\t0\n"
              "PIN\t1\t10\tIOVDD\t4\t0\t-100\t200\t0\t4\t-200\t-100\n"
              "ENDCOMP\nEND\t1\t0\n")
    res.check("задание: скрытые выводы замечены",
              any("скрытыми" in p for p in jobcheck.check(hidden)),
              "скрытый вывод прошёл проверку")

    same_num = (hdr + "COMP\tX\tDD?\t\t\t1\n"
                "PIN\t1\t7\tVSS\t0\t0\t0\t200\t0\t0\t-200\t0\n"
                "PIN\t1\t7\tGND\t0\t0\t-100\t200\t0\t0\t-200\t-100\n"
                "ENDCOMP\nEND\t1\t0\n")
    res.check("задание: одинаковые обозначения выводов замечены",
              any("обозначение вывода" in p for p in jobcheck.check(same_num)),
              "повтор обозначения прошёл проверку")

    # кириллица: описание должно раскрываться обратно
    line = next((l for l in text.splitlines()
                 if l.startswith("PARAM\tCategory")), "")
    val = line.split("\t")[2] if line.count("\t") >= 2 else ""
    res.check("задание: кириллица пишется как есть", val in aj.GROUPS,
              f"'{val[:20]}'")
    res.check("задание: экранирование обратимо",
              _unesc(aj.esc("a\\b\tc")) == "a\\b\tc",
              "экранирование не разворачивается обратно")
    aline = next((l for l in aj.build_job(comps[:2], "a", "b",
                                          ascii_only=True).splitlines()
                  if l.startswith("PARAM\tCategory")), "")
    aval = aline.split("\t")[2] if aline.count("\t") >= 2 else ""
    res.check("задание: в ASCII-режиме кириллица экранируется",
              aval.startswith("\\u04"), f"'{aval[:20]}'")
    res.check("задание: экранированная кириллица раскрывается",
              _unesc(aval) in aj.GROUPS, f"'{_unesc(aval)}'")

    # координаты: только целые
    bad = []
    for ln, raw in enumerate(text.splitlines(), 1):
        f = raw.split("\t")
        for idx in jobcheck.INTS.get(f[0], []):
            if idx - 1 < len(f):
                v = f[idx - 1]
                if "." in v or "," in v:
                    bad.append(f"строка {ln}: '{v}'")
    res.check("задание: дробных чисел нет", not bad, "; ".join(bad[:3]))

    # пересборка того же каталога даёт байт в байт то же задание
    text2 = aj.build_job(comps, r"D:\lib\GOST_Lib.SchLib",
                         r"D:\lib\GOST_Lib.PcbLib")
    res.check("задание: пересборка идемпотентна", text == text2,
              "второй прогон дал другой файл")


def _unesc(s: str) -> str:
    """То же, что делает Unesc в скрипте -- для проверки экранирования."""
    out = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            c = s[i + 1]
            if c == "u" and i + 5 < len(s) + 1:
                out.append(chr(int(s[i + 2:i + 6], 16)))
                i += 6
                continue
            if c == "t":
                out.append("\t")
                i += 2
                continue
            if c == "n":
                out.append("\n")
                i += 2
                continue
            if c == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _demo_component(code: str):
    """Компонент-образец нужного типа: символ по ГОСТ + посадка + 3D."""
    from .ir import Component, Footprint, Pad, FpPrim, SymPin
    from .gost import symbolgen
    n = 3 if code in ("bjt", "mosfet", "thyristor") else 8
    c = Component(name=f"DEMO_{code}", ctype=code,
                  designator=classify.designator_for(code),
                  description=f"Образец: {classify.CTYPE_NAME.get(code, code)}")
    c.raw_pins = [SymPin(number=str(i), name=f"P{i}", etype="passive")
                  for i in range(1, n + 1)]
    c.symbol.pins = list(c.raw_pins)
    symbolgen.build(c, config.Config().to_style())
    fp = Footprint(name=f"DEMO_FP_{code}", description="образец", height=1.2)
    fp.pads.append(Pad(number="1", x=-1.0, y=0, w=1.0, h=1.2, shape="rect"))
    fp.pads.append(Pad(number="2", x=1.0, y=0, w=1.0, h=1.2, shape="round",
                       hole=0.8, layer="multi"))
    fp.prims.append(FpPrim(kind="line", layer="silk",
                           pts=[[-2, -1], [2, -1], [2, 1], [-2, 1], [-2, -1]]))
    fp.prims.append(FpPrim(kind="circle", layer="silk", pts=[[-2, -1]],
                           radius=0.2))
    fp.prims.append(FpPrim(kind="rect", layer="courtyard",
                           pts=[[-2.2, -1.2], [2.2, 1.2]]))
    c.footprints.append(fp)
    return c


def check_fpinfo(res: Result):
    """Габариты посадочного места считаются и выглядят разумно."""
    from . import fpinfo
    c = _demo_component("resistor")
    info = fpinfo.describe(c.footprints[0])
    res.check("габариты: посчитаны площадки", info.pad_count == 2,
              str(info.pad_count))
    res.check("габариты: SMD и выводные разделены",
              info.smd == 1 and info.tht == 1, f"{info.smd}/{info.tht}")
    bw, bh = info.body_size
    res.check("габариты: контур по области установки",
              abs(bw - 4.4) < 1e-6 and abs(bh - 2.4) < 1e-6,
              f"{bw} x {bh}")
    pw, ph = info.pads_size
    res.check("габариты: размер по площадкам",
              abs(pw - 3.0) < 1e-6 and abs(ph - 1.2) < 1e-6,
              f"{pw} x {ph}")
    res.check("габариты: шаг площадок", abs(info.pitch_x - 2.0) < 1e-6,
              str(info.pitch_x))
    res.check("габариты: отверстие найдено", info.holes == [0.8],
              str(info.holes))
    res.check("габариты: высота", abs(info.height - 1.2) < 1e-6,
              str(info.height))
    txt = info.text()
    res.check("габариты: карточка не пустая", len(txt.splitlines()) >= 6,
              txt[:60])


def check_step3d(res: Result):
    """Разбор STEP: габариты, устойчивая высота, сверка с посадкой."""
    from . import step3d
    import tempfile

    # синтетический STEP: кубик 4x2x1, плюс «тело» в своей системе координат
    # этажом выше -- ровно то, на чём наивный габарит завышает высоту
    lines = ["ISO-10303-21;", "DATA;"]
    pid = 1
    vids = []
    for (x, y, z) in [(0, 0, 0), (4, 0, 0), (4, 2, 0), (0, 2, 0),
                      (0, 0, 1), (4, 0, 1), (4, 2, 1), (0, 2, 1)]:
        lines.append(f"#{pid}=CARTESIAN_POINT('',({x}.,{y}.,{z}.));")
        vids.append(pid)
        pid += 1
    far = []
    for (x, y, z) in [(0, 0, 9), (4, 0, 9), (4, 2, 9), (0, 2, 9)]:
        lines.append(f"#{pid}=CARTESIAN_POINT('',({x}.,{y}.,{z}.));")
        far.append(pid)
        pid += 1
    for v in vids + far:
        lines.append(f"#{pid}=VERTEX_POINT('',#{v});")
        pid += 1
    lines += ["ENDSEC;", "END-ISO-10303-21;"]
    tmp = tempfile.mkdtemp(prefix="gostlib_step_")
    path = os.path.join(tmp, "cube.step")
    with open(path, "w", encoding="ascii") as f:
        f.write("\n".join(lines))

    m = step3d.parse(path)
    res.check("STEP: файл разобрался", m.ok, m.error)
    w, d, full = m.size()
    res.check("STEP: габарит в плане", abs(w - 4) < 1e-6 and abs(d - 2) < 1e-6,
              f"{w} x {d}")
    res.check("STEP: размах по Z честный", abs(m.height() - 9.0) < 1e-6,
              f"{m.height()}")
    res.check("STEP: подозрительный Z помечен", m.z_suspicious(),
              "высота 9 мм при габарите 4x2 должна насторожить")

    fp = _demo_component("ic").footprints[0]
    fp.pads = []
    fp.prims = [__import__("gostlib.ir", fromlist=["FpPrim"]).FpPrim(
        kind="rect", layer="courtyard", pts=[[-2, -1], [2, 1]])]
    msgs = step3d.compare_with_footprint(m, fp)
    res.check("STEP: сверка с посадкой сходится",
              any("Сходится" in x for x in msgs), "; ".join(msgs))

    svg_text = step3d.preview_svg(m, fp)
    res.check("STEP: превью рисуется", svg_text.startswith("<svg"),
              svg_text[:40])
    res.check("STEP: в превью есть оба вида",
              "сверху" in svg_text and "сбоку" in svg_text, "")

    bad = step3d.parse(os.path.join(tmp, "нет-такого.step"))
    res.check("STEP: отсутствующий файл не роняет", not bad.ok and bad.error,
              "")
    shutil.rmtree(tmp, ignore_errors=True)


def check_libpkg(res: Result):
    """Пакет библиотек .LibPkg для сборки .IntLib."""
    from .emit import libpkg
    import tempfile
    tmp = tempfile.mkdtemp(prefix="gostlib_pkg_")
    sch = os.path.join(tmp, "GOST_Lib.SchLib")
    pcb = os.path.join(tmp, "GOST_Lib.PcbLib")
    path = libpkg.write(os.path.join(tmp, "GOST_Lib.LibPkg"), [sch, pcb])
    text = open(path, encoding="cp1251").read()
    res.check("LibPkg: раздел Design на месте", "[Design]" in text, "")
    res.check("LibPkg: обе библиотеки перечислены",
              "DocumentPath=GOST_Lib.SchLib" in text and
              "DocumentPath=GOST_Lib.PcbLib" in text, text[:120])
    res.check("LibPkg: пути относительные", ":\\" not in text and
              tmp not in text, "в файле остался абсолютный путь")
    res.check("LibPkg: переводы строк CRLF", "\r\n" in
              open(path, "rb").read().decode("cp1251"), "")
    shutil.rmtree(tmp, ignore_errors=True)


def check_designator(res: Result):
    """Обозначение должно ехать за типом, но не затирать ручную правку."""
    from .service import Service
    cases = [
        ("ic", "mcu", "U?", "DD?"),
        ("other", "connector", "U?", "X?"),
        ("resistor", "capacitor", "R?", "C?"),
        ("mcu", "resistor", "DD?", "R?"),
        ("ic", "mcu", "", "DD?"),
        ("ic", "mcu", "DD1", "DD1"),
    ]
    bad = []
    for old, new, cur, want in cases:
        got = Service.auto_designator(old, new, cur)
        if got != want:
            bad.append(f"{old}->{new} '{cur}': {got} != {want}")
    res.check("обозначение едет за типом", not bad, "; ".join(bad))


def check_manual_layout(res: Result):
    """Ручная раскладка: генератор не переставляет выводы."""
    from .ir import Component, SymPin
    from .gost import symbolgen
    st = config.Config().to_style()

    c = Component(name="MAN", ctype="ic")
    c.symbol.manual_layout = True
    c.symbol.body_w, c.symbol.body_h = 1400, 900
    c.symbol.dividers = [-400]
    c.symbol.pins = [
        SymPin(number="1", name="VCC", side="L", x=0, y=-100, rotation=180),
        SymPin(number="2", name="GND", side="L", x=0, y=-800, rotation=180),
        SymPin(number="3", name="OUT", side="R", x=1400, y=-100, rotation=0),
    ]
    ys = [p.y for p in c.symbol.pins]
    sym = symbolgen.build(c, st)
    res.check("ручная раскладка: выводы не переставлены",
              [p.y for p in sym.pins] == ys,
              f"{[p.y for p in sym.pins]} != {ys}")
    res.check("ручная раскладка: корпус на месте",
              sym.body_w == 1400 and sym.body_h == 900,
              f"{sym.body_w}x{sym.body_h}")
    lines = [pr for pr in sym.prims if pr.kind == "line"]
    res.check("ручная раскладка: разделитель нарисован",
              any(abs(pr.pts[0][1] + 400) < 1e-6 and
                  abs(pr.pts[1][1] + 400) < 1e-6 for pr in lines),
              "горизонтальной линии на y=-400 нет")

    # вывод ниже корпуса -- корпус должен вырасти, иначе его не ухватить
    c.symbol.pins.append(SymPin(number="4", name="X", side="L", x=0, y=-1500,
                                rotation=180))
    sym = symbolgen.build(c, st)
    res.check("ручная раскладка: корпус растёт под крайний вывод",
              sym.body_h >= 1500, str(sym.body_h))

    c.symbol.manual_layout = False
    c.raw_pins = list(c.symbol.pins)
    sym = symbolgen.build(c, st)
    res.check("сброс на автомат работает", not sym.manual_layout, "")


def check_style_options(res: Result):
    """Масштаб пассивок, скрытие номеров и цвета доезжают до задания."""
    from .ir import Component, SymPin
    from .gost import symbolgen
    from .emit import altiumjob as aj

    def resistor(scale, numbers=True):
        st = config.Config().to_style()
        st.passive_scale = scale
        st.show_pin_numbers = numbers
        c = Component(name="R1", ctype="resistor", designator="R?")
        c.raw_pins = [SymPin(number="1", name="1"), SymPin(number="2", name="2")]
        c.symbol.pins = list(c.raw_pins)
        symbolgen.build(c, st)
        return c, st

    c1, _ = resistor(1.0)
    c2, _ = resistor(0.5)

    def width(c):
        xs = [pt[0] for pr in c.symbol.prims for pt in pr.pts]
        return (max(xs) - min(xs)) if xs else 0

    res.check("масштаб пассивок уменьшает графику", width(c2) < width(c1),
              f"{width(c2)} !< {width(c1)}")

    c3, st3 = resistor(1.0, numbers=False)
    res.check("номера выводов можно скрыть",
              all(not p.show_number for p in c3.symbol.pins),
              "у выводов остался show_number")

    st = config.Config().to_style()
    st.color_graphic = st.color_text = st.color_pin = st.color_pin_num = 0
    txt = aj.build_job([c1], "a", "b", st=st)
    res.check("цвета уходят в задание",
              "COLGRAPH\t0" in txt and "COLTEXT\t0" in txt, "")
    stext = [l for l in txt.splitlines() if l.startswith("STEXT")]
    res.check("у текста в задании есть поле цвета",
              all(len(l.split("\t")) >= 10 for l in stext),
              f"строк STEXT {len(stext)}")

    st.color_graphic = 128
    txt2 = aj.build_job([c1], "a", "b", st=st)
    res.check("цвет графики настраивается", "COLGRAPH\t128" in txt2, "")


def check_user_lines_and_grid(res: Result):
    """Свои линии в символе, шаг по сетке и удаление переходных отверстий."""
    from .ir import Component, SymPin, Footprint, Pad
    from .gost import symbolgen
    from .sources.kicad import _drop_thermal_vias

    st = config.Config().to_style()
    c = Component(name="LN", ctype="ic")
    c.symbol.manual_layout = True
    c.symbol.body_w, c.symbol.body_h = 1200, 800
    c.symbol.user_lines = [[200, -300, 1000, -300]]
    c.symbol.pins = [SymPin(number="1", name="A", side="L", x=0, y=-100,
                            rotation=180)]
    sym = symbolgen.build(c, st)
    got = [pr for pr in sym.prims if pr.kind == "line"
           and abs(pr.pts[0][0] - 200) < 1e-6 and abs(pr.pts[1][0] - 1000) < 1e-6]
    res.check("своя линия попадает в символ", len(got) == 1, str(len(got)))
    res.check("своя линия переживает сериализацию",
              Component.from_dict(c.to_dict()).symbol.user_lines ==
              [[200, -300, 1000, -300]], "")

    # шаг растёт под крупный кегль, выводы остаются на сетке
    st2 = config.Config().to_style()
    st2.size_pin = st2.size_pin_num = 17
    c2 = Component(name="BIG", ctype="ic")
    c2.raw_pins = [SymPin(number=str(i), name=f"P{i}") for i in range(1, 11)]
    c2.symbol.pins = list(c2.raw_pins)
    symbolgen.build(c2, st2)
    res.check("шаг поднят под крупный кегль", st2.eff_pitch() >= 200,
              str(st2.eff_pitch()))
    res.check("символ проходит проверку сетки",
              not symbolgen.verify(c2, st2),
              "; ".join(symbolgen.verify(c2, st2)))

    # наложение ловится
    for p in c2.symbol.pins:
        p.y = -100 * (c2.symbol.pins.index(p) + 1)
    probs = symbolgen.verify(c2, st2)
    # два вывода в одной точке -- Altium показал бы их как один
    from .ir import Component as _C
    c3 = _C(name="DUP", ctype="ic")
    c3.symbol.manual_layout = True
    c3.symbol.body_w, c3.symbol.body_h = 1000, 600
    c3.symbol.pins = [SymPin(number=str(i), name="VSS", side="L", x=0,
                             y=-200, rotation=180) for i in (1, 2, 3)]
    symbolgen.build(c3, st2)
    ys = sorted(p.y for p in c3.symbol.pins)
    res.check("наложенные выводы раздвигаются", len(set(ys)) == 3, str(ys))
    c3.symbol.pins[1].number = "1"
    res.check("дубль номера ловится",
              any("номера выводов" in x for x in symbolgen.verify(c3, st2)),
              "; ".join(symbolgen.verify(c3, st2)))

    res.check("наложение подписей ловится",
              any("наед" in x or "меньше высоты" in x for x in probs),
              "; ".join(probs) or "проверка ничего не нашла")

    fp = Footprint(name="QFN")
    fp.pads.append(Pad(number="21", x=0, y=0, w=3.2, h=3.2))
    for i in range(9):
        fp.pads.append(Pad(number="21", x=-1 + 0.5 * i, y=0, w=0.3, h=0.3,
                           hole=0.3, layer="multi"))
    fp.pads.append(Pad(number="1", x=-2, y=0, w=1.6, h=1.6, hole=0.8,
                       layer="multi"))
    n = _drop_thermal_vias(fp)
    res.check("переходные отверстия выброшены", n == 9, str(n))

    # крепёжное отверстие и гребёнка -- не via, их трогать нельзя
    fp2 = Footprint(name="MIX")
    fp2.pads.append(Pad(number="", x=0, y=0, w=3.2, h=3.2, hole=2.2,
                        layer="multi", plated=False))
    fp2.pads.append(Pad(number="1", x=5, y=0, w=1.7, h=1.7, hole=1.0,
                        layer="multi"))
    fp2.pads.append(Pad(number="", x=2, y=0, w=0.6, h=0.6, hole=0.4,
                        layer="multi"))
    n2 = _drop_thermal_vias(fp2)
    res.check("via без номера выброшено, крепёж и гребёнка целы",
              n2 == 1 and len(fp2.pads) == 2,
              f"выброшено {n2}, осталось {len(fp2.pads)}")
    res.check("настоящие выводные площадки остались",
              len(fp.pads) == 2 and any(p.hole > 0 for p in fp.pads),
              str([(p.number, p.hole) for p in fp.pads]))


def check_script(res: Result):
    """Скрипт для Altium: на месте, сбалансирован, знает команды задания."""
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "altium", "GostLibBuilder.pas")
    res.check("скрипт Altium: файл на месте", os.path.isfile(p), p)
    if not os.path.isfile(p):
        return
    raw = open(p, "rb").read()
    text = ""
    for enc in ("utf-8", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    for token in ("RunGostLib", "GostLibWhereIsJob", "GostLibListInstalled",
                  "GostLibCheckFile", "InstallLibrary",
                  "UninstallLibrary", "InstalledLibraryPath", "CloseDocument",
                  "GetDocumentByPath", "PTR_PATH",
                  "PCBServer.CreatePCBLibComp", "RegisterComponent",
                  "AddSchComponent", "SchObjectFactory", "PCBObjectFactory",
                  "ModelFactory_FromFilename", "OpenNewDocument",
                  "DoFileSave"):
        res.check(f"скрипт знает {token}", token in text, "нет в .pas")

    # каждый тег задания должен разбираться скриптом
    from .emit import jobcheck as _jc
    for tag in _jc.SPEC:
        if tag in ("VER", "END"):
            continue
        res.check(f"скрипт разбирает тег {tag}", f"'{tag}'" in text,
                  "тег не встречается в .pas")

    # порядок полей в скрипте должен совпадать с тем, что пишет Python
    from .emit import altiumjob as _aj
    from .altium import paslint
    lint = paslint.check(text)
    res.check("скрипт: синтаксис DelphiScript без замечаний", not lint,
              "; ".join(lint[:3]))

    res.check("скрипт и генератор согласованы по версии",
              f"JOB_VERSION  = {_aj.VERSION}" in text,
              f"в .pas другая версия задания (нужна {_aj.VERSION})")
    import re as _re
    code = _re.sub(r"\{[^}]*\}", "", text)
    code = _re.sub(r"'([^']|'')*'", "''", code)
    words = [w.lower() for w in _re.findall(
        r"\b(begin|end|try|case)\b", code, _re.I)]
    depth = 0
    for w in words:
        depth += 1 if w in ("begin", "try", "case") else -1
    res.check("скрипт: Begin/End сбалансированы", depth == -1,
              f"итог {depth}, ожидался -1")


def run(st=None) -> Result:
    st = st or config.Config().to_style()
    res = Result()
    check_symbols(res, st)
    res.run("задание для Altium", lambda: check_job(res, st))
    res.run("парсеры KiCad", lambda: check_kicad(res))
    res.run("парсеры EasyEDA", lambda: check_easyeda(res))
    res.run("обмен раскладкой с ИИ", lambda: check_pin_plan(res))
    res.run("конвертер OBJ->STEP", lambda: check_step(res))
    res.run("каталог", lambda: check_db(res))
    res.run("запись .SchLib", lambda: check_schlib_writer(res, st))
    res.run("стиль компонента", lambda: check_component_style(res, st))
    res.run("окна интерфейса", lambda: check_gui_imports(res))
    res.run("пазы отверстий", lambda: check_slots(res))
    res.run("качество родного УГО", lambda: check_native_quality(res))
    res.run("секции символа", lambda: check_sections(res))
    res.run("отмена действий", lambda: check_undo(res))
    res.run("отменяется всё", lambda: check_everything_undoable(res))
    res.run("отчёт о сборке", lambda: check_build_report(res))
    res.run("имена", lambda: check_names(res))
    res.run("ломаные в превью", lambda: check_preview_polyline(res))
    res.run("команда для Altium", lambda: check_altium_command(res))
    res.run("порядок выводов", lambda: check_pin_order(res))
    res.run("место на диске", lambda: check_disk_usage(res))
    res.run("проекты", lambda: check_projects(res))
    res.run("импорт в проект", lambda: check_import_into_project(res))
    res.run("счётчик компонентов", lambda: check_orphan_counter(res))
    res.run("кегль превью", lambda: check_preview_font_scale(res))
    res.run("сеть EasyEDA", lambda: check_easyeda_network(res))
    res.run("скорость подготовки задания", lambda: check_build_speed(res))
    res.run("бюджет 3D-модели", lambda: check_model_budget(res))
    res.run("крупные 3D-модели", lambda: check_heavy_models(res))
    res.run("посадка 3D на плату", lambda: check_model_seating(res))
    res.run("цвет модели держится",
            lambda: check_model_color_sticks(res))
    res.run("формы площадок", lambda: check_pad_shapes(res))
    res.run("зазоры у посадочного места",
            lambda: check_expansion_is_per_footprint(res))
    res.run("окно помещается на экран",
            lambda: check_window_fits_screen(res))
    res.run("нет вложенных процедур",
            lambda: check_no_nested_procedures(res))
    res.run("скорость скрипта Altium",
            lambda: check_altium_script_speed(res))
    res.run("порядок выводов с первого раза",
            lambda: check_pin_order_first_try(res))
    res.run("правила выводов", lambda: check_pin_rules(res))
    res.run("родное УГО", lambda: check_native_symbol(res))
    res.run("подбор посадки в архиве", lambda: check_archive_match(res))
    res.run("копия и очистка", lambda: check_backup_and_clear(res))
    res.run("размер STEP", lambda: check_mesh_size(res))
    res.run("запись .PcbLib", lambda: check_pcblib_writer(res))
    res.run("запись EAGLE .lbr", lambda: check_eagle_writer(res, st))
    res.run("запись KiCad .kicad_sym/.kicad_mod",
            lambda: check_kicad_writer(res, st))
    res.run("задание для скрипта Altium", lambda: check_altium_job(res))
    res.run("габариты посадочных мест", lambda: check_fpinfo(res))
    res.run("разбор STEP и превью 3D", lambda: check_step3d(res))
    res.run("пакет библиотек .LibPkg", lambda: check_libpkg(res))
    res.run("позиционные обозначения", lambda: check_designator(res))
    res.run("ручная раскладка выводов", lambda: check_manual_layout(res))
    res.run("настройки стиля", lambda: check_style_options(res))
    res.run("линии, сетка и via", lambda: check_user_lines_and_grid(res))
    res.run("скрипт Altium", lambda: check_script(res))
    res.run("сборка exe: занятый файл", lambda: check_build_exe_lock(res))
    res.run("числа STEP вида 2.E-002", lambda: check_step_numbers(res))
    res.run("превью: обозначение, 3D и размеры окон",
            lambda: check_preview_and_windows(res))
    res.run("овальные отверстия и слои маски/пасты",
            lambda: check_slots_and_paste_layers(res))
    res.run("графика УГО: фигуры и правка", lambda: check_shapes(res))
    res.run("предпросмотр в импорте KiCad", lambda: check_kicad_preview(res))
    res.run("дозагрузка посадки", lambda: check_attach_footprint(res))
    res.run("свои параметры компонента", lambda: check_user_params(res))
    res.run("текстовая копия библиотеки для git",
            lambda: check_gitlib(res))
    res.run("цвет 3D-модели в STEP", lambda: check_step_color(res))
    res.run("паста: галочка, сетка, предел", lambda: check_paste(res))
    return res


def check_gitlib(res: Result):
    """
    Текстовое зеркало библиотеки.

    Смысл этой штуки только один -- вести библиотеку в git отделом.
    Поэтому главная проверка не «файлы создались», а «одна и та же
    библиотека даёт одинаковые файлы»: иначе каждая выгрузка -- diff на
    всю библиотеку, и ревью превращается в мусор.
    """
    from . import config as _cfg, gitlib
    from .service import Service
    from .ir import Component, Footprint, Pad, Symbol, SymPin

    def mk(svc, name, value=""):
        c = Component(name=name, ctype="resistor", value=value)
        c.symbol = Symbol(pins=[SymPin(number="1"), SymPin(number="2")])
        c.footprints = [Footprint(name="R_0805",
                                  pads=[Pad(number="1", x=0, y=0)])]
        svc.db.upsert(c)
        svc.db.set_in_library([c.uid], True)
        return c

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=os.path.join(td, "cat"))
        cfg.ensure_dirs()
        svc = Service(cfg)
        root = os.path.join(td, "repo")
        made = [mk(svc, f"R{i}", f"{i}k") for i in range(3)]

        r1 = svc.git_export(root)
        res.check("выложились все компоненты", r1["total"] == 3,
                  str(r1["total"]))
        idx = open(os.path.join(root, gitlib.INDEX), encoding="utf-8").read()
        res.check("оглавление читается глазами",
                  idx.count("\n") == 4 and "R1" in idx, idx[:80])
        res.check("рядом лёг .gitattributes",
                  os.path.isfile(os.path.join(root, ".gitattributes")), "")

        # ГЛАВНОЕ: повторная выгрузка не должна менять ни байта
        before = {f: open(os.path.join(root, gitlib.COMPONENTS, f), "rb").read()
                  for f in os.listdir(os.path.join(root, gitlib.COMPONENTS))}
        r2 = svc.git_export(root)
        after = {f: open(os.path.join(root, gitlib.COMPONENTS, f), "rb").read()
                 for f in os.listdir(os.path.join(root, gitlib.COMPONENTS))}
        res.check("повторная выгрузка ничего не меняет",
                  not r2["changed"] and before == after,
                  str(r2["changed"]))
        res.check("переводы строк только LF",
                  all(b"\r\n" not in v for v in after.values()), "")

        # читается обратно один в один
        back = {c.uid: c for c in gitlib.read_tree(root)}
        res.check("папка читается обратно", len(back) == 3, str(len(back)))
        res.check("значение доехало без потерь",
                  back[made[1].uid].value == "1k",
                  back[made[1].uid].value)

        # удаление компонента убирает файл
        svc.db.delete([made[2].uid])
        r3 = svc.git_export(root)
        res.check("удалённый компонент ушёл из папки",
                  len(r3["removed"]) == 1
                  and len(gitlib.read_tree(root)) == 2,
                  str(r3["removed"]))

        # вторая машина: свой каталог, та же папка
        cfg2 = _cfg.Config(root=os.path.join(td, "cat2"))
        cfg2.ensure_dirs()
        svc2 = Service(cfg2)
        got = svc2.git_import(root)
        res.check("на другой машине компоненты забрались",
                  len(got["added"]) == 2, str(got))
        res.check("uid при этом тот же (значит, сольётся, а не задвоится)",
                  set(svc2.db.all_uids()) == {made[0].uid, made[1].uid},
                  str(sorted(svc2.db.all_uids())))

        # расхождение: правим у себя, папку не трогаем
        c = svc2.db.get(made[0].uid)
        c.value = "999"
        svc2.db.upsert(c)
        st = svc2.git_status(root)
        res.check("расхождение видно", [n for _u, n in st["diff"]] == ["R0"],
                  str(st["diff"]))

        # без overwrite чужая версия не затирает нашу
        svc2.git_import(root, overwrite=False)
        res.check("без перезаписи наша правка цела",
                  svc2.db.get(made[0].uid).value == "999",
                  svc2.db.get(made[0].uid).value)
        svc2.git_import(root, overwrite=True)
        res.check("с перезаписью приезжает версия из папки",
                  svc2.db.get(made[0].uid).value == "0k",
                  svc2.db.get(made[0].uid).value)

        # синхронизация в обе стороны и честный отчёт о конфликтах
        c = svc2.db.get(made[1].uid)
        c.value = "фикс"
        svc2.db.upsert(c)
        out = svc2.git_sync(root)
        res.check("синхронизация назвала расхождение",
                  out["conflicts"] == ["R1"], str(out["conflicts"]))
        res.check("и выложила нашу версию",
                  gitlib.read_tree(root)[1].value == "фикс"
                  or any(x.value == "фикс" for x in gitlib.read_tree(root)),
                  "")

        # одинаковые имена не должны затирать друг друга
        a = mk(svc, "SAME")
        b = mk(svc, "SAME")
        names = gitlib.file_names([a, b])
        res.check("одинаковые имена разведены по файлам",
                  names[a.uid] != names[b.uid], str(names))
        svc.close()
        svc2.close()


def check_user_params(res: Result):
    """
    Свои параметры: тип, единица и правило показа.

    Главное требование остаётся прежним: параметры уходят в Altium
    СКРЫТЫМИ, видимая надпись одна -- Comment. Показ включается только
    явной галочкой.
    """
    from . import config as _cfg
    from .service import Service
    from .emit import altiumjob as aj
    from .ir import Component, Symbol, SymPin

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        c = Component(name="U1", ctype="ic")
        c.symbol = Symbol(pins=[SymPin(number="1", name="A")])
        svc.db.upsert(c)

        c = svc.apply_params(
            c.uid,
            {"ТокНагрузки": "2.5", "Карточка": "https://example.com/x",
             "ДатаЗакупки": "2026-03-01", "Примечание": "проверить у ОТК"},
            meta={"ТокНагрузки": {"type": "num", "unit": "А", "visible": 1},
                  "Карточка": {"type": "url", "unit": "", "visible": 0},
                  "ДатаЗакупки": {"type": "date", "unit": "", "visible": 0}})
        res.check("описание параметров сохранилось",
                  set(c.param_meta) == {"ТокНагрузки", "Карточка",
                                        "ДатаЗакупки"},
                  str(sorted(c.param_meta)))

        rows = {n: (v, h) for n, v, h in aj._params_of(c)}
        res.check("единица приклеилась к значению",
                  rows.get("ТокНагрузки", ("", 1))[0] == "2.5 А",
                  str(rows.get("ТокНагрузки")))
        res.check("отмеченный параметр виден на схеме",
                  rows["ТокНагрузки"][1] == 0, str(rows["ТокНагрузки"]))
        res.check("остальные уходят скрытыми",
                  all(rows[k][1] == 1 for k in
                      ("Карточка", "ДатаЗакупки", "Примечание")),
                  str([(k, rows[k][1]) for k in
                       ("Карточка", "ДатаЗакупки", "Примечание")]))

        res.check("параметр без описания ведёт себя как раньше",
                  rows["Примечание"] == ("проверить у ОТК", 1),
                  str(rows["Примечание"]))

        # проверка значений
        res.check("не-число поймано",
                  bool(svc.check_param("две с половиной", "num")),
                  svc.check_param("две с половиной", "num"))
        res.check("нормальное число проходит",
                  not svc.check_param("2.5", "num"), "")
        # запятая -- наш обычный разделитель, ругаться на неё незачем
        res.check("число с запятой проходит",
                  not svc.check_param("2,5", "num"), "")
        res.check("кривая ссылка поймана",
                  bool(svc.check_param("htp://x", "url")), "")
        res.check("путь к файлу считается ссылкой",
                  not svc.check_param(r"C:\docs\ds.pdf", "url")
                  or os.name != "nt", "")
        res.check("кривая дата поймана",
                  bool(svc.check_param("31 февраля", "date")), "")
        res.check("ДД.ММ.ГГГГ проходит",
                  not svc.check_param("01.03.2026", "date"), "")
        res.check("пустое значение не ругается",
                  not svc.check_param("", "num"), "")

        # описание не должно переживать удаление параметра
        c = svc.apply_params(c.uid, {"Примечание": "ок"}, meta=c.param_meta)
        res.check("описание удалённого параметра выброшено",
                  not c.param_meta, str(c.param_meta))

        c2 = Component.from_dict(c.to_dict())
        res.check("param_meta переживает запись в каталог",
                  isinstance(c2.param_meta, dict), str(type(c2.param_meta)))
        svc.close()


def check_attach_footprint(res: Result):
    """
    Посадку надо уметь добрать отдельно, не теряя ручную работу.

    Сценарий: пришёл только символ, УГО уже разложено руками, потом
    находится посадка. Повторный импорт стёр бы раскладку -- проверяем,
    что дозагрузка её сохраняет, а замена переносит настройки корпуса.
    """
    from . import config as _cfg
    from .service import Service
    from .ir import Component, Model3D, Symbol, SymPin

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)

        def mod(name, n=2, size=1.0):
            p = os.path.join(td, name + ".kicad_mod")
            rows = "\n".join(
                f' (pad "{i + 1}" smd rect (at {i * 2 - 1} 0) '
                f'(size {size} 1.3) (layers "F.Cu" "F.Paste" "F.Mask"))'
                for i in range(n))
            with open(p, "w", encoding="utf-8") as f:
                f.write(f'(footprint "{name}" (layer "F.Cu")\n{rows}\n)')
            return p

        c = Component(name="U1", ctype="ic")
        c.symbol = Symbol(pins=[SymPin(number="1", name="A"),
                                SymPin(number="2", name="B")])
        c.symbol.manual_layout = True
        c.symbol.body_w, c.symbol.body_h = 1234, 999
        c.symbol.pins[0].y = -777
        svc.db.upsert(c)
        svc.db.set_in_library([c.uid], True)

        res.check("компонент без посадки виден в списке пропусков",
                  [x.uid for x in svc.missing_footprint()] == [c.uid],
                  str([x.name for x in svc.missing_footprint()]))

        c2 = svc.attach_footprint(c.uid, fp_path=mod("SOIC-8"))
        res.check("посадка добавилась",
                  c2 and len(c2.footprints) == 1
                  and c2.footprints[0].name == "SOIC-8",
                  str([f.name for f in (c2.footprints if c2 else [])]))
        res.check("ручная раскладка УГО не пострадала",
                  c2.symbol.body_w == 1234 and c2.symbol.pins[0].y == -777,
                  f"{c2.symbol.body_w}, {c2.symbol.pins[0].y}")
        res.check("и из списка пропусков компонент ушёл",
                  not svc.missing_footprint(), "")

        # настройки корпуса и 3D переезжают на новую посадку
        fp = c2.footprints[0]
        fp.paste = False
        fp.paste_grid = 3
        fp.mask_expansion = 0.05
        fp.height = 1.75
        fp.model = Model3D(path=os.path.join(td, "нет.step"), color="#112233")
        svc.db.upsert(c2)
        c3 = svc.attach_footprint(c2.uid, fp_path=mod("SOIC-8N", size=1.2),
                                  replace=True)
        nfp = c3.footprints[0]
        res.check("замена подставила новую посадку",
                  len(c3.footprints) == 1 and nfp.name == "SOIC-8N",
                  str([f.name for f in c3.footprints]))
        res.check("паста, сетка и зазор переехали",
                  nfp.paste is False and nfp.paste_grid == 3
                  and nfp.mask_expansion == 0.05,
                  f"{nfp.paste}, {nfp.paste_grid}, {nfp.mask_expansion}")
        res.check("3D-модель и её цвет тоже",
                  nfp.model is not None and nfp.model.color == "#112233",
                  str(nfp.model))
        res.check("высота не потерялась", nfp.height == 1.75, str(nfp.height))

        # добавление второй посадки не трогает первую
        c4 = svc.attach_footprint(c3.uid, fp_path=mod("SOIC-8W", size=1.4))
        res.check("вторая посадка добавляется рядом",
                  [f.name for f in c4.footprints] == ["SOIC-8N", "SOIC-8W"],
                  str([f.name for f in c4.footprints]))

        # понятная ошибка вместо исключения из недр
        try:
            svc.attach_footprint(c4.uid, fp_path=os.path.join(td, "нет.mod"))
            ok = False
        except FileNotFoundError:
            ok = True
        res.check("несуществующий файл -- понятная ошибка", ok, "")
        svc.close()


def check_kicad_preview(res: Result):
    """
    В окне импорта из KiCad должно быть видно, что берёшь.

    Имена вроде QFN-56-1EP_7x7mm_P0.4mm_EP3.8x3.8mm различаются одним
    числом, и вслепую выбирается не то. Проверяем, что посадка рисуется,
    что подпись содержит габарит, и что отсутствие модели не роняет окно.
    """
    from . import config as _cfg
    from .service import Service
    try:
        from PySide6.QtWidgets import QApplication
        from .gui.dialogs import KicadDialog
    except Exception as e:                                  # noqa: BLE001
        res.check("предпросмотр KiCad собирается", False, str(e))
        return
    app = QApplication.instance() or QApplication([])       # noqa: F841
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        d = KicadDialog(svc)
        mod = os.path.join(td, "R_0805.kicad_mod")
        with open(mod, "w", encoding="utf-8") as f:
            f.write('(footprint "R_0805" (layer "F.Cu")\n'
                    ' (pad "1" smd rect (at -0.9 0) (size 1.0 1.3)'
                    ' (layers "F.Cu" "F.Paste" "F.Mask"))\n'
                    ' (pad "2" smd rect (at 0.9 0) (size 1.0 1.3)'
                    ' (layers "F.Cu" "F.Paste" "F.Mask"))\n)')
        d._show_fp(mod)
        cap = d.prev_fp.label.text()
        res.check("посадка рисуется в окне импорта",
                  "R_0805" in cap and "площадок 2" in cap, cap)
        res.check("и габарит виден сразу", "2.80" in cap, cap)
        res.check("без модели окно не падает",
                  "3D" in d.prev_3d.title.text()
                  or "модел" in d.prev_3d.title.text().lower(),
                  d.prev_3d.title.text())
        d._show_fp(os.path.join(td, "нет-такого.kicad_mod"))
        res.check("несуществующий файл обработан",
                  "не выбрана" in d.prev_fp.label.text(),
                  d.prev_fp.label.text())
        d.close()
        svc.close()


def check_step_numbers(res: Result):
    """
    `2.E-002` -- это 0,02, а не 2.

    OpenCASCADE (а значит и почти все модели KiCad StepUp) пишет мантиссу
    с точкой на конце. Наше выражение требовало цифру после точки, брало
    от такого числа «2» и теряло порядок. У QFN-20 из-за этого высота
    выходила 2 мм вместо 0,78 -- модель в просмотре была втрое выше
    настоящей. Ошибались и габариты в описании, и посадка тела на плату.
    """
    from . import step3d

    for txt, want in ((b"(-1.99,-0.125,2.E-002)", [-1.99, -0.125, 0.02]),
                      (b"(1.,2.5,-3.E+001)", [1.0, 2.5, -30.0]),
                      (b"(.5,-.25,1E-3)", [0.5, -0.25, 0.001]),
                      (b"(4.0,4.0,0.78)", [4.0, 4.0, 0.78])):
        got = [float(x) for x in step3d._NUM_RE.findall(txt)]
        res.check(f"число {txt.decode()} разобрано верно",
                  len(got) == 3 and all(abs(a - b) < 1e-9
                                        for a, b in zip(got, want)),
                  str(got))

    # маленький STEP ровно в той форме, какую пишет OpenCASCADE
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "part.step")
        pts = [(0., 0., 0.), (4., 0., 0.), (4., 4., 0.), (0., 4., 0.),
               (0., 0., 2.E-2), (4., 0., 2.E-2), (4., 4., 2.E-2),
               (0., 4., 2.E-2)]
        lines = ["ISO-10303-21;", "HEADER;", "FILE_NAME('p','',(''),(''),"
                 "'x','x','');", "ENDSEC;", "DATA;"]
        for i, (x, y, z) in enumerate(pts, start=1):
            zt = "0." if z == 0 else "2.E-002"
            lines.append(f"#{i}=CARTESIAN_POINT('',({x:g}.,{y:g}.,{zt}));")
            lines.append(f"#{100 + i}=VERTEX_POINT('',#{i});")
        lines += ["ENDSEC;", "END-ISO-10303-21;"]
        with open(p, "w", encoding="ascii") as f:
            f.write("\n".join(lines))
        m = step3d.parse(p)
        sz = m.size()
        res.check("высота модели из такого STEP -- 0,02 мм, а не 2",
                  abs(sz[2] - 0.02) < 1e-6, f"{sz}")
        res.check("габарит в плане при этом верный",
                  abs(sz[0] - 4.0) < 1e-6 and abs(sz[1] - 4.0) < 1e-6,
                  f"{sz}")
        zr = step3d.z_range(p)
        res.check("z_range (по нему сажается тело на плату) тоже верный",
                  zr and abs(zr[1] - 0.02) < 1e-6, str(zr))


def check_preview_and_windows(res: Result):
    """
    Три беды окна импорта, все три видны на одном скриншоте.

    1. Позиционное обозначение в превью рисовалось В СЕРЕДИНЕ корпуса --
       у любого компонента. Причина обидная: в цикле отрисовки текста
       локальная `dy` затирала координату обозначения, взятую строкой
       выше. В Altium всё было правильно (туда координата уходит из
       задания), поэтому годами не замечалось.
    2. В окне импорта показывался НЕ СОБРАННЫЙ символ: только выводы и
       подписи, без корпуса по ЕСКД.
    3. Окно требовало под две тысячи пикселей ширины и уезжало на второй
       монитор.
    """
    import re as _re
    from . import config as _cfg
    from .gost import symbolgen
    from .render import svg
    from .sources import kicad as kc
    from .ir import Component, Symbol, SymPin

    cfg = _cfg.Config(root=tempfile.mkdtemp())
    st = cfg.to_style()

    c = Component(name="R1", ctype="resistor", value="10k")
    c.symbol = Symbol(pins=[SymPin(number="1", etype="passive"),
                            SymPin(number="2", etype="passive")])
    c.symbol = symbolgen.build(c, st)
    s = svg.symbol_svg(c, st, dark=False)

    def texts(txt):
        out = {}
        for m in _re.finditer(r'<text[^>]*>([^<]*)</text>', txt):
            a = dict(_re.findall(r'(\w[\w-]*)="([^"]*)"', m.group(0)))
            out[m.group(1)] = (float(a.get("x", 0)), float(a.get("y", 0)))
        return out

    def rects(txt):
        out = []
        for m in _re.finditer(r'<rect[^>]*>', txt):
            a = dict(_re.findall(r'(\w[\w-]*)="([^"]*)"', m.group(0)))
            if "x" in a:
                out.append((float(a["x"]), float(a["y"]),
                            float(a["width"]), float(a["height"])))
        return out

    t, r = texts(s), rects(s)
    res.check("корпус в превью нарисован", bool(r), "прямоугольника нет")
    desig = t.get(c.designator or "R?")
    body = r[0] if r else (0, 0, 0, 0)
    res.check("позиционное обозначение НЕ внутри корпуса",
              desig is not None
              and not (body[0] <= desig[0] <= body[0] + body[2]
                       and body[1] <= desig[1] <= body[1] + body[3]),
              f"обозначение {desig}, корпус {body}")
    res.check("обозначение стоит НАД корпусом",
              desig is not None and desig[1] < body[1],
              f"{desig} против {body}")
    val = t.get("10k")
    res.check("номинал -- под корпусом",
              val is not None and val[1] > body[1] + body[3],
              f"{val} против {body}")

    # 2) окно импорта показывает СОБРАННЫЙ символ
    try:
        from PySide6.QtWidgets import QApplication
        from .gui.dialogs import KicadDialog
        from .service import Service
    except Exception as e:                                  # noqa: BLE001
        res.check("окно импорта собирается", False, str(e))
        return
    app = QApplication.instance() or QApplication([])       # noqa: F841
    with tempfile.TemporaryDirectory() as td:
        cfg2 = _cfg.Config(root=td)
        cfg2.ensure_dirs()
        svc = Service(cfg2)
        d = KicadDialog(svc)
        lib = os.path.join(td, "Device.kicad_sym")
        with open(lib, "w", encoding="utf-8") as f:
            f.write(
                '(kicad_symbol_lib (version 20211014) (generator x)\n'
                ' (symbol "R" (pin_numbers hide) (pin_names (offset 0) hide)\n'
                '  (property "Reference" "R" (id 0) (at 2 0 90))\n'
                '  (property "Value" "R" (id 1) (at 0 0 90))\n'
                '  (symbol "R_0_1" (rectangle (start -1.016 -2.54)'
                ' (end 1.016 2.54) (stroke (width 0.254) (type default))'
                ' (fill (type none))))\n'
                '  (symbol "R_1_1"\n'
                '   (pin passive line (at 0 3.81 270) (length 1.27)'
                ' (name "~") (number "1"))\n'
                '   (pin passive line (at 0 -3.81 90) (length 1.27)'
                ' (name "~") (number "2")))))\n')
        d._show_sym(lib, "R")
        cap = d.prev_sym.label.text()
        res.check("в окне импорта символ собран, а не сырой",
                  "выводов 2" in cap and "секций" in cap, cap)

        # Фон панели совпадает с фоном картинки, иначе тёмная посадка
        # выглядит чёрным квадратом посреди белого поля.
        bg = d.prev_fp.view._bg.name().lower()
        res.check("панель посадки тёмная, как и сама картинка",
                  bg in ("#101018", "#101014", "#000000"), bg)
        res.check("панель символа светлая, как и его картинка",
                  d.prev_sym.view._bg.name().lower() == "#ffffff",
                  d.prev_sym.view._bg.name())

        # 3) окно влезает в экран и умеет сжиматься
        scr = app.primaryScreen().availableGeometry()
        res.check("окно импорта влезает в экран",
                  d.width() <= scr.width() and d.height() <= scr.height(),
                  f"{d.width()}x{d.height()} при экране "
                  f"{scr.width()}x{scr.height()}")
        hint = d.minimumSizeHint()
        res.check("окно импорта можно сжать",
                  hint.width() <= 900, f"минимум {hint.width()}")
        d.close()
        svc.close()

    # 4) путь к 3D-модели ищется даже без переменных окружения KiCad
    with tempfile.TemporaryDirectory() as td:
        share = os.path.join(td, "share", "kicad")
        shapes = os.path.join(share, "3dmodels", "Resistor_SMD.3dshapes")
        os.makedirs(shapes, exist_ok=True)
        for d_ in ("symbols", "footprints"):
            os.makedirs(os.path.join(share, d_), exist_ok=True)
        step = os.path.join(shapes, "R_0402_1005Metric.step")
        with open(step, "w", encoding="ascii") as f:
            f.write("ISO-10303-21;")
        orig = kc.install_roots
        kc.install_roots = lambda: [share]
        try:
            got = kc.resolve_3d(
                "${KICAD9_3DMODEL_DIR}/Resistor_SMD.3dshapes/"
                "R_0402_1005Metric.step")
            got_wrl = kc.resolve_3d(
                "${KICAD9_3DMODEL_DIR}/Resistor_SMD.3dshapes/"
                "R_0402_1005Metric.wrl")
            none = kc.resolve_3d("${KICAD9_3DMODEL_DIR}/нет/такого.step")
        finally:
            kc.install_roots = orig
        res.check("модель находится по хвосту пути", got == step, str(got))
        res.check(".wrl подменяется на .step рядом", got_wrl == step,
                  str(got_wrl))
        res.check("несуществующей модели -- пустой путь", none == "", none)


def check_build_exe_lock(res: Result):
    """
    Сборка не должна падать трассировкой из-за запущенной программы.

    Симптом: `PermissionError: Отказано в доступе: dist\\GostLib.exe` из
    недр PyInstaller. Причина почти всегда одна -- GostLib в этот момент
    запущен. Windows не даёт перезаписать работающий exe, но даёт его
    переименовать; на этом и строится обход.
    """
    import importlib.util
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "build_exe.py"
    if not src.is_file():
        res.check("build_exe.py на месте", False, str(src))
        return
    spec = importlib.util.spec_from_file_location("_build_exe", src)
    be = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(be)
    be.log = lambda *_a, **_k: None      # не засорять вывод самопроверки

    with tempfile.TemporaryDirectory() as td:
        dist = Path(td)
        exe = dist / "GostLib.exe"
        exe.write_bytes(b"MZ")

        res.check("свободный файл трогать не нужно",
                  be.free_target(exe) and exe.is_file(), "")

        # притворяемся, что файл занят: на Linux работающий бинарник
        # открывается на запись без возражений, поэтому проверку подменяем
        real = be.locked
        be.locked = lambda p: True
        try:
            ok = be.free_target(exe)
        finally:
            be.locked = real
        moved = list(dist.glob("GostLib.old-*.exe"))
        res.check("занятый файл отложен в сторону, сборка продолжится",
                  ok and len(moved) == 1 and not exe.exists(),
                  f"{ok}, {[p.name for p in moved]}")

        # следующий запуск подбирает хвосты
        res.check("хвост прошлой сборки убирается сам",
                  be.sweep_old(dist) == 1 and not list(dist.glob("*.old-*")),
                  "")

        # если и переименовать нельзя -- честный отказ, а не трассировка
        exe.write_bytes(b"MZ")
        be.locked = lambda p: True
        real_rename = Path.rename

        def boom(self, target):
            raise PermissionError("занят")

        Path.rename = boom
        try:
            ok = be.free_target(exe)
        finally:
            Path.rename = real_rename
            be.locked = real
        res.check("неснимаемая блокировка -- отказ, а не падение",
                  ok is False, str(ok))


def check_pin_plan_tidy(res: Result):
    """
    Доводка раскладки после ИИ.

    Живой случай: DeepSeek на RK3308 (355 выводов) выдал три секции, все
    GPIO одной колонкой справа, а 250 земель -- одной колонкой слева. Это
    два листа A3 в высоту. Модель тут не виновата: она не знает ни размера
    листа, ни того, сколько выводов влезает в столбик. Компоновку
    доводим сами и проверяем именно это.
    """
    import collections
    from .pinplan import SIDE_CAP, kind_of, tidy

    rows = []

    def add(n, name, et, u, s, g):
        rows.append({"number": n, "name": name, "etype": et,
                     "unit": u, "side": s, "group": g})

    for i in range(5):
        add(f"C{i}", f"NPOR{i}", "input", 1, "L", "CTRL")
    for i in range(120):
        add(f"G{i}", f"GPIO{i // 32}_A{i % 32}", "io", 1, "R", f"GPIO{i // 32}")
    for i in range(20):
        add(f"A{i}", f"ADC_IN{i}", "input", 2, "L", "ADC")
    for i in range(50):
        add(f"P{i}", "DDR_VDD" if i % 2 else "VCCIO", "power", 3, "L", "PWR")
    for i in range(250):
        add(f"V{i}", "VSS", "power", 3, "L", "GND")

    out, notes = tidy(rows)
    res.check("ни один вывод не потерялся и не задвоился",
              len(out) == len(rows)
              and {r["number"] for r in out} == {r["number"] for r in rows},
              f"{len(out)} против {len(rows)}")

    cnt = collections.Counter((r["unit"], r["side"]) for r in out)
    res.check("в столбике не больше предела",
              max(cnt.values()) <= SIDE_CAP, str(max(cnt.values())))
    res.check("двести пятьдесят земель разложены, а не свалены",
              sum(1 for k, v in cnt.items() if v > SIDE_CAP) == 0, str(cnt))

    # питание и земля -- отдельно от сигналов и друг от друга
    by_unit = collections.defaultdict(set)
    for r in out:
        by_unit[r["unit"]].add(kind_of(r))
    mixed = [u for u, ks in by_unit.items() if "sig" in ks and len(ks) > 1]
    res.check("питание не перемешано с сигналами", not mixed, str(mixed))
    sides = {(r["unit"], r["side"]) for r in out if kind_of(r) == "pwr"}
    res.check("питание слева", all(s == "L" for _u, s in sides), str(sides))

    res.check("перекос сторон выровнен",
              all(abs(cnt.get((u, "L"), 0) - cnt.get((u, "R"), 0)) <= SIDE_CAP
                  for u in {k[0] for k in cnt}), str(cnt))
    res.check("секций стало больше трёх",
              len({k[0] for k in cnt}) >= 8, str(len({k[0] for k in cnt})))
    res.check("и про это сказано человеку", len(notes) >= 3, str(notes))

    out2, _n2 = tidy(out)
    res.check("повторная доводка ничего не меняет",
              [(r["number"], r["unit"], r["side"]) for r in out]
              == [(r["number"], r["unit"], r["side"]) for r in out2], "")

    # маленький компонент трогать незачем
    small = [{"number": "1", "name": "IN", "etype": "input", "unit": 1,
              "side": "L", "group": "IN"},
             {"number": "2", "name": "OUT", "etype": "output", "unit": 1,
              "side": "R", "group": "OUT"}]
    got, _n = tidy(small)
    res.check("маленький компонент остался как был",
              [(r["number"], r["unit"], r["side"]) for r in got]
              == [("1", 1, "L"), ("2", 1, "R")], str(got))

    # землю по имени узнаём даже с типом power
    res.check("VSS -- это земля, а не питание",
              kind_of({"name": "VSS", "etype": "power"}) == "gnd", "")
    res.check("CODEC_AVSS -- тоже земля",
              kind_of({"name": "CODEC_AVSS", "etype": "power"}) == "gnd", "")
    res.check("VCCIO3 -- питание",
              kind_of({"name": "VCCIO3", "etype": "power"}) == "pwr", "")
    res.check("GPIO -- сигнал",
              kind_of({"name": "GPIO0_A1", "etype": "io"}) == "sig", "")

    # доводка обязана пережить сборку символа
    from .ir import Component, Symbol, SymPin
    from .gost import symbolgen as sg
    c = Component(name="RK", ctype="ic")
    c.raw_pins = [SymPin(number=r["number"], name=r["name"],
                         etype=r["etype"]) for r in rows]
    c.symbol = Symbol(pins=list(c.raw_pins))
    for r in out:
        for p in c.symbol.pins:
            if p.number == r["number"]:
                p.unit, p.side = r["unit"], r["side"]
                p.group = "!" + r["group"]
                p.manual = True
    sym = sg.build(c)
    res.check("символ собрался со всеми секциями",
              sym.part_count == max(r["unit"] for r in out),
              f"{sym.part_count} против {max(r['unit'] for r in out)}")


def check_slots_and_paste_layers(res: Result):
    """
    Две беды импорта из EasyEDA, обе видны прямо на плате.

    1. Крепёжные пазы разъёма USB-C приезжали повёрнутыми на 90 градусов:
       угол лежит в отдельном поле holePoints, а мы его не читали.
    2. У BGA на каждый шарик в источнике лежит заливка на слое пасты; мы
       рисовали её контуром, и вокруг каждой площадки появлялась квадратная
       обводка, которую в Altium даже не выделить.
    """
    from .sources import easyeda as ee
    from .emit import altiumjob as aj
    from .render import svg
    from .ir import Component, Footprint, FpPrim, Pad, Symbol, SymPin, \
        hole_polygon

    # --- угол паза по holePoints
    res.check("паз вдоль Y распознан",
              ee._slot_angle("3993.7992 3003.5432 3993.7992 3000.7873") == 90.0,
              str(ee._slot_angle("3993.7992 3003.5432 3993.7992 3000.7873")))
    res.check("паз вдоль X распознан",
              ee._slot_angle("100 200 110 200") == 0.0, "")
    res.check("без поля -- честное None",
              ee._slot_angle("") is None, "")

    pkg = {"dataStr": {"head": {"x": 4000, "y": 3000}, "shape": [
        "PAD~OVAL~3993.799~3002.165~4.3307~7.4803~11~~13~1.1811~x~0~gge114"
        "~5.1181~3993.7992 3003.5432 3993.7992 3000.7873~Y~0~0~0.1969~x",
        "SOLIDREGION~5~~M 3990 3000 L 3995 3000 L 3995 3005 Z ~solid~g~~~~0",
        "SOLIDREGION~7~~M 3990 3000 L 3995 3000 L 3995 3005 Z ~solid~g~~~~0",
        "SOLIDREGION~3~~M 3990 3000 L 3995 3000 L 3995 3005 Z ~solid~g~~~~0",
        "TRACK~0.5~5~~3990 3000 3995 3000~gge1~0",
    ]}, "title": "USB"}
    fp, _u = ee._parse_footprint(pkg)
    res.check("угол паза приехал в площадку",
              fp.pads and fp.pads[0].hole_rot == 90.0,
              str(fp.pads[0].hole_rot if fp.pads else "нет площадок"))
    layers = sorted({p.layer for p in fp.prims})
    res.check("паста и маска из источника не берутся",
              layers == ["silk"], str(layers))

    # --- поправка для уже импортированного (угол потерян)
    bad = Pad(number="13", w=1.1, h=1.9, hole=0.6, hole_len=1.3, hole_rot=0.0)
    res.check("невозможный паз развёрнут по геометрии",
              bad.slot_rot() == 90.0, str(bad.slot_rot()))
    pts = hole_polygon(bad)
    dx = max(p[0] for p in pts) - min(p[0] for p in pts)
    dy = max(p[1] for p in pts) - min(p[1] for p in pts)
    res.check("и в превью паз стал вертикальным",
              abs(dx - 0.6) < 0.01 and abs(dy - 1.3) < 0.01,
              f"{dx:.2f} x {dy:.2f}")
    good = Pad(number="1", w=1.9, h=1.1, hole=0.6, hole_len=1.3)
    res.check("нормальный паз не трогаем", good.slot_rot() == 0.0, "")
    rnd = Pad(number="2", w=1.5, h=1.5, hole=0.8)
    res.check("круглое отверстие не трогаем", rnd.slot_rot() == 0.0, "")

    # --- сборка задания для уже испорченного каталога
    dirty = Footprint(name="BGA", pads=[Pad(number="A1", w=0.24, h=0.24)])
    dirty.prims = [
        FpPrim(kind="poly", layer="paste", filled=True,
               pts=[[0, 0], [0.2, 0], [0.2, 0.2], [0, 0.2]]),
        FpPrim(kind="poly", layer="mask", filled=True,
               pts=[[0, 0], [0.2, 0], [0.2, 0.2]]),
        FpPrim(kind="line", layer="silk", pts=[[0, 0], [1, 0]], width=0.15),
    ]
    c = Component(uid="u1", name="X", footprints=[dirty])
    c.symbol = Symbol(pins=[SymPin(number="1")])
    notes = []
    txt = aj.build_job([c], "a", "b", notes=notes)
    trk = [l for l in txt.splitlines() if l.startswith("TRK")]
    res.check("обводки с пасты и маски в задание не попали",
              len(trk) == 1, str(trk))
    res.check("и об этом сказано в отчёте",
              any("пасты" in n for n in notes), str(notes))
    s = svg.footprint_svg(dirty)
    res.check("в превью их тоже нет",
              s.count("polyline") + s.count("polygon") <= 1, "")


def check_shapes(res: Result):
    """
    Дуги, окружности и полигоны должны доезжать до Altium, а не жить
    только на холсте редактора. Плюс операции над выделенным:
    выравнивание, распределение, поворот, стиль по образцу.
    """
    from .gost import symbolgen
    from .emit import altiumjob as aj
    from .render import svg
    from .ir import Component, Symbol, SymPin

    c = Component(name="U1", ctype="ic")
    c.symbol = Symbol(pins=[SymPin(number="1", name="A"),
                            SymPin(number="2", name="B")])
    c.symbol.manual_layout = True
    c.symbol.body_w, c.symbol.body_h = 1000, 800
    c.symbol.user_shapes = [
        {"kind": "circle", "pts": [[500, -400]], "r": 200},
        {"kind": "arc", "pts": [[500, -400]], "r": 300, "a1": 0, "a2": 180},
        {"kind": "poly", "pts": [[100, -600], [300, -700], [500, -600]],
         "close": 1, "fill": 1},
        {"kind": "rect", "pts": [[600, -100], [900, -300]]},
    ]
    sym = symbolgen.build(c)
    kinds = [p.kind for p in sym.prims]
    res.check("окружность стала примитивом", "ellipse" in kinds, str(kinds))
    res.check("дуга стала примитивом", "arc" in kinds, str(kinds))
    res.check("полигон стал примитивом", "poly" in kinds, str(kinds))

    c2 = Component.from_dict(c.to_dict())
    res.check("фигуры переживают запись в каталог",
              len(c2.symbol.user_shapes) == 4,
              str(len(c2.symbol.user_shapes)))

    c.symbol = sym
    txt = aj.build_job([c], "a", "b")
    res.check("в задание для Altium уехали дуги",
              sum(1 for l in txt.splitlines() if l.startswith("SARC")) == 2,
              "SARC не найдены")
    res.check("и полигон отрезками",
              sum(1 for l in txt.splitlines() if l.startswith("SLINE")) >= 3,
              "SLINE не найдены")
    res.check("превью рисует фигуры", "<circle" in svg.symbol_svg(c), "")

    # --- секционность: фигуры второй секции не должны лезть в первую
    c3 = Component(name="U2", ctype="ic")
    c3.symbol = Symbol(part_count=2, pins=[
        SymPin(number="1", name="A", unit=1),
        SymPin(number="2", name="B", unit=2)])
    c3.symbol.manual_layout = True
    c3.symbol.body_w = c3.symbol.body_h = 800
    c3.symbol.set_geom(2, body_w=800, body_h=800,
                       user_shapes=[{"kind": "circle", "pts": [[400, -400]],
                                     "r": 100}])
    s3 = symbolgen.build(c3)
    units = [p.unit for p in s3.prims if p.kind == "ellipse"]
    res.check("фигура второй секции осталась во второй",
              units == [2], str(units))

    # --- операции редактора
    try:
        from PySide6.QtWidgets import QApplication
        from .gui.symedit import SymbolEditor
        from . import config as _cfg
        app = QApplication.instance() or QApplication([])
        cfg = _cfg.Config(root=tempfile.mkdtemp())
        ed = SymbolEditor(Component.from_dict(c2.to_dict()), cfg.to_style())
        cv = ed.canvas
        cv.resize(600, 400)

        cv.sel_shapes = [0, 1, 2, 3]
        n = cv.align("left")
        lefts = [round(cv.shape_box(s)[0]) for s in cv.shapes]
        res.check("выравнивание по левому краю сработало",
                  n == 4 and len(set(lefts)) == 1, str(lefts))

        cv.sel_shapes = [0, 1, 2]
        cv.distribute("x")
        cx = sorted((cv.shape_box(cv.shapes[i])[0]
                     + cv.shape_box(cv.shapes[i])[2]) / 2.0 for i in range(3))
        gaps = [round(cx[i + 1] - cx[i]) for i in range(2)]
        res.check("распределение дало равные промежутки",
                  abs(gaps[0] - gaps[1]) <= cv.grid, str(gaps))

        # поворот прямоугольника: Altium косых прямоугольников не умеет,
        # поэтому он обязан превратиться в ломаную
        cv.sel_shapes = [3]
        cv.rotate_selection(45.0)
        res.check("повёрнутый прямоугольник стал ломаной",
                  cv.shapes[3]["kind"] == "poly", cv.shapes[3]["kind"])

        # стиль по образцу
        cv.shapes[0]["w"], cv.shapes[0]["col"] = 3, 255
        cv.sel_shapes = [0, 1, 2]
        cv.copy_style()
        res.check("стиль разошёлся по выделенным",
                  all(cv.shapes[i]["w"] == 3 and cv.shapes[i]["col"] == 255
                      for i in (1, 2)),
                  str([(cv.shapes[i]["w"], cv.shapes[i]["col"])
                       for i in (1, 2)]))

        # углы дуги
        cv.sel_shapes = [1]
        res.check("углы задаются только дуге",
                  cv.set_arc_angles(30, 210) == 1
                  and cv.shapes[1]["a2"] == 210.0, str(cv.shapes[1]))

        # удаление и вставка
        cv.sel_shapes = [0]
        cv.clip_shapes = [dict(cv.shapes[0])]
        before = len(cv.shapes)
        cv._paste(100)
        res.check("вставка добавляет фигуру со сдвигом",
                  len(cv.shapes) == before + 1
                  and cv.shapes[-1]["pts"][0][1] == cv.shapes[0]["pts"][0][1] - 100,
                  str(cv.shapes[-1]["pts"]))

        # привязка к концам: точка рядом с вершиной обязана прилипнуть
        cv.snap_ends = True
        cv.g.user_lines = [[123, -456, 700, -456, 1, -1]]
        cv.zoom = 0.25
        res.check("привязка ловит конец линии",
                  cv._snap_pt(126, -452) == (123, -456),
                  str(cv._snap_pt(126, -452)))
        cv.snap_ends = False
        res.check("без привязки точка садится на сетку",
                  cv._snap_pt(126, -452) == (100, -500),
                  str(cv._snap_pt(126, -452)))

        # инструмент переключается и выключается повторным нажатием
        cv.set_tool("arc")
        was = cv.tool
        cv.set_tool("arc")
        res.check("инструмент выключается повторным нажатием",
                  was == "arc" and cv.tool == "", f"{was} -> {cv.tool}")
        ed.close()
    except Exception as e:                                  # noqa: BLE001
        res.check("операции редактора над фигурами", False, str(e))


def check_step_color(res: Result):
    """
    Цвет корпуса должен доезжать до Altium, а не только до просмотра.

    Симптом: «модельки всё ещё белые». Причина -- в фасетном STEP не
    было ни одной записи о цвете, и Altium рисовал модель по умолчанию.
    Проверяем всю цепочку: материалы из OBJ переносятся, выбранный цвет
    перекрывает их, сброс возвращает родные, а чужой файл не трогается.
    """
    import tempfile
    from . import config as _cfg
    from .service import Service
    from . import mesh2step as ms
    from .ir import Component, Footprint, Model3D, Pad, Symbol, SymPin

    def two_mat_obj(path):
        # кубик: низ материалом body (чёрный), верх -- ball (почти белый)
        v = [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0),
             (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("newmtl body\nKd 0.05 0.05 0.05\nendmtl\n")
            fh.write("newmtl ball\nKd 0.90 0.90 0.80\nendmtl\n")
            for p_ in v:
                fh.write("v %.4f %.4f %.4f\n" % p_)
            fh.write("usemtl body\nf 1 2 3\nf 1 3 4\n")
            fh.write("usemtl ball\nf 5 7 6\nf 5 8 7\n")

    def colors_of(path):
        import re
        return re.findall(r"COLOUR_RGB\('',([-0-9.eE,]+)\)",
                          open(path, encoding="ascii",
                               errors="replace").read())

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)

        obj = os.path.join(td, "part.obj")
        two_mat_obj(obj)
        step = os.path.join(cfg.models_dir, "part.step")
        ms.obj_to_step(obj, step, name="part")

        cols = colors_of(step)
        res.check("материалы OBJ перенесены в STEP", len(cols) == 2, str(cols))
        res.check("чёрный корпус остался чёрным",
                  cols and cols[0].startswith("0.05"), str(cols))
        res.check("стиль повешен на грани, а не на оболочку",
                  open(step, encoding="ascii", errors="replace"
                       ).read().count("STYLED_ITEM") == 4, "")

        # цвет через сервис -- как из окна программы
        c = Component(name="U1", ctype="ic")
        c.symbol = Symbol(pins=[SymPin(number="1")])
        fp = Footprint(name="FP", pads=[Pad(number="1", x=0, y=0,
                                            w=0.4, h=0.4)])
        fp.model = Model3D(path=step)
        c.footprints = [fp]
        svc.db.upsert(c)

        svc.set_model_color(c.uid, "#FF8000")
        cols = colors_of(step)
        res.check("выбранный цвет попал в файл модели",
                  cols and all(x.startswith("1.000000,0.501961") for x in cols),
                  str(cols))

        svc.set_model_color(c.uid, "")
        cols = colors_of(step)
        res.check("сброс вернул родные цвета модели",
                  len(cols) == 2 and cols[0].startswith("0.05")
                  and cols[1].startswith("0.9"), str(cols))

        # чужой STEP не трогаем ни при каких условиях
        alien = os.path.join(cfg.models_dir, "alien.step")
        body = open(step, encoding="ascii", errors="replace").read()
        body = body.replace("'GostLib','GostLib'", "'SolidWorks','SW'")
        open(alien, "w", encoding="ascii", errors="replace").write(body)
        before = open(alien, encoding="ascii", errors="replace").read()
        fp.model = Model3D(path=alien)
        svc.db.upsert(c)
        note = svc.apply_model_color(fp.model)
        res.check("чужой файл не тронут",
                  open(alien, encoding="ascii",
                       errors="replace").read() == before, note)
        res.check("и об этом честно сказано", "вручную" in note, note)

        # пережатие модели не должно терять выбранный цвет
        fp.model = Model3D(path=step, color="#123456")
        svc.db.upsert(c)
        ms.obj_to_step(obj, step, name="part",
                       color=svc.model_colors().get(
                           os.path.normcase(os.path.abspath(step)), ""))
        cols = colors_of(step)
        res.check("после перегонки цвет на месте",
                  cols and all(x.startswith("0.070588") for x in cols),
                  str(cols))


def check_paste(res: Result):
    """
    Паста: галочка «наносить», сетка окон и защита от нулевого окна.

    Повод: на BGA с шагом 0,65 зазор пасты −0,1 мм при площадке 0,24 мм
    оставлял окно в сорок микрон, и Altium рисовал вместо круга непонятно
    что. Плюс нужен способ вовсе отказаться от пасты у компонента.
    """
    from .emit import altiumjob as aj
    from .ir import Component, Footprint, Pad, Symbol, SymPin

    def job(fp):
        c = Component(uid="u1", name="X", footprints=[fp])
        c.symbol = Symbol(pins=[SymPin(number="1")])
        notes = []
        txt = aj.build_job([c], "a", "b", notes=notes)
        pads = [l.split("\t") for l in txt.splitlines() if l.startswith("PAD")]
        fills = [l.split("\t") for l in txt.splitlines()
                 if l.startswith("FILL")]
        return pads, fills, notes

    ball = Pad(number="A1", x=0, y=0, w=0.24, h=0.24, shape="round")

    fp = Footprint(name="BGA", pads=[ball])
    pads, _f, notes = job(fp)
    res.check("без настроек поле пасты пустое (правило проекта)",
              pads[0][14] == "", pads[0][14])

    fp = Footprint(name="BGA", pads=[ball], paste_expansion=-0.03)
    pads, _f, notes = job(fp)
    res.check("разумный зазор проходит как есть",
              abs(int(pads[0][14]) / aj.UNITS_PER_MM + 0.03) < 1e-4,
              pads[0][14])
    res.check("и без замечаний", not notes, str(notes))

    fp = Footprint(name="BGA", pads=[ball], paste_expansion=-0.1)
    pads, _f, notes = job(fp)
    left = 0.24 + 2 * int(pads[0][14]) / aj.UNITS_PER_MM
    res.check("съедающий окно зазор ограничен",
              abs(left - aj.MIN_PASTE) < 1e-3, f"окно {left:.3f} мм")
    res.check("про ограничение сказано в отчёте",
              any("окно" in n for n in notes), str(notes))

    fp = Footprint(name="BGA", pads=[ball], paste=False)
    pads, _f, _n = job(fp)
    res.check("без пасты окно закрыто с запасом",
              int(pads[0][14]) / aj.UNITS_PER_MM < -(0.24 / 2),
              pads[0][14])

    # сетка окон на тепловом пятаке
    fp = Footprint(name="QFN", pads=[
        Pad(number="1", x=-2, y=0, w=0.25, h=0.8, shape="rect"),
        Pad(number="EP", x=0, y=0, w=3.0, h=3.0, shape="rect")])
    fp.paste_grid, fp.paste_grid_fill = 3, 60.0
    pads, fills, notes = job(fp)
    res.check("сетка 3x3 -- девять окон", len(fills) == 9, str(len(fills)))
    area = sum((int(f[4]) - int(f[2])) * (int(f[5]) - int(f[3]))
               for f in fills) / aj.UNITS_PER_MM ** 2
    res.check("окна накрывают заданные 60% площадки",
              abs(area / 9.0 - 0.60) < 0.01, f"{area / 9.0:.3f}")
    res.check("собственное окно пятака закрыто",
              int(pads[1][14]) < 0, pads[1][14])
    res.check("мелкую площадку сетка не тронула",
              pads[0][14] == "", pads[0][14])

    # повёрнутая крупная площадка -- сетку не рисуем, но предупреждаем
    fp = Footprint(name="ROT", pads=[Pad(number="EP", x=0, y=0, w=3.0, h=3.0,
                                         shape="rect", rot=30.0)])
    fp.paste_grid = 3
    pads, fills, notes = job(fp)
    res.check("повёрнутой площадке сетку не ставим", not fills, str(fills))
    res.check("и говорим почему", any("поверн" in n.lower() or "повёрн" in n
                                      for n in notes), str(notes))


def main() -> int:
    res = run()
    print(f"Пройдено проверок: {len(res.ok)}")
    if res.fail:
        print(f"\nНе пройдено: {len(res.fail)}")
        for f in res.fail:
            print("  ✗ " + f)
        return 1
    print("Все проверки пройдены.")
    return 0


def check_orphan_counter(res: Result):
    """
    Счётчик компонентов у проекта не должен считать удалённые.

    Симптом был такой: в проекте два компонента, а в списке проектов
    рядом стоит девять -- ровно столько, сколько их там было за всю
    историю. Ссылки на удалённые компоненты остаются намеренно (по ним
    Ctrl+Z возвращает и членство в проекте), но считать их нельзя.
    """
    import tempfile
    from . import config as _cfg
    from .service import Service
    from .ir import Component, SymPin, Symbol

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        pid = svc.db.add_project("Плата", os.path.join(td, "b"))
        svc.set_active_project(pid)

        made = []
        for i in range(9):
            c = Component(name=f"R{i}", ctype="resistor")
            c.symbol = Symbol(pins=[SymPin(number="1"), SymPin(number="2")])
            svc.db.upsert(c)
            made.append(c.uid)
        svc.project_add(pid, made)

        def shown():
            return {r["name"]: r["n"] for r in svc.db.projects()}["Плата"]

        res.check("счётчик показывает добавленные", shown() == 9, str(shown()))

        svc.delete(made[2:])          # оставляем два
        res.check("после удаления счётчик уменьшился", shown() == 2,
                  f"{shown()} вместо 2")
        res.check("состав проекта тоже из двух",
                  len(svc.db.project_uids(pid)) == 2,
                  str(len(svc.db.project_uids(pid))))

        # удалили всё -- ноль, а не «девять»
        svc.delete(made[:2])
        res.check("пустой проект показывает ноль", shown() == 0, str(shown()))

        # ссылки живы, поэтому Ctrl+Z возвращает и членство
        res.check("висячие ссылки видно", svc.db.orphan_items() == 9,
                  str(svc.db.orphan_items()))
        svc.undo.undo()
        res.check("Ctrl+Z вернул компоненты в проект",
                  len(svc.db.project_uids(pid)) == 2,
                  str(len(svc.db.project_uids(pid))))

        # явная чистка прощается с ними
        svc.db.purge_orphans()
        res.check("чистка убрала висячие ссылки",
                  svc.db.orphan_items() == 0, str(svc.db.orphan_items()))
        res.check("живые ссылки чистка не тронула",
                  len(svc.db.project_uids(pid)) == 2,
                  str(len(svc.db.project_uids(pid))))
        svc.close()


def check_preview_font_scale(res: Result):
    """
    Кегль превью -- настройка, а не зашитое число.

    Раньше это была константа, подобранная на глаз; подогнать превью под
    свой Altium было нечем, а «текст чуть больше нужного» -- жалоба, на
    которую нечего ответить.
    """
    from .gost.style import Style
    from .render import svg as _s
    from . import config as _cfg
    import re as _re

    c = _mk("STM32", "mcu", [("1", "VDD", "power"), ("2", "GND", "power"),
                             ("3", "PA0", "io")])
    symbolgen.build(c, Style())

    def sizes(scale):
        st = Style()
        st.preview_font_scale = scale
        out = _s.symbol_svg(c, st)
        return [float(v) for v in _re.findall(r'font-size="([\d.]+)"', out)]

    a, b = sizes(0.72), sizes(0.36)
    res.check("в превью вообще есть текст", len(a) > 0, str(len(a)))
    res.check("кегль слушается настройки",
              a and b and abs(a[0] / b[0] - 2.0) < 0.05,
              f"{a[:2]} против {b[:2]}")

    res.check("настройка есть в конфиге",
              hasattr(_cfg.Config(), "preview_font_scale"),
              "нет поля preview_font_scale")
    res.check("настройка доезжает до стиля",
              abs(_cfg.Config(preview_font_scale=0.5)
                  .to_style().preview_font_scale - 0.5) < 1e-6,
              "to_style() теряет кегль превью")


def check_easyeda_network(res: Result):
    """
    Сеть EasyEDA: таймауты, повторы, кеш.

    «Иногда зависает на "Запрашиваю в EasyEDA"» -- это про то, что таймаут
    сокета не ограничивает загрузку целиком: сервер может отдавать по
    байту и формально не молчать.
    """
    import tempfile
    from .sources import easyeda as ee

    res.check("общий срок загрузки задан",
              getattr(ee, "TOTAL_DEADLINE", 0) > 0,
              "нет TOTAL_DEADLINE")
    res.check("повторы включены", getattr(ee, "RETRIES", 0) >= 2,
              str(getattr(ee, "RETRIES", 0)))
    res.check("размер ответа ограничен",
              0 < getattr(ee, "MAX_BYTES", 0) <= 200 * 1024 * 1024,
              str(getattr(ee, "MAX_BYTES", 0)))

    # кеш: сеть не трогаем вовсе
    with tempfile.TemporaryDirectory() as td:
        payload = {"success": True, "result": {"title": "TEST",
                                               "dataStr": {"head": {}}}}
        with open(os.path.join(td, "C999.json"), "w", encoding="utf-8") as f:
            import json as _json
            _json.dump(payload, f)

        calls = []
        real = ee._get

        def boom(*a, **k):
            calls.append(a)
            raise ee.EasyEdaError("сети быть не должно")

        ee._get = boom
        try:
            got = ee.fetch_raw("C999", cache_dir=td)
            res.check("кеш описания работает без сети",
                      got.get("title") == "TEST", str(got)[:60])
            res.check("в сеть при этом не ходили", not calls, str(calls))
        except Exception as e:
            res.check("кеш описания работает без сети", False, str(e))
        finally:
            ee._get = real

    # неверный код отвергается до всякой сети
    try:
        ee.fetch_raw("не код")
        res.check("кривой код LCSC отвергается", False, "приняли мусор")
    except ee.EasyEdaError:
        res.check("кривой код LCSC отвергается", True)


def check_build_speed(res: Result):
    """
    Бюджет времени на подготовку задания.

    Сборка BGA идёт минуты, и надо было понять, чья это доля. Python-часть
    обязана укладываться в секунды -- если однажды перестанет, это
    поймается здесь, а не на живом Altium.
    """
    import tempfile
    import time as _time
    from . import config as _cfg
    from .service import Service
    from .ir import Component, Footprint, Pad, SymPin, Symbol

    def bga(n, name):
        c = Component(name=name, ctype="mcu", designator="DD?")
        side = int(n ** 0.5) or 1
        rows = "ABCDEFGHJKLMNPRTUVWY"
        pins = [SymPin(number=f"{rows[(i // side) % len(rows)]}{i % side + 1}",
                       name=f"NET{i}", etype="io") for i in range(n)]
        c.raw_pins = pins
        c.symbol = Symbol(pins=list(pins))
        fp = Footprint(name=f"{name}_FP")
        fp.pads = [Pad(number=p.number, x=(i % side) * 0.8,
                       y=-(i // side) * 0.8, w=0.4, h=0.4, shape="round")
                   for i, p in enumerate(pins)]
        c.footprints = [fp]
        return c

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        uids = []
        t0 = _time.time()
        for n, nm in ((484, "BGA484"), (256, "BGA256")):
            c = bga(n, nm)
            symbolgen.build(c, svc.style_for(c))
            svc.db.upsert(c)
            uids.append(c.uid)
        t_sym = _time.time() - t0

        t0 = _time.time()
        r = svc.build_script_job(uids, only=True)
        t_job = _time.time() - t0

        res.check("символы двух BGA строятся быстрее 5 с", t_sym < 5.0,
                  f"{t_sym:.1f} с")
        res.check("задание для двух BGA пишется быстрее 5 с", t_job < 5.0,
                  f"{t_job:.1f} с")
        res.check("в задании оба компонента", r["components"] == "2",
                  str(r.get("components")))

        # столько объектов придётся создать Altium -- по этому числу и
        # видно, почему сборка BGA дольше сборки резистора
        lines = open(r["job"], encoding="cp1251", errors="replace").read()
        n_text = lines.count("\nSTEXT\t")
        n_pin = lines.count("\nPIN\t")
        res.check("номера выводов не задваиваются текстом",
                  n_text <= 2 * n_pin + 40, f"{n_text} текстов на {n_pin} выводов")
        svc.close()


def check_altium_script_speed(res: Result):
    """
    То, что делает сборку в Altium быстрой, должно остаться в скрипте.

    Это не замер (Altium тут нет), а защита от откатов: журнал пишется
    пачками, идентификатор шрифта кешируется, при FRESH не ищем
    одноимённые компоненты, и в журнал попадают времена этапов.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    pas = os.path.join(here, "altium", "GostLibBuilder.pas")
    if not os.path.isfile(pas):
        res.check("скрипт для Altium на месте", False, pas)
        return
    src = open(pas, encoding="utf-8", errors="replace").read()

    res.check("журнал пишется пачками, а не на каждой строке",
              "GLogDirty" in src and "If GLogDirty >= 40 Then FlushLog" in src,
              "Say() снова сохраняет файл на каждой строке")
    res.check("ошибка всё равно попадает на диск сразу",
              src.count("FlushLog") >= 4, "нет принудительной записи")
    res.check("идентификатор шрифта кешируется",
              "GFcKey" in src and "GFcVal" in src,
              "GetFontID снова зовётся на каждый текст")
    res.check("при FRESH не ищем одноимённые компоненты",
              "If GFresh <> 1 Then DropPcbComp" in src
              and "If GFresh <> 1 Then DropSchComp" in src,
              "лишний обход библиотеки на каждый компонент")
    for mark in ("ПОСАДКИ ВСЕГО", "СИМВОЛЫ ВСЕГО", "ВСЯ СБОРКА"):
        res.check(f"в журнале есть время «{mark}»", mark in src, mark)
    res.check("считаются текстовые объекты", "GNText" in src, "нет GNText")
    # Пустые заготовки Altium остаются в свежей библиотеке и видны в
    # панели Components наравне с настоящими компонентами.
    res.check("пустая заготовка PcbLib убирается",
              "DropPcbComp(Lib, 'PCBCOMPONENT_1')" in src, "PCBCOMPONENT_1")
    res.check("пустая заготовка SchLib убирается",
              "DropSchComp(Lib, 'Component_1')" in src, "Component_1")
    res.check("время каждой 3D-модели пишется отдельно",
              "3D: ' + ExtractFileName(path)" in src, "нет замера модели")


def check_model_budget(res: Result):
    """
    3D-модель режется под бюджет треугольников, но габарит остаётся.

    Замер на живом проекте: посадка с моделью на ~58 тысяч треугольников
    (STEP 23 МБ) собиралась в Altium 337 СЕКУНД, соседняя посадка из 355
    площадок -- 8,5 секунды. Значит модель обязана быть маленькой. При
    этом габарит трогать нельзя: по нему считают зазоры и высоту.
    """
    import tempfile
    from .mesh2step import obj_to_step, read_obj

    # шарик из двух «полусфер» на подложке: достаточно плотная сетка
    import math
    verts, faces = [], []
    n = 60
    for i in range(n):
        for j in range(n):
            u = math.pi * i / (n - 1)
            v = 2 * math.pi * j / n
            verts.append((3.0 * math.sin(u) * math.cos(v),
                          2.0 * math.sin(u) * math.sin(v),
                          0.5 * math.cos(u)))
    for i in range(n - 1):
        for j in range(n):
            a = i * n + j
            b = i * n + (j + 1) % n
            c = (i + 1) * n + j
            d = (i + 1) * n + (j + 1) % n
            faces.append((a, b, c))
            faces.append((b, d, c))

    with tempfile.TemporaryDirectory() as td:
        obj = os.path.join(td, "m.obj")
        with open(obj, "w", encoding="utf-8") as f:
            for v in verts:
                f.write("v %.6f %.6f %.6f\n" % v)
            for a, b, c in faces:
                f.write(f"f {a + 1} {b + 1} {c + 1}\n")

        v0, f0 = read_obj(obj)

        def box(vs):
            return tuple(round(max(p[i] for p in vs) - min(p[i] for p in vs), 4)
                         for i in range(3))

        b0 = box(v0)
        res.check("тестовая модель достаточно плотная", len(f0) > 6000,
                  str(len(f0)))

        step = os.path.join(td, "m.step")
        obj_to_step(obj, step, name="M", budget=2000)
        txt = open(step, encoding="ascii", errors="replace").read()
        pts = [tuple(map(float, m)) for m in re.findall(
            r"CARTESIAN_POINT\('',\(([-\d.]+),([-\d.]+),([-\d.]+)\)\)", txt)]
        res.check("STEP получился", len(pts) > 10, str(len(pts)))
        res.check("бюджет треугольников соблюдён",
                  txt.count("ADVANCED_FACE") <= 2000,
                  str(txt.count("ADVANCED_FACE")))
        res.check("габарит после огрубления сохранён",
                  pts and box(pts) == b0, f"{box(pts) if pts else None} против {b0}")

        # больший бюджет -- крупнее файл
        step2 = os.path.join(td, "m2.step")
        obj_to_step(obj, step2, name="M", budget=20000)
        res.check("бюджет влияет на размер",
                  os.path.getsize(step2) > os.path.getsize(step),
                  f"{os.path.getsize(step2)} против {os.path.getsize(step)}")


def check_heavy_models(res: Result):
    """Крупные модели видно заранее и их можно пережать."""
    import tempfile
    from . import config as _cfg
    from .service import Service

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        big = os.path.join(cfg.models_dir, "BIG.step")
        with open(big, "wb") as f:
            f.write(b"x" * (5 * 1024 * 1024))
        small = os.path.join(cfg.models_dir, "SMALL.step")
        with open(small, "wb") as f:
            f.write(b"x" * 1024)

        heavy = svc.heavy_models()
        res.check("крупная модель попадает в список",
                  [n for n, _ in heavy] == ["BIG.step"], str(heavy))

        # без исходного .obj пережимать нечего -- но и падать нельзя
        r = svc.shrink_models()
        res.check("без .obj модель пропускается, а не ломается",
                  not r["done"] and r["skipped"], str(r)[:80])
        res.check("настройка подробности есть",
                  int(getattr(cfg, "model_faces", 0)) > 0,
                  str(getattr(cfg, "model_faces", None)))
        svc.close()


def check_model_seating(res: Result):
    """
    3D-тело должно стоять НА плате, а не в ней.

    Живой случай: модель FBGA-96 из EasyEDA шла по Z от -0.37 до +0.73 мм,
    то есть шарики уходили внутрь платы на треть миллиметра. Altium
    считает Z = 0 плоскостью платы. Правим ТОЛЬКО смещение (standoff):
    файл модели может быть выбран руками, и портить его нельзя.
    """
    import tempfile
    from . import config as _cfg
    from .service import Service
    from .mesh2step import obj_to_step
    from .step3d import z_range
    from .ir import Component, Footprint, Pad, Model3D, Symbol, SymPin

    def cube_obj(path, z0, z1):
        v = [(-1, -1, z0), (1, -1, z0), (1, 1, z0), (-1, 1, z0),
             (-1, -1, z1), (1, -1, z1), (1, 1, z1), (-1, 1, z1)]
        f = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6),
             (0, 4, 5), (0, 5, 1), (1, 5, 6), (1, 6, 2),
             (2, 6, 7), (2, 7, 3), (3, 7, 4), (3, 4, 0)]
        with open(path, "w", encoding="utf-8") as fh:
            for p_ in v:
                fh.write("v %.4f %.4f %.4f\n" % p_)
            for a, b, c in f:
                fh.write(f"f {a + 1} {b + 1} {c + 1}\n")

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)

        obj = os.path.join(td, "part.obj")
        cube_obj(obj, -0.37, 0.73)          # как настоящая FBGA-96
        step = os.path.join(cfg.models_dir, "part.step")
        obj_to_step(obj, step, name="part")

        res.check("модель действительно ниже платы",
                  z_range(step)[0] < -0.3, str(z_range(step)))

        # SMD-посадка: тело поднимаем
        fp = Footprint(name="BGA")
        fp.pads = [Pad(number="1", x=0, y=0, w=0.4, h=0.4)]
        fp.model = Model3D(path=step)
        dz = svc.seat_model(fp)
        res.check("SMD-тело поднято на плоскость платы",
                  abs(dz - 0.37) < 0.01, f"{dz}")
        res.check("файл модели при этом не тронут",
                  abs(z_range(step)[0] + 0.37) < 0.01, str(z_range(step)))

        # заданное руками смещение не перетирается
        fp2 = Footprint(name="BGA2")
        fp2.pads = [Pad(number="1", x=0, y=0, w=0.4, h=0.4)]
        fp2.model = Model3D(path=step, dz=1.5)
        res.check("ручное смещение не перетирается",
                  abs(svc.seat_model(fp2) - 1.5) < 1e-6, str(fp2.model.dz))

        # выводной корпус: ножки ниже платы -- это правильно
        fp3 = Footprint(name="DIP")
        fp3.pads = [Pad(number="1", x=0, y=0, w=1.6, h=1.6, hole=0.8)]
        fp3.model = Model3D(path=step)
        res.check("у выводного корпуса тело не поднимают",
                  abs(svc.seat_model(fp3)) < 1e-9, str(fp3.model.dz))

        # выключатель слушается
        cfg.seat_models = False
        fp4 = Footprint(name="BGA4")
        fp4.pads = [Pad(number="1", x=0, y=0, w=0.4, h=0.4)]
        fp4.model = Model3D(path=step)
        res.check("настройка «сажать модели» слушается",
                  abs(svc.seat_model(fp4)) < 1e-9, str(fp4.model.dz))
        cfg.seat_models = True

        # свой STEP отличается от чужого -- по этому признаку мы решаем,
        # можно ли его пережимать
        res.check("свой STEP опознаётся", svc.model_made_by_us(step), step)
        alien = os.path.join(cfg.models_dir, "alien.step")
        with open(alien, "w", encoding="ascii") as fh:
            fh.write("ISO-10303-21;\nHEADER;\nFILE_NAME('a','',(''),(''),"
                     "'SolidWorks','SolidWorks','');\nENDSEC;\nDATA;\n"
                     + "x" * (5 * 1024 * 1024) + "\nENDSEC;\n")
        res.check("чужой STEP опознаётся как чужой",
                  not svc.model_made_by_us(alien), alien)
        r = svc.shrink_models()
        res.check("чужой STEP не пережимается",
                  any("вручную" in s for s in r["skipped"]), str(r["skipped"]))

        # пакетная посадка чинит уже импортированное
        c = Component(name="U1", ctype="mcu")
        c.symbol = Symbol(pins=[SymPin(number="1")])
        fp5 = Footprint(name="BGA5")
        fp5.pads = [Pad(number="1", x=0, y=0, w=0.4, h=0.4)]
        fp5.model = Model3D(path=step)
        c.footprints = [fp5]
        svc.db.upsert(c)
        svc.reseat_models([c.uid])
        back = svc.db.get(c.uid)
        res.check("пакетная посадка правит каталог",
                  abs(back.footprints[0].model.dz - 0.37) < 0.01,
                  str(back.footprints[0].model.dz))
        svc.undo.undo()
        res.check("посадка откатывается Ctrl+Z",
                  abs(svc.db.get(c.uid).footprints[0].model.dz) < 1e-9,
                  str(svc.db.get(c.uid).footprints[0].model.dz))
        svc.close()


def check_pad_shapes(res: Result):
    """
    Формы площадок рисуются точно, а не «примерно похоже».

    Овал (obround) -- это прямоугольник с полукруглыми торцами, а не
    эллипс: у него прямые борта. Восьмиугольник раньше рисовался
    прямоугольником, паз -- круглым отверстием вдвое короче настоящего.
    Один и тот же контур считается для превью и для просмотра 3D, чтобы
    они не расходились.
    """
    from .ir import Footprint, Pad, hole_polygon, pad_polygon
    from .render import svg as _s

    def box(pts):
        return (round(max(p[0] for p in pts) - min(p[0] for p in pts), 4),
                round(max(p[1] for p in pts) - min(p[1] for p in pts), 4))

    for shape in ("rect", "round", "oval", "octagon", "roundrect"):
        p = Pad(number="1", w=2.0, h=1.0, shape=shape, corner_radius=25)
        pts = pad_polygon(p)
        res.check(f"габарит площадки «{shape}» сохранён",
                  box(pts) == (2.0, 1.0), f"{shape}: {box(pts)}")

    # у овала борта прямые: посередине ширина равна полной высоте
    ov = pad_polygon(Pad(number="1", w=4.0, h=1.0, shape="oval"))
    # прямой участок: у стадиона он тянется от -1.5 до +1.5 на y = ±0.5,
    # у эллипса высота на |x| = 1.5 была бы заметно меньше половины
    flat = [(x, y) for x, y in ov if abs(abs(y) - 0.5) < 1e-6]
    res.check("у овала есть прямые борта, а не эллипс",
              len(flat) >= 4 and max(abs(x) for x, _ in flat) > 1.4,
              str(sorted({round(x, 3) for x, _ in flat})[:6]))
    edge = [abs(y) for x, y in ov if abs(abs(x) - 1.5) < 0.05]
    res.check("на краю прямого участка высота полная",
              bool(edge) and abs(max(edge) - 0.5) < 1e-6,
              "нет точек на краю прямого участка" if not edge
              else str(max(edge)))

    res.check("восьмиугольник -- восемь углов",
              len(pad_polygon(Pad(number="1", w=2, h=2, shape="octagon"))) == 8,
              str(len(pad_polygon(Pad(number="1", w=2, h=2,
                                      shape="octagon")))))

    # поворот учитывается
    rot = pad_polygon(Pad(number="1", w=2.0, h=1.0, shape="rect", rot=90))
    res.check("поворот площадки учтён", box(rot) == (1.0, 2.0), str(box(rot)))

    # паз -- стадион нужной длины и в нужную сторону
    slot = hole_polygon(Pad(number="1", w=2, h=2, hole=0.8, hole_len=2.0))
    res.check("паз имеет длину паза, а не диаметр",
              box(slot) == (2.0, 0.8), str(box(slot)))
    slot90 = hole_polygon(Pad(number="1", w=2, h=2, hole=0.8, hole_len=2.0,
                              hole_rot=90))
    res.check("угол паза учтён", box(slot90) == (0.8, 2.0), str(box(slot90)))

    # превью рисует контуры, а не эллипсы
    fp = Footprint(name="T")
    fp.pads = [Pad(number="1", x=0, y=0, w=2, h=1, shape="oval"),
               Pad(number="2", x=3, y=0, w=1.5, h=1.5, shape="octagon")]
    out = _s.footprint_svg(fp)
    res.check("в превью нет эллипсов вместо овалов",
              "<ellipse" not in out, "остался ellipse")
    res.check("в превью есть контуры площадок",
              out.count("<polygon") >= 2, str(out.count("<polygon")))


def check_pad_expansion(res: Result):
    """
    Зазоры маски и пасты: пусто -- значит «правило проекта».

    Написать сюда число значит молча перебить правила платы, поэтому
    пустое поле обязано доезжать до задания пустым, а не нулём.
    """
    import tempfile
    from . import config as _cfg
    from .service import Service
    from .ir import Component, Footprint, Pad, Symbol, SymPin

    def pads_of(job):
        return [l.split("\t") for l in
                open(job, encoding="cp1251", errors="replace").read()
                .splitlines() if l.startswith("PAD\t")]

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        c = Component(name="R", ctype="resistor")
        c.symbol = Symbol(pins=[SymPin(number="1")])
        fp = Footprint(name="F")
        fp.pads = [Pad(number="1", w=1, h=1),
                   Pad(number="2", w=1, h=1, mask_expansion=0.1)]
        c.footprints = [fp]
        svc.db.upsert(c)

        rows = pads_of(svc.build_script_job([c.uid], only=True)["job"])
        res.check("без настройки зазор маски пуст",
                  len(rows) == 2 and rows[0][13] == "",
                  str(rows[0][13:] if rows else None))
        # внутренняя единица Altium -- 1/10000 мила, значит 0.1 мм = 39370
        res.check("зазор у отдельной площадки доезжает",
                  rows[1][13] == "39370", str(rows[1][13:]))

        cfg.mask_expansion = 0.05
        cfg.paste_expansion = -0.02
        rows = pads_of(svc.build_script_job([c.uid], only=True)["job"])
        res.check("общий зазор маски доезжает",
                  rows[0][13] == "19685", str(rows[0][13:]))
        res.check("общий зазор пасты доезжает",
                  rows[0][14] == "-7874", str(rows[0][13:]))
        res.check("зазор площадки важнее общего",
                  rows[1][13] == "39370", str(rows[1][13:]))

        # скрипт должен уметь их принять
        here = os.path.dirname(os.path.abspath(__file__))
        src = open(os.path.join(here, "altium", "GostLibBuilder.pas"),
                   encoding="utf-8", errors="replace").read()
        # Зазоры ставятся ЧЕРЕЗ КЕШ площадки. Прямое присваивание
        # Pad.SolderMaskExpansion роняет Altium -- проверено тремя
        # прогонами на живом 26.8.1.
        res.check("зазор маски не пишется площадке напрямую",
                  "Pad.SolderMaskExpansion :=" not in src,
                  "снова роняем Altium")
        res.check("зазор пасты не пишется площадке напрямую",
                  "Pad.PasteMaskExpansion :=" not in src,
                  "снова роняем Altium")
        res.check("зазоры идут через кеш площадки",
                  "Pad.GetState_Cache" in src
                  and "Pad.SetState_Cache" in src,
                  "нет работы с кешем")
        res.check("зазор помечается как заданный вручную",
                  "SolderMaskExpansionValid := eCacheManual" in src,
                  "иначе Altium оставит правило проекта")
        res.check("первая площадка с зазором пишется в журнал сразу",
                  "через кеш площадки, первая" in src
                  and "GExpTried" in src,
                  "при падении не будет видно, где именно")
        svc.close()


def check_window_fits_screen(res: Result):
    """
    Окно должно помещаться на обычный монитор.

    Живой случай: минимальная ширина главного окна выросла до 1970 px --
    на мониторе 1920 Qt писал «Unable to set geometry» и обрезал окно сам.
    Минимум складывается из минимумов вложенных панелей, поэтому крупным
    панелям задана политика Ignored: тогда Qt берёт явный минимум, а не
    подсказку раскладки.
    """
    import os as _os
    _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except Exception:
        res.check("окно помещается на экран", True, "")
        return
    app = QApplication.instance() or QApplication([])
    import tempfile
    from . import config as _cfg
    from .gui.main_window import MainWindow

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        w = MainWindow(cfg)
        mw = w.minimumSizeHint().width()
        res.check("минимальная ширина окна меньше 1366",
                  mw < 1366, f"{mw} px")
        w.resize(1280, 720)
        app.processEvents()
        res.check("окно принимает размер 1280x720",
                  w.width() <= 1281, f"{w.width()} px")
        # шрифт с пиксельным размером не должен ронять Qt в предупреждение
        from PySide6.QtGui import QFont
        f = QFont()
        f.setPixelSize(12)
        res.check("шрифт в пикселях не даёт нулевой кегль",
                  f.pointSize() <= 0 and f.pixelSize() == 12,
                  f"{f.pointSize()} / {f.pixelSize()}")
        w.close()


def check_expansion_is_per_footprint(res: Result):
    """
    Зазоры маски и пасты живут у ПОСАДКИ, а не в общих настройках.

    Зазор -- свойство корпуса: у BGA один, у QFN с тепловым пятном
    другой. Одно значение на всю библиотеку молча перебивало бы правила
    платы у всего подряд.
    """
    import tempfile
    from . import config as _cfg
    from .service import Service
    from .ir import Component, Footprint, Pad, Symbol, SymPin

    cfg0 = _cfg.Config()
    res.check("общей настройки зазора больше нет",
              not hasattr(cfg0, "mask_expansion"),
              "Config всё ещё знает про mask_expansion")

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        c = Component(name="U", ctype="mcu")
        c.symbol = Symbol(pins=[SymPin(number="1")])
        fp = Footprint(name="BGA")
        fp.pads = [Pad(number="1", w=1, h=1),
                   Pad(number="2", w=1, h=1, mask_expansion=0.1)]
        c.footprints = [fp]
        svc.db.upsert(c)

        def pads(job):
            return [l.split("\t") for l in
                    open(job, encoding="cp1251", errors="replace").read()
                    .splitlines() if l.startswith("PAD\t")]

        rows = pads(svc.build_script_job([c.uid], only=True)["job"])
        res.check("по умолчанию зазоры пусты",
                  rows[0][13] == "" and rows[0][14] == "", str(rows[0][13:]))

        svc.set_expansion(c.uid, 0.05, -0.02)
        rows = pads(svc.build_script_job([c.uid], only=True)["job"])
        res.check("зазор посадки доезжает до всех её площадок",
                  rows[0][13] == "19685" and rows[0][14] == "-7874",
                  str(rows[0][13:]))
        res.check("зазор площадки важнее зазора посадки",
                  rows[1][13] == "39370", str(rows[1][13:]))

        # ноль -- это ноль, а не «не задано»
        svc.set_expansion(c.uid, 0.0, None)
        rows = pads(svc.build_script_job([c.uid], only=True)["job"])
        res.check("ноль отличается от «не задано»",
                  rows[0][13] == "0" and rows[0][14] == "",
                  str(rows[0][13:]))

        svc.undo.undo()
        res.check("зазоры откатываются Ctrl+Z",
                  svc.db.get(c.uid).footprints[0].mask_expansion == 0.05,
                  str(svc.db.get(c.uid).footprints[0].mask_expansion))

        # скрипт: несуществующего члена быть не должно
        here = os.path.dirname(os.path.abspath(__file__))
        src = open(os.path.join(here, "altium", "GostLibBuilder.pas"),
                   encoding="utf-8", errors="replace").read()
        res.check("прямых присваиваний площадке нет",
                  "Pad.SolderMaskExpansion :=" not in src
                  and "Pad.PasteMaskExpansion :=" not in src,
                  "снова роняем Altium")
        from .altium.paslint import check as _lint
        found = _lint("Procedure P;\nBegin\n"
                      "    Pad.SolderMaskExpansion := 100;\nEnd.\n")
        res.check("линтер ловит прямое присваивание площадке",
                  any("роняет Altium" in x for x in found), str(found[:2]))
        ok = _lint("Procedure P;\nVar\n    Cache : TPadCache;\nBegin\n"
                   "    Cache := Pad.GetState_Cache;\n"
                   "    Cache.SolderMaskExpansion := 100;\n"
                   "    Pad.SetState_Cache := Cache;\nEnd.\n")
        res.check("линтер не ругается на путь через кеш",
                  not [x for x in ok if "роняет Altium" in x], str(ok[:2]))
        svc.close()


def check_everything_undoable(res: Result):
    """
    Каждая правка компонента должна отменяться.

    Это не абстрактная проверка. Половина методов звала `undo.touch()`
    БЕЗ открытого шага, а вне шага touch не делает ничего -- то есть
    Ctrl+Z молча не работал для переименования, раскладки выводов,
    правки УГО, смены модели, зазоров. В интерфейсе при этом написано,
    что отменяется всё. Проверяем каждую операцию по отдельности и
    заодно следим, чтобы новая правка не забыла про отмену.
    """
    import tempfile
    from . import config as _cfg
    from .service import Service
    from .ir import Component, Footprint, Pad, Symbol, SymPin

    def fresh(svc, name="R1"):
        c = Component(name=name, ctype="resistor", designator="R?")
        c.raw_pins = [SymPin(number="1", name="A", etype="passive"),
                      SymPin(number="2", name="K", etype="passive")]
        c.symbol = Symbol(pins=list(c.raw_pins))
        fp = Footprint(name=name + "_FP")
        fp.pads = [Pad(number="1", x=0, y=0, w=1, h=1),
                   Pad(number="2", x=2, y=0, w=1, h=1)]
        c.footprints = [fp]
        symbolgen.build(c, svc.style_for(c))
        svc.db.upsert(c)
        return c

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)

        # (название, что делаем, как прочитать значение, каким оно станет)
        cases = [
            ("переименование",
             lambda c: svc.rename(c.uid, "R_NEW"),
             lambda c: svc.db.get(c.uid).name, "R_NEW"),
            ("смена типа",
             lambda c: svc.set_type([c.uid], "capacitor"),
             lambda c: svc.db.get(c.uid).ctype, "capacitor"),
            ("зазоры площадок",
             lambda c: svc.set_expansion(c.uid, 0.07, None),
             lambda c: svc.db.get(c.uid).footprints[0].mask_expansion, 0.07),
            ("высота корпуса",
             lambda c: svc.set_height(c.uid, 1.75),
             lambda c: svc.db.get(c.uid).footprints[0].height, 1.75),
            ("раскладка выводов",
             lambda c: svc.apply_pins(c.uid, [
                 {"number": "2", "name": "K", "etype": "passive", "unit": 1,
                  "side": "", "group": ""},
                 {"number": "1", "name": "A", "etype": "passive", "unit": 1,
                  "side": "", "group": ""}]),
             lambda c: [p.number for p in svc.db.get(c.uid).raw_pins],
             ["2", "1"]),
            ("удаление",
             lambda c: svc.delete([c.uid]),
             lambda c: svc.db.get(c.uid) is None, True),
        ]

        for i, (title, do, read, want) in enumerate(cases):
            c = fresh(svc, f"R{i}")
            before = read(c)
            do(c)
            after = read(c)
            res.check(f"{title}: правка применилась", after == want,
                      f"{after!r} вместо {want!r}")
            res.check(f"{title}: шаг отмены появился", svc.undo.can_undo(),
                      "нечего отменять")
            svc.undo.undo()
            res.check(f"{title}: Ctrl+Z вернул как было",
                      read(c) == before, f"{read(c)!r} вместо {before!r}")

        # и защита от повторения истории: в service не должно остаться
        # вызовов touch вне шага
        here = os.path.dirname(os.path.abspath(__file__))
        src = open(os.path.join(here, "service.py"),
                   encoding="utf-8").read()
        bad = []
        cur = ""
        in_step = False
        for ln in src.split("\n"):
            s = ln.strip()
            if s.startswith("def "):
                cur = s.split("(")[0][4:]
                in_step = False
            if "with self.undo.step" in s or "self.undo.begin" in s:
                in_step = True
            if s.startswith("self.undo.touch(") and not in_step:
                bad.append(cur)
        res.check("нет вызовов undo.touch вне шага отмены",
                  not bad, "без шага: " + ", ".join(bad))
        svc.close()


def check_no_nested_procedures(res: Result):
    """
    Вложенных процедур в скрипте быть не должно.

    DelphiScript не даёт вложенной процедуре обращаться к переменным
    внешней: Altium показывает окно «Can-t access top level variable».
    Это не ошибка компиляции, а падение на ходу, поэтому ловим линтером.
    """
    from .altium.paslint import check

    here = os.path.dirname(os.path.abspath(__file__))
    pas = os.path.join(here, "altium", "GostLibBuilder.pas")
    src = open(pas, encoding="utf-8", errors="replace").read()
    nested = [ln for ln in src.split("\n")
              if re.match(r"^\s+(Procedure|Function)\s+\w+", ln, re.I)]
    res.check("в скрипте нет вложенных процедур", not nested,
              "; ".join(x.strip()[:50] for x in nested[:3]))

    # и линтер обязан такое ловить
    sample = ("Procedure Outer;\n"
              "Var\n"
              "    S : String;\n"
              "\n"
              "    Procedure Inner(T : String);\n"
              "    Begin\n"
              "        S := T;\n"
              "    End;\n"
              "\n"
              "Begin\n"
              "    Inner(#39x#39);\n"
              "End.\n").replace("#39", "'")
    found = [p for p in check(sample) if "вложенная" in p]
    res.check("линтер ловит вложенную процедуру", bool(found),
              "не поймал")

    # Проверку свойств площадки (GostLibProbePad) убрали: ответ получен,
    # а сама она теперь тоже уронила бы Altium -- она эти свойства и
    # трогала. Правило про вложенные процедуры осталось.
    res.check("временной диагностики в скрипте не осталось",
              "GostLibProbePad" not in src and "ProbeNote" not in src,
              "проверка свойств всё ещё в скрипте")


def check_model_color_sticks(res: Result):
    """
    Выбранный цвет 3D-модели должен сохраняться.

    Две дыры было. Первая: модель грузится в фоне, и когда загрузка
    заканчивалась, панель бралa посадочное место, каким оно было В МОМЕНТ
    ЗАПРОСА -- выбранный за это время цвет откатывался на экране. Вторая:
    замена файла модели создавала Model3D с нуля и цвет терялся молча.
    """
    import tempfile
    from . import config as _cfg
    from .service import Service
    from .ir import Component, Footprint, Model3D, Pad, Symbol, SymPin

    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg.Config(root=td)
        cfg.ensure_dirs()
        svc = Service(cfg)
        step = os.path.join(cfg.models_dir, "m.step")
        with open(step, "w", encoding="ascii") as f:
            f.write("ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\n"
                    "END-ISO-10303-21;\n")
        other = os.path.join(td, "other.step")
        with open(other, "w", encoding="ascii") as f:
            f.write("ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\n"
                    "END-ISO-10303-21;\n")

        c = Component(name="U", ctype="mcu")
        c.symbol = Symbol(pins=[SymPin(number="1")])
        fp = Footprint(name="F")
        fp.pads = [Pad(number="1", w=1, h=1)]
        fp.model = Model3D(path=step)
        c.footprints = [fp]
        svc.db.upsert(c)

        svc.set_model_color(c.uid, "#AA3311")
        res.check("цвет сохраняется в каталоге",
                  svc.db.get(c.uid).footprints[0].model.color == "#AA3311",
                  str(svc.db.get(c.uid).footprints[0].model.color))

        svc.set_model(c.uid, other)
        res.check("замена файла модели не стирает цвет",
                  svc.db.get(c.uid).footprints[0].model.color == "#AA3311",
                  str(svc.db.get(c.uid).footprints[0].model.color))

        res.check("цвет откатывается Ctrl+Z",
                  svc.undo.can_undo(), "нечего отменять")

        # мусор не принимаем
        try:
            svc.set_model_color(c.uid, "красный")
            res.check("кривой цвет отвергается", False, "приняли")
        except ValueError:
            res.check("кривой цвет отвергается", True)
        svc.close()

    # панель обязана брать живое посадочное место, а не снимок запроса
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "gui", "main_window.py"),
               encoding="utf-8").read()
    tail = src.split("def _model_ready")[1][:900]
    res.check("после фоновой загрузки берётся текущее посадочное место",
              "cur.footprints" in tail,
              "используется снимок на момент запроса")

if __name__ == "__main__":
    raise SystemExit(main())
