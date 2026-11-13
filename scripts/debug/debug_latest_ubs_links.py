import email as em
import re
import html as html_lib
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
if not nums:
    print("no emails")
    raise SystemExit(0)

num = nums[-1]
_, msg_data = mail.fetch(num, "(RFC822)")
msg = em.message_from_bytes(msg_data[0][1])
subject = fetcher._decode_header(msg.get("Subject"))
body, html_body = fetcher._extract_body_and_html(msg)
print("subject", subject)
print("body_len", len(body))
print("body_preview", body[:1000].replace("\n", " "))

anchors = re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html_body, re.IGNORECASE | re.DOTALL)
print("anchors", len(anchors))
for idx, (href, text) in enumerate(anchors, 1):
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_lib.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    href = fetcher._clean_url(href)
    print(f"{idx:02d}. text={text[:120]!r}")
    print(f"    href={href}")
mail.logout()
