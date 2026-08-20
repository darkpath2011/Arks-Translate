// Arks — frontend reader.
// Image-based rendering: each PDF page is rendered as PNG, and word/sentence
// bounding boxes from PyMuPDF are positioned as transparent overlays above.

const STOPWORDS = new Set([
  "a","an","the","and","or","but","if","then","else","for","nor","so","yet",
  "i","you","he","she","it","we","they","me","him","her","us","them",
  "my","your","his","its","our","their","mine","yours","hers","ours","theirs",
  "this","that","these","those",
  "is","am","are","was","were","be","been","being","have","has","had","having",
  "do","does","did","doing","done",
  "can","could","will","would","shall","should","may","might","must",
  "in","on","at","by","for","with","about","against","between","into","through",
  "during","before","after","above","below","to","from","up","down","out","off",
  "over","under","again","further","once","here","there","when","where","why","how",
  "all","any","both","each","few","more","most","other","some","such","no","not",
  "only","own","same","than","too","very","just","also","now",
  "of","as","because","since","unless","although","while","whom","whose","which","who","what",
  "one","two","three","four","five","six","seven","eight","nine","ten",
  "yes","okay","ok",
]);

const state = {
  papers: [],
  paper: null,
  pageNum: 0,
  pageCount: 0,
  page: null,              // current page object from server
  blockTranslations: [],   // zh per text-block
  translationStatus: "none",
  wordStates: {},          // lemma -> {count, status}
  popover: null,
  openHelpHash: null,
  translateOpen: false,
  focusMode: false,
  sectionHintShown: {},
  recentClicks: [],
  translationStream: null,
  zoom: 1,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function init() {
  bindUI();
  try {
    await loadPapers();
  } catch (e) {
    console.error("loadPapers failed", e);
    $("#paper-title").textContent = "Network error";
    return;
  }
  if (state.papers.length === 0) {
    $("#paper-title").textContent = "No papers yet";
    return;
  }
  state.paper = state.papers[0];
  $("#paper-title").textContent = state.paper.title;
  state.pageCount = Math.max(state.paper.page_count || 0, ...state.paper.sections.map((s) => s.end_page || 0), 1);
  if (!state.pageCount) state.pageCount = 1;
  renderTOC();
  renderPages();
  try {
    await gotoPage(1);
  } catch (e) {
    console.error("gotoPage failed", e);
  }
  startFlowTracking();
}

async function loadPapers() {
  const res = await fetch("/api/papers");
  state.papers = await res.json();
}

function bindUI() {
  $("#toc-toggle").addEventListener("click", () => {
    if (window.matchMedia("(max-width: 760px)").matches) {
      document.body.classList.toggle("sidebar-open");
    } else {
      document.body.classList.toggle("sidebar-collapsed");
    }
  });
  $("#focus-btn").addEventListener("click", toggleFocus);
  $("#translate-btn").addEventListener("click", toggleTranslation);
  $("#export-menu-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    const menu = $("#export-menu");
    menu.hidden = !menu.hidden;
  });
  $("#export-partial-btn").addEventListener("click", () => { $("#export-menu").hidden = true; exportCurrentPage("partial", $("#export-partial-btn")); });
  $("#export-full-btn").addEventListener("click", () => { $("#export-menu").hidden = true; exportCurrentPage("full", $("#export-full-btn")); });
  $("#zoom-out").addEventListener("click", () => setZoom(state.zoom - 0.1));
  $("#zoom-in").addEventListener("click", () => setZoom(state.zoom + 0.1));
  $("#zoom-reset").addEventListener("click", () => setZoom(1));
  $("#prev-page").addEventListener("click", () => gotoPage(state.pageNum - 1));
  $("#next-page").addEventListener("click", () => gotoPage(state.pageNum + 1));
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input,textarea")) return;
    if (e.key === "ArrowLeft") gotoPage(state.pageNum - 1);
    if (e.key === "ArrowRight") gotoPage(state.pageNum + 1);
    if (e.key === "Escape") closePopover();
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".export-menu-wrap")) $("#export-menu").hidden = true;
    if (state.popover && !state.popover.contains(e.target) && !e.target.closest(".word-layer")) {
      closePopover();
    }
    if (state.openHelpHash && !e.target.closest(".sent-help") && !e.target.closest(".sentence-layer")) {
      const help = document.querySelector(".sent-help");
      if (help) help.remove();
      state.openHelpHash = null;
      const layers = $$(".sentence-layer");
      layers.forEach((l) => l.classList.remove("has-help"));
    }
  });
}

