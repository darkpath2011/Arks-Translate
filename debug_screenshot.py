"""Debug v2: capture console + network for arks reader."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    page.on("console", lambda msg: print(f"CONSOLE [{msg.type}]: {msg.text}"))
    page.on("pageerror", lambda err: print(f"PAGE ERROR: {err}"))
    page.on("response", lambda r: print(f"  -> {r.status} {r.url}") if "/api/" in r.url or "/static/" in r.url else None)
    page.on("requestfailed", lambda r: print(f"  XX FAIL {r.url} -> {r.failure}"))

    page.goto("http://127.0.0.1:8912/")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    print("\n--- DOM ---")
    info = page.evaluate("""() => ({
      title: document.title,
      paperTitle: document.getElementById('paper-title')?.textContent,
      pageContainerDisplay: document.getElementById('page-container')?.style.display,
      pageImageSrc: document.getElementById('page-image')?.src,
      pageImageNatural: [document.getElementById('page-image')?.naturalWidth, document.getElementById('page-image')?.naturalHeight],
      pageImageComplete: document.getElementById('page-image')?.complete,
      n_sentences: document.querySelectorAll('.sentence-layer').length,
      n_words: document.querySelectorAll('.word-layer').length,
    })""")
    print(info)

    browser.close()