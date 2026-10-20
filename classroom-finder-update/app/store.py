"""SQLite storage and the occupancy rules.

Only person *counts* are ever stored. No camera frames reach the server.

Status rules (computed when a room is read, not stored):
  unknown   - no report yet, or the room's agent has gone quiet (stale)
  occupied  - more than `occupied_above` students counted right now (default: more than 7)
  free      - reports are fresh and `occupied_above` students or fewer are counted,
              including 0. The count is still shown, so "3 students spotted" is free.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    building       TEXT NOT NULL,
    floor          INTEGER NOT NULL,
    capacity       INTEGER NOT NULL,
    count          INTEGER,
    last_update    REAL,
    last_person_at REAL
);
"""

DEFAULT_STALE_AFTER = 90   # seconds without a report before a room is "unknown"
DEFAULT_OCCUPIED_ABOVE = 7   # more students than this and the room is "occupied"
MAX_COUNT = 1000


class Store:
    def __init__(self, db_path, rooms_file=None,
                 stale_after=DEFAULT_STALE_AFTER, occupied_above=DEFAULT_OCCUPIED_ABOVE):
        self.db_path = str(db_path)
        self.stale_after = stale_after
        self.occupied_above = occupied_above
        self._lock = threading.Lock()
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        if rooms_file:
            self.seed_rooms(rooms_file)

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return _Transaction(conn)

    # ---- rooms -----------------------------------------------------------
    def seed_rooms(self, rooms_file):
        """Make the rooms table match rooms.json.

        Rooms in the file are added or updated (keeping their live data). Rooms that
        are no longer in the file are removed. "name" and "capacity" are optional.
        """
        rooms = json.loads(Path(rooms_file).read_text(encoding="utf-8"))
        ids = [str(r["id"]) for r in rooms]
        with self._lock, self._conn() as conn:
            for r in rooms:
                rid = str(r["id"])
                conn.execute(
                    "INSERT INTO rooms (id, name, building, floor, capacity) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                    "building=excluded.building, floor=excluded.floor, "
                    "capacity=excluded.capacity",
                    (rid, r.get("name") or f"Room {rid}", r.get("building") or "Main building",
                     int(r["floor"]), int(r.get("capacity") or 0)),
                )
            if ids:
                marks = ",".join("?" * len(ids))
                conn.execute(f"DELETE FROM rooms WHERE id NOT IN ({marks})", ids)

    def list_rooms(self, building=None, floor=None, status=None, now=None):
        now = time.time() if now is None else now
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM rooms ORDER BY building, floor, id").fetchall()
        rooms = [self._to_dict(r, now) for r in rows]
        if building:
            rooms = [r for r in rooms if r["building"] == building]
        if floor is not None:
            rooms = [r for r in rooms if r["floor"] == floor]
        if status:
            rooms = [r for r in rooms if r["status"] == status]
        return rooms

    def get_room(self, room_id, now=None):
        now = time.time() if now is None else now
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        return self._to_dict(row, now) if row else None

    def summary(self, now=None):
        rooms = self.list_rooms(now=now)
        out = {"free": 0, "occupied": 0, "unknown": 0, "total": len(rooms)}
        for r in rooms:
            out[r["status"]] += 1
        return out

    # ---- occupancy -------------------------------------------------------
    def record_occupancy(self, room_id, count, now=None):
        """Store a people count for a room. Returns False if the room does not exist."""
        now = time.time() if now is None else now
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE rooms SET count = ?, last_update = ?, "
                "last_person_at = CASE WHEN ? > 0 THEN ? ELSE last_person_at END "
                "WHERE id = ?",
                (count, now, count, now, room_id),
            )
            return cur.rowcount > 0

    # ---- helpers ---------------------------------------------------------
    def status_for(self, row, now):
        if row["last_update"] is None or now - row["last_update"] > self.stale_after:
            return "unknown"
        if row["count"] is not None and row["count"] > self.occupied_above:
            return "occupied"
        return "free"

    def _to_dict(self, row, now):
        status = self.status_for(row, now)
        free_for = None
        if status == "free" and row["last_person_at"] is not None:
            free_for = max(0, int(now - row["last_person_at"]))
        return {
            "id": row["id"],
            "name": row["name"],
            "building": row["building"],
            "floor": row["floor"],
            "capacity": row["capacity"],
            "count": row["count"],
            "status": status,
            "free_for_seconds": free_for,
            "seconds_since_update": (
                None if row["last_update"] is None else max(0, int(now - row["last_update"]))
            ),
        }


class _Transaction:
    """Context manager: commit on success, roll back on error, always close."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
        return False