function setZoom(value) {
  state.zoom = Math.min(2.5, Math.max(0.6, Math.round(value * 10) / 10));
  const container = $("#page-container");
  if (container) {
    container.style.width = `${880 * state.zoom}px`;
    container.style.maxWidth = "none";
  }
  const label = $("#zoom-reset");
  if (label) label.textContent = `${Math.round(state.zoom * 100)}%`;
  // Reflowed image dimensions change overlay scale; re-render after layout.
  requestAnimationFrame(() => {
    if (state.page && $("#page-image").complete) renderPage();
  });
}

function toggleFocus() {
  state.focusMode = !state.focusMode;
  document.body.dataset.focus = state.focusMode ? "on" : "off";
}

function toggleTranslation() {
  state.translateOpen = !state.translateOpen;
  $("#translate-btn").dataset.active = state.translateOpen ? "true" : "false";
  if (state.translateOpen) startTranslationStream();
  renderTranslationStrip();
}

// ---- TOC / pager ----------------------------------------------------------

function renderTOC() {
  const list = $("#toc-list");
  list.innerHTML = "";
  for (const s of state.paper.sections) {
    const li = document.createElement("li");
    li.dataset.sectionId = s.id;
    li.innerHTML = `<span><strong>${escapeHtml(s.title)}</strong>${
      s.hint_zh ? `<span class="toc-hint">${escapeHtml(s.hint_zh)}</span>` : ""
    }</span>`;
    li.addEventListener("click", () => gotoPage(s.start_page));
    list.appendChild(li);
  }
  highlightCurrentTOC();
}

function highlightCurrentTOC() {
  const cur = state.paper.sections.find(
    (s) => state.pageNum >= s.start_page && state.pageNum <= s.end_page
  );
  $$("#toc-list li").forEach((li) => {
    li.dataset.current = li.dataset.sectionId == (cur && cur.id) ? "true" : "false";
  });
}

function renderPages() {
  const list = $("#page-list");
  list.innerHTML = "";
  for (let i = 1; i <= state.pageCount; i++) {
    const li = document.createElement("li");
    li.dataset.page = i;
    li.innerHTML = `<span>Page</span><span class="pnum">${i}</span>`;
    li.addEventListener("click", () => gotoPage(i));
    list.appendChild(li);
  }
  highlightCurrentPage();
}

function highlightCurrentPage() {
  $$("#page-list li").forEach((li) => {
    li.dataset.current = li.dataset.page == state.pageNum ? "true" : "false";
  });
}

function renderPager() {
  $("#prev-page").disabled = state.pageNum <= 1;
  $("#next-page").disabled = state.pageNum >= state.pageCount;
  $("#page-status").textContent = `Page ${state.pageNum} / ${state.pageCount}`;
}

// ---- Page navigation ------------------------------------------------------

async function gotoPage(n) {
  if (n < 1 || n > state.pageCount || n === state.pageNum) return;
  if (state.translationStream) {
    try { state.translationStream.controller.abort(); } catch {}
    state.translationStream = null;
  }
  closePopover();
  removeHelp();
  state.pageNum = n;
  highlightCurrentTOC();
  highlightCurrentPage();
  renderPager();
  await loadPage(n);
  renderPage();
  renderTranslationStrip();
  maybeShowSectionHint();
  prefetchNext();
  $("#reader").scrollTo({ top: 0, behavior: "instant" });
}

async function loadPage(n) {
  const res = await fetch(`/api/papers/${state.paper.id}/page/${n}`);
  if (!res.ok) return;
  const data = await res.json();
  state.page = data;
  state.blockTranslations = data.translation_zh || [];
  state.translationStatus = data.translation_status || "none";
  updateExportButtons();
}

function prefetchNext() {
  const n = state.pageNum + 1;
  if (n > state.pageCount) return;
  fetch(`/api/papers/${state.paper.id}/page/${n}`).catch(() => {});
}

// ---- Page rendering (image + overlay) -------------------------------------

