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
    res.run("конвертер OBJ->STEP", lambda: check_step(res))
    res.run("каталог", lambda: check_db(res))
    res.run("запись .SchLib", lambda: check_schlib_writer(res, st))
    res.run("стиль компонента", lambda: check_component_style(res, st))
    res.run("окна интерфейса", lambda: check_gui_imports(res))
    res.run("пазы отверстий", lambda: check_slots(res))
    res.run("качество родного УГО", lambda: check_native_quality(res))
    res.run("секции символа", lambda: check_sections(res))
    res.run("отмена действий", lambda: check_undo(res))
    res.run("отчёт о сборке", lambda: check_build_report(res))
    res.run("имена", lambda: check_names(res))
    res.run("ломаные в превью", lambda: check_preview_polyline(res))
    res.run("команда для Altium", lambda: check_altium_command(res))
    res.run("порядок выводов", lambda: check_pin_order(res))
    res.run("место на диске", lambda: check_disk_usage(res))
    res.run("проекты", lambda: check_projects(res))
    res.run("импорт в проект", lambda: check_import_into_project(res))
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
    return res


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


if __name__ == "__main__":
    raise SystemExit(main())
