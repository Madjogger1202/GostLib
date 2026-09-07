"""
Отчёт о сборке: что просили и что реально построилось.

Скрипт внутри Altium кладёт рядом с заданием файл `<задание>.done` --
список того, что он создал. Здесь этот список сверяется с самим заданием.

Без такой сверки «нажал собрать, а обновилось не всё» невозможно ни
подтвердить, ни опровергнуть: журнал длинный, панель Components кеширует
состав, а взгляд на пару компонентов ничего не доказывает.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Report:
    job: str = ""
    done_path: str = ""
    fresh: bool = False           # отчёт относится к этому заданию
    age: float = 0.0              # сколько секунд назад построено
    schlib: str = ""
    pcblib: str = ""
    asked_sym: List[str] = field(default_factory=list)
    asked_fp: List[str] = field(default_factory=list)
    made_sym: List[str] = field(default_factory=list)
    made_fp: List[str] = field(default_factory=list)
    warn: int = 0
    err: int = 0
    error: str = ""

    @property
    def missing_sym(self) -> List[str]:
        have = {s.strip().lower() for s in self.made_sym}
        return [s for s in self.asked_sym if s.strip().lower() not in have]

    @property
    def missing_fp(self) -> List[str]:
        have = {s.strip().lower() for s in self.made_fp}
        return [s for s in self.asked_fp if s.strip().lower() not in have]

    @property
    def extra_sym(self) -> List[str]:
        want = {s.strip().lower() for s in self.asked_sym}
        return [s for s in self.made_sym if s.strip().lower() not in want]

    @property
    def ok(self) -> bool:
        return (self.fresh and not self.error and not self.err
                and not self.missing_sym and not self.missing_fp)

    def lines(self) -> List[str]:
        if self.error:
            return [self.error]
        if not self.fresh:
            return ["Altium ещё не собирал это задание."]
        out = [f"Символов: {len(self.made_sym)} из {len(self.asked_sym)}",
               f"Посадок:  {len(self.made_fp)} из {len(self.asked_fp)}"]
        if self.err:
            out.append(f"Ошибок в скрипте: {self.err}")
        if self.warn:
            out.append(f"Предупреждений: {self.warn}")
        if self.missing_sym:
            out.append("")
            out.append("Не построились символы: "
                       + ", ".join(self.missing_sym[:12])
                       + (" …" if len(self.missing_sym) > 12 else ""))
        if self.missing_fp:
            out.append("Не построились посадки: "
                       + ", ".join(self.missing_fp[:12])
                       + (" …" if len(self.missing_fp) > 12 else ""))
        if self.extra_sym:
            out.append("Лишние в библиотеке: "
                       + ", ".join(self.extra_sym[:8]))
        if self.ok:
            out.append("")
            out.append("Всё на месте.")
        return out


def _read(path: str) -> List[List[str]]:
    raw = b""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return []
    for enc in ("cp1251", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return []
    return [ln.split("\t") for ln in text.splitlines() if ln.strip()]


def asked_from_job(job_path: str) -> Dict[str, List[str]]:
    """Что задание просило построить: имена компонентов и посадок."""
    sym: List[str] = []
    fp: List[str] = []
    for f in _read(job_path):
        if f[0] == "COMP" and len(f) > 1:
            sym.append(f[1])
        elif f[0] == "FP" and len(f) > 1:
            fp.append(f[1])
    return {"sym": sym, "fp": fp}


def read(job_path: str, max_age: float = 0.0) -> Report:
    """
    Отчёт по заданию. `max_age` -- сколько секунд отчёт считается свежим
    (0 -- не проверять возраст).
    """
    r = Report(job=job_path, done_path=job_path + ".done")
    if not job_path or not os.path.isfile(job_path):
        r.error = "задание не найдено"
        return r
    asked = asked_from_job(job_path)
    r.asked_sym, r.asked_fp = asked["sym"], asked["fp"]

    if not os.path.isfile(r.done_path):
        return r
    try:
        r.age = max(0.0, time.time() - os.path.getmtime(r.done_path))
    except OSError:
        r.age = 0.0
    if max_age and r.age > max_age:
        return r        # отчёт от прошлой сборки -- не выдаём за свежий

    rows = _read(r.done_path)
    if not rows or rows[0][0] != "VER":
        r.error = "отчёт скрипта повреждён"
        return r
    for f in rows:
        tag = f[0]
        val = f[1] if len(f) > 1 else ""
        if tag == "SCH":
            r.made_sym.append(val)
        elif tag == "PCB":
            r.made_fp.append(val)
        elif tag == "SCHLIB":
            r.schlib = val
        elif tag == "PCBLIB":
            r.pcblib = val
        elif tag == "WARN":
            r.warn = int(val or 0)
        elif tag == "ERR":
            r.err = int(val or 0)
        elif tag == "JOB" and val and os.path.abspath(val) != \
                os.path.abspath(job_path):
            r.error = ("отчёт относится к другому заданию: "
                       + os.path.basename(val))
            return r
    r.fresh = True
    return r


def clear(job_path: str):
    """
    Убрать старый отчёт перед запуском.

    Иначе после неудачной сборки утилита показала бы прошлый успешный
    результат, и «не обновилось» опять осталось бы незамеченным.
    """
    p = (job_path or "") + ".done"
    if p and os.path.isfile(p):
        try:
            os.remove(p)
        except OSError:
            pass


def wait(job_path: str, timeout: float = 180.0, step: float = 0.5
         ) -> Optional[Report]:
    """Дождаться отчёта. None -- не дождались за отведённое время."""
    end = time.time() + timeout
    while time.time() < end:
        if os.path.isfile(job_path + ".done"):
            time.sleep(0.2)          # дать скрипту дописать файл
            return read(job_path)
        time.sleep(step)
    return None
