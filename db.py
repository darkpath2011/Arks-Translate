"""SQLite helpers for Arks.

Single connection, threadsafe via check_same_thread=False.
Keep it small — no ORM. Just query() / execute() / executemany().
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DB_PATH = Path(__file__).parent / ".cache" / "arks.db"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _conn = _connect()
                _conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
                # Forward-compatible migration for databases created before page_json.
                try:
                    _conn.execute("ALTER TABLE pages ADD COLUMN page_json TEXT")
                except sqlite3.OperationalError:
                    pass
                for column, kind in (("part_of_speech", "TEXT"), ("pronunciation", "TEXT"), ("word_form", "TEXT"), ("memory_tip", "TEXT")):
                    try:
                        _conn.execute(f"ALTER TABLE words ADD COLUMN {column} {kind}")
                    except sqlite3.OperationalError:
                        pass
    return _conn


def now() -> int:
    return int(time.time())


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return list(get_conn().execute(sql, params))


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> None:
    get_conn().execute(sql, params)


def upsert_paper(paper_id: str, title: str, source_path: str) -> None:
    execute(
        "INSERT OR IGNORE INTO papers(id, title, source_path, created_at) VALUES (?,?,?,?)",
        (paper_id, title, source_path, now()),
    )


def save_page_blocks(paper_id: str, page_num: int, blocks: list[dict], page_data: dict | None = None) -> None:
    execute(
        "INSERT OR REPLACE INTO pages(paper_id, page_num, blocks_json, page_json) VALUES (?,?,?,?)",
        (paper_id, page_num, json.dumps(blocks, ensure_ascii=False), json.dumps(page_data or {}, ensure_ascii=False)),
    )


def load_page_blocks(paper_id: str, page_num: int) -> list[dict] | None:
    row = query_one(
        "SELECT blocks_json FROM pages WHERE paper_id=? AND page_num=?",
        (paper_id, page_num),
    )
    if row is None:
        return None
    return json.loads(row["blocks_json"])

def load_page(paper_id: str, page_num: int) -> dict | None:
    row = query_one("SELECT blocks_json, page_json FROM pages WHERE paper_id=? AND page_num=?", (paper_id, page_num))
    if row is None:
        return None
    data = json.loads(row["page_json"] or "{}")
    data.setdefault("blocks", json.loads(row["blocks_json"] or "[]"))
    return data


def save_section(paper_id: str, section_id: int, title: str, start_page: int, end_page: int) -> None:
    execute(
        "INSERT OR REPLACE INTO sections(paper_id, section_id, title, start_page, end_page) "
        "VALUES (?,?,?,?,?)",
        (paper_id, section_id, title, start_page, end_page),
    )


def list_sections(paper_id: str) -> list[sqlite3.Row]:
    return query(
        "SELECT * FROM sections WHERE paper_id=? ORDER BY section_id",
        (paper_id,),
    )


def update_section_hint(paper_id: str, section_id: int, hint: str, status: str) -> None:
    execute(
        "UPDATE sections SET hint_zh=?, hint_status=? WHERE paper_id=? AND section_id=?",
        (hint, status, paper_id, section_id),
    )


def get_page_translation(paper_id: str, page_num: int) -> tuple[str, list[str]]:
    """Returns (status, zh_per_block). status in {none, streaming, done, error}."""
    row = query_one(
        "SELECT status, zh_json FROM page_translations WHERE paper_id=? AND page_num=?",
        (paper_id, page_num),
    )
    if row is None:
        return ("none", [])
    return (row["status"], json.loads(row["zh_json"]) if row["zh_json"] else [])


def init_page_translation(paper_id: str, page_num: int) -> None:
    execute(
        "INSERT INTO page_translations(paper_id, page_num, status, zh_json, updated_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(paper_id, page_num) DO UPDATE SET status=excluded.status, zh_json=excluded.zh_json, updated_at=excluded.updated_at",
        (paper_id, page_num, "streaming", "[]", now()),
    )


def append_page_translation_block(paper_id: str, page_num: int, zh_block: str) -> None:
    row = query_one(
        "SELECT zh_json FROM page_translations WHERE paper_id=? AND page_num=?",
        (paper_id, page_num),
    )
    arr = json.loads(row["zh_json"]) if row and row["zh_json"] else []
    arr.append(zh_block)
    execute(
        "UPDATE page_translations SET zh_json=?, updated_at=? WHERE paper_id=? AND page_num=?",
        (json.dumps(arr, ensure_ascii=False), now(), paper_id, page_num),
    )


def finish_page_translation(paper_id: str, page_num: int, status: str = "done") -> None:
    execute(
        "UPDATE page_translations SET status=?, updated_at=? WHERE paper_id=? AND page_num=?",
        (status, now(), paper_id, page_num),
    )


def get_or_create_word_state(paper_id: str, word: str) -> sqlite3.Row:
    execute(
        "INSERT OR IGNORE INTO word_state(paper_id, word, count, status, last_seen) "
        "VALUES (?,?,0,'new',?)",
        (paper_id, word, now()),
    )
    return query_one(
        "SELECT * FROM word_state WHERE paper_id=? AND word=?",
        (paper_id, word),
    )


def increment_word_count(paper_id: str, word: str) -> sqlite3.Row:
    execute(
        "UPDATE word_state SET count=count+1, last_seen=? WHERE paper_id=? AND word=?",
        (now(), paper_id, word),
    )
    row = query_one(
        "SELECT * FROM word_state WHERE paper_id=? AND word=?",
        (paper_id, word),
    )
    # promote to learning after 2 sightings
    if row and row["count"] >= 2 and row["status"] == "new":
        execute(
            "UPDATE word_state SET status='learning' WHERE paper_id=? AND word=?",
            (paper_id, word),
        )
        row = query_one(
            "SELECT * FROM word_state WHERE paper_id=? AND word=?",
            (paper_id, word),
        )
    return row


def mark_word_known(paper_id: str, word: str) -> None:
    execute(
        "UPDATE word_state SET status='known' WHERE paper_id=? AND word=?",
        (paper_id, word),
    )


def demote_word(paper_id: str, word: str) -> None:
    execute(
        "UPDATE word_state SET status='learning' WHERE paper_id=? AND word=?",
        (paper_id, word),
    )


def get_word_definition(lemma: str) -> sqlite3.Row | None:
    return query_one("SELECT * FROM words WHERE lemma=?", (lemma,))


def save_word_definition(
    lemma: str,
    zh: str,
    en_simple: str,
    part_of_speech: str = "",
    pronunciation: str = "",
    word_form: str = "",
    memory_tip: str = "",
    freq_rank: int | None = None,
) -> None:
    execute(
        "INSERT OR REPLACE INTO words(lemma, zh, en_simple, part_of_speech, pronunciation, word_form, memory_tip, freq_rank) VALUES (?,?,?,?,?,?,?,?)",
        (lemma, zh, en_simple, part_of_speech, pronunciation, word_form, memory_tip, freq_rank),
    )


def get_sentence_help(paper_id: str, sent_hash: str) -> sqlite3.Row | None:
    return query_one(
        "SELECT * FROM sentence_helps WHERE paper_id=? AND sent_hash=?",
        (paper_id, sent_hash),
    )


def save_sentence_help(
    paper_id: str,
    sent_hash: str,
    page_num: int,
    keywords: list[str],
    structure: str,
    en_simple: str,
    zh: str,
) -> None:
    execute(
        "INSERT OR REPLACE INTO sentence_helps(paper_id, sent_hash, page_num, keywords_json, structure, en_simple, zh) "
        "VALUES (?,?,?,?,?,?,?)",
        (paper_id, sent_hash, page_num, json.dumps(keywords, ensure_ascii=False), structure, en_simple, zh),
    )


def llm_cache_get(cache_key: str) -> str | None:
    row = query_one("SELECT response FROM llm_cache WHERE cache_key=?", (cache_key,))
    return row["response"] if row else None


def llm_cache_put(cache_key: str, response: str) -> None:
    execute(
        "INSERT OR REPLACE INTO llm_cache(cache_key, response, created_at) VALUES (?,?,?)",
        (cache_key, response, now()),
    )


def list_papers() -> list[sqlite3.Row]:
    return query("SELECT id, title, source_path FROM papers ORDER BY created_at DESC")