function renderPage() {
  const container = $("#page-container");
  const img = $("#page-image");
  const overlay = $("#text-overlay");
  container.style.display = "";
  overlay.innerHTML = "";

  // Once image loads, compute scale and render overlay.
  img.onload = () => {
    console.log("image loaded", img.naturalWidth, img.naturalHeight);
    const page = state.page;
    const rect = img.getBoundingClientRect();
    // PDF geometry is expressed in page points.  The raster image is rendered
    // at 1.5x for clarity, so image_w/image_h must not be used as the source
    // coordinate system here.
    const scaleX = rect.width / page.page_w;
    const scaleY = rect.height / page.page_h;
    // In a normal PDF both scales should match; if not, fall back to per-axis.
    const sx = scaleX;
    const sy = scaleY;

    // Group word boxes by sentence
    const bySent = new Map();
    const pageWords = new Set();
    for (const wb of page.word_boxes) {
      if (wb.sent_id === undefined) continue;
      if (!bySent.has(wb.sent_id)) bySent.set(wb.sent_id, []);
      bySent.get(wb.sent_id).push(wb);
    }

    // Render one sentence layer per sentence
    for (const [sentId, words] of bySent) {
      const sent = page.sentences.find((s) => s.id === sentId);
      if (!sent) continue;
      const sLayer = document.createElement("div");
      sLayer.className = "sentence-layer";
      sLayer.dataset.s = sent.hash;
      sLayer.dataset.sentence = sent.text;
      sLayer.style.left = (sent.x * sx) + "px";
      sLayer.style.top = (sent.y * sy) + "px";
      sLayer.style.width = (sent.w * sx) + "px";
      sLayer.style.height = (sent.h * sy) + "px";
      sLayer.addEventListener("click", (e) => {
        // If the click was on a word layer, ignore — the word handler will fire
        if (e.target.classList.contains("word-layer")) return;
        handleSentenceClick(sent, sLayer);
      });
      overlay.appendChild(sLayer);

      // Render word layers on top
      for (const wb of words) {
        const lemma = wb.lemma;
        // Skip stopwords AND very short non-alphabetic
        if (STOPWORDS.has(lemma)) continue;
        // Skip pure-numeric tokens
        if (/^\d+$/.test(wb.text)) continue;
        pageWords.add(lemma);
        const wLayer = document.createElement("div");
        wLayer.className = "word-layer";
        wLayer.dataset.w = lemma;
        wLayer.dataset.original = wb.text;
        wLayer.style.left = (wb.x * sx) + "px";
        wLayer.style.top = (wb.y * sy) + "px";
        wLayer.style.width = (wb.w * sx) + "px";
        wLayer.style.height = (wb.h * sy) + "px";
        const ws = state.wordStates[lemma];
        if (ws) wLayer.dataset.status = ws.status;
        wLayer.title = wb.text;
        wLayer.addEventListener("click", (e) => {
          e.stopPropagation();
          handleWordClick(wLayer, wb.text, sent.text);
        });
        overlay.appendChild(wLayer);
      }
    }
    loadWordStates([...pageWords]);
  };
  img.src = `/api/papers/${state.paper.id}/page/${state.pageNum}/image?t=${Date.now()}`;
}

async function loadWordStates(words) {
  const missing = words.filter((word) => !state.wordStates[word]);
  if (!missing.length) return;
  try {
    const res = await fetch("/api/words/states", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paper_id: state.paper.id, words: missing }),
    });
    if (res.ok) {
      const states = await res.json();
      for (const [word, value] of Object.entries(states)) {
        state.wordStates[word] = value;
        $$(`.word-layer[data-w="${cssEscape(word)}"]`).forEach((el) => { el.dataset.status = value.status; });
      }
    }
  } catch {}
}

// ---- Word click → popover -------------------------------------------------

async function handleWordClick(wLayer, originalText, sentence) {
  const word = wLayer.dataset.w;
  closePopover();
  let state_after = state.wordStates[word];
  try {
    const res = await fetch("/api/words/state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paper_id: state.paper.id, word, action: "click" }),
    });
    state_after = await res.json();
    state.wordStates[word] = state_after;
  } catch {}
  if (state_after && state_after.status === "known") {
    wLayer.dataset.status = "known";
    return;
  }
  wLayer.dataset.status = state_after ? state_after.status : "new";

  const pop = renderPopover(word, originalText, sentence, state_after || { status: "new" });
  document.getElementById("popover-root").appendChild(pop);
  state.popover = pop;
  positionPopover(pop, wLayer);
  streamWordLookup(word, sentence, pop);
}

