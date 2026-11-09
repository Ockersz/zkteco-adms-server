"""
Database storage layer for ZKTeco ADMS Server.
Supports SQLite (zero-setup default) and MySQL.
"""

import sqlite3
import logging
from typing import Optional, List, Dict, Tuple
from datetime import datetime

import config

logger = logging.getLogger("adms.storage")


def get_sqlite_connection():
    """Get SQLite database connection with row factory."""
    conn = sqlite3.connect(config.DB_SQLITE_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def get_mysql_connection():
    """Get MySQL database connection."""
    try:
        import mysql.connector
        return mysql.connector.connect(
            host=config.DB_HOST,
            port=config.DB_PORT,
            user=config.DB_USER,
            password=config.DB_PASS,
            database=config.DB_NAME,
            autocommit=False,
        )
    except ImportError:
        logger.error("mysql-connector-python is not installed. Install via: pip install mysql-connector-python")
        raise


def init_db():
    """Initialize database tables and indexes."""
    if config.DB_ENGINE == "sqlite":
        with get_sqlite_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS attendance_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    serial_number TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    status INTEGER DEFAULT 0,
                    verify_type INTEGER DEFAULT 1,
                    work_code INTEGER DEFAULT 0,
                    raw_line TEXT,
                    synced INTEGER DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(serial_number, user_id, timestamp)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_att_user_time 
                ON attendance_records (user_id, timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_att_synced 
                ON attendance_records (synced)
            """)
            conn.commit()
            logger.info("SQLite database initialized at: %s", config.DB_SQLITE_PATH)
    else:
        conn = get_mysql_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS attendance_records (
                    id BIGINT NOT NULL AUTO_INCREMENT,
                    serial_number VARCHAR(50) NOT NULL,
                    user_id VARCHAR(50) NOT NULL,
                    timestamp DATETIME NOT NULL,
                    status INT DEFAULT 0,
                    verify_type INT DEFAULT 1,
                    work_code INT DEFAULT 0,
                    raw_line VARCHAR(255) NULL,
                    synced CHAR(1) DEFAULT 'N',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_device_user_time (serial_number, user_id, timestamp),
                    KEY idx_user_time (user_id, timestamp),
                    KEY idx_synced (synced)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
            """)
            conn.commit()
            logger.info("MySQL database initialized successfully on %s:%s", config.DB_HOST, config.DB_PORT)
        finally:
            cursor.close()
            conn.close()


def save_punches(records: List[Dict], serial_number: str) -> Tuple[int, int]:
    """
    Insert attendance punches into database.
    Prevents duplicate entries via unique constraints.
    Returns: (inserted_count, duplicate_count)
    """
    if not records:
        return (0, 0)

    inserted = 0
    duplicates = 0

    if config.DB_ENGINE == "sqlite":
        with get_sqlite_connection() as conn:
            cursor = conn.cursor()
            for r in records:
                try:
                    cursor.execute("""
                        INSERT INTO attendance_records 
                        (serial_number, user_id, timestamp, status, verify_type, work_code, raw_line, synced)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                    """, (
                        serial_number,
                        str(r["user_id"]),
                        r["timestamp"],
                        r.get("status", 0),
                        r.get("verify_type", 1),
                        r.get("work_code", 0),
                        r.get("raw_line", ""),
                    ))
                    inserted += 1
                except sqlite3.IntegrityError:
                    duplicates += 1
            conn.commit()
    else:
        conn = get_mysql_connection()
        try:
            cursor = conn.cursor()
            for r in records:
                try:
                    cursor.execute("""
                        INSERT INTO attendance_records 
                        (serial_number, user_id, timestamp, status, verify_type, work_code, raw_line, synced)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'N')
                    """, (
                        serial_number,
                        str(r["user_id"]),
                        r["timestamp"],
                        r.get("status", 0),
                        r.get("verify_type", 1),
                        r.get("work_code", 0),
                        r.get("raw_line", ""),
                    ))
                    inserted += 1
                except Exception as ex:
                    # Duplicate entry check in MySQL (error code 1062)
                    if getattr(ex, "errno", None) == 1062:
                        duplicates += 1
                    else:
                        logger.error("Error inserting punch into MySQL: %s", ex)
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    return (inserted, duplicates)


def get_unsynced_punches(limit: int = 100) -> List[Dict]:
    """Fetch unsynced punches for webhook forwarding."""
    results = []
    if config.DB_ENGINE == "sqlite":
        with get_sqlite_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, serial_number, user_id, timestamp, status, verify_type, work_code
                FROM attendance_records
                WHERE synced = 0
                ORDER BY timestamp ASC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            for row in rows:
                results.append(dict(row))
    else:
        conn = get_mysql_connection()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT id, serial_number, user_id, timestamp, status, verify_type, work_code
                FROM attendance_records
                WHERE synced = 'N'
                ORDER BY timestamp ASC
                LIMIT %s
            """, (limit,))
            rows = cursor.fetchall()
            for r in rows:
                if isinstance(r["timestamp"], datetime):
                    r["timestamp"] = r["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
                results.append(r)
        finally:
            cursor.close()
            conn.close()

    return results


def mark_punches_synced(ids: List[int]):
    """Mark records as successfully pushed/forwarded."""
    if not ids:
        return
    if config.DB_ENGINE == "sqlite":
        with get_sqlite_connection() as conn:
            cursor = conn.cursor()
            placeholders = ",".join(["?"] * len(ids))
            cursor.execute(f"UPDATE attendance_records SET synced = 1 WHERE id IN ({placeholders})", ids)
            conn.commit()
    else:
        conn = get_mysql_connection()
        try:
            cursor = conn.cursor()
            placeholders = ",".join(["%s"] * len(ids))
            cursor.execute(f"UPDATE attendance_records SET synced = 'Y' WHERE id IN ({placeholders})", ids)
            conn.commit()
        finally:
            cursor.close()
            conn.close()

