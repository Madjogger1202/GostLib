"""
Отмена действий над каталогом.

Правки компонентов и состава проектов идут пачками и вслепую: выделил
двадцать строк, назначил тип, понял, что не тот. Возвращать руками
двадцать компонентов -- работа на полчаса.

Способ выбран самый простой из надёжных: перед каждым изменением
запоминается ПОЛНОЕ прежнее состояние затронутых компонентов (их JSON) и
состав затронутых проектов. Откат просто кладёт это состояние обратно.
Дифф был бы экономнее, но на каталоге в тысячи компонентов и такой
снимок весит килобайты, зато откатывается что угодно, включая правки,
о которых журнал ничего не знает.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# Больше и не надо: глубже двадцати шагов никто не отматывает, а память
# держать незачем.
MAX_STEPS = 20


@dataclass
class Step:
    title: str
    when: float = field(default_factory=time.time)
    # uid -> JSON компонента; None означает «компонента не было»
    comps: Dict[str, Optional[str]] = field(default_factory=dict)
    # id проекта -> список uid, входивших в него
    projects: Dict[int, List[str]] = field(default_factory=dict)
    # активный проект до действия
    active: Optional[int] = None

    def __bool__(self) -> bool:
        return bool(self.comps or self.projects or self.active is not None)


class UndoStack:
    """
    Журнал отмены поверх каталога.

    Пользуются им так::

        with undo.step("Назначить тип"):
            ...правки...

    Всё, что тронуто внутри, запоминается до правки -- при условии, что
    перед изменением вызван `touch(uid)`.
    """

    def __init__(self, db, log: Optional[Callable[[str], None]] = None):
        self.db = db
        self.log = log or (lambda s: None)
        self._done: List[Step] = []
        self._undone: List[Step] = []
        self._cur: Optional[Step] = None
        self._depth = 0

    # ------------------------------------------------------------ запись ---
    def step(self, title: str):
        return _StepCtx(self, title)

    def begin(self, title: str):
        self._depth += 1
        if self._depth == 1:
            self._cur = Step(title=title)
            self._cur.active = self._active()

    def end(self):
        self._depth = max(0, self._depth - 1)
        if self._depth or self._cur is None:
            return
        if self._cur:
            self._done.append(self._cur)
            del self._done[:-MAX_STEPS]
            self._undone.clear()
        self._cur = None

    def touch(self, uid: str):
        """Запомнить компонент до изменения. Повторные вызовы игнорируются."""
        if self._cur is None or uid in self._cur.comps:
            return
        row = self.db.get_row(uid)
        self._cur.comps[uid] = row["data"] if row else None

    def touch_many(self, uids):
        for u in uids:
            self.touch(u)

    def touch_project(self, pid: int):
        if self._cur is None or pid in self._cur.projects:
            return
        try:
            self._cur.projects[int(pid)] = list(self.db.project_uids(int(pid)))
        except Exception:
            pass

    # ------------------------------------------------------------- откат ---
    def can_undo(self) -> bool:
        return bool(self._done)

    def can_redo(self) -> bool:
        return bool(self._undone)

    def undo_title(self) -> str:
        return self._done[-1].title if self._done else ""

    def redo_title(self) -> str:
        return self._undone[-1].title if self._undone else ""

    def undo(self) -> str:
        return self._apply(self._done, self._undone, "Отменено")

    def redo(self) -> str:
        return self._apply(self._undone, self._done, "Возвращено")

    def clear(self):
        self._done.clear()
        self._undone.clear()

    # ---------------------------------------------------------- внутреннее -
    def _active(self) -> Optional[int]:
        cfg = getattr(self.db, "_cfg", None)
        return int(getattr(cfg, "active_project", 0)) if cfg else None

    def _apply(self, src: List[Step], dst: List[Step], word: str) -> str:
        if not src:
            return ""
        step = src.pop()
        # снимаем встречное состояние, чтобы шаг можно было повторить
        back = Step(title=step.title)
        from .ir import Component
        for uid in step.comps:
            row = self.db.get_row(uid)
            back.comps[uid] = row["data"] if row else None
        for pid in step.projects:
            try:
                back.projects[pid] = list(self.db.project_uids(pid))
            except Exception:
                back.projects[pid] = []

        for uid, data in step.comps.items():
            if data is None:
                self.db.delete([uid])
            else:
                self.db.upsert(Component.from_dict(json.loads(data)))
        for pid, uids in step.projects.items():
            cur = set(self.db.project_uids(pid))
            want = set(uids)
            if cur - want:
                self.db.project_remove(pid, cur - want)
            if want - cur:
                self.db.project_add(pid, want - cur)

        dst.append(back)
        del dst[:-MAX_STEPS]
        self.log(f"{word}: {step.title}")
        return step.title


class _StepCtx:
    def __init__(self, stack: UndoStack, title: str):
        self.stack = stack
        self.title = title

    def __enter__(self):
        self.stack.begin(self.title)
        return self.stack

    def __exit__(self, *exc):
        self.stack.end()
        return False
