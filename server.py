"""FastAPI server for Arks. SSE for all streaming endpoints.

Run via `python arks.py` which wraps uvicorn.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import ai_client
import db
import pdf_export
import pdf_extract
from prompts import (
    page_translate_prompt,
    parse_json_array,
    parse_json_line,
    section_hint_prompt,
    sentence_help_prompt,
    word_lookup_prompt,
)

log = logging.getLogger("arks")

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
PAPERS_DIR = BASE_DIR / "papers"

app = FastAPI(title="Arks Translate", version="0.1.0")

# Locks per (paper_id, page_num) so concurrent requests for the same page
# don't double-bill the API.
_page_locks: dict[tuple[str, int], asyncio.Lock] = {}
_section_locks: dict[tuple[str, int], asyncio.Lock] = {}


def _page_lock(paper_id: str, page_num: int) -> asyncio.Lock:
    key = (paper_id, page_num)
    lock = _page_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _page_locks[key] = lock
    return lock


def _section_lock(paper_id: str, section_id: int) -> asyncio.Lock:
    key = (paper_id, section_id)
    lock = _section_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _section_locks[key] = lock
    return lock


def _parse_json_object(raw: str, feature: str) -> dict | None:
    """Parse an LLM response expected to be one JSON object without crashing SSE."""
    parsed = parse_json_line(raw)
    if isinstance(parsed, dict):
        return parsed
    # Some providers wrap a valid object in a one-element JSON array.
    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        return parsed[0]
    if isinstance(parsed, list) and parsed and all(isinstance(item, str) for item in parsed):
        # A few OpenAI-compatible responses ignore the object instruction and
        # return [Chinese meaning, simple-English explanation].
        recovered_list = [item.strip() for item in parsed if item.strip()]
        if recovered_list:
            return {
                "zh": recovered_list[0],
                "en_simple": recovered_list[1] if len(recovered_list) > 1 else "",
            }
    # Last-resort recovery for models that prepend a sentence or emit simple
    # key/value lines around the JSON payload.
    compact = raw.strip()
    recovered: dict[str, str] = {}
    for key in ("zh", "en_simple", "structure"):
        match = re.search(
            rf"['\"`]?{key}['\"`]?\s*[:=]\s*['\"`]?(.+?)['\"`]?\s*(?:[,\n]|$)",
            compact,
            re.I,
        )
        if match:
            recovered[key] = match.group(1).strip().rstrip("`\"'")
    if recovered:
        return recovered
    log.warning("%s returned %s instead of a JSON object; raw=%r", feature, type(parsed).__name__, compact[:500])
    return None


def _json_text(value: object) -> str:
    """Accept only scalar JSON values for text fields from an LLM response."""
    return value.strip() if isinstance(value, str) else ""


def _is_part_of_speech_only(value: str) -> bool:
    """Reject a POS label accidentally returned in place of a Chinese meaning."""
    compact = re.sub(r"[()（）,，;；/\\s]+", "", value.lower())
    pos_words = (
        "名词", "动词", "形容词", "副词", "介词", "连词", "代词", "冠词",
        "数词", "感叹词", "noun", "verb", "adjective", "adverb", "preposition",
        "conjunction", "pronoun", "article", "numeral", "interjection",
    )
    return bool(compact) and all(part in "".join(pos_words) for part in re.findall(r"[a-z]+|[\u3400-\u9fff]+", compact))


# ---- Static & root --------------------------------------------------------


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---- Papers ---------------------------------------------------------------


@app.get("/api/papers")
async def list_papers():
    papers = db.list_papers()
    out = []
    for p in papers:
        sections = db.list_sections(p["id"])
        page_count_row = db.query_one("SELECT COUNT(*) AS n FROM pages WHERE paper_id=?", (p["id"],))
        out.append(
            {
                "id": p["id"],
                "title": p["title"],
                "source_path": p["source_path"],
                "page_count": int(page_count_row["n"] if page_count_row else 0),
                "sections": [
                    {
                        "id": s["section_id"],
                        "title": s["title"],
                        "start_page": s["start_page"],
                        "end_page": s["end_page"],
                        "hint_zh": s["hint_zh"],
                        "hint_status": s["hint_status"],
                    }
                    for s in sections
                ],
            }
        )
    return out


@app.post("/api/papers/import")
async def import_paper(file: UploadFile = File(...)):
    """Receive PDF, parse it, return paper_id."""
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    # sanitize filename
    name = Path(file.filename or "paper.pdf").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported")
    dest = PAPERS_DIR / name
    dest.write_bytes(await file.read())
    paper_id = pdf_extract.extract_pdf(dest)
    return {"paper_id": paper_id}


@app.post("/api/papers/import-path")
async def import_paper_by_path(payload: dict):
    """Import a PDF by local file path (for the bundled test paper)."""
    path = Path(payload.get("path", ""))
    if not path.exists() or path.suffix.lower() != ".pdf":
        raise HTTPException(400, f"PDF not found: {path}")
    paper_id = pdf_extract.extract_pdf(path)
    return {"paper_id": paper_id}


@app.get("/api/papers/{paper_id}/toc")
async def get_toc(paper_id: str):
    sections = db.list_sections(paper_id)
    return [
        {
            "id": s["section_id"],
            "title": s["title"],
            "start_page": s["start_page"],
            "end_page": s["end_page"],
            "hint_zh": s["hint_zh"],
            "hint_status": s["hint_status"],
        }
        for s in sections
    ]


@app.get("/api/papers/{paper_id}/page/{page_num}")
async def get_page(paper_id: str, page_num: int):
    page = db.load_page(paper_id, page_num)
    if page is None:
        raise HTTPException(404, "Page not found")
    # Cached pages produced before native word boxes were introduced contain
    # estimated coordinates. Refresh once so existing imports become precise.
    if page.get("coordinate_version") != 2:
        paper = db.query_one("SELECT source_path FROM papers WHERE id=?", (paper_id,))
        if paper and Path(paper["source_path"]).is_file():
            pdf_extract.extract_pdf(Path(paper["source_path"]))
            page = db.load_page(paper_id, page_num)
    status, zh = db.get_page_translation(paper_id, page_num)
    return {
        "page_num": page_num,
        "blocks": page["blocks"],
        "image_path": page["image_path"],
        "page_w": page["page_w"],
        "page_h": page["page_h"],
        "image_w": page["image_w"],
        "image_h": page["image_h"],
        "word_boxes": page["word_boxes"],
        "sentences": page["sentences"],
        "translation_status": status,
        "translation_zh": zh,
    }


@app.get("/api/papers/{paper_id}/page/{page_num}/image")
async def get_page_image(paper_id: str, page_num: int):
    page = db.load_page(paper_id, page_num)
    if page is None or not page["image_path"]:
        raise HTTPException(404, "Image not found")
    img_abs = BASE_DIR / ".cache" / "pages" / page["image_path"]
    if not img_abs.exists():
        raise HTTPException(404, "Image file missing")
    return FileResponse(img_abs, media_type="image/png")


# ---- Page translation (SSE) -----------------------------------------------


async def _translate_page_events(paper_id: str, page_num: int, ai: ai_client.AIClient):
    blocks = db.load_page_blocks(paper_id, page_num)
    if blocks is None:
        yield {"type": "error", "message": "page not found"}
        return

    # If already done, replay cached result then exit
    status, zh_cached = db.get_page_translation(paper_id, page_num)
    if status == "done":
        yield {"type": "replay", "zh": zh_cached}
        yield {"type": "done"}
        return

    # Acquire lock to avoid duplicate streams
    async with _page_lock(paper_id, page_num):
        status2, _ = db.get_page_translation(paper_id, page_num)
        if status2 == "done":
            _, zh_cached = db.get_page_translation(paper_id, page_num)
            yield {"type": "replay", "zh": zh_cached}
            yield {"type": "done"}
            return

        db.init_page_translation(paper_id, page_num)

        # MiniMax's OpenAI-compatible endpoint caps one completion at 2048
        # tokens. Start with a few blocks at a time, then split only the
        # oversized batch so a long block cannot discard earlier results.
        zh_arr: list[str] = []
        batch_size = 4
        pending = [(start, blocks[start : start + batch_size]) for start in range(0, len(blocks), batch_size)]
        while pending:
            start, batch = pending.pop(0)
            prompt = page_translate_prompt(batch)
            full: list[str] = []
            try:
                async for chunk in ai.stream(prompt, temperature=0.2, max_tokens=2048):
                    full.append(chunk)
                    yield {"type": "delta", "content": chunk}
            except Exception as e:
                # A length-limited JSON array is not recoverable by parsing.
                # Split the current batch and retry its halves in order.
                if "token limit" in str(e).lower() and len(batch) > 1:
                    midpoint = max(1, len(batch) // 2)
                    pending[0:0] = [
                        (start, batch[:midpoint]),
                        (start + midpoint, batch[midpoint:]),
                    ]
                    log.warning(
                        "translate batch too long; splitting page=%s blocks=%s-%s into %s+%s",
                        page_num,
                        start + 1,
                        start + len(batch),
                        len(batch[:midpoint]),
                        len(batch[midpoint:]),
                    )
                    continue
                log.exception("translate batch failed: page=%s blocks=%s-%s", page_num, start + 1, start + len(batch))
                db.finish_page_translation(paper_id, page_num, "error")
                yield {"type": "error", "message": str(e)}
                return

            batch_zh = parse_json_array("".join(full).strip(), len(batch))
            zh_arr.extend(batch_zh)
            for zh in batch_zh:
                db.append_page_translation_block(paper_id, page_num, zh)
            yield {"type": "blocks", "zh": zh_arr}

        db.finish_page_translation(paper_id, page_num, "done")
        yield {"type": "blocks", "zh": zh_arr}
        yield {"type": "done"}


@app.get("/api/papers/{paper_id}/page/{page_num}/translate")
async def translate_page(paper_id: str, page_num: int):
    try:
        ai = ai_client.AIClient()
    except RuntimeError as e:
        raise HTTPException(503, str(e))

    async def event_gen():
        async for evt in _translate_page_events(paper_id, page_num, ai):
            yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/papers/{paper_id}/page/{page_num}/export")
async def export_page_translation(paper_id: str, page_num: int, payload: dict):
    """Download saved pages, or translate and export the complete source PDF."""
    mode = payload.get("mode", "partial")
    if mode not in {"partial", "full"}:
        raise HTTPException(400, "mode must be partial or full")
    paper = db.query_one("SELECT source_path FROM papers WHERE id=?", (paper_id,))
    blocks = db.load_page_blocks(paper_id, page_num)
    if paper is None or blocks is None:
        raise HTTPException(404, "Paper page not found")

    rows = db.query("SELECT page_num FROM pages WHERE paper_id=? ORDER BY page_num", (paper_id,))
    if mode == "full":
        try:
            ai = None
            for row in rows:
                pnum = int(row["page_num"])
                status, _ = db.get_page_translation(paper_id, pnum)
                if status == "done":
                    continue
                if ai is None:
                    ai = ai_client.AIClient()
                async for _ in _translate_page_events(paper_id, pnum, ai):
                    pass
                status, _ = db.get_page_translation(paper_id, pnum)
                if status != "done":
                    raise RuntimeError(f"page {pnum} could not finish translation")
        except Exception as e:
            log.exception("full-PDF export translation failed")
            raise HTTPException(502, f"Could not finish full PDF translation: {e}") from e

    translated_pages = []
    for row in rows:
        pnum = int(row["page_num"])
        pblocks = db.load_page_blocks(paper_id, pnum) or []
        status, ptranslations = db.get_page_translation(paper_id, pnum)
        if mode == "full" and status != "done":
            raise HTTPException(502, f"Page {pnum} has no completed translation")
        if mode == "full" or any((item or "").strip() for item in ptranslations):
            translated_pages.append((pnum, pblocks, ptranslations))
    if not translated_pages:
        raise HTTPException(409, "No translated pages are available")
    output_path = BASE_DIR / "output" / "pdf" / (
        f"{paper_id}-full-translated.pdf" if mode == "full" else f"{paper_id}-translated-pages.pdf"
    )
    try:
        pdf_export.export_translated_pages(Path(paper["source_path"]), translated_pages, output_path)
    except Exception as e:
        log.exception("PDF export failed")
        raise HTTPException(500, f"PDF export failed: {e}") from e
    return FileResponse(output_path, media_type="application/pdf", filename=output_path.name)


# ---- Section hint (SSE) ---------------------------------------------------


async def _section_hint_events(paper_id: str, section_id: int, ai: ai_client.AIClient):
    sections = db.list_sections(paper_id)
    section = next((s for s in sections if s["section_id"] == section_id), None)
    if section is None:
        yield {"type": "error", "message": "section not found"}
        return

    if section["hint_status"] == "done" and section["hint_zh"]:
        yield {"type": "replay", "zh": section["hint_zh"]}
        yield {"type": "done"}
        return

    async with _section_lock(paper_id, section_id):
        # find first paragraph in section page range
        first_para = ""
        for p in range(section["start_page"], section["end_page"] + 1):
            blocks = db.load_page_blocks(paper_id, p)
            if not blocks:
                continue
            for b in blocks:
                if b["kind"] == "p" and b.get("sents"):
                    first_para = " ".join(b["sents"])[:300]
                    break
            if first_para:
                break

        prompt = section_hint_prompt(section["title"], first_para)
        full: list[str] = []
        try:
            # The output remains constrained by the prompt, but MiniMax also
            # spends completion budget on reasoning before the final hint.
            async for chunk in ai.stream(prompt, temperature=0.3, max_tokens=512):
                full.append(chunk)
                yield {"type": "delta", "content": chunk}
        except Exception as e:
            log.exception("section hint failed")
            db.update_section_hint(paper_id, section_id, "", "error")
            yield {"type": "error", "message": str(e)}
            return

        text = "".join(full).strip()
        # Strip stray quotes
        text = text.strip('"').strip("「").strip("」").strip()
        db.update_section_hint(paper_id, section_id, text, "done")
        yield {"type": "done", "zh": text}


@app.get("/api/papers/{paper_id}/section/{section_id}/hint")
async def section_hint(paper_id: str, section_id: int):
    try:
        ai = ai_client.AIClient()
    except RuntimeError as e:
        raise HTTPException(503, str(e))

    async def event_gen():
        async for evt in _section_hint_events(paper_id, section_id, ai):
            yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- Word lookup (SSE) ----------------------------------------------------


async def _word_lookup_events(word: str, sentence: str, ai: ai_client.AIClient):
    cached = db.get_word_definition(word.lower())
    if cached and cached["zh"] and not _is_part_of_speech_only(cached["zh"]) and cached["en_simple"] and cached["pronunciation"] and cached["part_of_speech"]:
        yield {"type": "replay", "zh": cached["zh"], "en_simple": cached["en_simple"], "part_of_speech": cached["part_of_speech"], "pronunciation": cached["pronunciation"], "word_form": cached["word_form"] or "", "memory_tip": cached["memory_tip"] or ""}
        yield {"type": "done"}
        return

    prompt = word_lookup_prompt(word, sentence)
    full: list[str] = []
    try:
        async for chunk in ai.stream(prompt, temperature=0.2, max_tokens=384):
            full.append(chunk)
            yield {"type": "delta", "content": chunk}
    except Exception as e:
        log.exception("word lookup failed")
        yield {"type": "error", "message": str(e)}
        return

    raw = "".join(full).strip()
    parsed = _parse_json_object(raw, "word lookup")
    if parsed is None:
        yield {"type": "error", "message": "The model returned an invalid dictionary response."}
        return
    zh = _json_text(parsed.get("zh"))
    if not zh:
        # MiniMax occasionally follows the instruction but chooses a generic
        # dictionary key such as meaning/translation/definition.
        for alias in ("meaning", "translation", "definition", "chinese", "中文"):
            candidate = _json_text(parsed.get(alias))
            if candidate:
                zh = candidate
                break
    if not zh:
        # Last fallback: use the first scalar value containing CJK characters.
        for value in parsed.values():
            candidate = _json_text(value)
            if re.search(r"[\u3400-\u9fff]", candidate):
                zh = candidate
                break
    if _is_part_of_speech_only(zh):
        # Do not let a valid-looking POS tag contaminate the primary meaning.
        # Retry once with an explicit correction before reporting a malformed reply.
        correction = (
            "Your previous Chinese meaning was only a part-of-speech label. "
            "Return the actual Chinese dictionary meaning for this word, not its word class. "
            "For example, a valid meaning is '休闲；闲暇时间', while '名词 noun' is invalid.\n\n"
        ) + prompt
        full = []
        try:
            async for chunk in ai.stream(correction, temperature=0.1, max_tokens=384):
                full.append(chunk)
                yield {"type": "delta", "content": chunk}
        except Exception as e:
            log.exception("word lookup correction failed")
            yield {"type": "error", "message": str(e)}
            return
        parsed = _parse_json_object("".join(full).strip(), "word lookup correction")
        if parsed is None:
            yield {"type": "error", "message": "The model returned an invalid dictionary response."}
            return
        zh = _json_text(parsed.get("zh"))
        en_simple = _json_text(parsed.get("en_simple"))
        part_of_speech = _json_text(parsed.get("part_of_speech"))
        pronunciation = _json_text(parsed.get("pronunciation"))
        word_form = _json_text(parsed.get("word_form"))
        memory_tip = _json_text(parsed.get("memory_tip"))
    else:
        en_simple = _json_text(parsed.get("en_simple"))
        part_of_speech = _json_text(parsed.get("part_of_speech"))
        pronunciation = _json_text(parsed.get("pronunciation"))
        word_form = _json_text(parsed.get("word_form"))
        memory_tip = _json_text(parsed.get("memory_tip"))
    if not zh or _is_part_of_speech_only(zh):
        log.warning("word lookup returned no Chinese field; payload=%r", parsed)
        yield {"type": "error", "message": "The model did not return a Chinese meaning."}
        return
    db.save_word_definition(word.lower(), zh, en_simple, part_of_speech, pronunciation, word_form, memory_tip)
    yield {"type": "done", "zh": zh, "en_simple": en_simple, "part_of_speech": part_of_speech, "pronunciation": pronunciation, "word_form": word_form, "memory_tip": memory_tip}


@app.get("/api/words/lookup")
async def word_lookup(w: str, s: str = ""):
    if not w or not re.match(r"^[A-Za-z]+$", w):
        raise HTTPException(400, "invalid word")
    try:
        ai = ai_client.AIClient()
    except RuntimeError as e:
        raise HTTPException(503, str(e))

    async def event_gen():
        async for evt in _word_lookup_events(w, s, ai):
            yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- Sentence help (SSE) --------------------------------------------------


async def _sentence_help_events(
    paper_id: str, sent_hash: str, page_num: int, sentence: str, ai: ai_client.AIClient
):
    cached = db.get_sentence_help(paper_id, sent_hash)
    if cached and cached["zh"]:
        yield {
            "type": "replay",
            "keywords": json.loads(cached["keywords_json"]) if cached["keywords_json"] else [],
            "structure": cached["structure"],
            "en_simple": cached["en_simple"],
            "zh": cached["zh"],
        }
        yield {"type": "done"}
        return

    prompt = sentence_help_prompt(sentence)
    full: list[str] = []
    try:
        # MiniMax M3 may spend part of its budget on internal reasoning before
        # emitting the JSON payload; 240 can truncate a normal sentence help.
        async for chunk in ai.stream(prompt, temperature=0.2, max_tokens=512):
            full.append(chunk)
            yield {"type": "delta", "content": chunk}
    except Exception as e:
        log.exception("sentence help failed")
        yield {"type": "error", "message": str(e)}
        return

    raw = "".join(full).strip()
    parsed = _parse_json_object(raw, "sentence help")
    if parsed is None:
        yield {"type": "error", "message": "The model returned an invalid sentence-help response."}
        return
    keywords = parsed.get("keywords", [])
    if isinstance(keywords, str):
        keywords = [k.strip() for k in re.split(r"[,;]\s*", keywords) if k.strip()]
    elif not isinstance(keywords, list):
        keywords = []
    else:
        keywords = [str(k).strip() for k in keywords if str(k).strip()]
    structure = _json_text(parsed.get("structure"))
    en_simple = _json_text(parsed.get("en_simple"))
    zh = _json_text(parsed.get("zh"))
    if zh or en_simple:
        db.save_sentence_help(paper_id, sent_hash, page_num, keywords, structure, en_simple, zh)
    yield {
        "type": "done",
        "keywords": keywords,
        "structure": structure,
        "en_simple": en_simple,
        "zh": zh,
    }


@app.post("/api/sentences/help")
async def sentence_help(req: Request):
    body = await req.json()
    paper_id = body.get("paper_id", "")
    sentence = (body.get("sentence") or "").strip()
    page_num = int(body.get("page_num", 0))
    if not paper_id or not sentence:
        raise HTTPException(400, "missing fields")
    sent_hash = hashlib.sha1(sentence.encode("utf-8")).hexdigest()[:16]
    try:
        ai = ai_client.AIClient()
    except RuntimeError as e:
        raise HTTPException(503, str(e))

    async def event_gen():
        async for evt in _sentence_help_events(paper_id, sent_hash, page_num, sentence, ai):
            yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- Word state -----------------------------------------------------------


@app.post("/api/words/state")
async def update_word_state(req: Request):
    body = await req.json()
    paper_id = body.get("paper_id", "")
    word = (body.get("word") or "").lower()
    action = body.get("action", "")
    if not paper_id or not word:
        raise HTTPException(400, "missing fields")
    if action == "click":
        row = db.increment_word_count(paper_id, word)
    elif action == "know":
        db.mark_word_known(paper_id, word)
        row = db.get_or_create_word_state(paper_id, word)
    elif action == "forget":
        db.demote_word(paper_id, word)
        row = db.get_or_create_word_state(paper_id, word)
    else:
        raise HTTPException(400, "invalid action")
    return {
        "word": word,
        "count": row["count"],
        "status": row["status"],
    }


@app.get("/api/words/state")
async def get_word_state(paper_id: str, word: str):
    word = word.lower()
    row = db.get_or_create_word_state(paper_id, word)
    return {"word": word, "count": row["count"], "status": row["status"]}


@app.post("/api/words/states")
async def get_word_states(req: Request):
    """Return all vocabulary states needed to render one PDF page in one request."""
    body = await req.json()
    paper_id = body.get("paper_id", "")
    words = body.get("words", [])
    if not paper_id or not isinstance(words, list):
        raise HTTPException(400, "missing fields")
    result = {}
    for raw_word in words:
        word = str(raw_word or "").lower().strip()
        if not word or not re.match(r"^[a-z]+$", word):
            continue
        row = db.get_or_create_word_state(paper_id, word)
        result[word] = {"word": word, "count": row["count"], "status": row["status"]}
    return result


# ---- Health & config ------------------------------------------------------


@app.get("/api/health")
async def health():
    cfg = ai_client.get_config()
    return {
        "ok": True,
        "model": cfg["model"],
        "has_api_key": bool(cfg["api_key"]),
        "base_url": cfg["base_url"],
    }
