# db.py
import sqlite3
from datetime import datetime, timezone
from typing import List, Dict, Any

DB_PATH = "harmonics.db"


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            timeframe TEXT,
            pattern_type TEXT,
            direction TEXT,
            score REAL,
            x_time TEXT,
            a_time TEXT,
            b_time TEXT,
            c_time TEXT,
            d_time TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_pattern(symbol: str,
                 timeframe: str,
                 pattern_type: str,
                 direction: str,
                 score: float,
                 points: dict):
    conn = get_conn()
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    c.execute("""
        INSERT INTO patterns
        (symbol, timeframe, pattern_type, direction, score,
         x_time, a_time, b_time, c_time, d_time, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        symbol,
        timeframe,
        pattern_type,
        direction,
        score,
        points.get("x"),
        points.get("a"),
        points.get("b"),
        points.get("c"),
        points.get("d"),
        now
    ))
    conn.commit()
    conn.close()


def list_patterns(limit: int = 50,
                  symbol: str | None = None,
                  timeframe: str | None = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    c = conn.cursor()

    q = "SELECT id, symbol, timeframe, pattern_type, direction, score, d_time, created_at FROM patterns"
    params = []
    where = []

    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if timeframe:
        where.append("timeframe = ?")
        params.append(timeframe)

    if where:
        q += " WHERE " + " AND ".join(where)

    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = c.execute(q, params).fetchall()
    conn.close()

    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "symbol": r[1],
            "timeframe": r[2],
            "pattern_type": r[3],
            "direction": r[4],
            "score": r[5],
            "d_time": r[6],
            "created_at": r[7],
        })
    return out


def stats():
    conn = get_conn()
    c = conn.cursor()
    total = c.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
    by_symbol = c.execute("""
        SELECT symbol, COUNT(*) FROM patterns
        GROUP BY symbol
        ORDER BY COUNT(*) DESC
    """).fetchall()
    by_tf = c.execute("""
        SELECT timeframe, COUNT(*) FROM patterns
        GROUP BY timeframe
        ORDER BY COUNT(*) DESC
    """).fetchall()
    conn.close()
    return {
        "total": total,
        "by_symbol": by_symbol,
        "by_timeframe": by_tf,
    }
