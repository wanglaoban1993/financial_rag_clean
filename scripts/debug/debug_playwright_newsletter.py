import email as em
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml
from config_loader import load_config

from ingestion.email_fetcher import QQEmailFetcher, sync_playwright, PlaywrightTimeoutError


cfg = load_config(strict=True)

fetcher = QQEmailFetcher(cfg["email"])
mail = fetcher.connect()
mail.select("INBOX")
_, data = mail.search(None, f'(FROM "{cfg["email"]["target_sender"]}")')
num = data[0].split()[-1]
_, msg_data = mail.fetch(num, "(RFC822)")
msg = em.message_from_bytes(msg_data[0][1])
_, html_body = fetcher._extract_body_and_html(msg)
anchors = fetcher._prioritize_anchors(fetcher._extract_html_anchors(html_body))
target_url = anchors[0]["url"]
mail.logout()

print("target_url", target_url)
print("playwright_available", bool(sync_playwright))

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(locale="de-DE")
    page = context.new_page()
    response = page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeoutError:
        pass

    print("final_url", page.url)
    print("response_status", response.status if response else None)
    print("title", page.title())

    html = page.content()
    print("html_len", len(html))

    patterns = [
        r"als\s+pdf\s+lesen",
        r"highs?\s*&\s*lows?",
        r"highlights?",
        r"newsletter",
        r"pdf",
        r"dax",
        r"nvidia",
    ]
    lowered = html.lower()
    for pattern in patterns:
        print(pattern, bool(re.search(pattern, lowered, re.IGNORECASE)))

    text = page.locator("body").inner_text(timeout=5000)
    print("text_preview", text[:3000].replace("\n", " "))

    print("frames", len(page.frames))
    for frame in page.frames:
        print("frame_url", frame.url)
        frame_name = getattr(frame, "name", None)
        if callable(frame_name):
            try:
                print("frame_name", frame_name())
            except Exception:
                pass

        try:
            frame_text = frame.locator("body").inner_text(timeout=3000)
        except Exception:
            frame_text = ""
        if frame_text:
            print("frame_text_preview", frame_text[:1500].replace("\n", " "))

        frame_hits = []
        try:
            link_locator = frame.locator("a")
            for idx in range(min(link_locator.count(), 120)):
                link = link_locator.nth(idx)
                try:
                    label = re.sub(r"\s+", " ", link.inner_text(timeout=1000)).strip()
                except Exception:
                    label = ""
                try:
                    href = link.evaluate("el => el.href")
                except Exception:
                    href = None
                if re.search(r"als\s+pdf\s+lesen|highs?|lows?|highlight|newsletter|pdf|dax|nvidia", label, re.IGNORECASE):
                    frame_hits.append((label, href))
        except Exception:
            pass

        try:
            button_locator = frame.locator("button")
            for idx in range(min(button_locator.count(), 60)):
                button = button_locator.nth(idx)
                try:
                    label = re.sub(r"\s+", " ", button.inner_text(timeout=1000)).strip()
                except Exception:
                    label = ""
                if re.search(r"als\s+pdf\s+lesen|highs?|lows?|highlight|newsletter|pdf|dax|nvidia", label, re.IGNORECASE):
                    frame_hits.append((label, None))
        except Exception:
            pass

        print("frame_hits", len(frame_hits))
        for label, href in frame_hits[:30]:
            print("frame_label=", label)
            print("frame_href=", href)

    for selector in ["iframe", "script", "a", "button"]:
        print(selector, page.locator(selector).count())

    iframes = page.locator("iframe")
    for idx in range(min(iframes.count(), 10)):
        frame = iframes.nth(idx)
        print("iframe_src", frame.get_attribute("src"))

    scripts = page.locator("script")
    script_hits = []
    for idx in range(min(scripts.count(), 40)):
        try:
            script_text = scripts.nth(idx).inner_text(timeout=1000)
        except Exception:
            continue
        if re.search(r"dax|nvidia|pdf|daily.?trader|graphql|api|json", script_text, re.IGNORECASE):
            script_hits.append(re.sub(r"\s+", " ", script_text)[:500])
    print("script_hits", len(script_hits))
    for hit in script_hits[:10]:
        print("script_hit", hit)

    candidates = []
    for locator in [page.locator("a"), page.locator("button")]:
        count = locator.count()
        for idx in range(min(count, 120)):
            element = locator.nth(idx)
            try:
                label = re.sub(r"\s+", " ", element.inner_text(timeout=1000)).strip()
            except Exception:
                label = ""
            try:
                href = element.get_attribute("href", timeout=1000)
            except Exception:
                href = None
            if re.search(r"als\s+pdf\s+lesen|highs?|lows?|highlight|newsletter|pdf", label, re.IGNORECASE):
                candidates.append((label, href))

    print("candidates")
    for label, href in candidates:
        print("label=", label)
        print("href=", href)

    browser.close()