function renderPopover(word, original, sentence, ws) {
  const pop = document.createElement("div");
  pop.className = "popover";

  const head = document.createElement("div");
  head.className = "head";
  head.innerHTML = `<span class="word">${escapeHtml(original)}</span><span class="status">${ws.status}</span>`;
  pop.appendChild(head);

  const ctx = document.createElement("div");
  ctx.className = "context";
  const lowerSent = sentence.toLowerCase();
  const idx = lowerSent.indexOf(word);
  const start = Math.max(0, idx - 30);
  const end = Math.min(sentence.length, idx + word.length + 30);
  ctx.textContent = "…" + sentence.slice(start, end) + "…";
  pop.appendChild(ctx);

  const zh = document.createElement("div");
  zh.className = "zh";
  zh.dataset.role = "zh";
  pop.appendChild(zh);

  const partOfSpeech = document.createElement("span");
  partOfSpeech.className = "part-of-speech";
  partOfSpeech.dataset.role = "part-of-speech";
  pop.appendChild(partOfSpeech);

  const pronunciation = document.createElement("div");
  pronunciation.className = "pronunciation";
  pronunciation.dataset.role = "pronunciation";
  pop.appendChild(pronunciation);

  const clues = document.createElement("div");
  clues.className = "memory-clues";
  const wordForm = document.createElement("div");
  wordForm.dataset.role = "word-form";
  const memoryTip = document.createElement("div");
  memoryTip.dataset.role = "memory-tip";
  clues.append(wordForm, memoryTip);
  pop.appendChild(clues);

  const en = document.createElement("div");
  en.className = "en";
  en.dataset.role = "en";
  pop.appendChild(en);

  const loading = document.createElement("div");
  loading.className = "loading";
  loading.dataset.role = "loading";
  loading.textContent = "loading…";
  pop.appendChild(loading);

  return pop;
}

function positionPopover(pop, anchor) {
  const rect = anchor.getBoundingClientRect();
  pop.style.left = Math.min(window.innerWidth - 340, Math.max(8, rect.left)) + "px";
  pop.style.top = (rect.bottom + 6) + "px";
}

function closePopover() {
  if (state.popover) {
    state.popover.remove();
    state.popover = null;
  }
}

async function postWordState(word, action) {
  try {
    const res = await fetch("/api/words/state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paper_id: state.paper.id, word, action }),
    });
    const j = await res.json();
    state.wordStates[word] = j;
    $$(`.word-layer[data-w="${cssEscape(word)}"]`).forEach((el) => {
      el.dataset.status = j.status;
    });
    state.recentClicks.push(Date.now());
  } catch {}
}

async function streamWordLookup(word, sentence, popover) {
  const zh = popover.querySelector("[data-role='zh']");
  const en = popover.querySelector("[data-role='en']");
  const pronunciation = popover.querySelector("[data-role='pronunciation']");
  const partOfSpeech = popover.querySelector("[data-role='part-of-speech']");
  const wordForm = popover.querySelector("[data-role='word-form']");
  const memoryTip = popover.querySelector("[data-role='memory-tip']");
  const loading = popover.querySelector("[data-role='loading']");
  const url = `/api/words/lookup?w=${encodeURIComponent(word)}&s=${encodeURIComponent(sentence)}`;
  let terminal = false;
  try {
    const res = await fetch(url);
    if (!res.ok) {
      loading.textContent = "unavailable";
      return;
    }
    await consumeSSE(res, (evt) => {
      if (evt.type === "replay" || evt.type === "done") {
        terminal = true;
        if (evt.zh) zh.textContent = evt.zh;
        if (evt.en_simple) en.textContent = evt.en_simple;
        if (evt.part_of_speech) partOfSpeech.textContent = evt.part_of_speech;
        if (evt.pronunciation) pronunciation.textContent = evt.pronunciation;
        if (evt.word_form) wordForm.textContent = String(evt.word_form).replace(/^词形\s*[:：]?\s*/i, "");
        if (evt.memory_tip) memoryTip.textContent = evt.memory_tip;
        loading.style.display = "none";
      } else if (evt.type === "delta") {
        loading.textContent = "receiving…";
      } else if (evt.type === "error") {
        terminal = true;
        loading.textContent = "error: " + (evt.message || "");
      }
    });
    if (!terminal) loading.textContent = "interrupted";
  } catch (e) {
    loading.textContent = "unavailable";
  }
}

