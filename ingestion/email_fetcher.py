import imaplib
import email
import re
import time
import html
import hashlib
import requests
from datetime import datetime, timedelta
from pathlib import Path
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

from lxml import html as lxml_html

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
    PlaywrightTimeoutError = Exception


class _FetchedResponse:
    def __init__(self, url: str, headers: dict, content: bytes, text: str = ""):
        self.url = url
        self.headers = headers
        self.content = content
        self.text = text

    def raise_for_status(self):
        return None

class QQEmailFetcher:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.pdf_dir = Path("data/pdfs")
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        self.newsletter_dir = Path(self.cfg.get("newsletter_output_dir", "data/newsletters"))
        self.newsletter_dir.mkdir(parents=True, exist_ok=True)
        self.max_urls_per_email = int(self.cfg.get("max_urls_per_email", 20))
        self.max_nested_urls_per_link = int(self.cfg.get("max_nested_urls_per_link", 20))
        self.request_timeout = self.cfg.get("request_timeout", (6, 18))
        self.min_page_text_chars = int(self.cfg.get("min_page_text_chars", 200))
        self.browser_fallback_enabled = bool(self.cfg.get("browser_fallback_enabled", True))
        self.browser_timeout_ms = int(self.cfg.get("browser_timeout_ms", 45000))
        self.download_linked_pdfs = bool(self.cfg.get("download_linked_pdfs", False))
        # When target_sender is a forwarder, only keep messages whose body still shows
        # this original sender (e.g. in a "-------- Forwarded message --------" block
        # or footer/unsubscribe text). Empty disables this filter.
        self.forwarded_from_marker = (self.cfg.get("forwarded_from_marker") or "").strip().lower()
        # Only fetch messages received within this many days. 0/empty disables the cutoff.
        self.max_email_age_days = int(self.cfg.get("max_email_age_days", 0) or 0)
        # Message-IDs seen in this process that failed the forwarded_from_marker filter.
        self._rejected_message_ids: set[str] = set()

    def connect(self):
        mail = imaplib.IMAP4_SSL(self.cfg["imap_server"], self.cfg["port"])
        mail.login(self.cfg["username"], self.cfg["password"])
        return mail

    def _connect_with_retry(self, attempts: int = 3, base_delay_s: float = 30.0):
        """Gmail answers "Too many simultaneous connections" (and the odd network
        blip) transiently; retry a few times with growing pauses before giving up."""
        for attempt in range(1, attempts + 1):
            try:
                return self.connect()
            except (imaplib.IMAP4.error, OSError) as e:
                if attempt == attempts:
                    raise
                delay = base_delay_s * attempt
                print(f"[WARN] IMAP connect failed ({e}); retry {attempt}/{attempts - 1} in {delay:.0f}s")
                time.sleep(delay)

    @staticmethod
    def _message_id_from_header(raw_header: bytes) -> str:
        return (email.message_from_bytes(raw_header).get("Message-ID") or "").strip()

    def fetch_new_emails(self, skip_message_ids: Optional[set[str]] = None) -> list[dict]:
        """返回来自目标发件人的新邮件列表

        skip_message_ids: Message-IDs the caller has already ingested. They are
        recognised from the headers alone, so a message is never downloaded or
        re-processed twice. This is what makes include_seen=True safe: reading a
        mail elsewhere (phone, webmail) can't hide it from us, and a mail whose
        processing failed is simply picked up again on the next run."""
        skip_message_ids = skip_message_ids or set()
        mail = self._connect_with_retry()
        raw_messages: list[bytes] = []
        sender = self.cfg["target_sender"]
        include_seen = bool(self.cfg.get("include_seen", False))
        mark_as_seen = bool(self.cfg.get("mark_as_seen", True))

        try:
            mail.select("INBOX")

            since_clause = ""
            if self.max_email_age_days > 0:
                cutoff = datetime.now() - timedelta(days=self.max_email_age_days)
                since_clause = f' SINCE "{cutoff.strftime("%d-%b-%Y")}"'

            # 默认只拉取未读；调试时可配置 include_seen=True 拉取全部。
            if include_seen:
                status, data = mail.search(None, f'(FROM "{sender}"{since_clause})')
            else:
                status, data = mail.search(None, f'(UNSEEN FROM "{sender}"{since_clause})')

            for num in data[0].split():
                _, head = mail.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                if head and head[0] and len(head[0]) > 1:
                    known = self._message_id_from_header(head[0][1])
                    if known and (known in skip_message_ids or known in self._rejected_message_ids):
                        continue
                # PEEK: fetching must not itself flip the \Seen flag; mark_as_seen decides that.
                _, msg_data = mail.fetch(num, "(BODY.PEEK[])")
                if msg_data and msg_data[0] and len(msg_data[0]) > 1:
                    raw_messages.append(msg_data[0][1])
                if mark_as_seen:
                    try:
                        mail.store(num, "+FLAGS", "\\Seen")
                    except imaplib.IMAP4.abort as exc:
                        print(f"[WARN] failed to mark email as seen before disconnect: {exc}")
                        break
        finally:
            try:
                mail.logout()
            except Exception:
                pass

        results = []
        for raw_message in raw_messages:
            msg = email.message_from_bytes(raw_message)

            subject = self._decode_header(msg["Subject"])
            body, html_body = self._extract_body_and_html(msg)

            if self.forwarded_from_marker:
                haystack = f"{body}\n{html_body}".lower()
                if self.forwarded_from_marker not in haystack:
                    # Not one of the forwards we want; remember so a seen-inclusive
                    # search doesn't download it again on every run.
                    rejected = (msg.get("Message-ID") or "").strip()
                    if rejected:
                        self._rejected_message_ids.add(rejected)
                    continue

            message_id = (msg.get("Message-ID") or "").strip()
            email_date = self._parse_email_date(msg.get("Date"))
            bundle_path, assets_dir = self._prepare_newsletter_paths(subject, message_id, body)
            pdf_paths, landing_texts = self._download_pdfs(msg, body, html_body, assets_dir=assets_dir)
            if landing_texts:
                extra_sections = [text for text in landing_texts if text and text not in body]
                if extra_sections:
                    body = f"{body}\n\n" + "\n\n".join(extra_sections) if body else "\n\n".join(extra_sections)
            newsletter_path = self._save_newsletter_markdown(subject, body, bundle_path)

            results.append({
                "subject": subject,
                "from": sender,
                "body": body,
                "message_id": message_id,
                "date": email_date,
                "pdf_paths": pdf_paths,
                "newsletter_path": newsletter_path,
                "source": "email"
            })

        return results

    def _decode_header(self, value: str) -> str:
        if not value:
            return ""
        decoded, enc = decode_header(value)[0]
        if isinstance(decoded, bytes):
            return decoded.decode(enc or "utf-8", errors="ignore")
        return decoded

    def _parse_email_date(self, raw_date: str | None) -> str:
        """Parse the RFC 2822 'Date' header into an ISO date string. Empty if unparseable."""
        if not raw_date:
            return ""
        try:
            return parsedate_to_datetime(raw_date).date().isoformat()
        except (TypeError, ValueError):
            return ""

    def _prepare_newsletter_paths(self, subject: str, message_id: str, body: str) -> tuple[Path, Path]:
        stable_key = message_id or hashlib.sha1(f"{subject}|{body[:2000]}".encode("utf-8")).hexdigest()
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", subject).strip("_")[:80] or "ubs_dailytrader"
        stem = f"{datetime.now():%Y%m%d}_{safe_name}_{hashlib.sha1(stable_key.encode('utf-8')).hexdigest()[:10]}"
        bundle_path = self.newsletter_dir / f"{stem}.md"
        assets_dir = self.newsletter_dir / f"{stem}_assets"
        return bundle_path, assets_dir

    def _save_newsletter_markdown(self, subject: str, body: str, bundle_path: Path) -> Optional[str]:
        if not body or "ubs" not in body.lower() or "dailytrader" not in body.lower():
            return None

        markdown = (
            f"# {subject}\n\n"
            f"- Source: UBS DailyTrader newsletter\n"
            f"- Collected: {datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
            f"---\n\n{body.strip()}\n"
        )
        bundle_path.write_text(markdown, encoding="utf-8")
        return str(bundle_path)

    def _decode_part_payload(self, part) -> str:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""

        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="ignore")
        except LookupError:
            return payload.decode("utf-8", errors="ignore")

    def _cleanup_plain_text(self, text: str) -> str:
        if not text:
            return ""
        cleaned = html.unescape(text)
        cleaned = cleaned.replace("\r", "\n")
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    def _html_to_text(self, html_body: str) -> str:
        if not html_body:
            return ""

        text = html_body
        # Drop script/style blocks before removing tags to avoid CSS/JS noise.
        text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)

        # Preserve structural separators for better chunking.
        text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</\s*(p|div|li|tr|h[1-6])\s*>", "\n", text, flags=re.IGNORECASE)

        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)

        # Remove common CSS artifact lines that can dominate embeddings.
        lines = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if re.search(r"\{\s*[^}]*:\s*[^}]*\}", line):
                continue
            if re.search(r"\.(woff2?|ttf|eot|otf)\b", line, re.IGNORECASE):
                continue
            lines.append(line)

        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()

    def _extract_body_and_html(self, msg) -> tuple[str, str]:
        text_parts = []
        html_parts = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", "")).lower()
                if "attachment" in content_disposition:
                    continue

                decoded_text = self._decode_part_payload(part)
                if not decoded_text:
                    continue

                if content_type == "text/plain":
                    text_parts.append(decoded_text)
                elif content_type == "text/html":
                    html_parts.append(decoded_text)
        else:
            content_type = msg.get_content_type()
            decoded_text = self._decode_part_payload(msg)
            if content_type == "text/html":
                html_parts.append(decoded_text)
            else:
                text_parts.append(decoded_text)

        body = self._cleanup_plain_text("\n".join(text_parts))
        html_body = "\n".join(html_parts).strip()

        # HTML often contains the only meaningful content in newsletter emails.
        html_text = self._html_to_text(html_body)
        if body and html_text:
            body = f"{body}\n\n{html_text}" if html_text not in body else body
        elif not body and html_text:
            body = html_text

        return body, html_body

    def _extract_html_links(self, html_body: str) -> list[str]:
        if not html_body:
            return []
        links = re.findall(r'href=["\'](https?://[^"\']+)["\']', html_body, re.IGNORECASE)
        return [self._clean_url(link) for link in links if link]

    def _extract_html_anchors(self, html_body: str) -> list[dict[str, str]]:
        if not html_body:
            return []

        anchors = []
        for href, raw_text in re.findall(
            r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            html_body,
            re.IGNORECASE | re.DOTALL,
        ):
            text = re.sub(r"<[^>]+>", " ", raw_text)
            text = html.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
            url = self._clean_url(href)
            if url:
                anchors.append({"url": url, "text": text})
        return anchors

    def _clean_url(self, url: str) -> str:
        return url.strip().strip('"\'()[]<>.,;')

    def _guess_pdf_filename(self, original_url: str, final_url: str, headers: dict) -> str:
        content_disposition = headers.get("Content-Disposition", "")
        match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", content_disposition, re.IGNORECASE)
        if match:
            filename = unquote(match.group(1)).strip()
        else:
            final_name = Path(urlparse(final_url).path).name
            original_name = Path(urlparse(original_url).path).name
            filename = final_name or original_name or "downloaded_report.pdf"

        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"

        # 清理Windows非法文件名字符
        filename = re.sub(r'[\\/:*?"<>|]', "_", filename)
        return filename

    def _request_url(self, url: str):
        headers = {
            "Accept": "application/pdf,application/json,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://www.ubs.com/",
        }
        if curl_requests is not None:
            # Browser TLS/HTTP fingerprint impersonation — plain `requests` gets
            # blocked (403) by UBS's campaign-link WAF even with a spoofed
            # User-Agent, since the TLS handshake itself still looks like a bot.
            return curl_requests.get(
                url,
                timeout=self.request_timeout,
                allow_redirects=True,
                headers=headers,
                impersonate="chrome",
            )
        return requests.get(
            url,
            timeout=self.request_timeout,
            allow_redirects=True,
            headers={**headers, "User-Agent": "Mozilla/5.0"},
        )

    def _should_use_browser_fallback(self, url: str, exc: Exception) -> bool:
        if not self.browser_fallback_enabled or sync_playwright is None:
            return False
        if "secure.ubs.com/campaign/r/" not in url.lower():
            return False

        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        exc_text = str(exc)
        return status_code in {403, 404} or "403" in exc_text or "404" in exc_text

    def _should_prefer_browser(self, url: str) -> bool:
        if not self.browser_fallback_enabled or sync_playwright is None:
            return False

        lowered = url.lower()
        return (
            "secure.ubs.com/campaign/r/" in lowered
            or "keyinvest-de.ubs.com/daily-trader" in lowered
            or "de.keyinvest-newsletter.net/" in lowered
        )

    def _request_url_with_browser(self, url: str) -> Optional[_FetchedResponse]:
        if sync_playwright is None:
            return None

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    locale="de-DE",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/127.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=self.browser_timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeoutError:
                    pass

                final_url = page.url
                headers = dict(response.headers) if response is not None else {}
                content_type = headers.get("content-type", "")

                if "application/pdf" in content_type.lower() and response is not None:
                    content = response.body()
                    text = ""
                else:
                    frame_fragments = [page.content()]
                    for frame in page.frames:
                        if frame == page.main_frame:
                            continue
                        try:
                            frame_html = frame.content()
                        except Exception:
                            frame_html = ""
                        if frame_html:
                            frame_fragments.append(frame_html)

                        synthetic_anchors = []
                        try:
                            frame_links = frame.locator("a")
                            for idx in range(min(frame_links.count(), 200)):
                                link = frame_links.nth(idx)
                                try:
                                    label = re.sub(r"\s+", " ", link.inner_text(timeout=500)).strip()
                                except Exception:
                                    label = ""
                                try:
                                    href = link.evaluate("el => el.href")
                                except Exception:
                                    href = None
                                if href:
                                    synthetic_anchors.append((href, label))
                        except Exception:
                            pass

                        try:
                            frame_buttons = frame.locator("button")
                            for idx in range(min(frame_buttons.count(), 100)):
                                button = frame_buttons.nth(idx)
                                try:
                                    label = re.sub(r"\s+", " ", button.inner_text(timeout=500)).strip()
                                except Exception:
                                    label = ""
                                try:
                                    onclick = button.get_attribute("onclick", timeout=500)
                                except Exception:
                                    onclick = None
                                if onclick and re.search(r"https?://|/", onclick):
                                    synthetic_anchors.append((onclick, label))
                        except Exception:
                            pass

                        if synthetic_anchors:
                            frame_fragments.append(
                                "\n".join(
                                    f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'
                                    for href, label in synthetic_anchors
                                )
                            )

                        try:
                            frame_text = frame.locator("body").inner_text(timeout=1000)
                        except Exception:
                            frame_text = ""
                        if frame_text:
                            frame_fragments.append(frame_text)

                    text = "\n".join(fragment for fragment in frame_fragments if fragment)
                    content = text.encode("utf-8", errors="ignore")

                browser.close()
                return _FetchedResponse(final_url, headers, content, text)
        except Exception as exc:
            print(f"Browser fallback failed: {url} — {exc}")
            return None

    def _fetch_response(self, url: str):
        if self._should_prefer_browser(url):
            browser_resp = self._request_url_with_browser(url)
            if browser_resp is not None:
                return browser_resp

        try:
            resp = self._request_url(url)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            browser_resp = self._request_url_with_browser(url) if self._should_use_browser_fallback(url, exc) else None
            if browser_resp is not None:
                return browser_resp
            raise

    def _looks_like_meaningful_page_text(self, text: str) -> bool:
        if not text:
            return False
        cleaned = re.sub(r"https?://\S+", " ", text)
        return len(cleaned.strip()) >= self.min_page_text_chars

    def _anchor_score(self, url: str, text: str) -> int:
        haystack = f"{text} {url}".lower()
        score = 0
        if self._is_topic_url(url):
            score += 200
        if "highs" in haystack or "lows" in haystack or "lowlights" in haystack or "highlights" in haystack:
            score += 110
        if "newsletter" in haystack or "zum newsletter" in haystack:
            score += 100
        if "web-browser" in haystack or "browser" in haystack:
            score += 95
        if "daily trader" in haystack:
            score += 40
        if ".pdf" in haystack:
            score += 30
        if "campaign/r/" in haystack:
            score += 10
        return score

    def _is_topic_url(self, url: str) -> bool:
        return bool(
            re.match(
                r"^https://de\.keyinvest-newsletter\.net/\d{4}/\d{2}/\d{2}/[^/?#]+/?(?:[?#].*)?$",
                url,
                re.IGNORECASE,
            )
        )

    def _select_topic_anchors(self, anchors: list[dict[str, str]]) -> list[dict[str, str]]:
        candidates = []
        dates = []
        date_pattern = re.compile(
            r"^https://de\.keyinvest-newsletter\.net/(\d{4}/\d{2}/\d{2})/",
            re.IGNORECASE,
        )
        for anchor in self._prioritize_anchors(anchors):
            url = anchor.get("url", "").split("#", 1)[0]
            if not self._is_topic_url(url):
                continue
            match = date_pattern.match(url)
            if match:
                dates.append(match.group(1))
            candidates.append({**anchor, "url": url})

        if not candidates:
            return []

        publication_date = max(set(dates), key=dates.count) if dates else None
        topics = []
        seen_urls = set()
        for anchor in candidates:
            url = anchor.get("url", "")
            if url in seen_urls or (publication_date and f"/{publication_date}/" not in url):
                continue
            seen_urls.add(url)
            topics.append(anchor)
        return topics

    def _extract_image_urls(self, html_body: str, page_url: str) -> list[str]:
        image_urls = []
        for raw_url in re.findall(r'<img\b[^>]*\bsrc=["\']([^"\']+)', html_body, re.IGNORECASE):
            cleaned = self._clean_url(urljoin(page_url, raw_url))
            if cleaned.startswith("http"):
                image_urls.append(cleaned)
        return list(dict.fromkeys(image_urls))

    def _to_markdown_table(self, table_element) -> str:
        rows = []
        for row in table_element.xpath(".//tr"):
            cells = [
                re.sub(r"\s+", " ", cell.text_content()).strip()
                for cell in row.xpath("./th|./td")
            ]
            if any(cells):
                rows.append(cells)

        if not rows:
            return ""

        width = max(len(row) for row in rows)
        normalized = [row + [""] * (width - len(row)) for row in rows]
        header = normalized[0]
        separator = ["---"] * width
        body_rows = normalized[1:] if len(normalized) > 1 else []

        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(separator) + " |",
        ]
        for row in body_rows:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

    def _download_image_asset(
        self,
        image_url: str,
        assets_dir: Path,
        used_names: set[str],
        referer_url: str,
    ) -> Optional[str]:
        image_url = html.unescape(image_url)

        content = b""
        try:
            response = requests.get(
                image_url,
                timeout=self.request_timeout,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": referer_url,
                },
            )
            response.raise_for_status()
            content = response.content
        except Exception:
            try:
                browser_response = self._fetch_response(image_url)
                content = browser_response.content or b""
            except Exception:
                return None

        if not content:
            return None

        raw_name = Path(urlparse(image_url).path).name or "image"
        raw_name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name)
        if "." not in raw_name:
            raw_name = f"{raw_name}.png"

        base = Path(raw_name).stem
        suffix = Path(raw_name).suffix or ".png"
        filename = f"{base}{suffix}"
        counter = 2
        while filename in used_names:
            filename = f"{base}_{counter}{suffix}"
            counter += 1

        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / filename).write_bytes(content)
        used_names.add(filename)
        return filename

    def _structured_article_markdown(self, html_body: str, page_url: str, assets_dir: Optional[Path]) -> str:
        try:
            document = lxml_html.fromstring(html_body)
        except Exception:
            return self._html_to_text(html_body)

        container = None
        for expr in (
            "//article[1]",
            "//main[1]",
            "//div[contains(@class, 'entry-content')][1]",
            "//body[1]",
        ):
            nodes = document.xpath(expr)
            if nodes:
                container = nodes[0]
                break
        if container is None:
            container = document

        fragments: list[str] = []
        seen_text_blocks = set()
        used_names: set[str] = set()

        for node in container.xpath(
            ".//*[self::h1 or self::h2 or self::h3 or self::h4 or self::p or self::li or self::table or self::img]"
        ):
            tag = node.tag.lower() if isinstance(node.tag, str) else ""

            if tag in {"h1", "h2", "h3", "h4", "p", "li"}:
                text = re.sub(r"\s+", " ", node.text_content()).strip()
                if not text or text in seen_text_blocks:
                    continue
                seen_text_blocks.add(text)
                if tag == "h1":
                    fragments.append(f"### {text}")
                elif tag == "h2":
                    fragments.append(f"#### {text}")
                elif tag in {"h3", "h4"}:
                    fragments.append(f"##### {text}")
                elif tag == "li":
                    fragments.append(f"- {text}")
                else:
                    fragments.append(text)
                continue

            if tag == "table":
                table_md = self._to_markdown_table(node)
                if table_md:
                    fragments.append(table_md)
                continue

            if tag == "img":
                src = node.get("src")
                if not src:
                    continue
                image_url = self._clean_url(urljoin(page_url, html.unescape(src)))
                if not image_url.startswith("http"):
                    continue
                if assets_dir is not None:
                    local_name = self._download_image_asset(image_url, assets_dir, used_names, page_url)
                    if local_name:
                        relative_path = f"{assets_dir.name}/{local_name}"
                        alt = (node.get("alt") or "image").strip() or "image"
                        fragments.append(f"![{alt}]({relative_path})")
                        continue
                fragments.append(f"![]({image_url})")

        return "\n\n".join(fragments).strip()

    def _prioritize_anchors(self, anchors: list[dict[str, str]]) -> list[dict[str, str]]:
        ranked = sorted(
            anchors,
            key=lambda item: (self._anchor_score(item.get("url", ""), item.get("text", "")), len(item.get("text", ""))),
            reverse=True,
        )

        prioritized = []
        seen_urls = set()
        for anchor in ranked:
            url = anchor.get("url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            prioritized.append(anchor)
        return prioritized

    def _select_candidate_urls(self, body: str, html_body: str) -> list[str]:
        explicit_pdf_urls = re.findall(r'https?://\S+\.pdf', body, re.IGNORECASE)
        generic_urls = re.findall(r'https?://\S+', body)
        html_anchors = self._extract_html_anchors(html_body)
        prioritized_anchors = self._prioritize_anchors(html_anchors)

        strong_targets = [
            anchor["url"]
            for anchor in prioritized_anchors
            if self._anchor_score(anchor.get("url", ""), anchor.get("text", "")) >= 90
        ]
        weak_targets = [
            anchor["url"]
            for anchor in prioritized_anchors
            if 0 < self._anchor_score(anchor.get("url", ""), anchor.get("text", "")) < 90
        ]

        candidate_urls = []
        source_urls = explicit_pdf_urls + strong_targets + weak_targets
        if not source_urls:
            source_urls = explicit_pdf_urls + [anchor["url"] for anchor in prioritized_anchors] + generic_urls

        for url in source_urls:
            cleaned = self._clean_url(url)
            if cleaned.startswith("http"):
                candidate_urls.append(cleaned)

        return list(dict.fromkeys(candidate_urls))

    def _is_pdf_response(self, resp) -> bool:
        final_url = resp.url
        content_type = resp.headers.get("Content-Type", "").lower()
        return (
            "application/pdf" in content_type
            or final_url.lower().split("?", 1)[0].endswith(".pdf")
            or resp.content.startswith(b"%PDF")
        )

    def _extract_urls_from_json_obj(self, obj) -> list[str]:
        urls = []
        if isinstance(obj, dict):
            for value in obj.values():
                urls.extend(self._extract_urls_from_json_obj(value))
        elif isinstance(obj, list):
            for item in obj:
                urls.extend(self._extract_urls_from_json_obj(item))
        elif isinstance(obj, str):
            urls.extend(re.findall(r'https?://\S+', obj, re.IGNORECASE))
        return [self._clean_url(u) for u in urls if u]

    def _extract_nested_candidate_urls(self, resp) -> list[str]:
        content_type = resp.headers.get("Content-Type", "").lower()
        candidates = []

        if "application/json" in content_type:
            try:
                data = resp.json()
                candidates.extend(self._extract_urls_from_json_obj(data))
            except Exception:
                pass

        text = ""
        try:
            text = resp.text
        except Exception:
            text = ""

        if text:
            if "text/html" in content_type or "<html" in text.lower():
                candidates.extend(re.findall(r'href=["\'](https?://[^"\']+)["\']', text, re.IGNORECASE))
            candidates.extend(re.findall(r'https?://\S+', text, re.IGNORECASE))

        cleaned = [self._clean_url(u) for u in candidates if u]
        return list(dict.fromkeys(cleaned))

    def _collect_content_from_response(
        self,
        resp,
        source_url: str,
        seen_filenames: set,
    ) -> tuple[list[str], list[str], list[dict[str, str]]]:
        pdf_paths: list[str] = []
        page_texts: list[str] = []
        nested_anchors: list[dict[str, str]] = []

        if self.download_linked_pdfs and self._is_pdf_response(resp):
            filename = self._guess_pdf_filename(source_url, resp.url, resp.headers)
            path = self.pdf_dir / filename
            if filename not in seen_filenames and not path.exists():
                seen_filenames.add(filename)
                path.write_bytes(resp.content)
                pdf_paths.append(str(path))
            else:
                print(f"Skip existed PDFs: {filename}")
            return pdf_paths, page_texts, nested_anchors

        content_type = resp.headers.get("Content-Type", "").lower()
        text = ""
        try:
            text = resp.text
        except Exception:
            text = ""

        if text and ("text/html" in content_type or "<html" in text.lower()):
            extracted_text = self._html_to_text(text)
            if self._looks_like_meaningful_page_text(extracted_text):
                page_texts.append(extracted_text)
            nested_anchors = self._extract_html_anchors(text)

        return pdf_paths, page_texts, nested_anchors

    def _collect_from_url(
        self,
        url: str,
        seen_urls: set,
        seen_filenames: set,
        assets_dir: Optional[Path] = None,
    ) -> tuple[list[str], list[str]]:
        if url in seen_urls:
            return [], []
        seen_urls.add(url)

        try:
            resp = self._fetch_response(url)
        except Exception as exc:
            print(f"Content fetch failed: {url} — {exc}")
            return [], []

        pdf_paths, page_texts, nested_anchors = self._collect_content_from_response(resp, url, seen_filenames)
        if pdf_paths:
            return pdf_paths, page_texts

        if self._is_topic_url(url):
            return pdf_paths, page_texts

        # The landing-page shell is not useful RAG context. Keep only the linked topics.
        page_texts = []
        nested_candidates = self._select_topic_anchors(nested_anchors)

        nested_pdf_paths: list[str] = []
        for anchor in nested_candidates[: self.max_nested_urls_per_link]:
            nested_url = anchor.get("url", "")
            if not nested_url or nested_url in seen_urls:
                continue
            try:
                nested_resp = self._fetch_response(nested_url)
            except Exception:
                continue

            pdf_hits, nested_texts, _ = self._collect_content_from_response(nested_resp, nested_url, seen_filenames)
            if pdf_hits:
                nested_pdf_paths.extend(pdf_hits)
            structured_text = self._structured_article_markdown(nested_resp.text, nested_url, assets_dir)
            if nested_texts or structured_text:
                title = anchor.get("text", "").strip() or "UBS DailyTrader topic"
                content = structured_text if structured_text else "\n\n".join(nested_texts)
                article = f"## {title}\n\nSource URL: {nested_url}\n\n{content}"
                page_texts.append(article)

        return nested_pdf_paths, page_texts

    def _download_pdfs(
        self,
        msg,
        body: str,
        html_body: str,
        assets_dir: Optional[Path] = None,
    ) -> tuple[list[str], list[str]]:
        """Collect UBS newsletter topic pages and optionally retain direct PDF attachments."""
        paths = []
        landing_texts = []
        seen_urls: set = set()
        seen_filenames: set = set()
        
        if self.download_linked_pdfs:
            for part in msg.walk():
                if part.get_filename():
                    filename = self._decode_header(part.get_filename())
                    if filename.lower().endswith(".pdf"):
                        path = self.pdf_dir / filename
                        if filename in seen_filenames or path.exists():
                            print(f"Skip existing PDF attachment: {filename}")
                            continue
                        seen_filenames.add(filename)
                        path.write_bytes(part.get_payload(decode=True))
                        paths.append(str(path))
        
        # A DailyTrader email contains many tracking links. The labeled newsletter entry
        # is the sole root page; its iframe supplies the actual topic article URLs.
        newsletter_entries = [
            anchor["url"]
            for anchor in self._prioritize_anchors(self._extract_html_anchors(html_body))
            if "newsletter" in anchor.get("text", "").lower()
            or "web-browser" in anchor.get("text", "").lower()
            or "web browser" in anchor.get("text", "").lower()
        ]
        deduped_urls = list(dict.fromkeys(newsletter_entries))
        limited_urls = deduped_urls[:1]

        if not limited_urls:
            print("No labeled UBS DailyTrader newsletter link found in email HTML.")
            return paths, landing_texts

        for idx, url in enumerate(limited_urls, 1):
            try:
                print(f"Checking link {idx}/{len(limited_urls)}: {url}")
                saved_paths, page_texts = self._collect_from_url(
                    url,
                    seen_urls,
                    seen_filenames,
                    assets_dir=assets_dir,
                )
                if saved_paths:
                    paths.extend(saved_paths)
                for page_text in page_texts:
                    if page_text not in landing_texts:
                        landing_texts.append(page_text)
            except Exception as e:
                print(f"PDF download failed: {url} — {e}")
        
        return paths, landing_texts