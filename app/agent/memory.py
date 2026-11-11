"""In-process session memory (per-session recent turns, bound to one user)."""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field


class SessionForbidden(Exception):
    pass


@dataclass
class Turn:
    question: str
    standalone: str
    intent: str
    sql: str
    entities: list[dict] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows_preview: list[list] = field(default_factory=list)
    row_count: int = 0
    resolved_dates: dict = field(default_factory=dict)


@dataclass
class Session:
    session_id: str
    user_id: str
    turns: deque = field(default_factory=lambda: deque(maxlen=5))
    touched: float = 0.0


class SessionStore:
    def __init__(self, ttl_s: int = 7200, max_sessions: int = 200, clock=time.monotonic):
        self.ttl, self.max, self.clock = ttl_s, max_sessions, clock
        self._s: OrderedDict[str, Session] = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self):
        now = self.clock()
        for k in [k for k, s in self._s.items() if now - s.touched > self.ttl]:
            del self._s[k]
        while len(self._s) > self.max:
            self._s.popitem(last=False)

    def open(self, session_id: str | None, user_id: str) -> str:
        with self._lock:
            sid = session_id or uuid.uuid4().hex
            s = self._s.get(sid)
            if s is not None and self.clock() - s.touched > self.ttl:
                del self._s[sid]
                s = None
            if s is not None and s.user_id != user_id:
                raise SessionForbidden(sid)
            if s is None:
                s = Session(sid, user_id)
                self._s[sid] = s
            s.touched = self.clock()
            self._s.move_to_end(sid)
            self._prune()
            return sid

    def recent(self, session_id: str, n: int = 3) -> list[Turn]:
        with self._lock:
            s = self._s.get(session_id)
            return list(s.turns)[-n:] if s else []

    def add(self, session_id: str, turn: Turn) -> None:
        turn.sql = turn.sql[:1500]
        turn.rows_preview = [[(str(c)[:200] if isinstance(c, str) else c) for c in r] for r in turn.rows_preview[:5]]
        with self._lock:
            s = self._s.get(session_id)
            if s is None:
                return
            s.turns.append(turn)
            s.touched = self.clock()
            self._s.move_to_end(session_id)
            self._prune()

    def __len__(self):
        return len(self._s)