// ---- Sentence click → help panel ------------------------------------------

function handleSentenceClick(sent, layerEl) {
  removeHelp();
  const help = renderHelpPanel(sent, layerEl);
  document.body.appendChild(help);
  positionHelp(help, layerEl);
  layerEl.classList.add("has-help");
  state.openHelpHash = sent.hash;
  streamSentenceHelp(help, sent.text);
  state.recentClicks.push(Date.now());
}

function removeHelp() {
  $$(".sent-help").forEach((el) => el.remove());
  $$(".sentence-layer").forEach((el) => el.classList.remove("has-help"));
}

function renderHelpPanel(sent, layerEl) {
  const help = document.createElement("details");
  help.className = "sent-help";
  help.dataset.s = sent.hash;
  help.dataset.layer = sent.hash;
  help.open = true;

  const head = document.createElement("div");
  head.className = "sent-help-head";
  head.innerHTML = `<span>Help · ${escapeHtml(truncate(sent.text, 60))}</span><button class="close" aria-label="close">✕</button>`;
  head.querySelector(".close").addEventListener("click", () => {
    help.remove();
    layerEl.classList.remove("has-help");
  });
  help.appendChild(head);

  const sections = [
    { key: "keywords", label: "① Keywords" },
    { key: "structure", label: "② Structure" },
    { key: "en_simple", label: "③ Simple English" },
    { key: "zh", label: "④ 中文" },
  ];
  for (const sec of sections) {
    const ds = document.createElement("details");
    ds.className = "sent-help-section";
    ds.dataset.field = sec.key;
    const sum = document.createElement("summary");
    sum.textContent = sec.label;
    ds.appendChild(sum);
    const body = document.createElement("div");
    body.className = sec.key === "zh" ? "zh" : (sec.key === "keywords" ? "keywords" : "body");
    body.dataset.role = "body";
    body.textContent = "…";
    ds.appendChild(body);
    help.appendChild(ds);
  }

  const status = document.createElement("div");
  status.className = "sent-help-status";
  status.dataset.role = "status";
  status.textContent = "loading…";
  help.appendChild(status);

  return help;
}

function positionHelp(help, layerEl) {
  const rect = layerEl.getBoundingClientRect();
  const containerRect = $("#page-container").getBoundingClientRect();
  // Position below the layer, anchored to container if room
  const top = rect.bottom + 6;
  help.style.left = Math.max(8, containerRect.left + 8) + "px";
  help.style.top = top + "px";
  help.style.width = Math.min(640, containerRect.width - 16) + "px";
}

function fillHelpFields(help, payload) {
  if (payload.keywords && payload.keywords.length) {
    const k = help.querySelector(`[data-field="keywords"] .keywords`);
    if (k) {
      k.innerHTML = "";
      for (const w of payload.keywords) {
        const span = document.createElement("span");
        span.className = "kw";
        span.textContent = w;
        k.appendChild(span);
      }
    }
  }
  const map = { structure: "body", en_simple: "body", zh: "zh" };
  for (const [field, sel] of Object.entries(map)) {
    if (payload[field]) {
      const el = help.querySelector(`[data-field="${field}"] .${sel}`);
      if (el) el.textContent = payload[field];
    }
  }
}

async function streamSentenceHelp(help, sentence) {
  const status = help.querySelector("[data-role='status']");
  let terminal = false;
  try {
    const res = await fetch("/api/sentences/help", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        paper_id: state.paper.id,
        sentence,
        page_num: state.pageNum,
      }),
    });
    if (!res.ok) {
      status.textContent = "unavailable";
      return;
    }
    await consumeSSE(res, (evt) => {
      if (evt.type === "replay" || evt.type === "done") {
        terminal = true;
        fillHelpFields(help, evt);
        status.textContent = "done";
      } else if (evt.type === "delta") {
        status.textContent = "receiving…";
      } else if (evt.type === "error") {
        terminal = true;
        status.textContent = "error";
      }
    });
    if (!terminal) status.textContent = "interrupted";
  } catch {
    status.textContent = "unavailable";
  }
}

