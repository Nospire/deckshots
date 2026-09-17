"""SQLite storage for the DeckShots server (WAL mode, safe for a few worker threads)."""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH: Path | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS shots(
    id TEXT PRIMARY KEY,
    device TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,              -- screenshot | clip | video
    lane TEXT NOT NULL DEFAULT 'fast', -- fast | slow (needs transcoding)
    appid TEXT,
    game TEXT,
    filename TEXT NOT NULL,
    taken_at TEXT,
    size INTEGER,
    status TEXT NOT NULL,            -- received | processing | failed | sent
    attempts INTEGER DEFAULT 0,
    next_try REAL DEFAULT 0,
    error TEXT,
    created REAL,
    sent_at REAL,
    message_ids TEXT,
    UNIQUE(device, sha256)
);
CREATE TABLE IF NOT EXISTS names(appid TEXT PRIMARY KEY, name TEXT, checked REAL);
CREATE TABLE IF NOT EXISTS device_state(
    device TEXT PRIMARY KEY,
    host TEXT,
    agent_version TEXT,
    last_seen REAL,
    queue_new INTEGER DEFAULT 0,
    queue_uploaded INTEGER DEFAULT 0,
    paused INTEGER DEFAULT 0
);
"""

MIGRATIONS = [
    ("shots", "lane", "ALTER TABLE shots ADD COLUMN lane TEXT NOT NULL DEFAULT 'fast'"),
]


def init(path: Path):
    global DB_PATH
    DB_PATH = path
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)
        for table, column, ddl in MIGRATIONS:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            if column not in cols:
                con.execute(ddl)
        con.execute("CREATE INDEX IF NOT EXISTS shots_status ON shots(status, lane, next_try)")
        # anything left in 'processing' after a crash goes back to the queue
        con.execute("UPDATE shots SET status='received' WHERE status='processing'")
        # rows from before lanes existed: recordings that need transcoding belong to the slow lane
        con.execute("UPDATE shots SET lane='slow' WHERE kind='video' AND status!='sent' AND lower(filename) NOT LIKE '%.mp4' "
                    "AND lower(filename) NOT LIKE '%.mov' AND lower(filename) NOT LIKE '%.m4v'")


@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ----------------------------------------------------------------------------- shots
def find_dup(device: str, sha: str):
    with connect() as con:
        return con.execute("SELECT id, status, filename FROM shots WHERE device=? AND sha256=?", (device, sha)).fetchone()


def delete_shot(shot_id: str):
    with connect() as con:
        con.execute("DELETE FROM shots WHERE id=?", (shot_id,))


def insert_shot(**f):
    with connect() as con:
        con.execute(
            "INSERT INTO shots(id,device,sha256,kind,lane,appid,game,filename,taken_at,size,status,created) "
            "VALUES(:id,:device,:sha256,:kind,:lane,:appid,:game,:filename,:taken_at,:size,'received',:created)", f)


def get_shot(shot_id: str, device: str | None = None):
    with connect() as con:
        if device:
            return con.execute("SELECT * FROM shots WHERE id=? AND device=?", (shot_id, device)).fetchone()
        return con.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()


def pending_candidates(lane: str, now: float, limit: int = 300):
    with connect() as con:
        return con.execute(
            "SELECT s.* FROM shots s LEFT JOIN device_state d ON d.device = s.device "
            "WHERE s.lane=? AND s.status IN ('received','failed') AND s.next_try <= ? AND COALESCE(d.paused,0)=0 "
            "ORDER BY s.created LIMIT ?", (lane, now, limit)).fetchall()


def group_pending(device: str, appid: str, kind: str, now: float, limit: int):
    with connect() as con:
        return con.execute(
            "SELECT * FROM shots WHERE device=? AND appid=? AND kind=? AND status IN ('received','failed') AND next_try <= ? "
            "ORDER BY created LIMIT ?", (device, appid, kind, now, limit)).fetchall()


def claim(ids: list[str]) -> bool:
    """Atomically move rows to 'processing'; False if another worker got (some of) them first.
    BEGIN IMMEDIATE makes check+update one critical section, so a losing worker never touches
    rows the winner already owns."""
    marks = ",".join("?" * len(ids))
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        free = con.execute(f"SELECT COUNT(*) FROM shots WHERE id IN ({marks}) AND status IN ('received','failed')", ids).fetchone()[0]
        if free != len(ids):
            con.execute("ROLLBACK")
            return False
        con.execute(f"UPDATE shots SET status='processing' WHERE id IN ({marks})", ids)
        return True


def mark_sent(ids: list[str], message_ids: list[int]):
    with connect() as con:
        for i in ids:
            con.execute("UPDATE shots SET status='sent', sent_at=?, message_ids=?, error=NULL WHERE id=?",
                        (time.time(), ",".join(map(str, message_ids)), i))


def mark_failed(ids: list[str], error: str, retry_max: int):
    with connect() as con:
        for i in ids:
            row = con.execute("SELECT attempts FROM shots WHERE id=?", (i,)).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            delay = min(retry_max, 15 * 2 ** min(attempts, 8))
            con.execute("UPDATE shots SET status='failed', attempts=?, next_try=?, error=? WHERE id=?",
                        (attempts, time.time() + delay, error[:500], i))
    return delay


def release(ids: list[str]):
    with connect() as con:
        marks = ",".join("?" * len(ids))
        con.execute(f"UPDATE shots SET status='received' WHERE id IN ({marks}) AND status='processing'", ids)


# ----------------------------------------------------------------------------- names
def get_name(appid: str):
    with connect() as con:
        return con.execute("SELECT name, checked FROM names WHERE appid=?", (appid,)).fetchone()


def put_name(appid: str, name: str):
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO names(appid,name,checked) VALUES(?,?,?)", (appid, name, time.time()))


# ----------------------------------------------------------------------------- devices
def touch_device(device: str, host: str = "", version: str = "", q_new: int | None = None, q_up: int | None = None):
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO device_state(device) VALUES(?)", (device,))
        con.execute("UPDATE device_state SET host=COALESCE(NULLIF(?,''),host), agent_version=COALESCE(NULLIF(?,''),agent_version), "
                    "last_seen=?, queue_new=COALESCE(?,queue_new), queue_uploaded=COALESCE(?,queue_uploaded) WHERE device=?",
                    (host, version, time.time(), q_new, q_up, device))


def set_paused(device: str, paused: bool):
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO device_state(device) VALUES(?)", (device,))
        con.execute("UPDATE device_state SET paused=? WHERE device=?", (1 if paused else 0, device))


def device_state(device: str):
    with connect() as con:
        return con.execute("SELECT * FROM device_state WHERE device=?", (device,)).fetchone()


def pending_count(device: str | None = None) -> int:
    with connect() as con:
        if device:
            return con.execute("SELECT COUNT(*) FROM shots WHERE device=? AND status!='sent'", (device,)).fetchone()[0]
        return con.execute("SELECT COUNT(*) FROM shots WHERE status!='sent'").fetchone()[0]


def stats(device: str) -> dict:
    now = time.time()
    day, week = now - 86400, now - 7 * 86400
    with connect() as con:
        q = lambda sql, *a: con.execute(sql, a).fetchone()[0]  # noqa: E731
        out = {
            "total": q("SELECT COUNT(*) FROM shots WHERE device=? AND status='sent'", device),
            "today": q("SELECT COUNT(*) FROM shots WHERE device=? AND status='sent' AND sent_at>=?", device, day),
            "week": q("SELECT COUNT(*) FROM shots WHERE device=? AND status='sent' AND sent_at>=?", device, week),
            "bytes": q("SELECT COALESCE(SUM(size),0) FROM shots WHERE device=? AND status='sent'", device),
            "pending": q("SELECT COUNT(*) FROM shots WHERE device=? AND status!='sent'", device),
            "failed": q("SELECT COUNT(*) FROM shots WHERE device=? AND status='failed'", device),
            "by_kind": {r[0]: r[1] for r in con.execute(
                "SELECT kind, COUNT(*) FROM shots WHERE device=? AND status='sent' GROUP BY kind", (device,))},
            "top_games": con.execute(
                "SELECT COALESCE(NULLIF(s.game,''), n.name, 'appid ' || s.appid) AS g, COUNT(*) AS c "
                "FROM shots s LEFT JOIN names n ON n.appid = s.appid "
                "WHERE s.device=? AND s.status='sent' GROUP BY g ORDER BY c DESC LIMIT 5", (device,)).fetchall(),
            "last_sent": q("SELECT MAX(sent_at) FROM shots WHERE device=? AND status='sent'", device),
        }
    return out
