
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[3] / "nexus.db"


def get_connection():
    connection = sqlite3.connect(DB_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    with get_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                target TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)