// ---- SSE helper -----------------------------------------------------------

async function consumeSSE(res, onEvent) {
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const evt = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of evt.split("\n")) {
        if (!line.startsWith("data:")) continue;
        let p;
        try { p = JSON.parse(line.slice(5).trim()); } catch { continue; }
        onEvent(p);
      }
    }
  }
}

// ---- Translation strip ----------------------------------------------------

function renderTranslationStrip() {
  const old = $(".translate-strip");
  if (old) old.remove();
  if (!state.translateOpen || !state.page) return;

  const strip = document.createElement("div");
  strip.className = "translate-strip";
  const heading = document.createElement("h4");
  heading.textContent = "中文翻译 · Page " + state.pageNum;
  strip.appendChild(heading);

  const status = document.createElement("div");
  status.className = "status";
  status.dataset.role = "translate-status";
  strip.appendChild(status);

  const exportActions = document.createElement("div");
  exportActions.className = "export-actions";
  const partial = document.createElement("button");
  partial.className = "ghostbtn";
  partial.textContent = "导出已翻译内容";
  partial.disabled = !state.blockTranslations.some(Boolean);
  partial.addEventListener("click", () => exportCurrentPage("partial", partial));
  const full = document.createElement("button");
  full.className = "ghostbtn";
  full.textContent = "翻译整份 PDF 后导出";
  full.addEventListener("click", () => exportCurrentPage("full", full));
  exportActions.append(partial, full);
  strip.appendChild(exportActions);

  for (let i = 0; i < state.page.blocks.length; i++) {
    const div = document.createElement("div");
    div.className = "zh-block";
    div.dataset.blockIdx = i;
    div.textContent = state.blockTranslations[i] || "";
    strip.appendChild(div);
  }
  $(".reader").insertBefore(strip, $(".pager"));
  updateTranslationStripStatus();
}

function updateTranslationStripStatus() {
  const s = $("[data-role='translate-status']");
  if (!s || !state.page) return;
  const total = state.page.blocks.length;
  const done = state.blockTranslations.filter(Boolean).length;
  if (state.translationStatus === "done") s.textContent = `Done · ${done}/${total}`;
  else if (state.translationStatus === "streaming") s.textContent = `Streaming… ${done}/${total}`;
  else if (state.translationStatus === "error") s.textContent = `Error — click 译文 to retry`;
  else s.textContent = `Click 译文 to translate`;
}

function updateExportButtons() {
  const partial = $("#export-partial-btn");
  if (partial) partial.disabled = !state.blockTranslations.some(Boolean);
}

async function startTranslationStream() {
  if (state.translationStatus === "done") return;
  if (state.translationStream) return;
  state.translationStatus = "streaming";
  updateTranslationStripStatus();

  const url = `/api/papers/${state.paper.id}/page/${state.pageNum}/translate`;
  const ctrl = new AbortController();
  let terminal = false;
  state.translationStream = { controller: ctrl };

  try {
    const res = await fetch(url, { signal: ctrl.signal });
    if (!res.ok) {
      state.translationStatus = "error";
      updateTranslationStripStatus();
      return;
    }
    await consumeSSE(res, (evt) => {
      if (evt.type === "blocks") {
        state.blockTranslations = evt.zh;
        const divs = $(".translate-strip")?.querySelectorAll(".zh-block");
        if (divs) {
          for (let i = 0; i < divs.length; i++) divs[i].textContent = state.blockTranslations[i] || "";
        }
        updateExportButtons();
        updateTranslationStripStatus();
        updateExportButtons();
      } else if (evt.type === "replay") {
        terminal = true;
        state.blockTranslations = evt.zh || [];
        state.translationStatus = "done";
        const divs = $(".translate-strip")?.querySelectorAll(".zh-block");
        if (divs) {
          for (let i = 0; i < divs.length; i++) divs[i].textContent = state.blockTranslations[i] || "";
        }
        updateTranslationStripStatus();
      } else if (evt.type === "done") {
        terminal = true;
        state.translationStatus = "done";
        updateTranslationStripStatus();
        updateExportButtons();
      } else if (evt.type === "error") {
        terminal = true;
        state.translationStatus = "error";
        updateTranslationStripStatus();
      }
    });
    if (!terminal) {
      state.translationStatus = "error";
      updateTranslationStripStatus();
    }
    state.translationStream = null;
  } catch (e) {
    if (e.name !== "AbortError") {
      state.translationStatus = "error";
      updateTranslationStripStatus();
    }
    state.translationStream = null;
  }
}

