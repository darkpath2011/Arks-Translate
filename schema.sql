-- Arks Translate schema
-- Idempotent: every CREATE uses IF NOT EXISTS

CREATE TABLE IF NOT EXISTS papers (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    source_path TEXT,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS sections (
    paper_id    TEXT,
    section_id  INTEGER,
    title       TEXT,
    start_page  INTEGER,
    end_page    INTEGER,
    hint_zh     TEXT,
    hint_status TEXT DEFAULT 'none',
    PRIMARY KEY (paper_id, section_id)
);

CREATE TABLE IF NOT EXISTS pages (
    paper_id    TEXT,
    page_num    INTEGER,
    blocks_json TEXT,
    page_json   TEXT,
    PRIMARY KEY (paper_id, page_num)
);

CREATE TABLE IF NOT EXISTS page_translations (
    paper_id    TEXT,
    page_num    INTEGER,
    status      TEXT DEFAULT 'none',
    zh_json     TEXT,
    updated_at  INTEGER,
    PRIMARY KEY (paper_id, page_num)
);

CREATE TABLE IF NOT EXISTS words (
    lemma       TEXT PRIMARY KEY,
    zh          TEXT,
    en_simple   TEXT,
    part_of_speech TEXT,
    pronunciation TEXT,
    word_form   TEXT,
    memory_tip  TEXT,
    freq_rank   INTEGER
);

CREATE TABLE IF NOT EXISTS word_state (
    paper_id    TEXT,
    word        TEXT,
    count       INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'new',
    last_seen   INTEGER,
    PRIMARY KEY (paper_id, word)
);

CREATE TABLE IF NOT EXISTS sentence_helps (
    paper_id    TEXT,
    sent_hash   TEXT,
    page_num    INTEGER,
    keywords_json TEXT,
    structure   TEXT,
    en_simple   TEXT,
    zh          TEXT,
    PRIMARY KEY (paper_id, sent_hash)
);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key   TEXT PRIMARY KEY,
    response    TEXT,
    created_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_pages_paper
    ON pages(paper_id, page_num);

CREATE INDEX IF NOT EXISTS idx_word_state_paper
    ON word_state(paper_id, word);
