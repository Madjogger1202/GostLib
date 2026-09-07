"""
Каталог компонентов на SQLite.

Хранит полный Component в виде JSON плюс распакованные поля для поиска,
сортировки и фильтрации, а параметры -- отдельной таблицей, чтобы можно
было фильтровать по «ток > 1 А» или «интерфейс содержит SPI».
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .ir import Component

SCHEMA = """
CREATE TABLE IF NOT EXISTS components (
    uid          TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    ctype        TEXT NOT NULL DEFAULT 'other',
    designator   TEXT,
    manufacturer TEXT,
    mpn          TEXT,
    description  TEXT,
    value        TEXT,
    package      TEXT,
    pincount     INTEGER DEFAULT 0,
    footprints   TEXT,
    source       TEXT,
    source_ref   TEXT,
    tags         TEXT,
    notes        TEXT,
    in_library   INTEGER DEFAULT 0,
    created      REAL,
    updated      REAL,
    data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_comp_name ON components(name);
CREATE INDEX IF NOT EXISTS ix_comp_type ON components(ctype);
CREATE INDEX IF NOT EXISTS ix_comp_mpn  ON components(mpn);

CREATE TABLE IF NOT EXISTS params (
    uid   TEXT NOT NULL,
    key   TEXT NOT NULL,
    value TEXT,
    num   REAL,
    PRIMARY KEY (uid, key)
);
CREATE INDEX IF NOT EXISTS ix_par_key ON params(key);

CREATE TABLE IF NOT EXISTS history (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    REAL,
    uid   TEXT,
    what  TEXT
);

-- Проекты Altium: у каждого своя библиотека, собранная из общего
-- каталога. Компонент живёт в каталоге один раз, а в проекты входит
-- ссылкой -- поэтому правка компонента доезжает во все проекты сразу.
CREATE TABLE IF NOT EXISTS projects (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    path     TEXT NOT NULL DEFAULT '',   -- папка проекта Altium
    lib_dir  TEXT NOT NULL DEFAULT '',   -- куда класть .SchLib/.PcbLib
    lib_name TEXT NOT NULL DEFAULT '',   -- имя библиотеки проекта
    note     TEXT NOT NULL DEFAULT '',
    created  REAL
);

CREATE TABLE IF NOT EXISTS project_items (
    project_id INTEGER NOT NULL,
    uid        TEXT NOT NULL,
    added      REAL,
    PRIMARY KEY (project_id, uid)
);
CREATE INDEX IF NOT EXISTS ix_pi_uid ON project_items(uid);

-- индекс установленных библиотек KiCad, чтобы поиск был мгновенным
CREATE TABLE IF NOT EXISTS kicad_index (
    kind     TEXT NOT NULL,          -- 'sym' | 'fp'
    nickname TEXT NOT NULL,
    path     TEXT NOT NULL,
    name     TEXT NOT NULL,
    lname    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ki_name ON kicad_index(kind, lname);
"""

# Десятичные приставки, латинские и русские. Порядок разбора -- от длинных
# к коротким, чтобы «мк» не спутать с «м».
_SI = {
    "p": 1e-12, "п": 1e-12,
    "n": 1e-9,  "н": 1e-9,
    "u": 1e-6,  "µ": 1e-6, "мк": 1e-6,
    "m": 1e-3,  "м": 1e-3,
    "k": 1e3,   "к": 1e3,
    "M": 1e6,   "М": 1e6, "Мег": 1e6,
    "G": 1e9,   "Г": 1e9,
    "T": 1e12,  "Т": 1e12,
    "R": 1.0,   "Ом": 1.0, "Е": 1.0,
}


def to_number(text: str) -> Optional[float]:
    """'4.7к' -> 4700.0, '100 нФ' -> 1e-7, '3.3 В' -> 3.3."""
    if text is None:
        return None
    s = str(text).strip().replace(",", ".")
    m = re.match(r"^([-+]?\d*\.?\d+)\s*([^\s\d]{0,3})", s)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    suf = m.group(2) or ""
    for k in sorted(_SI, key=len, reverse=True):
        if suf.startswith(k):
            return v * _SI[k]
    return v


class Catalog:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        # check_same_thread=False: импорт идёт в фоновом потоке GUI,
        # доступ сериализуется собственным замком
        self.conn = sqlite3.connect(path, check_same_thread=False,
                                    timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------- запись ----
    def upsert(self, c: Component) -> str:
        now = time.time()
        row = self.get_row(c.uid)
        created = row["created"] if row else now
        pkg = c.params.get("Package", "") or (
            c.footprints[0].name if c.footprints else "")
        self.conn.execute(
            """INSERT INTO components
               (uid,name,ctype,designator,manufacturer,mpn,description,value,
                package,pincount,footprints,source,source_ref,tags,notes,
                in_library,created,updated,data)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       COALESCE((SELECT in_library FROM components WHERE uid=?),0),
                       ?,?,?)
               ON CONFLICT(uid) DO UPDATE SET
                 name=excluded.name, ctype=excluded.ctype,
                 designator=excluded.designator,
                 manufacturer=excluded.manufacturer, mpn=excluded.mpn,
                 description=excluded.description, value=excluded.value,
                 package=excluded.package, pincount=excluded.pincount,
                 footprints=excluded.footprints, source=excluded.source,
                 source_ref=excluded.source_ref, tags=excluded.tags,
                 notes=excluded.notes, updated=excluded.updated,
                 data=excluded.data""",
            (c.uid, c.name, c.ctype, c.designator, c.manufacturer, c.mpn,
             c.description, c.value, pkg, len(c.symbol.pins or c.raw_pins),
             ";".join(f.name for f in c.footprints), c.source, c.source_ref,
             ",".join(c.tags), c.notes, c.uid, created, now, c.to_json()))
        self.conn.execute("DELETE FROM params WHERE uid=?", (c.uid,))
        self.conn.executemany(
            "INSERT OR REPLACE INTO params(uid,key,value,num) VALUES (?,?,?,?)",
            [(c.uid, k, str(v), to_number(v)) for k, v in c.params.items()])
        self.conn.commit()
        return c.uid

    def set_in_library(self, uids: Iterable[str], flag: bool = True):
        self.conn.executemany("UPDATE components SET in_library=? WHERE uid=?",
                              [(1 if flag else 0, u) for u in uids])
        self.conn.commit()

    def delete(self, uids: Iterable[str]):
        uids = list(uids)
        self.conn.executemany("DELETE FROM components WHERE uid=?",
                              [(u,) for u in uids])
        self.conn.executemany("DELETE FROM params WHERE uid=?",
                              [(u,) for u in uids])
        self.conn.commit()

    def clear_components(self) -> int:
        """Убрать из каталога все компоненты. Индекс KiCad не трогаем."""
        n = self.conn.execute("SELECT COUNT(*) n FROM components"
                              ).fetchone()["n"]
        self.conn.execute("DELETE FROM params")
        self.conn.execute("DELETE FROM components")
        self.conn.commit()
        try:
            self.conn.execute("VACUUM")
        except Exception:
            pass          # база просто останется большего размера
        return int(n)

    # ---------------------------------------------------------- проекты ----
    def projects(self) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM project_items i "
            "             WHERE i.project_id = p.id) AS n "
            "FROM projects p ORDER BY p.name"))

    def project(self, pid: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM projects WHERE id=?",
                                 (pid,)).fetchone()

    def project_by_name(self, name: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM projects WHERE name=?",
                                 (name,)).fetchone()

    def add_project(self, name: str, path: str = "", lib_dir: str = "",
                    lib_name: str = "", note: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO projects(name,path,lib_dir,lib_name,note,created) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET path=excluded.path, "
            "  lib_dir=excluded.lib_dir, lib_name=excluded.lib_name, "
            "  note=excluded.note",
            (name, path, lib_dir, lib_name, note, time.time()))
        self.conn.commit()
        row = self.project_by_name(name)
        return int(row["id"]) if row else int(cur.lastrowid)

    def update_project(self, pid: int, **kw):
        allowed = {"name", "path", "lib_dir", "lib_name", "note"}
        sets = {k: v for k, v in kw.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k}=?" for k in sets)
        self.conn.execute(f"UPDATE projects SET {cols} WHERE id=?",
                          list(sets.values()) + [pid])
        self.conn.commit()

    def delete_project(self, pid: int):
        """Убрать проект. Компоненты остаются в общем каталоге."""
        self.conn.execute("DELETE FROM project_items WHERE project_id=?",
                          (pid,))
        self.conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        self.conn.commit()

    def project_add(self, pid: int, uids: Iterable[str]) -> int:
        rows = [(pid, u, time.time()) for u in uids]
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT OR IGNORE INTO project_items(project_id,uid,added) "
            "VALUES (?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def project_remove(self, pid: int, uids: Iterable[str]) -> int:
        rows = [(pid, u) for u in uids]
        if not rows:
            return 0
        self.conn.executemany(
            "DELETE FROM project_items WHERE project_id=? AND uid=?", rows)
        self.conn.commit()
        return len(rows)

    def project_uids(self, pid: int) -> List[str]:
        return [r["uid"] for r in self.conn.execute(
            "SELECT i.uid FROM project_items i "
            "JOIN components c ON c.uid = i.uid "
            "WHERE i.project_id=? ORDER BY c.name", (pid,))]

    def projects_of(self, uid: str) -> List[str]:
        return [r["name"] for r in self.conn.execute(
            "SELECT p.name FROM projects p JOIN project_items i "
            "ON i.project_id = p.id WHERE i.uid=? ORDER BY p.name", (uid,))]

    def log(self, uid: str, what: str):
        self.conn.execute("INSERT INTO history(ts,uid,what) VALUES (?,?,?)",
                          (time.time(), uid, what))
        self.conn.commit()

    # ------------------------------------------------------------- чтение ----
    def get_row(self, uid: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM components WHERE uid=?", (uid,))
        return cur.fetchone()

    def get(self, uid: str) -> Optional[Component]:
        r = self.get_row(uid)
        return Component.from_json(r["data"]) if r else None

    def params_of(self, uid: str) -> Dict[str, str]:
        cur = self.conn.execute("SELECT key,value FROM params WHERE uid=?", (uid,))
        return {r["key"]: r["value"] for r in cur.fetchall()}

    def types(self) -> List[Tuple[str, int]]:
        cur = self.conn.execute(
            "SELECT ctype, COUNT(*) n FROM components GROUP BY ctype ORDER BY 1")
        return [(r["ctype"], r["n"]) for r in cur.fetchall()]

    def param_keys(self) -> List[str]:
        cur = self.conn.execute(
            "SELECT DISTINCT key FROM params ORDER BY key")
        return [r["key"] for r in cur.fetchall()]

    def search(self, text: str = "", ctype: str = "", only_lib: bool = False,
               param_filters: Optional[List[Tuple[str, str, str]]] = None,
               order: str = "updated DESC", limit: int = 5000
               ) -> List[sqlite3.Row]:
        """
        text -- подстрока по имени/парт-номеру/описанию/производителю/корпусу;
        param_filters -- список (ключ, оператор, значение),
            оператор: '=', '~', '>', '<', '>=', '<='
        """
        sql = ["SELECT c.* FROM components c"]
        where: List[str] = []
        args: List[Any] = []
        joins = 0
        for key, op, val in (param_filters or []):
            joins += 1
            a = f"p{joins}"
            sql.append(f"JOIN params {a} ON {a}.uid=c.uid AND {a}.key=?")
            args.append(key)
            if op == "~":
                where.append(f"{a}.value LIKE ?")
                args.append(f"%{val}%")
            elif op == "=":
                where.append(f"{a}.value = ?")
                args.append(val)
            else:
                num = to_number(val)
                where.append(f"{a}.num {op} ?")
                args.append(num if num is not None else 0.0)
        if text:
            like = f"%{text}%"
            where.append("(c.name LIKE ? OR c.mpn LIKE ? OR c.description LIKE ? "
                         "OR c.manufacturer LIKE ? OR c.package LIKE ? "
                         "OR c.tags LIKE ? OR c.value LIKE ?)")
            args += [like] * 7
        if ctype:
            where.append("c.ctype = ?")
            args.append(ctype)
        if only_lib:
            where.append("c.in_library = 1")
        if where:
            sql.append("WHERE " + " AND ".join(where))
        safe_order = order if re.fullmatch(
            r"[A-Za-z_]+( (ASC|DESC))?", order or "") else "updated DESC"
        sql.append(f"ORDER BY c.{safe_order} LIMIT {int(limit)}")
        return self.conn.execute(" ".join(sql), args).fetchall()

    def all_uids(self, only_lib: bool = False) -> List[str]:
        q = "SELECT uid FROM components"
        if only_lib:
            q += " WHERE in_library=1"
        return [r["uid"] for r in self.conn.execute(q).fetchall()]

    def stats(self) -> Dict[str, int]:
        c = self.conn.execute(
            "SELECT COUNT(*) a, SUM(in_library) b FROM components").fetchone()
        return {"total": c["a"] or 0, "in_library": c["b"] or 0}

    # --------------------------------------------------- индекс KiCad -------
    def set_kicad_index(self, rows: Iterable[Tuple[str, str, str, str]]):
        """rows: (kind, nickname, path, name)."""
        rows = list(rows)
        self.conn.execute("DELETE FROM kicad_index")
        self.conn.executemany(
            "INSERT INTO kicad_index(kind,nickname,path,name,lname) "
            "VALUES (?,?,?,?,?)",
            [(k, n, p, nm, nm.lower()) for k, n, p, nm in rows])
        self.conn.commit()

    def kicad_clear(self):
        self.conn.execute("DELETE FROM kicad_index")
        self.conn.commit()

    def kicad_stats(self) -> Dict[str, int]:
        r = self.conn.execute(
            "SELECT SUM(kind='sym') s, SUM(kind='fp') f, "
            "COUNT(DISTINCT path) p FROM kicad_index").fetchone()
        return {"symbols": r["s"] or 0, "footprints": r["f"] or 0,
                "files": r["p"] or 0}

    def kicad_search(self, kind: str, query: str, limit: int = 400
                     ) -> List[sqlite3.Row]:
        """
        query: 'Device:R', 'R', 'RP2040' -- точные совпадения идут первыми.
        """
        lib, _, name = (query or "").rpartition(":")
        name = (name or query or "").strip().lower()
        sql = ["SELECT * FROM kicad_index WHERE kind=?"]
        args: List[Any] = [kind]
        if lib:
            sql.append("AND LOWER(nickname)=?")
            args.append(lib.strip().lower())
        if name:
            sql.append("AND lname LIKE ?")
            args.append(f"%{name}%")
        sql.append("ORDER BY (lname=?) DESC, LENGTH(name), nickname, name")
        args.append(name)
        sql.append(f"LIMIT {int(limit)}")
        return self.conn.execute(" ".join(sql), args).fetchall()


def _synchronized(cls):
    """Один соединение SQLite на всё приложение -- доступ под общим замком."""
    import functools
    names = ("upsert", "set_in_library", "delete", "log", "get_row", "get",
             "params_of", "types", "param_keys", "search", "all_uids",
             "stats", "set_kicad_index", "kicad_clear", "kicad_stats",
             "kicad_search")

    def wrap(f):
        @functools.wraps(f)
        def inner(self, *a, **k):
            with self.lock:
                return f(self, *a, **k)
        return inner

    for nm in names:
        fn = getattr(cls, nm, None)
        if callable(fn):
            setattr(cls, nm, wrap(fn))
    return cls


Catalog = _synchronized(Catalog)