async function exportCurrentPage(mode, button) {
  if (!state.page) return;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = mode === "full" ? "正在翻译整份 PDF 并导出…" : "正在导出…";
  try {
    const res = await fetch(`/api/papers/${state.paper.id}/page/${state.pageNum}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Export failed");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = mode === "partial"
      ? `${state.paper.id}-translated-pages.pdf`
      : `${state.paper.id}-full-translated.pdf`;
    link.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert(e.message || "导出失败");
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

// ---- Section hint --------------------------------------------------------

function maybeShowSectionHint() {
  if (!state.paper) return;
  const cur = state.paper.sections.find(
    (s) => state.pageNum >= s.start_page && state.pageNum <= s.end_page
  );
  if (!cur) {
    $("#section-hint").hidden = true;
    return;
  }
  if (cur.hint_zh) {
    showSectionHint(cur);
  } else if (!state.sectionHintShown[cur.id]) {
    state.sectionHintShown[cur.id] = true;
    streamSectionHint(cur.id);
  }
}

function showSectionHint(section) {
  const hint = $("#section-hint");
  hint.hidden = false;
  hint.querySelector(".section-hint-label").textContent = section.title + " —";
  hint.querySelector(".section-hint-body").textContent = section.hint_zh || "…";
}

async function streamSectionHint(sectionId) {
  const url = `/api/papers/${state.paper.id}/section/${sectionId}/hint`;
  try {
    const res = await fetch(url);
    if (!res.ok) return;
    let acc = "";
    await consumeSSE(res, (evt) => {
      if (evt.type === "delta") {
        // For section hint, content is raw text (not JSON-escaped), just append
        acc += evt.content || "";
        const cur = state.paper.sections.find((s) => s.id === sectionId);
        if (cur) {
          cur.hint_zh = acc;
          showSectionHint(cur);
        }
      } else if (evt.type === "replay") {
        acc = evt.zh || "";
        const cur = state.paper.sections.find((s) => s.id === sectionId);
        if (cur) {
          cur.hint_zh = acc;
          cur.hint_status = "done";
          showSectionHint(cur);
        }
      } else if (evt.type === "done") {
        if (evt.zh) acc = evt.zh;
        const cur = state.paper.sections.find((s) => s.id === sectionId);
        if (cur) {
          cur.hint_zh = acc;
          cur.hint_status = "done";
          showSectionHint(cur);
        }
        renderTOC();
      }
    });
  } catch {}
}

// ---- Flow tracking --------------------------------------------------------

let flowBannerEl = null;
function startFlowTracking() {
  setInterval(() => {
    const cutoff = Date.now() - 5 * 60 * 1000;
    state.recentClicks = state.recentClicks.filter((t) => t >= cutoff);
    const rate = state.recentClicks.length / 5;
    if (rate >= 6 && !state.translateOpen) showFlowBanner();
  }, 30000);
}

function showFlowBanner() {
  if (flowBannerEl) return;
  flowBannerEl = document.createElement("div");
  flowBannerEl.className = "flow-banner";
  flowBannerEl.innerHTML = `Reading seems dense. Open the Chinese for this page? <button>Open</button>`;
  flowBannerEl.querySelector("button").addEventListener("click", () => {
    state.translateOpen = true;
    $("#translate-btn").dataset.active = "true";
    startTranslationStream();
    renderTranslationStrip();
    hideFlowBanner();
  });
  document.body.appendChild(flowBannerEl);
  requestAnimationFrame(() => flowBannerEl.dataset.show = "true");
  setTimeout(hideFlowBanner, 8000);
}
function hideFlowBanner() {
  if (!flowBannerEl) return;
  flowBannerEl.dataset.show = "false";
  setTimeout(() => {
    if (flowBannerEl) {
      flowBannerEl.remove();
      flowBannerEl = null;
    }
  }, 250);
}

// ---- Util -----------------------------------------------------------------

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function cssEscape(s) {
  if (window.CSS && CSS.escape) return CSS.escape(s);
  return s.replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c);
}
function truncate(s, n) {
  return s.length <= n ? s : s.slice(0, n) + "…";
}

init();
