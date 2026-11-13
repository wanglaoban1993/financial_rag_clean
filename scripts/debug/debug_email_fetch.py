import email as em
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml
from config_loader import load_config
from ingestion.email_fetcher import QQEmailFetcher

cfg = load_config(strict=True)

fetcher = QQEmailFetcher(cfg["email"])
mail = fetcher.connect()
mail.select("INBOX")
status, data = mail.search(None, f'(FROM "{cfg["email"]["target_sender"]}")')
nums = data[0].split()
print("emails", len(nums))
if nums:
    num = nums[-1]
    _, msg_data = mail.fetch(num, "(RFC822)")
    msg = em.message_from_bytes(msg_data[0][1])
    subject = fetcher._decode_header(msg.get("Subject"))
    body, html_body = fetcher._extract_body_and_html(msg)
    message_id = (msg.get("Message-ID") or "").strip()
    bundle_path, assets_dir = fetcher._prepare_newsletter_paths(subject, message_id, body)
    pdf_paths, landing_texts = fetcher._download_pdfs(msg, body, html_body, assets_dir=assets_dir)

    if landing_texts:
        extra_sections = [text for text in landing_texts if text and text not in body]
        if extra_sections:
            body = f"{body}\n\n" + "\n\n".join(extra_sections) if body else "\n\n".join(extra_sections)

    newsletter_path = fetcher._save_newsletter_markdown(subject, body, bundle_path)

    print("subject", subject)
    print("body_len", len(body))
    print("body_preview", body[:2000].replace("\n", " "))
    print("pdf_paths", pdf_paths)
    print("newsletter_path", newsletter_path)

    links = fetcher._extract_html_links(html_body)
    print("html_links", len(links))
    for idx, link in enumerate(links[:20], 1):
        print(f"link_{idx}", link)
mail.logout()
