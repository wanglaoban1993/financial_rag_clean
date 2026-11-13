
"""Merged watchlist processor for ActiveTrader CSV files.

Features
--------
- Preserves the stock target-price workflow from csv_analyse.py.
- Adds ETF/Fund enrichment from watchlist_enricher.py.
- Supports single CSV input or batch-folder processing.
- Writes XLSX and CSV outputs.
"""

import argparse
import contextlib
import csv
import fcntl
import glob
import io
import json
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import pandas as pd
import requests
import yfinance as yf
import yaml
from bs4 import BeautifulSoup
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
import onvista_http
import onvista_urls
import target_cache
from config_loader import resolve_config_path

try:
    from curl_cffi import requests as curl_cffi_requests
except ImportError:
    curl_cffi_requests = None


for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = resolve_config_path()
SYMBOL_OVERRIDES_PATH = SCRIPT_DIR / "isin_symbol_overrides.yaml"
SYMBOL_CANDIDATES_PATH = SCRIPT_DIR / "isin_symbol_candidates.yaml"
SUBCATEGORIES_PATH = SCRIPT_DIR / "subcategories.csv"
YAHOO_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
JUSTETF_PROFILE_URL = "https://www.justetf.com/en/etf-profile.html?isin={isin}"
FINANZEN_BASE_URL = "https://www.finanzen.net"
MARKETSCREENER_BASE_URL = "https://www.marketscreener.com"
ONVISTA_BASE_URL = "https://www.onvista.de"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}
MARKETSCREENER_LOGIN_STATE: dict[int, bool] = {}
MARKETSCREENER_NAME_CACHE: dict[str, list[str]] = {}
YAHOO_API_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept-Language": HEADERS["Accept-Language"],
    "Accept": "application/json,text/plain,*/*",
}
ONVISTA_OUTPUT_COLUMNS = []
MANUAL_FUND_TICKERS = {
    "IE00B4L5Y983": "IWDA.AS",
    "IE00BK5BQT80": "VWCE.DE",
    "IE00B3RBWM25": "VUSA.L",
    "IE00B5BMR087": "CSPX.L",
    "DE0005933931": "EXS1.DE",
    "LU1681038243": "X500.PA",
    "IE00BJ0KDQ92": "XDWD.DE",
    "LU0392495700": "LU0392495700.F",
    "LU1900066207": "AMUS.PA",
    "LU0292109344": "XMBR.DE",
    "FR0010342592": "LQQ.PA",
    "LU1829221024": "CU2.PA",
    "IE00B53SZB19": "CNDX.L",
    "IE0032077012": "EQQQ.DE",
}
ENRICHABLE_FUND_TYPES = {"ETF", "MUTUALFUND", "FUND"}
ETF_KEYWORDS = [
    " ETF",
    " UCITS",
    " ISHARES",
    " XTRACKERS",
    " AMUNDI",
    " LYXOR",
    " SPDR",
    " INVESCO",
    " WISDOMTREE",
    " UBS ETF",
    " MSCI ",
    " STOXX",
    " FTSE",
]
FUND_KEYWORDS = [
    "FDS",
    " FUND",
    " SICAV",
    " JPM-",
    " FTI-",
    " TEMPLETON",
    " BLACKROCK",
    " M+G",
    " NOMURA",
    " PICTET",
    " INV-",
    " PRIV.",
]
CERTIFICATE_KEYWORDS = [
    " CERT",
    " ZERT",
    " OPEN END",
    " O.E.",
    " ENDZ",
    " TURBO",
    " FAKTOR",
    " KNOCK",
    " WARRANT",
    " OPTIONSSCHEIN",
    " VONTOBEL",
    " BNP PAR",
    " SOCIETE GENERALE",
    " SGA ",
    " LANG+SCHWARZ O.E.",
]
INDEX_KEYWORDS = [
    " INDEX",
    " IDX",
    " NIKKEI",
    " TOPIX",
    " HANG SENG",
    " CSI 300",
    " DAX",
    " S&P 500",
]
UPDATE_TYPE_ALIASES = {
    "stock": "equity",
    "stocks": "equity",
    "equity": "equity",
    "share": "equity",
    "shares": "equity",
    "etf": "etf",
    "etfs": "etf",
    "fund": "fund",
    "funds": "fund",
    "fds": "fund",
    "index": "index",
    "indexes": "index",
    "indices": "index",
    "certificate": "certificate",
    "certificates": "certificate",
    "other": "other",
    "all": "all",
}
INSTRUMENT_TYPE_SUMMARY_ORDER = ["equity", "etf", "fund", "index", "certificate", "other", "missing_isin"]

# ── Technical Analysis parameters ────────────────────────────────────
TA_MACD_FAST, TA_MACD_SLOW, TA_MACD_SIGNAL = 12, 26, 9
TA_STOCH_K, TA_STOCH_D, TA_STOCH_SMOOTH    = 14, 3, 3
TA_LOOKBACK_DAYS                            = 183
RETURN_LOOKBACK_DAYS                        = 400
YAHOO_BATCH_MIN_ROWS                        = 100
TAXONOMY_STOPWORDS = {
    "and", "the", "for", "with", "including", "companies", "company", "providers", "provider",
    "platforms", "platform", "covering", "based", "medical", "digital", "hardware", "software",
    "services", "service", "systems", "system", "tools", "manufacturers", "manufacturer",
}
SECTOR_FILE_HINTS = {
    "AIR & SPACE": ("air space", "air_space", "airspace", "aerospace"),
    "Data Center": ("data center", "data_center", "datacenter"),
    "Defense":     ("defense", "defence"),
    "Europa Branchen": ("europa branchen", "europe branchen", "europe sectors", "europa"),
    "Health":      ("health",),
    "Photonic":     ("photonic", "photonics"),
    "Tech":        ("tech",),
    "Power":       ("power",),
    "Robotic":     ("robotic", "robot"),
    "Energy":      ("energy",),
    "Finance":     ("finance", "finanz"),
    "Commodities": ("commodit",),
    "Industry":    ("industr",),
    "Portfolio":   ("portfolio",),
    "US Branchen": ("us branchen", "usa branchen", "us sectors", "usa sectors"),
}
ISIN_COUNTRY_MAP = {
    "AT": "AT",
    "AU": "AU",
    "BE": "BE",
    "CA": "CA",
    "CH": "CH",
    "CN": "CN",
    "DE": "DE",
    "DK": "DK",
    "ES": "ES",
    "FI": "FI",
    "FR": "FR",
    "GB": "UK",
    "HK": "HK",
    "IE": "IE",
    "IL": "IL",
    "IT": "IT",
    "JE": "JE",
    "JP": "JP",
    "KY": "KY",
    "LU": "LU",
    "NL": "NL",
    "NO": "NO",
    "SE": "SE",
    "SG": "SG",
    "TW": "TW",
    "US": "US",
    "KR": "KR",
    "MX": "MX",
    "UAE": "UAE",
}
COUNTRY_NAME_TO_CODE = {
    "austria": "AT",
    "australia": "AU",
    "belgium": "BE",
    "canada": "CA",
    "switzerland": "CH",
    "china": "CN",
    "germany": "DE",
    "denmark": "DK",
    "spain": "ES",
    "finland": "FI",
    "france": "FR",
    "united kingdom": "UK",
    "great britain": "UK",
    "hong kong": "HK",
    "ireland": "IE",
    "israel": "IL",
    "italy": "IT",
    "jersey": "JE",
    "japan": "JP",
    "cayman islands": "KY",
    "luxembourg": "LU",
    "netherlands": "NL",
    "norway": "NO",
    "sweden": "SE",
    "singapore": "SG",
    "taiwan": "TW",
    "united states": "US",
    "usa": "US",
    "south korea": "KR",
    "mexico": "MX",
    "united arab emirates": "UAE",
}
SECTOR_TAG_ALIASES = {
    "Health": {
        "Pharma": [
            "pharma", "pharm", "drug", "big pharma",
            "bristol myers", "merck co", "merck kgaa", "novartis",
            "roche holding", "roche", "jazz pharmac", "corcept",
        ],
        "Biotech": [
            "biotech", "biopharma", "bio", "biologics", "therapeut", "biosci",
            "biomarin", "gilead sciences", "gilead", "genmab", "incyte",
            "exelixis", "arcturus ther", "cytokinetics", "evotec", "halozyme",
            "harmony biosc", "structure therap", "tg therapeutics", "twist bioscience",
        ],
        "Med Tech": [
            "med tech", "medtech", "medical dev", "med tec", "devices", "surgical",
            "medtronic", "stryker", "boston scientific", "align technology", "align tech",
            "glaukos", "inogen", "iradimed", "mimedx", "johnson johnson",
            "pulse biosciences",
        ],
        "Gene Edit": ["gene edit", "crispr", "crispre", "editas", "beam", "intellia", "uniqueure", "unicure"],
        "Health Serv": [
            "health serv", "healthcare", "insurance", "cvs health", "unitedhealth",
            "care", "plegeservices",
            "adapthealth", "fresenius", "encomp healt", "medifast",
        ],
        "Diagnostics": [
            "diagnostic", "diagnostics", "lab", "gen test", "genetics", "imaging", "messen",
            "lantheus", "neogenomics", "semler scientific", "mettler toledo",
        ],
        "Med Digital": [
            "med digital", "med info", "digital health", "contract research", "cro",
            "lonza", "schrodinger", "rezolve",
        ],
        "Specialty": [
            "specialty", "vask", "vascular", "diabetes", "insuli", "rare disease", "transplant",
            "tarsus pharmac", "utd therap",
        ],
    },
    "Tech": {
        "Chip Design": [
            "chip design", "chip", "chips", "asic", "gpu", "cpu", "soc", "fabless",
            "mixed signal", "halbleiter",
            "analog devices", "cirrus logic", "intel corp", "intel", "marvell tech",
            "marvell", "micron tech", "micron", "nxp semiconduct", "nxp",
            "on semicond", "renesas", "samsung elec", "samsung", "sk hynix",
            "texas instr", "elmos", "vicor corp", "vicor",
        ],
        "Chip Equip": [
            "chip equip", "chip masch", "equipment", "wafer", "photomask",
            "packaging", "chip zube", "chip test", "prufer", "probe",
            "acm research", "aehr test", "aixtron", "carl zeiss", "coherent corp",
            "coherent", "cohu inc", "cohu", "delta el th", "entegris",
            "formfactor", "jenoptik", "lumentum", "photronics", "soitec",
            "suss microtec", "technotrans", "teradyne", "tokyo electron",
            "ultra clean", "universal display", "veeco", "wacker chemie",
        ],
        "Foundry": ["foundry", "tsmc", "taiwan semicon"],
        "Data Center": [
            "data center", "data centre", "server", "servers", "hpc", "data center betreiber",
            "asustek", "hon hai", "lenovo", "lg elect", "modine", "netapp",
            "seagate tech", "seagate", "western digital",
        ],
        "Network": [
            "network", "netzwerke", "netzwerk", "telekom", "router", "switch",
            "amphenol", "credo tec", "extreme networks", "ringcentral",
        ],
        "Infra Build": [
            "infra", "kabel", "cable", "fiber", "glasfaser", "cooling",
            "kuhlung", "kuehlung", "stromsystem", "power", "electrical",
            "mastec inc", "mastec",
        ],
        "5G": ["5g", "wireless"],
        "Cloud": [
            "cloud", "cloud infrastructure", "cloud plattform", "cloud platform", "connectivity",
            "microsoft corp", "microsoft", "oracle corp", "oracle",
        ],
        "Ent Software": [
            "enterprise software", "erp", "hr", "workflow", "business soft",
            "business software", "work management",
            "adobe inc", "adobe", "appfolio", "asana", "atlassian",
            "autodesk", "constellation sw", "figma", "gitlab",
            "monday com", "nemetschek", "sage grp",
            "sap se", "sap", "servicenow", "tyler techs", "veeva",
            "workday", "ptc inc", "ptc",
        ],
        "IT Services": [
            "it services", "beratung", "consult", "counsel", "service", "services",
            "adesso", "aecom", "amdocs", "bechtle", "caci intl", "cancom",
            "computacenter", "exlservice", "fujitsu", "ibex", "infosys",
            "intl bus mach", "jacobs eng", "jacobs", "nagarro", "teamviewer",
            "toshiba tec", "usu ventures",
        ],
        "CRM/Mktg": [
            "crm", "marketing", "mktg", "ad tech", "werbung",
            "hubspot", "perfect corp", "salesforce",
        ],
        "Cybersec": [
            "cyber", "security", "zero trust", "cloud sec", "itsec",
            "axon enterprise", "check point", "cloudflare", "commvault",
            "crowdstrike", "cybeats", "cyberark", "datadog", "osi systems",
            "securize it",
        ],
        "Data & AI": [
            "data", "analytics", "analytic", "database", "ai", "ki",
            "mongodb", "palantir", "similarweb",
        ],
        "Quantum": [
            "quantum", "quanten",
            "ionq", "rigetti",
        ],
        "Fintech": [
            "fintech", "finance", "finanz", "bank", "insurance",
            "intuit", "shopify", "ss c", "gft",
        ],
        "EdTech": [
            "edtech", "edu", "education",
            "coursera",
        ],
        "Auto Tech": [
            "auto", "autos", "ev", "sensor", "smart cars", "autonomous",
            "cerence", "continental ag", "continental",
        ],
        "Media/Games": [
            "gaming", "streaming", "media", "games", "metaverse",
            "draftkings", "el arts", "electronic arts", "flutter entmt",
            "match gr", "meta platforms", "netease", "netflix", "nintendo",
            "pullup", "roblox", "roku", "shutterstock", "snap inc",
            "sony group", "sony", "ubisoft", "unity software", "unity",
        ],
    },
    "Power": {
        "Hydrogen White": ["hydrogen white", "itm power", "itm pwr", "protium clean", "thyssenkrupp nucera", "nucera"],
        "Fuel Cells BSZ": ["fuelcell energy", "plug power", "ballard pwr", "powercell sweden", "sfc energy", "nel asa"],
        "Solar EU": ["solaredge", "sma solar", "longi green", "sungrow"],
        "Solar US": ["sunpower corp", "sunrun inc"],
        "Offshore Wind": ["orsted a s", "orsted"],
        "Wind Turbines": ["ge vernova", "vernova", "vestas wind", "nordex"],
        "Power Utilities US": [
            "amer el pwr", "american electric",
            "duke en", "dte en", "dominion en",
            "exelon corp", "cms energy", "alliant energy",
            "nextera energy", "nrg energy",
            "ppl corp", "publ svc enter",
            "southern co", "xcel energy", "eversource",
        ],
        "Power Utilities EU": ["e on se", "enel s p a", "iberdrola", "oge en", "national grid"],
        "Power Utilities Asia": ["tenaga nasional", "tenaga", "utilities asia"],
        "Power Utilities Pipes": ["atmos en"],
        "Power Electronics": [
            "schneider elec", "nvent electric",
            "honeywell intl", "hubbell inc", "hubbell",
            "rexel s a", "eaton corp", "vicor corp", "powell ind",
        ],
        "Grid Infrastructure": [
            "quanta svcs", "quanta services",
            "prysmian", "unusual machs", "unusual machines",
            "ies hldgs", "ies holdings",
            "mitsubishi heavy", "hitachi",
        ],
        "Energy Storage BESS": ["tesla inc", "panasonic holdings"],
        "Generators Pumps": ["cummins inc", "generac", "generator", "2g energy"],
        "Energy Infrastructure": ["ameresco inc", "argan inc"],
        "Building Technology": ["johnson contr", "building tech", "building automat"],
        "HVAC Climate": ["trane technolog", "trane", "carrier global", "carrier", "hvac"],
        "Cooling Infrastructure": ["vertiv", "cooling infra", "data center cool"],
        "Utilities Services": ["emerson el", "utilities serv"],
        "Utilities Yield": ["vistra corp", "utilities yield"],
        "Energy Mgmt Software": ["nextpower inc", "willdan group", "energy mgmt", "demand response"],
        "Power Sector Diversified": [
            "siemens ag", "goodman group", "expand energy", "origin energy", "power sector div",
        ],
        "Energy Materials": ["solstice adv", "advanced materials", "energy materials"],
        "EV Charging Infrastructure": ["chargepoint", "evgo", "blink charg", "ev charg", "charging station"],
        "Drone Defense Tech": ["ondas inc", "ondas", "drone defense", "defense drone"],
    },
    "Robotic": {
        "Industrial Robotics": ["abb ltd", "midea group", "hitachi ltd"],
        "IoT Industrial": ["cisco systems", "honeywell intl", "intl bus mach", "ptc inc"],
        "CAD Engineering SW": ["cenit ag", "nemetschek"],
        "Logistics Automation": ["kion group", "jungheinrich", "symbotic inc", "xpo inc", "zebra tech"],
        "Robotics Sensors": ["cognex corp", "allegro microsys", "allegro micro"],
        "Actuators Motion": ["jiangsu hengli"],
        "Smart Glasses": ["essilorluxo", "xiaomi corp"],
        "Agritech": ["agco corp", "deere co"],
        "Medical Robotics": ["intuitive surgic", "stryker corp"],
        "Robotics Supply Chain": ["samsung el", "semtech corp"],
        "Boston Dynamics": ["boston dynamics", "softbank group", "softbank"],
        "Ocean Robotics": ["kraken robotics", "kraken", "ocean robot", "underwater", "subsea"],
        "Battery & Energy Storage": ["amperex", "catl", "cont amperex", "battery storage"],
        "Test & Measurement": ["keysight", "test measurement"],
        "Data Storage": ["seagate tech", "seagate tec", "western digital", "westn digital", "sandisk corp", "sandisk"],
        "Drones": ["ondas inc", "ondas", "ehang"],
        "Telecom Software": ["amdocs"],
    },
}


def load_local_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except Exception:
        return {}


def _format_http_error(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        status = e.response.status_code
        body = (e.response.text or "").strip()
        if len(body) > 500:
            body = body[:500] + "..."
        return f"HTTP {status}: {body}"
    return str(e)


LOCAL_CONFIG = load_local_config()


def _market_config() -> dict:
    raw = LOCAL_CONFIG.get("market")
    return raw if isinstance(raw, dict) else {}


def _marketscreener_credentials() -> tuple[str, str]:
    market_config = _market_config()
    username = str(market_config.get("marketscreener_account") or "").strip()
    password = str(market_config.get("marketscreener_password") or "").strip()
    return username, password


def _looks_logged_in_to_marketscreener(html: str) -> bool:
    text = re.sub(r"\s+", " ", str(html or "")).lower()
    return "log in" not in text and "logout" in text


def _discover_marketscreener_login_url(html: str, current_url: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for anchor in soup.find_all("a", href=True):
        anchor_text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).lower()
        href = _normalize_external_href(MARKETSCREENER_BASE_URL, str(anchor.get("href") or ""))
        if not href:
            continue
        lowered = href.lower()
        if "log in" in anchor_text or any(token in lowered for token in ("/login", "/signin", "/sign-in")):
            return href
    return current_url


def _marketscreener_login_form_payload(html: str, username: str, password: str) -> tuple[str, dict[str, str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    for form in soup.find_all("form"):
        inputs = form.find_all("input")
        if not any(str(field.get("type") or "").lower() == "password" for field in inputs):
            continue

        payload: dict[str, str] = {}
        username_field = ""
        password_field = ""
        for field in inputs:
            name = str(field.get("name") or "").strip()
            if not name:
                continue
            field_type = str(field.get("type") or "text").lower()
            if field_type in {"hidden", "submit"}:
                payload[name] = str(field.get("value") or "")
                continue
            if field_type == "password" or "password" in name.lower():
                password_field = name
                continue
            if field_type in {"email", "text"} or any(token in name.lower() for token in ("email", "user", "login", "identifier")):
                username_field = username_field or name

        if not username_field or not password_field:
            continue

        payload[username_field] = username
        payload[password_field] = password
        action = str(form.get("action") or "").strip()
        return action, payload
    return "", {}


def _ensure_marketscreener_login(session: requests.Session, timeout: int = 12) -> None:
    session_key = id(session)
    if MARKETSCREENER_LOGIN_STATE.get(session_key):
        return

    MARKETSCREENER_LOGIN_STATE[session_key] = True
    username, password = _marketscreener_credentials()
    if not username or not password:
        return

    try:
        home_response = session.get(MARKETSCREENER_BASE_URL, headers=HEADERS, timeout=timeout, allow_redirects=True)
    except Exception:
        return

    if _looks_logged_in_to_marketscreener(home_response.text):
        return

    login_url = _discover_marketscreener_login_url(home_response.text, str(home_response.url or MARKETSCREENER_BASE_URL))
    try:
        login_response = session.get(login_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    except Exception:
        return

    action, payload = _marketscreener_login_form_payload(login_response.text, username, password)
    if not payload:
        return

    submit_url = urljoin(str(login_response.url or login_url), action or "")
    if not submit_url:
        submit_url = str(login_response.url or login_url)

    submit_headers = dict(HEADERS)
    submit_headers["Referer"] = str(login_response.url or login_url)
    try:
        session.post(submit_url, headers=submit_headers, data=payload, timeout=timeout, allow_redirects=True)
    except Exception:
        return


def load_symbol_overrides() -> dict[str, dict[str, str]]:
    if not SYMBOL_OVERRIDES_PATH.exists():
        return {}
    try:
        with SYMBOL_OVERRIDES_PATH.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except Exception:
        return {}

    overrides: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return overrides

    for isin, value in raw.items():
        isin_key = str(isin or "").strip().upper()
        if not isin_key:
            continue
        if isinstance(value, str):
            symbol = value.strip()
            if symbol:
                overrides[isin_key] = {"symbol": symbol, "source": "LOCAL_OVERRIDE"}
            continue
        if isinstance(value, dict):
            symbol = str(value.get("symbol") or "").strip()
            if not symbol:
                continue
            source = str(value.get("source") or "LOCAL_OVERRIDE").strip() or "LOCAL_OVERRIDE"
            overrides[isin_key] = {"symbol": symbol, "source": source}
    return overrides


def load_symbol_candidates() -> dict[str, dict[str, str]]:
    if not SYMBOL_CANDIDATES_PATH.exists():
        return {}
    try:
        with SYMBOL_CANDIDATES_PATH.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _push_subcategories_to_gsheet() -> None:
    """Upload the full local subcategories.csv to the configured Google Sheet.

    Prerequisites in config.yaml (market section):
      gsheets_credentials_path: path to Service Account JSON key file
      subcategories_csv_url:    Google Sheet URL (already present)
    The Sheet must be shared (Edit) with the service account email.
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials as _SACredentials
    except ImportError:
        print("[subcategories] Missing dependencies: pip install gspread google-auth -- skipping Sheet sync")
        return

    if not SUBCATEGORIES_PATH.exists():
        return

    market_cfg = dict(LOCAL_CONFIG.get("market", {}) or {})
    cred_raw = str(market_cfg.get("gsheets_credentials_path") or "").strip()
    sheet_url = str(market_cfg.get("subcategories_csv_url") or "").strip()
    if not cred_raw or not sheet_url:
        return

    cred_path = Path(cred_raw) if Path(cred_raw).is_absolute() else SCRIPT_DIR / cred_raw
    if not cred_path.exists():
        print(f"[subcategories] Service Account JSON not found: {cred_path}")
        return

    sheet_id_match = re.match(r"https://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9-_]+)", sheet_url)
    if not sheet_id_match:
        print(f"[subcategories] Could not parse Sheet ID from URL: {sheet_url}")
        return
    sheet_id = sheet_id_match.group(1)

    gid_match = re.search(r"[?#&]gid=(\d+)", sheet_url)
    gid = int(gid_match.group(1)) if gid_match else 0

    try:
        creds = _SACredentials.from_service_account_file(
            str(cred_path),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(sheet_id).get_worksheet_by_id(gid)

        with SUBCATEGORIES_PATH.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))

        ws.clear()
        if rows:
            ws.update(rows, value_input_option="RAW")
        print(f"[subcategories] Synced {len(rows) - 1} rows to Google Sheet")
    except Exception as exc:
        print(f"[subcategories] Google Sheet sync failed: {exc}")


def _normalize_subcategories_source_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""

    match = re.match(r"https://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9-_]+)", raw)
    if not match:
        return raw

    sheet_id = match.group(1)
    gid_match = re.search(r"[?#&]gid=(\d+)", raw)
    if gid_match:
        gid = gid_match.group(1)
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"


def _parse_subcategory_rows_from_text(raw_text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    reader = csv.reader(io.StringIO(str(raw_text or "")))
    next(reader, None)
    for raw_row in reader:
        if not raw_row or len(raw_row) < 2:
            continue
        sector = str(raw_row[0] or "").strip()
        tag = str(raw_row[1] or "").strip()
        description = ",".join(raw_row[2:]).strip() if len(raw_row) > 2 else ""
        if sector and tag:
            rows.append({"Sector": sector, "Tag": tag, "Description": description})
    return rows


def _load_remote_subcategory_rows() -> list[dict[str, str]]:
    market_cfg = dict(LOCAL_CONFIG.get("market", {}) or {})
    source_url = _normalize_subcategories_source_url(
        str(market_cfg.get("subcategories_csv_url") or os.getenv("SUBCATEGORIES_CSV_URL", ""))
    )
    if not source_url:
        return []

    try:
        response = requests.get(
            source_url,
            headers={**YAHOO_API_HEADERS, "Accept": "text/csv,application/csv,text/plain,*/*"},
            timeout=12,
        )
        response.raise_for_status()
        rows = _parse_subcategory_rows_from_text(response.text)
        if rows:
            print(f"[taxonomy] loaded {len(rows)} rows from remote CSV")
        else:
            print(f"[taxonomy] remote CSV returned no usable rows: {source_url}")
        return rows
    except Exception as e:
        print(f"[taxonomy] remote fetch failed, using local file: {_format_http_error(e)}")
        return []


def load_subcategory_rows() -> list[dict[str, str]]:
    if SUBCATEGORIES_PATH.exists():
        try:
            with SUBCATEGORIES_PATH.open("r", encoding="utf-8", newline="") as handle:
                rows = _parse_subcategory_rows_from_text(handle.read())
        except Exception:
            rows = []
        if rows:
            print(f"[taxonomy] loaded {len(rows)} rows from local CSV")
            return rows

    remote_rows = _load_remote_subcategory_rows()
    return remote_rows


SYMBOL_OVERRIDES = load_symbol_overrides()
SYMBOL_CANDIDATES = load_symbol_candidates()
SYMBOL_CANDIDATES_LOCK = threading.Lock()
# ISINs this process has actually added/changed, as opposed to everything in
# SYMBOL_CANDIDATES (which also holds whatever was on disk when this process
# started). run_watchlist_*_parallel.sh runs one OS process per CSV file, so
# several of these run concurrently against the same isin_symbol_candidates.yaml
# — persisting only the dirty set (merged onto a fresh read, see
# _persist_symbol_candidates) keeps a slow sibling process from clobbering
# entries a faster one already wrote.
SYMBOL_CANDIDATES_DIRTY: set[str] = set()
SUBCATEGORY_ROWS = load_subcategory_rows()
USE_LOCAL_OVERRIDES = False
CLASSIFICATION_PROVIDER_OVERRIDE = ""
CLASSIFICATION_MODEL_OVERRIDE = ""
PROMOTABLE_OVERRIDE_TYPES = {"equity", "etf", "fund"}
PROMOTABLE_OVERRIDE_SOURCE_PREFIXES = ("OPENFIGI", "YAHOO_SEARCH")
DEFAULT_OPENFIGI_API_KEY = str(LOCAL_CONFIG.get("market", {}).get("openfigi_api_key", "") or os.getenv("OPENFIGI_API_KEY", ""))
DEFAULT_EODHD_API_KEY = str(LOCAL_CONFIG.get("market", {}).get("eodhd_api_key", "") or os.getenv("EODHD_API_KEY", ""))
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
OPENFIGI_EXCHANGE_TO_YAHOO_SUFFIX = {
    "GR": "DE",
    "GF": "DE",
    "GD": "DE",
    "GS": "DE",
    "GM": "DE",
    "FP": "PA",
    "LN": "L",
    "XL": "L",
    "IM": "MI",
    "NA": "AS",
    "AS": "AS",
    "SW": "SW",
    "SE": "SW",
    "HK": "HK",
    "JP": "T",
}
JUSTETF_EXCHANGE_TO_YAHOO_SUFFIX = {
    "Xetra": "DE",
    "gettex": "DE",
    "Euronext Paris": "PA",
    "Euronext Amsterdam": "AS",
    "Borsa Italiana": "MI",
    "London Stock Exchange": "L",
    "SIX Swiss Exchange": "SW",
}
JUSTETF_EXCHANGE_PREFERENCE = [
    "Xetra",
    "gettex",
    "Euronext Paris",
    "Borsa Italiana",
    "London Stock Exchange",
    "SIX Swiss Exchange",
    "Euronext Amsterdam",
]
_MARKET_FETCHER = None
INSTRUMENT_URL_CACHE: dict[tuple[str, str, str], str] = {}


def _yahoo_symbol_candidates(symbol: str) -> list[str]:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return []

    candidates: list[str] = []
    if "/" in raw:
        # Yahoo/yfinance class-share symbols are typically dash-based (e.g. BRK-B).
        candidates.append(raw.replace("/", "-"))
        candidates.append(raw.replace("/", "."))
    candidates.append(raw)

    unique: list[str] = []
    for cand in candidates:
        token = str(cand or "").strip().upper()
        if token and token not in unique:
            unique.append(token)
    return unique


def _normalize_yahoo_symbol(symbol: str) -> str:
    candidates = _yahoo_symbol_candidates(symbol)
    return candidates[0] if candidates else str(symbol or "").strip().upper()


def _has_yahoo_exchange_suffix(symbol: str) -> bool:
    return "." in str(symbol or "").strip()


def _should_refine_openfigi_symbol_with_yahoo(isin: str, symbol: str) -> bool:
    clean_isin = str(isin or "").strip().upper()
    clean_symbol = _normalize_yahoo_symbol(symbol)
    if not clean_isin or not clean_symbol:
        return False
    if _has_yahoo_exchange_suffix(clean_symbol):
        return False
    # Plain Yahoo symbols are usually fine for US listings; for most other
    # countries, a missing suffix can point at a different instrument.
    return not clean_isin.startswith("US")


def _should_prefer_yahoo_equity_symbol(openfigi_symbol: str, yahoo_symbol: Optional[str]) -> bool:
    openfigi_symbol = _normalize_yahoo_symbol(openfigi_symbol)
    yahoo_symbol = _normalize_yahoo_symbol(str(yahoo_symbol or ""))
    if not openfigi_symbol or not yahoo_symbol or openfigi_symbol == yahoo_symbol:
        return False
    if _has_yahoo_exchange_suffix(openfigi_symbol):
        return False
    if _has_yahoo_exchange_suffix(yahoo_symbol):
        return True
    return False


def _safe_yf_info(symbol: str) -> dict:
    """Fetch yfinance .info while suppressing provider noise on stdout/stderr."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return yf.Ticker(symbol).info or {}


def _safe_yf_download(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Download yfinance history while suppressing provider noise on stdout/stderr."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return yf.download(
            symbol,
            start=start,
            end=end,
            auto_adjust=True,
            progress=False,
        )


def _marketscreener_query_names(name: Optional[str], symbol: Optional[str]) -> list[str]:
    values: list[str] = []
    raw_name = re.sub(r"\s+", " ", str(name or "").strip())
    if raw_name:
        values.append(raw_name)

    normalized_symbol = _normalize_yahoo_symbol(str(symbol or "").strip()) if symbol else ""
    if normalized_symbol:
        cached = MARKETSCREENER_NAME_CACHE.get(normalized_symbol)
        if cached is None:
            cached = []
            try:
                info = _safe_yf_info(normalized_symbol)
            except Exception:
                info = {}
            for key in ("longName", "shortName"):
                candidate = re.sub(r"\s+", " ", str(info.get(key) or "").strip())
                if candidate and candidate not in cached:
                    cached.append(candidate)
            MARKETSCREENER_NAME_CACHE[normalized_symbol] = cached
        for candidate in cached:
            if candidate not in values:
                values.append(candidate)

    return values


def _resolve_symbol_via_local_override(isin: str) -> tuple[Optional[str], Optional[str]]:
    if not USE_LOCAL_OVERRIDES:
        return None, None
    if not isin:
        return None, None
    entry = SYMBOL_OVERRIDES.get(str(isin).strip().upper()) or {}
    symbol = _normalize_yahoo_symbol(str(entry.get("symbol") or "").strip())
    if not symbol:
        return None, None
    return symbol, str(entry.get("source") or "LOCAL_OVERRIDE")


def _get_market_fetcher():
    global _MARKET_FETCHER
    if _MARKET_FETCHER is not None:
        return _MARKET_FETCHER
    try:
        from ingestion.market_fetcher import MarketFetcher

        market_cfg = dict(LOCAL_CONFIG.get("market", {}) or {})
        market_cfg.setdefault("quote_retry_verbose", False)
        _MARKET_FETCHER = MarketFetcher(symbols=[], market_cfg=market_cfg)
    except Exception:
        _MARKET_FETCHER = False
    return _MARKET_FETCHER or None


def _resolve_market_fetcher_symbol(isin: str, wkn: Optional[str] = None, name: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    market_fetcher = _get_market_fetcher()
    if market_fetcher is None:
        return None, None

    for candidate in [isin, wkn, name]:
        token = str(candidate or "").strip()
        if not token:
            continue
        try:
            resolved_symbol = market_fetcher.resolve_symbol(token)
        except Exception:
            continue
        if resolved_symbol:
            return resolved_symbol, f"market_fetcher:{token}"
    return None, None


def _resolve_eodhd_symbol_via_market_fetcher(isin: str, wkn: Optional[str] = None, name: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    market_fetcher = _get_market_fetcher()
    resolved_symbol, source = _resolve_market_fetcher_symbol(isin, wkn=wkn, name=name)
    if market_fetcher is None or not resolved_symbol:
        return None, None
    try:
        return market_fetcher._to_eodhd_symbol(resolved_symbol), source
    except Exception:
        return None, None
    return None, None


def _persist_symbol_candidates() -> None:
    """Merge this process's new/changed entries onto a freshly-read copy of
    isin_symbol_candidates.yaml, under an exclusive file lock.

    Writing the full in-memory SYMBOL_CANDIDATES (as this used to do) instead
    of just the dirty subset would silently discard entries that sibling
    processes wrote after this process started but before it persists —
    run_watchlist_*_parallel.sh runs one process per CSV file, several
    concurrently, all against this same shared file.
    """
    with SYMBOL_CANDIDATES_LOCK:
        dirty_snapshot = {
            isin: SYMBOL_CANDIDATES[isin] for isin in SYMBOL_CANDIDATES_DIRTY if isin in SYMBOL_CANDIDATES
        }
    if not dirty_snapshot:
        return
    try:
        lock_path = SYMBOL_CANDIDATES_PATH.with_suffix(SYMBOL_CANDIDATES_PATH.suffix + ".lock")
        with open(lock_path, "w") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            try:
                on_disk = load_symbol_candidates()
                on_disk.update(dirty_snapshot)
                # Readers (fetch_finanzen_topflop.py) take no lock, so replace the
                # file in one step instead of truncating it and writing in place:
                # a read during the write would see an empty or half-written file.
                temp_path = SYMBOL_CANDIDATES_PATH.with_name(SYMBOL_CANDIDATES_PATH.name + ".tmp")
                with temp_path.open("w", encoding="utf-8") as handle:
                    yaml.safe_dump(on_disk, handle, allow_unicode=True, sort_keys=True)
                os.replace(temp_path, SYMBOL_CANDIDATES_PATH)
            finally:
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
    except Exception:
        pass


def _persist_symbol_overrides() -> None:
    try:
        payload = {
            isin: {"symbol": entry["symbol"], "source": entry.get("source") or "LOCAL_OVERRIDE"}
            for isin, entry in sorted(SYMBOL_OVERRIDES.items())
            if str(entry.get("symbol") or "").strip()
        }
        with SYMBOL_OVERRIDES_PATH.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=True)
    except Exception:
        pass


def _candidate_name_is_suspicious(name: str) -> bool:
    raw_text = str(name or "").upper()
    normalized_text = re.sub(r"[^A-Z0-9]+", " ", raw_text)
    keyword_groups = (ETF_KEYWORDS, FUND_KEYWORDS, CERTIFICATE_KEYWORDS, INDEX_KEYWORDS)
    for group in keyword_groups:
        for token in group:
            raw_token = str(token or "").strip().upper()
            normalized_token = re.sub(r"[^A-Z0-9]+", " ", raw_token).strip()
            if raw_token and raw_token in raw_text:
                return True
            if normalized_token and normalized_token in normalized_text:
                return True
    return False


def _symbol_looks_like_exchange_ticker(symbol: str) -> bool:
    clean = str(symbol or "").strip().upper()
    return bool(re.fullmatch(r"[A-Z0-9]{1,10}(?:\.[A-Z]{1,5})?", clean))


def _candidate_is_promotable(candidate: dict) -> bool:
    instrument_type = str(candidate.get("instrument_type") or "").strip().lower()
    symbol = str(candidate.get("symbol") or "").strip()
    source = str(candidate.get("source") or "").strip().upper()
    name = str(candidate.get("name") or "").strip()
    if instrument_type not in PROMOTABLE_OVERRIDE_TYPES:
        return False
    if not symbol or any(ch.isspace() for ch in symbol):
        return False
    if not any(source.startswith(prefix) for prefix in PROMOTABLE_OVERRIDE_SOURCE_PREFIXES):
        return False
    if not _symbol_looks_like_exchange_ticker(symbol):
        return False
    if instrument_type == "equity" and _candidate_name_is_suspicious(name):
        return False
    return True


def promote_symbol_candidates_to_overrides() -> list[str]:
    promoted: list[str] = []
    for isin, candidate in sorted(SYMBOL_CANDIDATES.items()):
        isin_key = str(isin or "").strip().upper()
        if not isin_key or isin_key in SYMBOL_OVERRIDES:
            continue
        if not isinstance(candidate, dict) or not _candidate_is_promotable(candidate):
            continue
        symbol = str(candidate.get("symbol") or "").strip()
        source = str(candidate.get("source") or "AUTO_PROMOTED").strip() or "AUTO_PROMOTED"
        SYMBOL_OVERRIDES[isin_key] = {"symbol": symbol, "source": source}
        promoted.append(f"{isin_key} -> {symbol} ({source})")
    if promoted:
        _persist_symbol_overrides()
    return promoted


def _record_symbol_candidate(
    isin: str,
    name: str,
    instrument_type: str,
    symbol: Optional[str],
    source: Optional[str],
    note: Optional[str] = None,
) -> None:
    if not isinstance(isin, str) or not isin.strip():
        return
    isin_key = isin.strip().upper()
    updated = {
        "name": str(name or "").strip(),
        "instrument_type": str(instrument_type or "").strip(),
        "symbol": str(symbol or "").strip(),
        "source": str(source or "").strip(),
        "note": str(note or "").strip(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with SYMBOL_CANDIDATES_LOCK:
        current = SYMBOL_CANDIDATES.get(isin_key, {})
        if current != updated:
            SYMBOL_CANDIDATES[isin_key] = updated
            SYMBOL_CANDIDATES_DIRTY.add(isin_key)


def _justetf_listing_to_symbol(listing: str, ticker: str) -> Optional[str]:
    suffix = JUSTETF_EXCHANGE_TO_YAHOO_SUFFIX.get(str(listing or "").strip())
    clean_ticker = str(ticker or "").strip().upper()
    if not clean_ticker or not suffix:
        return None
    return f"{clean_ticker}.{suffix}"


def _resolve_symbol_via_justetf(isin: str, session: requests.Session) -> tuple[Optional[str], Optional[str]]:
    if not isin:
        return None, None
    try:
        response = session.get(
            JUSTETF_PROFILE_URL.format(isin=isin.strip()),
            headers={**HEADERS, "Accept-Language": "en-US,en;q=0.9"},
            timeout=12,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        texts = [text.strip() for text in soup.stripped_strings if text and text.strip()]
        try:
            start = texts.index("Listings")
        except ValueError:
            return None, None

        entries: list[tuple[str, str, str]] = []
        bad_rows = 0
        row_start = start + 9
        for idx in range(row_start, len(texts), 8):
            row = texts[idx:idx + 8]
            if len(row) < 8:
                break
            listing, _trade_currency, ticker = row[0], row[1], row[2]
            if listing in JUSTETF_EXCHANGE_TO_YAHOO_SUFFIX and ticker not in {"-", ""}:
                symbol = _justetf_listing_to_symbol(listing, ticker)
                if symbol:
                    entries.append((listing, ticker, symbol))
                bad_rows = 0
                continue
            bad_rows += 1
            if entries and bad_rows >= 2:
                break

        if not entries:
            return None, None

        order = {name: pos for pos, name in enumerate(JUSTETF_EXCHANGE_PREFERENCE)}
        listing, _ticker, symbol = sorted(entries, key=lambda item: order.get(item[0], 999))[0]
        return symbol, f"JUSTETF:{listing}"
    except Exception:
        return None, None


def _openfigi_to_yahoo_symbol(ticker: str, exch_code: str) -> str:
    suffix = OPENFIGI_EXCHANGE_TO_YAHOO_SUFFIX.get(str(exch_code or "").upper(), "")
    symbol = f"{ticker}.{suffix}" if suffix else ticker
    return _normalize_yahoo_symbol(symbol)


def _instrument_type_from_openfigi_item(item: dict) -> Optional[str]:
    security_type = str(item.get("securityType") or "").strip().upper()
    security_type2 = str(item.get("securityType2") or "").strip().upper()
    if security_type == "ETP":
        return "etf"
    if security_type == "OPEN-END FUND" or security_type2 == "MUTUAL FUND":
        return "fund"
    return None


def _resolve_symbol_via_openfigi_with_hint(
    isin: str,
    api_key: str,
    allowed_suffixes: Optional[set[str]] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not isin or not api_key:
        return None, None, None
    try:
        response = requests.post(
            OPENFIGI_URL,
            headers={
                "Content-Type": "application/json",
                "X-OPENFIGI-APIKEY": api_key,
            },
            json=[{"idType": "ID_ISIN", "idValue": isin}],
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json() or []
        if not payload:
            return None, None, None
        data = payload[0].get("data") or []
        for item in data:
            ticker = str(item.get("ticker") or "").strip().upper()
            exch_code = str(item.get("exchCode") or "").strip().upper()
            if not ticker:
                continue
            symbol = _openfigi_to_yahoo_symbol(ticker, exch_code)
            suffix = symbol.rsplit(".", 1)[1].upper() if "." in symbol else ""
            if allowed_suffixes is None or suffix in allowed_suffixes or not suffix:
                return symbol, exch_code, _instrument_type_from_openfigi_item(item)
        return None, None, None
    except Exception:
        return None, None, None


def _resolve_symbol_via_openfigi(isin: str, api_key: str, allowed_suffixes: Optional[set[str]] = None) -> tuple[Optional[str], Optional[str]]:
    symbol, exch_code, _instrument_hint = _resolve_symbol_via_openfigi_with_hint(isin, api_key, allowed_suffixes=allowed_suffixes)
    return symbol, exch_code


WATCHLIST_REQUIRED_COLUMNS = {"WKN", "ISIN", "Name"}


def _read_watchlist_csv(path: str, *, dtype: object = None, keep_default_na: bool = True) -> pd.DataFrame:
    """Read ActiveTrader/watchlist CSV exports with either German ';' or normal ',' separators."""
    errors: list[str] = []
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        for sep in (";", ","):
            try:
                df = pd.read_csv(
                    path,
                    sep=sep,
                    encoding=encoding,
                    decimal="," if sep == ";" else ".",
                    skip_blank_lines=False,
                    engine="python",
                    dtype=dtype,
                    keep_default_na=keep_default_na,
                )
            except Exception as exc:
                errors.append(f"{encoding}/{sep}: {exc}")
                continue

            columns = {str(column).strip() for column in df.columns}
            if WATCHLIST_REQUIRED_COLUMNS.issubset(columns):
                # The currency column used to be called WAEHRUNG; files written
                # before the rename (and jobs still holding an old input copy)
                # are read as DEV so nothing breaks while the files catch up.
                if "WAEHRUNG" in df.columns and "DEV" not in df.columns:
                    df = df.rename(columns={"WAEHRUNG": "DEV"})
                if sep != ";" or encoding != "cp1252":
                    print(f"  [csv] detected encoding={encoding}, sep={sep!r}")
                # ActiveTrader exports (including files this script writes itself)
                # carry a "MNEMONIC:WKN;..." row as line 2. It isn't real data, but
                # pandas reads it as a normal row (WKN="MNEMONIC:WKN", rest blank).
                # Drop it here so it doesn't get processed as a phantom holding.
                if "WKN" in df.columns:
                    mnemonic_mask = df["WKN"].astype(str).str.strip().str.upper().str.startswith("MNEMONIC")
                    if mnemonic_mask.any():
                        df = df.loc[~mnemonic_mask].reset_index(drop=True)
                return df
            errors.append(f"{encoding}/{sep}: columns={list(df.columns)[:5]}")

    detail = "; ".join(errors[-4:])
    raise ValueError(f"Could not read watchlist CSV with required columns {sorted(WATCHLIST_REQUIRED_COLUMNS)}: {path}. {detail}")


def _parse_watchlist_date_column(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, format="%d.%m.%Y", errors="coerce")
    missing_mask = parsed.isna() & series.notna() & series.astype(str).str.strip().ne("")
    if missing_mask.any():
        parsed.loc[missing_mask] = pd.to_datetime(series.loc[missing_mask], errors="coerce")
    return parsed


def _coerce_watchlist_number(series: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")
    normalized = (
        series.astype(str)
        .str.strip()
        .str.replace("%", "", regex=False)
        .str.replace(",", ".", regex=False)
    )
    normalized = normalized.mask(normalized.str.lower().isin({"", "nan", "none"}))
    return pd.to_numeric(normalized, errors="coerce")


def read_activetrader_csv(path: str, *, clear_comments: bool = True, clear_comment3: bool = True) -> pd.DataFrame:
    # QUANTITY holds a TA signal code (e.g. "101", "-01"), not a number. Left to
    # auto-inference, pandas coerces that column to float64 whenever every
    # non-blank value in a given file happens to be numeric-parseable, turning
    # "101" into "101.0" — and process_bid_ask_only_dataframe() never rewrites
    # this column, so the corruption round-trips forever on the bid/ask cron.
    df = _read_watchlist_csv(path, dtype={"QUANTITY": str})
    df = df.loc[:, ~df.columns.str.match(r"^Unnamed")]
    original_columns = list(df.columns)
    df["_IS_SEPARATOR_ROW"] = df[original_columns].isna().all(axis=1)

    # Deduplicate: keep first occurrence per ISIN / WKN (separator rows are never dropped)
    for key_col in ("ISIN", "WKN"):
        if key_col not in df.columns:
            continue
        data_idx = df.index[~df["_IS_SEPARATOR_ROW"]]
        vals = df.loc[data_idx, key_col].astype(str).str.strip()
        valid_mask = vals.notna() & (vals != "") & (vals.str.lower() != "nan")
        dupe_mask = valid_mask & vals.duplicated(keep="first")
        drop_idx = data_idx[dupe_mask]
        if len(drop_idx):
            print(f"  [dedup] removed {len(drop_idx)} duplicate rows by {key_col}")
            df = df.drop(index=drop_idx).reset_index(drop=True)

    if "COMMENT" in df.columns:
        df["_SOURCE_COMMENT"] = df["COMMENT"].astype(object)
    else:
        df["_SOURCE_COMMENT"] = pd.NA

    # Ignore legacy comment values from the source CSV so enrichment always
    # writes the current, computed content. COMMENT3 (TA signal) is cleared
    # independently of COMMENT/COMMENT2 because _apply_ta_signal() appends to
    # whatever is already there rather than overwriting it — leaving stale
    # COMMENT3 content in place would make signals accumulate across reruns.
    comment_cols_to_clear = []
    if clear_comments:
        comment_cols_to_clear += ["COMMENT", "COMMENT2"]
    if clear_comment3:
        comment_cols_to_clear.append("COMMENT3")
    for comment_col in comment_cols_to_clear:
        if comment_col in df.columns:
            df[comment_col] = df[comment_col].astype(object)
            df.loc[~df["_IS_SEPARATOR_ROW"], comment_col] = ""

    for col in [*DIFF_PCT_COLUMNS, *DIFF_REF_COLUMNS, "Bid", "Ask"]:
        if col in df.columns:
            df[col] = _coerce_watchlist_number(df[col])

    return df


def read_csv_for_tagging(path: str) -> pd.DataFrame:
    df = _read_watchlist_csv(path, dtype=str, keep_default_na=False)
    df = df.loc[:, ~df.columns.str.match(r"^Unnamed")]
    original_columns = list(df.columns)
    df["_IS_SEPARATOR_ROW"] = df[original_columns].apply(
        lambda row: all(not str(value).strip() for value in row),
        axis=1,
    )
    if "COMMENT" not in df.columns:
        df["COMMENT"] = ""
    df["_SOURCE_COMMENT"] = df["COMMENT"].astype(object)
    df.loc[~df["_IS_SEPARATOR_ROW"], "COMMENT"] = ""
    return df


def _source_abbrev(source: str) -> str:
    mapping = {
        "yahoo": "YF",
        "eodhd": "EOD",
        "chartmill": "CM",
    }
    return mapping.get(source, "")


def _resolve_target_price_in_currency(data: dict, display_currency: str = "") -> tuple[Optional[float], str]:
    """Convert the analyst target price to display_currency (normally the row's
    DEV/Bid-Ask currency); falls back to its own quote currency, normalized
    to USD/EUR, if that conversion isn't possible."""
    tm = data.get("target_mean")
    cur = data.get("currency") or ""
    if tm is None:
        return None, ""
    converted_tm = _convert_price_to_currency(tm, cur, display_currency) if display_currency else None
    if converted_tm is not None:
        return converted_tm, display_currency
    return _convert_price_to_usd_or_eur(tm, cur)


def format_comment2(data: dict, display_currency: str = "", include_source_meta: bool = False) -> str:
    """Format the equity target price. If display_currency is given (normally the
    row's DEV/Bid-Ask currency), the price is converted to match it exactly;
    otherwise it falls back to its own quote currency, normalized to USD/EUR."""
    n = data.get("analysts")
    abbr = _source_abbrev(str(data.get("source") or "").strip())
    tm, _resolved_cur = _resolve_target_price_in_currency(data, display_currency)
    if tm is None:
        return ""
    base = f"{tm:.2f}".replace(".", ",")
    if not include_source_meta:
        return base
    if n and abbr:
        return f"{base} (n={n},{abbr})"
    if n:
        return f"{base} (n={n})"
    return base


def format_etf_comment2(laufende_kosten: object) -> str:
    if pd.isna(laufende_kosten):
        return "-"
    value = str(laufende_kosten or "").strip()
    if not value:
        return "-"
    number_match = re.search(r"([0-9]+(?:[.,][0-9]+)?)", value)
    if not number_match:
        return "-"
    normalized = number_match.group(1).replace(".", ",")
    return f"{normalized} %"


def format_etf_comment3(replikation: object, ertragsverwendung: object) -> str:
    parts = []
    for value in (replikation, ertragsverwendung):
        if pd.isna(value):
            continue
        text = str(value or "").strip()
        if text:
            parts.append(text)
    return "  |  ".join(parts)


def _map_ta_signal_for_display(signal: str) -> str:
    return str(signal or "").translate(str.maketrans({"!": "1", "?": "2"}))


# Exchanges that only ever quote in EUR, keyed by EODHD exchange code and by
# Yahoo ticker suffix. Used to label a price history's currency when the
# data source doesn't say — London/Swiss/US venues are deliberately absent
# because a single venue there can quote the same instrument in several
# currencies (GBX/USD/EUR on LSE), so guessing would be worse than unknown.
EUR_ONLY_EODHD_EXCHANGES = {"XETRA", "F", "BE", "DU", "HM", "HA", "MU", "STU", "PA", "AS", "BR", "MI", "MC", "LS", "VI", "HE", "IR"}
EUR_ONLY_YAHOO_SUFFIXES = {"DE", "F", "BE", "DU", "HM", "HA", "MU", "SG", "PA", "AS", "BR", "MI", "MC", "LS", "VI", "HE", "IR"}
EODHD_SEARCH_CACHE: dict[str, list[dict]] = {}
# Venue preference when picking one listing out of an EODHD search result:
# German EUR venues first (what the watchlists trade on), then any other.
EODHD_PREFERRED_EXCHANGES = ("XETRA", "F")


def _eodhd_search_listings(isin: str, api_key: str) -> list[dict]:
    if isin in EODHD_SEARCH_CACHE:
        return EODHD_SEARCH_CACHE[isin]
    listings: list[dict] = []
    try:
        resp = requests.get(
            f"https://eodhd.com/api/search/{isin}?api_token={api_key}&fmt=json",
            timeout=10,
        )
        if resp.status_code == 200:
            listings = [
                item for item in (resp.json() or [])
                if isinstance(item, dict) and str(item.get("ISIN", "")).upper() == isin.upper()
            ]
    except Exception:
        pass
    EODHD_SEARCH_CACHE[isin] = listings
    return listings


def _eodhd_listing_via_search(isin: str, api_key: str) -> tuple[Optional[str], str]:
    """(EODHD symbol, quote currency) for an ISIN via the search endpoint.

    The fundamentals endpoint (which used to supply Code/Exchange/CurrencyCode)
    is not part of the current EODHD plan and answers 403, and the
    MarketFetcher/OpenFIGI fallback often yields tickers EODHD doesn't know
    (e.g. DJGTEEX.XETRA instead of EXI2.XETRA), so resolve via search instead.
    """
    listings = _eodhd_search_listings(isin, api_key)
    if not listings:
        return None, ""

    def rank(item: dict) -> tuple[int, int]:
        exchange = str(item.get("Exchange", "")).upper()
        venue_rank = EODHD_PREFERRED_EXCHANGES.index(exchange) if exchange in EODHD_PREFERRED_EXCHANGES else len(EODHD_PREFERRED_EXCHANGES)
        return venue_rank, 0 if item.get("isPrimary") else 1

    best = min(listings, key=rank)
    code, exchange = str(best.get("Code", "")), str(best.get("Exchange", ""))
    if not code or not exchange:
        return None, ""
    return f"{code}.{exchange}", str(best.get("Currency") or "")


def _eodhd_symbol_currency(eodhd_symbol: str, isin: str, api_key: str) -> str:
    """Quote currency of one EODHD listing (e.g. "JREG.XETRA"), falling back
    to the exchange for EUR-only venues."""
    code, _, exchange = eodhd_symbol.rpartition(".")
    for listing in _eodhd_search_listings(isin, api_key):
        if str(listing.get("Code", "")).upper() == code.upper() and str(listing.get("Exchange", "")).upper() == exchange.upper():
            currency = str(listing.get("Currency") or "")
            if currency:
                return currency
    return "EUR" if exchange.upper() in EUR_ONLY_EODHD_EXCHANGES else ""


def _yahoo_symbol_currency(symbol: str) -> str:
    _, dot, suffix = symbol.rpartition(".")
    return "EUR" if dot and suffix.upper() in EUR_ONLY_YAHOO_SUFFIXES else ""


def _fetch_eodhd_ohlc(isin: str, api_key: str, start_date: str, wkn: Optional[str] = None, name: Optional[str] = None) -> Optional[pd.DataFrame]:
    """Fetch OHLC DataFrame from EODHD as a fallback for TA computation."""
    try:
        eodhd_symbol = None
        history_currency = ""
        resp = requests.get(
            f"https://eodhd.com/api/fundamentals/{isin}?api_token={api_key}&fmt=json",
            timeout=10,
        )
        if resp.status_code == 200:
            general = resp.json().get("General", {})
            ticker_code = general.get("Code", "")
            exchange = general.get("Exchange", "")
            history_currency = general.get("CurrencyCode", "")
            if ticker_code and exchange:
                eodhd_symbol = f"{ticker_code}.{exchange}"

        if not eodhd_symbol:
            eodhd_symbol, history_currency = _eodhd_listing_via_search(isin, api_key)
        if not eodhd_symbol:
            eodhd_symbol, _fallback_source = _resolve_eodhd_symbol_via_market_fetcher(isin, wkn=wkn, name=name)
        if not eodhd_symbol:
            return None
        if not history_currency:
            history_currency = _eodhd_symbol_currency(eodhd_symbol, isin, api_key)

        hist_resp = requests.get(
            f"https://eodhd.com/api/eod/{eodhd_symbol}"
            f"?api_token={api_key}&fmt=json&period=d&from={start_date}",
            timeout=10,
        )
        if hist_resp.status_code != 200:
            return None
        rows = hist_resp.json() or []
        if not rows:
            return None
        raw = pd.DataFrame(rows)
        raw.index = pd.to_datetime(raw["date"])
        history = pd.DataFrame({
            "Close": pd.to_numeric(raw["adjusted_close"], errors="coerce"),
            "High":  pd.to_numeric(raw["high"], errors="coerce"),
            "Low":   pd.to_numeric(raw["low"], errors="coerce"),
        })
        history.attrs["currency"] = history_currency
        return history
    except Exception:
        return None


def _extract_history_series(raw: pd.DataFrame, column_name: str) -> Optional[pd.Series]:
    if column_name not in raw:
        return None

    values = raw[column_name]
    if isinstance(values, pd.DataFrame):
        for _, series in values.items():
            numeric_series = pd.to_numeric(series, errors="coerce")
            if numeric_series.dropna().empty:
                continue
            return numeric_series
        return None

    numeric_series = pd.to_numeric(values, errors="coerce")
    if isinstance(numeric_series, pd.Series) and not numeric_series.dropna().empty:
        return numeric_series
    return None


def _fetch_price_history_with_source(
    ticker: str,
    isin: Optional[str] = None,
    eodhd_key: Optional[str] = None,
    wkn: Optional[str] = None,
    name: Optional[str] = None,
    prefer_eodhd: bool = False,
    lookback_days: int = TA_LOOKBACK_DAYS,
    context_label: str = "history",
) -> tuple[Optional[pd.Series], Optional[pd.Series], Optional[pd.Series], str]:
    if not ticker and not isin:
        return None, None, None, ""

    end_dt = datetime.today()
    start_dt = end_dt - timedelta(days=lookback_days)
    start_str = start_dt.strftime("%Y-%m-%d")

    close = high = low = None
    source = ""
    yahoo_failure_reason = ""
    eodhd_failure_reason = ""

    def _try_yahoo_history() -> bool:
        nonlocal close, high, low, source, yahoo_failure_reason
        if not ticker:
            yahoo_failure_reason = "missing ticker"
            return False
        for candidate in _yahoo_symbol_candidates(ticker):
            try:
                raw = _safe_yf_download(candidate, start_str, end_dt.strftime("%Y-%m-%d"))
            except Exception:
                raw = pd.DataFrame()
            if raw.empty:
                yahoo_failure_reason = f"empty download for {candidate}"
                continue
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            close = _extract_history_series(raw, "Close")
            high = _extract_history_series(raw, "High")
            low = _extract_history_series(raw, "Low")
            if close is None or close.dropna().empty:
                yahoo_failure_reason = f"no usable Close series for {candidate}"
                close = high = low = None
                continue
            close.attrs["currency"] = _yahoo_symbol_currency(candidate)
            source = "yahoo"
            print(f"    -> {context_label} history source YAHOO ({candidate})")
            return True
        if not yahoo_failure_reason:
            yahoo_failure_reason = "no Yahoo candidate returned usable history"
        return False

    def _try_eodhd_history() -> bool:
        nonlocal close, high, low, source, eodhd_failure_reason
        if not isin or not eodhd_key:
            eodhd_failure_reason = "missing ISIN or EODHD API key"
            return False
        ohlc = _fetch_eodhd_ohlc(isin, eodhd_key, start_str, wkn=wkn, name=name)
        if ohlc is None or ohlc.empty:
            eodhd_failure_reason = f"no OHLC rows for {isin}"
            return False
        close = pd.to_numeric(ohlc["Close"], errors="coerce") if "Close" in ohlc else None
        high = pd.to_numeric(ohlc["High"], errors="coerce") if "High" in ohlc else None
        low = pd.to_numeric(ohlc["Low"], errors="coerce") if "Low" in ohlc else None
        if close is None or close.dropna().empty:
            eodhd_failure_reason = f"no usable Close series for {isin}"
            close = high = low = None
            return False
        close.attrs["currency"] = ohlc.attrs.get("currency")
        source = "eodhd"
        print(f"    -> {context_label} history source EODHD ({isin})")
        return True

    if prefer_eodhd:
        if not _try_eodhd_history():
            print(f"    -> {context_label} history fallback EODHD -> YAHOO ({eodhd_failure_reason})")
            _try_yahoo_history()
    else:
        if not _try_yahoo_history():
            print(f"    -> {context_label} history fallback YAHOO -> EODHD ({yahoo_failure_reason})")
            _try_eodhd_history()

    return close, high, low, source


def _coalesce_market_price(bid: object, ask: object, fallback: object = None) -> Optional[float]:
    prices: list[float] = []
    for value in (bid, ask):
        numeric = pd.to_numeric(value, errors="coerce")
        if pd.notna(numeric):
            prices.append(float(numeric))
    if len(prices) == 2:
        return sum(prices) / 2.0
    if len(prices) == 1:
        return prices[0]

    fallback_numeric = pd.to_numeric(fallback, errors="coerce")
    if pd.notna(fallback_numeric):
        return float(fallback_numeric)
    return None


PRICE_REFERENCE_WINDOW_DAYS = 10
PRICE_REFERENCE_LOCAL_OUTLIER_RATIO = 5.0
PRICE_CURRENT_HISTORY_OUTLIER_RATIO = 20.0

# Only "% Differenz zum Vortageskurs" follows the live quote: it is measured
# from Ask (else Bid) against the last close before today. The reference close
# is kept in the CSV (REF_PREV_CLOSE) so a --bid-ask-only run can recompute it
# from a fresh Ask without refetching history. Equity references are in EUR;
# ETF/fund references are in the row's DEV at the time of the full run. The
# currency the reference is in is stored next to it (REF_CURRENCY) because
# neither the instrument type (not persisted: a --bid-ask-only run can only
# guess it) nor DEV (overwritten with the quote currency on every refresh) can
# tell a later run which currency to convert a fresh Ask into.
# DIFF_BASIS records which price that column was computed from:
# "Ask", "Bid" (Ask was missing) or "Close" (no quote at all). "ein Monat" and
# "ein Jahr" are always latest historical close vs the close 30/365 days ago.
DIFF_PCT_COLUMNS = (
    "% Differenz zum Vortageskurs",
    "% Differenz ein Monat",
    "% Differenz ein Jahr",
)
DIFF_REF_COLUMNS = ("REF_PREV_CLOSE",)
DIFF_REF_CURRENCY_COLUMN = "REF_CURRENCY"
DIFF_BASIS_COLUMN = "DIFF_BASIS"


def _outlier_ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    a_abs = abs(float(a))
    b_abs = abs(float(b))
    if a_abs == 0 or b_abs == 0:
        return None
    return max(a_abs, b_abs) / min(a_abs, b_abs)


def _series_entry_on_or_before(series: Optional[pd.Series], target: datetime) -> tuple[Optional[pd.Timestamp], Optional[float]]:
    if series is None or series.empty:
        return None, None
    valid_series = series.dropna().sort_index()
    if valid_series.empty:
        return None, None
    eligible = valid_series.loc[valid_series.index <= pd.Timestamp(target)]
    if eligible.empty:
        return None, None
    value = pd.to_numeric(eligible.iloc[-1], errors="coerce")
    if pd.isna(value):
        return None, None
    return pd.Timestamp(eligible.index[-1]), float(value)


def _series_value_on_or_before(series: Optional[pd.Series], target: datetime) -> Optional[float]:
    _timestamp, value = _series_entry_on_or_before(series, target)
    return value


def _series_window_median(series: Optional[pd.Series], center: pd.Timestamp, window_days: int = PRICE_REFERENCE_WINDOW_DAYS) -> Optional[float]:
    if series is None or series.empty or center is None:
        return None
    valid_series = series.dropna().sort_index()
    if valid_series.empty:
        return None
    center_ts = pd.Timestamp(center)
    start = center_ts - timedelta(days=window_days)
    end = center_ts + timedelta(days=window_days)
    window = pd.to_numeric(valid_series.loc[(valid_series.index >= start) & (valid_series.index <= end)], errors="coerce").dropna()
    if len(window) < 3:
        return None
    value = window.median()
    if pd.isna(value):
        return None
    return float(value)


def _robust_reference_value_on_or_before(
    series: Optional[pd.Series],
    target: datetime,
    label: str,
) -> Optional[float]:
    timestamp, value = _series_entry_on_or_before(series, target)
    if value is None or timestamp is None:
        return None

    local_median = _series_window_median(series, timestamp)
    ratio = _outlier_ratio(value, local_median)
    if ratio is not None and ratio >= PRICE_REFERENCE_LOCAL_OUTLIER_RATIO:
        print(
            f"    -> diff {label} reference outlier {value:.4g} on {timestamp.date()} "
            f"(local median {local_median:.4g}); using median"
        )
        return local_median
    return value


def _normalize_current_price_to_history_scale(current_price: Optional[float], valid_close: pd.Series) -> Optional[float]:
    if current_price is None or valid_close.empty:
        return current_price
    latest_close = pd.to_numeric(valid_close.iloc[-1], errors="coerce")
    if pd.isna(latest_close):
        return current_price
    latest_close = float(latest_close)
    ratio = _outlier_ratio(current_price, latest_close)
    if ratio is not None and ratio >= PRICE_CURRENT_HISTORY_OUTLIER_RATIO:
        print(
            f"    -> diff current price scale mismatch current={current_price:.4g}, "
            f"latest_close={latest_close:.4g}; using latest close"
        )
        return latest_close
    return current_price


def _percent_change(current_value: Optional[float], reference_value: Optional[float]) -> Optional[float]:
    if current_value is None or reference_value is None or reference_value == 0:
        return None
    return ((current_value / reference_value) - 1.0) * 100.0


def _quote_for_difference(ask: object, bid: object) -> tuple[Optional[float], str]:
    """Price the % Differenz columns are measured from: Ask, else Bid, else none."""
    for value, basis in ((ask, "Ask"), (bid, "Bid")):
        number = pd.to_numeric(value, errors="coerce")
        if pd.notna(number) and float(number) > 0:
            return float(number), basis
    return None, ""


def _compute_difference_columns(
    ticker: str,
    isin: Optional[str] = None,
    eodhd_key: Optional[str] = None,
    wkn: Optional[str] = None,
    name: Optional[str] = None,
    prefer_eodhd: bool = False,
    bid: object = None,
    ask: object = None,
    fallback_current_price: object = None,
    history_currency: Optional[str] = None,
    convert_to_eur: bool = True,
    quote_currency: Optional[str] = None,
    display_currency: Optional[str] = None,
) -> dict[str, Optional[float]]:
    # convert_to_eur=False computes the three changes in the instrument's own
    # quote currency (no FX conversion, so no currency lookup is needed); the
    # stored references are then expressed in display_currency (the row's
    # DEV) when the history currency is known and differs from it.
    # Vortageskurs is measured from Ask, else Bid (quote_currency is their
    # currency); only when neither exists does it fall back to the latest
    # historical close. Monat and Jahr always use the latest historical close.
    empty_result = {
        **{column: None for column in DIFF_PCT_COLUMNS},
        **{column: None for column in DIFF_REF_COLUMNS},
        DIFF_REF_CURRENCY_COLUMN: None,
        DIFF_BASIS_COLUMN: None,
    }
    close_history, _high_history, _low_history, _source = _fetch_price_history_with_source(
        ticker,
        isin=isin,
        eodhd_key=eodhd_key,
        wkn=wkn,
        name=name,
        prefer_eodhd=prefer_eodhd,
        lookback_days=RETURN_LOOKBACK_DAYS,
        context_label="diff",
    )
    if close_history is None or close_history.dropna().empty:
        return empty_result

    valid_close = close_history.dropna().sort_index()
    if convert_to_eur:
        history_currency = close_history.attrs.get("currency") or history_currency
        if not history_currency:
            return empty_result
        valid_close = _convert_history_to_eur(valid_close, history_currency).dropna().sort_index()
        ref_currency = "EUR"
    else:
        local_dates = pd.DatetimeIndex(pd.to_datetime(valid_close.index))
        if local_dates.tz is not None:
            local_dates = local_dates.tz_localize(None)
        valid_close.index = local_dates.normalize()
        ref_currency = _normalize_price_currency(close_history.attrs.get("currency") or history_currency)
        display_currency = _normalize_display_currency(display_currency or quote_currency)
        if ref_currency and display_currency and ref_currency != display_currency:
            rate = _fetch_fx_rate(ref_currency, display_currency)
            if rate is None:
                return empty_result
            valid_close = valid_close * rate
        ref_currency = display_currency or ref_currency
    if valid_close.empty:
        return empty_result
    latest_close = _series_value_on_or_before(valid_close, datetime.today())
    if latest_close is None:
        return empty_result

    # Current price in the same currency as the references. The quote's own
    # currency defaults to the reference currency when the caller has none.
    current_price = None
    basis = "Close"
    quote_price, quote_basis = _quote_for_difference(ask, bid)
    if quote_price is not None:
        if convert_to_eur:
            current_price = _convert_price_to_eur_at_date(quote_price, quote_currency or history_currency, datetime.today())
        elif quote_currency and ref_currency:
            current_price = _convert_price_to_currency(quote_price, quote_currency, ref_currency)
        else:
            current_price = quote_price
        ratio = _outlier_ratio(current_price, latest_close)
        if current_price is not None and ratio is not None and ratio >= PRICE_CURRENT_HISTORY_OUTLIER_RATIO:
            print(
                f"    -> diff quote scale mismatch {quote_basis}={current_price:.4g}, "
                f"latest_close={latest_close:.4g}; using latest close"
            )
            current_price = None
        if current_price is not None:
            basis = quote_basis
    if current_price is None:
        current_price = latest_close

    # Vortageskurs: with a live quote the reference is the last close dated
    # before today (intraday bars of today never count). Without one, keep the
    # historical meaning: latest close vs the close before it.
    today = pd.Timestamp(datetime.today().date())
    before_today = valid_close.loc[valid_close.index < today]
    previous_close = None
    if not before_today.empty:
        previous_close = _robust_reference_value_on_or_before(valid_close, pd.Timestamp(before_today.index[-1]), "previous close")
    if basis == "Close":
        percent_reference = None
        if len(valid_close) >= 2:
            percent_reference = _robust_reference_value_on_or_before(valid_close, pd.Timestamp(valid_close.index[-2]), "previous close")
    else:
        percent_reference = previous_close

    year_reference = _robust_reference_value_on_or_before(valid_close, datetime.today() - timedelta(days=365), "one-year")
    month_reference = _robust_reference_value_on_or_before(valid_close, datetime.today() - timedelta(days=30), "one-month")

    return {
        "% Differenz ein Jahr": _percent_change(latest_close, year_reference),
        "% Differenz ein Monat": _percent_change(latest_close, month_reference),
        "% Differenz zum Vortageskurs": _percent_change(current_price, percent_reference),
        "REF_PREV_CLOSE": previous_close,
        DIFF_REF_CURRENCY_COLUMN: ref_currency if previous_close is not None else None,
        DIFF_BASIS_COLUMN: basis,
    }


def _compute_ta_signal_with_source(
    ticker: str,
    isin: Optional[str] = None,
    eodhd_key: Optional[str] = None,
    wkn: Optional[str] = None,
    name: Optional[str] = None,
    prefer_eodhd: bool = False,
) -> tuple[str, str, str]:
    """Return 2-char TA signal string: first char=MACD, second=Stoch.
    '!' = Kauf (most recent signal is a buy crossover)
    '?' = Verkauf (most recent signal is a sell crossover)
    '-' = no crossover signal found in the lookback window
    Returns '' on hard errors (e.g. data download failure).
    Fallback: tries EODHD when yfinance returns no usable data and isin+eodhd_key are provided.
    """
    if not ticker and not isin:
        return "", "", "TA symbol missing"
    try:
        close, high, low, source = _fetch_price_history_with_source(
            ticker,
            isin=isin,
            eodhd_key=eodhd_key,
            wkn=wkn,
            name=name,
            prefer_eodhd=prefer_eodhd,
            lookback_days=TA_LOOKBACK_DAYS,
            context_label="ta",
        )

        if close is None:
            return "", "", "TA no history"
        if len(close.dropna()) < TA_MACD_SLOW + TA_MACD_SIGNAL + 5:
            return "", "", "TA insufficient history"

        # MACD(12,26,9) with pure pandas (no pandas_ta dependency)
        ema_fast = close.ewm(span=TA_MACD_FAST, adjust=False, min_periods=TA_MACD_FAST).mean()
        ema_slow = close.ewm(span=TA_MACD_SLOW, adjust=False, min_periods=TA_MACD_SLOW).mean()
        macd_line = ema_fast - ema_slow
        macd_sig = macd_line.ewm(span=TA_MACD_SIGNAL, adjust=False, min_periods=TA_MACD_SIGNAL).mean()
        macd_buy  = ((macd_line > macd_sig) & (macd_line.shift(1) <= macd_sig.shift(1))).fillna(False)
        macd_sell = ((macd_line < macd_sig) & (macd_line.shift(1) >= macd_sig.shift(1))).fillna(False)

        # Stochastic Slow(14,3,3) with pure pandas
        ll = low.rolling(window=TA_STOCH_K, min_periods=TA_STOCH_K).min()
        hh = high.rolling(window=TA_STOCH_K, min_periods=TA_STOCH_K).max()
        denom = (hh - ll).replace(0, pd.NA)
        fast_k = 100 * (close - ll) / denom
        stoch_k = fast_k.rolling(window=TA_STOCH_SMOOTH, min_periods=TA_STOCH_SMOOTH).mean()
        stoch_d = stoch_k.rolling(window=TA_STOCH_D, min_periods=TA_STOCH_D).mean()
        stoch_buy  = ((stoch_k > stoch_d) & (stoch_k.shift(1) <= stoch_d.shift(1))).fillna(False)
        stoch_sell = ((stoch_k < stoch_d) & (stoch_k.shift(1) >= stoch_d.shift(1))).fillna(False)

        def _last_char(buy_s: pd.Series, sell_s: pd.Series) -> str:
            last_buy  = buy_s[buy_s].index[-1]  if buy_s.any()  else None
            last_sell = sell_s[sell_s].index[-1] if sell_s.any() else None
            if last_buy is None and last_sell is None:
                return "-"
            if last_buy is None:
                return "?"
            if last_sell is None:
                return "!"
            return "!" if last_buy > last_sell else "?"

        return _last_char(macd_buy, macd_sell) + _last_char(stoch_buy, stoch_sell), source, ""
    except Exception:
        return "", "", "TA calculation error"


def compute_ta_signal(
    ticker: str,
    isin: Optional[str] = None,
    eodhd_key: Optional[str] = None,
    wkn: Optional[str] = None,
    name: Optional[str] = None,
    prefer_eodhd: bool = False,
) -> str:
    signal, _source, _reason = _compute_ta_signal_with_source(
        ticker,
        isin=isin,
        eodhd_key=eodhd_key,
        wkn=wkn,
        name=name,
        prefer_eodhd=prefer_eodhd,
    )
    return signal


def _apply_ta_signal(
    df: pd.DataFrame,
    idx: object,
    ticker: str,
    isin: Optional[str] = None,
    eodhd_key: Optional[str] = None,
    wkn: Optional[str] = None,
    name: Optional[str] = None,
    prefer_eodhd: bool = False,
) -> None:
    """Compute TA signal for *ticker* and write it to QUANTITY at *idx*."""
    signal, source, reason = _compute_ta_signal_with_source(
        ticker,
        isin=isin,
        eodhd_key=eodhd_key,
        wkn=wkn,
        name=name,
        prefer_eodhd=prefer_eodhd,
    )
    if not signal:
        if "ENRICH_NOTE" in df.columns:
            df.at[idx, "ENRICH_NOTE"] = _append_note(df.at[idx, "ENRICH_NOTE"], reason)
        return
    formatted_signal = _map_ta_signal_for_display(signal)
    df.at[idx, "QUANTITY"] = f"{formatted_signal[0]}0{formatted_signal[1:]}"
    print(f"    -> TA source {source.upper()} signal={signal}")


def _safe_sheet_name(name: str, used_names: set[str]) -> str:
    clean = re.sub(r"[\\/*?:\[\]]", "_", str(name or "Sheet")).strip() or "Sheet"
    clean = clean[:31] or "Sheet"
    candidate = clean
    suffix = 1
    while candidate in used_names:
        tail = f"_{suffix}"
        candidate = f"{clean[:31 - len(tail)]}{tail}"
        suffix += 1
    used_names.add(candidate)
    return candidate


def _preferred_search_token(isin: Optional[str], wkn: Optional[str], name: Optional[str]) -> str:
    for query in (isin, wkn, name):
        token = str(query or "").strip()
        if token:
            return token
    return ""


def _finanzen_search_url(query: str) -> str:
    return f"{FINANZEN_BASE_URL}/suchergebnis.asp?_search={quote_plus(query)}"


def _marketscreener_search_url(query: str) -> str:
    return f"{MARKETSCREENER_BASE_URL}/search/?q={quote_plus(query)}"


def _duckduckgo_html_search_url(query: str) -> str:
    return f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"


def _onvista_search_url(query: str) -> str:
    return f"{ONVISTA_BASE_URL}/suche/?searchValue={quote_plus(query)}"


def _normalize_external_href(base_url: str, href: str) -> str:
    raw = str(href or "").strip()
    if not raw:
        return ""
    if raw.startswith("http"):
        return raw
    if raw.startswith("/"):
        return f"{base_url}{raw}"
    return ""


def _looks_like_finanzen_detail_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return (
        lowered.startswith(FINANZEN_BASE_URL.lower())
        and "suchergebnis.asp" not in lowered
        and any(token in lowered for token in ("/aktien/", "/etf/", "/fonds/", "/zertifikate/", "/anleihen/"))
    )


def _looks_like_onvista_detail_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return (
        lowered.startswith(ONVISTA_BASE_URL.lower())
        and "/suche/" not in lowered
        and any(token in lowered for token in ("/aktien/", "/etf/", "/fonds/", "/derivate/", "/anleihen/"))
    )


def _looks_like_marketscreener_detail_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return (
        lowered.startswith(MARKETSCREENER_BASE_URL.lower())
        and "/search/" not in lowered
        and "/quote/" in lowered
    )


def _looks_like_access_denied_html(html: str) -> bool:
    lowered = str(html or "").lower()
    return "<title>access denied</title>" in lowered or "you don't have permission to access" in lowered


def _extract_finanzen_detail_url(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    candidates: list[str] = []
    for anchor in soup.find_all("a", href=True):
        url = _normalize_external_href(FINANZEN_BASE_URL, str(anchor.get("href") or ""))
        lowered = url.lower()
        if not _looks_like_finanzen_detail_url(url):
            continue
        if "@stboerse_" in lowered or "/nachricht/" in lowered or "/chart/" in lowered or "/depot/" in lowered:
            continue
        if url not in candidates:
            candidates.append(url)
    return sorted(candidates, key=len)[0] if candidates else ""


def _extract_onvista_detail_url(html: str, isin: Optional[str]) -> str:
    token = str(isin or "").strip().upper()
    soup = BeautifulSoup(html or "", "html.parser")
    preferred: list[str] = []
    fallback: list[str] = []
    for anchor in soup.find_all("a", href=True):
        url = _normalize_external_href(ONVISTA_BASE_URL, str(anchor.get("href") or ""))
        lowered = url.lower()
        if not _looks_like_onvista_detail_url(url):
            continue
        if any(blocked in lowered for blocked in ("/news/", "/orderbuch/", "/chart/", "/handelsplaetze/", "/historische-kurse/", "/kennzahlen/", "/unternehmensprofil/")):
            continue
        if token and token in url.upper():
            if url not in preferred:
                preferred.append(url)
            continue
        if url not in fallback:
            fallback.append(url)
    candidates = preferred or fallback
    return sorted(candidates, key=len)[0] if candidates else ""


def _extract_marketscreener_detail_url(html: str, isin: Optional[str], wkn: Optional[str], name: Optional[str]) -> str:
    candidates = _extract_marketscreener_detail_urls(html)
    if not candidates:
        return ""

    preferred: list[str] = []
    fallback: list[str] = []
    isin_token = str(isin or "").strip().upper()
    wkn_token = str(wkn or "").strip().upper()
    name_token = re.sub(r"\s+", " ", str(name or "").strip().upper())
    for href in candidates[:8]:
        score = 0
        upper_href = href.upper()
        if isin_token and isin_token in upper_href:
            score += 5
        if wkn_token and wkn_token in upper_href:
            score += 3
        if name_token and name_token[:15] and name_token[:15] in upper_href:
            score += 1
        if score > 0:
            preferred.append(href)
        else:
            fallback.append(href)

    candidates = preferred or fallback
    return candidates[0] if candidates else ""


def _extract_marketscreener_detail_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    hrefs: list[str] = []
    for anchor in soup.select("a[href*='/quote/']"):
        href = _normalize_external_href(MARKETSCREENER_BASE_URL, str(anchor.get("href") or ""))
        if href and href not in hrefs:
            hrefs.append(href)

    return hrefs


def _extract_duckduckgo_result_url(href: str) -> str:
    parsed = urlparse(str(href or "").strip())
    if parsed.netloc.lower() not in {"duckduckgo.com", "html.duckduckgo.com"}:
        return ""
    if parsed.path != "/l/":
        return ""
    target = parse_qs(parsed.query).get("uddg", [""])[0]
    return unquote(target).strip()


def _extract_marketscreener_detail_url_from_duckduckgo(html: str, isin: Optional[str], wkn: Optional[str], name: Optional[str], symbol: Optional[str]) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    candidates: list[str] = []
    for anchor in soup.find_all("a", href=True):
        target = _extract_duckduckgo_result_url(str(anchor.get("href") or ""))
        if not _looks_like_marketscreener_detail_url(target):
            continue
        if target not in candidates:
            candidates.append(target)

    best_url = ""
    best_score = 0
    tokens = [
        (str(isin or "").strip().upper(), 5),
        (str(wkn or "").strip().upper(), 3),
        (str(symbol or "").strip().upper(), 3),
        (re.sub(r"\s+", " ", str(name or "").strip().upper())[:24], 2),
        (re.sub(r"\s+", " ", str(name or "").strip().upper())[:15], 1),
    ]
    for candidate in candidates[:12]:
        upper_candidate = candidate.upper()
        score = 0
        for token, weight in tokens:
            if token and token in upper_candidate:
                score += weight
        if score > best_score:
            best_score = score
            best_url = candidate

    return best_url or (candidates[0] if candidates else "")


def resolve_marketscreener_url_via_duckduckgo(
    session: requests.Session,
    isin: Optional[str],
    wkn: Optional[str],
    name: Optional[str],
    symbol: Optional[str],
    timeout: int = 12,
) -> Optional[str]:
    queries: list[str] = []
    query_names = _marketscreener_query_names(name, symbol)
    if isinstance(isin, str) and isin.strip():
        queries.append(f"site:marketscreener.com/quote/ {isin.strip()}")
    if isinstance(symbol, str) and symbol.strip():
        if isinstance(isin, str) and isin.strip():
            queries.append(f"site:marketscreener.com/quote/ {symbol.strip()} {isin.strip()}")
        for query_name in query_names:
            queries.append(f"site:marketscreener.com/quote/ {symbol.strip()} {query_name}")
        queries.append(f"site:marketscreener.com/quote/ {symbol.strip()}")
    for query_name in query_names:
        queries.append(f"site:marketscreener.com/quote/ {query_name}")

    lookup_session = requests.Session()
    for query in queries:
        search_url = _duckduckgo_html_search_url(query)
        try:
            response = lookup_session.get(search_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        except Exception:
            continue

        direct_url = _extract_marketscreener_detail_url_from_duckduckgo(response.text, isin, wkn, name, symbol)
        if direct_url:
            return direct_url
    return None


def _score_marketscreener_detail_page(html: str, url: str, isin: Optional[str], wkn: Optional[str], name: Optional[str]) -> int:
    score = 0
    upper_html = str(html or "").upper()
    upper_url = str(url or "").upper()
    isin_token = str(isin or "").strip().upper()
    wkn_token = str(wkn or "").strip().upper()
    name_token = re.sub(r"\s+", " ", str(name or "").strip().upper())

    if isin_token and isin_token in upper_html:
        score += 5
    if isin_token and isin_token in upper_url:
        score += 2
    if wkn_token and wkn_token in upper_html:
        score += 3
    if wkn_token and wkn_token in upper_url:
        score += 1
    if name_token:
        compact_name = name_token[:24]
        if compact_name and compact_name in upper_html:
            score += 2
        elif name_token[:15] and name_token[:15] in upper_html:
            score += 1
    if "/QUOTE/" in upper_url:
        score += 1
    return score


def resolve_finanzen_url(
    session: requests.Session,
    isin: Optional[str],
    wkn: Optional[str],
    name: Optional[str],
    timeout: int = 12,
) -> Optional[str]:
    cache_key = (
        str(isin or "").strip().upper(),
        str(wkn or "").strip().upper(),
        str(name or "").strip().upper(),
    )
    cached = INSTRUMENT_URL_CACHE.get(cache_key)
    if cached:
        return cached

    queries = [q for q in [isin, wkn] if isinstance(q, str) and q.strip()]
    if isinstance(name, str) and name.strip():
        queries.append(name.strip())

    for query in queries:
        search_url = _finanzen_search_url(query)
        try:
            response = session.get(search_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        except Exception:
            continue

        final_url = str(response.url or "").strip()
        if _looks_like_finanzen_detail_url(final_url):
            INSTRUMENT_URL_CACHE[cache_key] = final_url
            return final_url

        extracted_url = _extract_finanzen_detail_url(response.text)
        if extracted_url:
            INSTRUMENT_URL_CACHE[cache_key] = extracted_url
            return extracted_url

    return None


def resolve_marketscreener_url(
    session: requests.Session,
    isin: Optional[str],
    wkn: Optional[str],
    name: Optional[str],
    symbol: Optional[str] = None,
    timeout: int = 12,
) -> Optional[str]:
    _ensure_marketscreener_login(session, timeout=timeout)

    cache_key = (
        str(isin or "").strip().upper(),
        str(wkn or "").strip().upper(),
        str(name or "").strip().upper(),
    )
    cached = INSTRUMENT_URL_CACHE.get(cache_key)
    if cached and (
        _looks_like_marketscreener_detail_url(cached)
        or cached.startswith(MARKETSCREENER_BASE_URL)
    ):
        return cached

    queries = [q for q in [isin, wkn] if isinstance(q, str) and q.strip()]
    if isinstance(symbol, str) and symbol.strip():
        queries.append(symbol.strip())
    if isinstance(name, str) and name.strip():
        queries.append(name.strip())

    for query in queries:
        search_url = _marketscreener_search_url(query)
        try:
            response = session.get(search_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        except Exception:
            continue

        if _looks_like_access_denied_html(response.text):
            continue

        final_url = str(response.url or "").strip()
        if _looks_like_marketscreener_detail_url(final_url):
            INSTRUMENT_URL_CACHE[cache_key] = final_url
            return final_url

        best_url = ""
        best_score = 0
        for candidate_url in _extract_marketscreener_detail_urls(response.text)[:8]:
            try:
                quote_response = session.get(candidate_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
            except Exception:
                continue

            resolved_url = str(quote_response.url or candidate_url).strip()
            score = _score_marketscreener_detail_page(quote_response.text, resolved_url, isin, wkn, name)
            if score > best_score:
                best_score = score
                best_url = resolved_url if _looks_like_marketscreener_detail_url(resolved_url) else candidate_url

        if best_url and best_score > 0:
            INSTRUMENT_URL_CACHE[cache_key] = best_url
            return best_url

        extracted_url = _extract_marketscreener_detail_url(response.text, isin, wkn, name)
        if extracted_url:
            INSTRUMENT_URL_CACHE[cache_key] = extracted_url
            return extracted_url

        INSTRUMENT_URL_CACHE[cache_key] = search_url
        return search_url

    duckduckgo_url = resolve_marketscreener_url_via_duckduckgo(session, isin, wkn, name, symbol, timeout=timeout)
    if duckduckgo_url:
        INSTRUMENT_URL_CACHE[cache_key] = duckduckgo_url
        return duckduckgo_url

    return None


def resolve_onvista_url(
    session: requests.Session,
    isin: Optional[str],
    wkn: Optional[str],
    name: Optional[str],
    timeout: int = 12,
) -> Optional[str]:
    queries = [q for q in [isin, wkn] if isinstance(q, str) and q.strip()]
    if isinstance(name, str) and name.strip():
        queries.append(name.strip())

    for query in queries:
        search_url = _onvista_search_url(query)
        try:
            response = onvista_http.get(session, search_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        except onvista_http.OnvistaBlocked:
            return None  # onvista is limiting us: more queries would only be refused too
        except Exception:
            continue

        final_url = str(response.url or "").strip()
        if _looks_like_onvista_detail_url(final_url):
            return final_url

        extracted_url = _extract_onvista_detail_url(response.text, isin)
        if extracted_url:
            return extracted_url
    return None


def build_instrument_url(
    session: requests.Session,
    isin: Optional[str],
    wkn: Optional[str],
    name: Optional[str],
    symbol: Optional[str] = None,
) -> str:
    marketscreener_url = resolve_marketscreener_url(session, isin, wkn, name, symbol=symbol)
    if marketscreener_url:
        return marketscreener_url

    onvista_url = resolve_onvista_url(session, isin, wkn, name)
    if onvista_url:
        return onvista_url

    fallback_query = _preferred_search_token(isin, wkn, name)
    if fallback_query:
        return _marketscreener_search_url(fallback_query)
    return ONVISTA_BASE_URL


def _parse_display_number_with_suffix(value: object) -> tuple[Optional[float], str]:
    if pd.isna(value):
        return None, ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), ""

    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return None, ""

    match = re.fullmatch(
        r"([+-]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?)\s*([A-Za-z]{2,5}|%)?(?:\s*(\([^)]*\)))?",
        text,
    )
    if not match:
        return None, ""

    number_text, unit, meta = match.groups()
    if "," in number_text and "." in number_text:
        if number_text.rfind(",") > number_text.rfind("."):
            number_text = number_text.replace(".", "").replace(",", ".")
        else:
            number_text = number_text.replace(",", "")
    elif "," in number_text:
        number_text = number_text.replace(".", "").replace(",", ".")

    try:
        numeric = float(number_text)
    except ValueError:
        return None, ""

    suffix_parts = [part for part in [unit, meta] if part]
    return numeric, " ".join(suffix_parts)


def _excel_literal_suffix_format(suffix: str) -> str:
    clean_suffix = str(suffix or "").strip().replace('"', '""')
    if not clean_suffix:
        return "#,##0.00"
    return f'#,##0.00 "{clean_suffix}"'


FX_TO_EUR_RATE_CACHE: dict[str, Optional[float]] = {}
FX_TO_EUR_HISTORY_CACHE: dict[str, pd.Series] = {}
# Prices quoted in a currency's subunit (pence, cents, agorot) and the currency
# they are 1/100 of. Yahoo reports London as "GBp", Johannesburg as "ZAc" and
# Tel Aviv as "ILA".
SUBUNIT_CURRENCIES = {"GBX": "GBP", "ZAC": "ZAR", "ILA": "ILS"}


def _normalize_price_currency(currency: object) -> str:
    raw_text = str(currency or "").strip()
    if raw_text in {"GBp", "GBX"}:
        return "GBX"
    raw = raw_text.upper()
    aliases = {
        "€": "EUR",
        "EURO": "EUR",
        "EUROS": "EUR",
        "$": "USD",
        "US$": "USD",
        "GBPE": "GBX",
        "PENCE": "GBX",
    }
    return aliases.get(raw, raw)


def _download_fx_to_eur_series(base_currency: str, start: str, end: str) -> Optional[pd.Series]:
    """Daily base_currency -> EUR closes from Yahoo. Some pairs (e.g. ILSEUR=X)
    come back empty or nearly so; fall back to the inverse pair EUR<base>=X."""
    raw = _safe_yf_download(f"{base_currency}EUR=X", start, end)
    close = _extract_history_series(raw, "Close") if raw is not None and not raw.empty else None
    if close is not None and not close.dropna().empty:
        return close
    raw = _safe_yf_download(f"EUR{base_currency}=X", start, end)
    inverse = _extract_history_series(raw, "Close") if raw is not None and not raw.empty else None
    if inverse is None:
        return None
    inverse = pd.to_numeric(inverse, errors="coerce").dropna()
    return 1.0 / inverse[inverse != 0] if not inverse.empty else None


def _fetch_fx_to_eur_rate(currency: object) -> Optional[float]:
    normalized = _normalize_price_currency(currency)
    if not normalized:
        return None
    if normalized == "EUR":
        return 1.0
    if normalized in FX_TO_EUR_RATE_CACHE:
        return FX_TO_EUR_RATE_CACHE[normalized]

    base_currency = SUBUNIT_CURRENCIES.get(normalized, normalized)
    symbol = f"{base_currency}EUR=X"
    try:
        end_dt = datetime.today()
        start_dt = end_dt - timedelta(days=10)
        close = _download_fx_to_eur_series(base_currency, start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
        rate = _series_value_on_or_before(close, end_dt) if close is not None else None
        if rate is not None and normalized in SUBUNIT_CURRENCIES:
            rate = rate / 100.0
        FX_TO_EUR_RATE_CACHE[normalized] = rate
        if rate is None:
            print(f"    ! FX conversion unavailable for {normalized} -> EUR ({symbol})")
        return rate
    except Exception as exc:
        FX_TO_EUR_RATE_CACHE[normalized] = None
        print(f"    ! FX conversion failed for {normalized} -> EUR ({exc})")
        return None


def _fetch_fx_history_to_eur(currency: object, start: object, end: object) -> Optional[pd.Series]:
    normalized = _normalize_price_currency(currency)
    if not normalized:
        return None
    if normalized == "EUR":
        return None
    if normalized in FX_TO_EUR_HISTORY_CACHE:
        return FX_TO_EUR_HISTORY_CACHE[normalized]

    base_currency = SUBUNIT_CURRENCIES.get(normalized, normalized)
    symbol = f"{base_currency}EUR=X"
    try:
        today = pd.Timestamp(datetime.today().date())
        requested_start = pd.Timestamp(start)
        requested_end = pd.Timestamp(end)
        start_date = min(requested_start, today - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        end_date = (max(requested_end, today) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        rates = _download_fx_to_eur_series(base_currency, start_date, end_date)
        if rates is None:
            return None
        rates = pd.to_numeric(rates, errors="coerce").dropna().sort_index()
        rate_index = pd.DatetimeIndex(pd.to_datetime(rates.index))
        if rate_index.tz is not None:
            rate_index = rate_index.tz_localize(None)
        rates.index = rate_index.normalize()
        rates = rates.loc[~rates.index.duplicated(keep="last")]
        if normalized in SUBUNIT_CURRENCIES:
            rates = rates / 100.0
        if rates.empty:
            return None
        FX_TO_EUR_HISTORY_CACHE[normalized] = rates
        return rates
    except Exception as exc:
        print(f"    ! FX history failed for {normalized} -> EUR ({exc})")
        return None


def _convert_price_to_eur_at_date(price: object, currency: object, date: object) -> Optional[float]:
    numeric_price = pd.to_numeric(price, errors="coerce")
    if pd.isna(numeric_price):
        return None
    normalized = _normalize_price_currency(currency)
    if normalized == "EUR":
        return float(numeric_price)
    target_date = pd.Timestamp(date)
    if target_date.tzinfo is not None:
        target_date = target_date.tz_localize(None)
    target_date = target_date.normalize()
    rates = _fetch_fx_history_to_eur(normalized, target_date - pd.Timedelta(days=10), target_date)
    if rates is None or rates.empty:
        return None
    eligible = rates.loc[rates.index <= target_date]
    if eligible.empty or (target_date - eligible.index[-1]).days > 7:
        return None
    return float(numeric_price) * float(eligible.iloc[-1])


def _convert_history_to_eur(prices: pd.Series, currency: object) -> pd.Series:
    numeric_prices = pd.to_numeric(prices, errors="coerce")
    price_dates = pd.DatetimeIndex(pd.to_datetime(numeric_prices.index))
    if price_dates.tz is not None:
        price_dates = price_dates.tz_localize(None)
    price_dates = price_dates.normalize()
    numeric_prices.index = price_dates

    normalized = _normalize_price_currency(currency)
    if normalized == "EUR":
        return numeric_prices
    if not normalized or numeric_prices.empty:
        return pd.Series(float("nan"), index=price_dates, dtype=float)

    rates = _fetch_fx_history_to_eur(
        normalized,
        price_dates.min() - pd.Timedelta(days=10),
        price_dates.max(),
    )
    if rates is None or rates.empty:
        return pd.Series(float("nan"), index=price_dates, dtype=float)
    aligned_rates = rates.reindex(price_dates, method="ffill", tolerance=pd.Timedelta(days=7))
    return numeric_prices * aligned_rates


def _convert_price_to_eur(price: object, currency: object) -> tuple[Optional[float], str]:
    numeric_price = pd.to_numeric(price, errors="coerce")
    if pd.isna(numeric_price):
        return None, ""
    normalized_currency = _normalize_price_currency(currency)
    rate = _fetch_fx_to_eur_rate(normalized_currency)
    if rate is None:
        return float(numeric_price), normalized_currency
    return float(numeric_price) * rate, "EUR"


DISPLAY_CURRENCIES = {"USD", "EUR"}


def _normalize_display_currency(currency: object) -> str:
    """Map a raw currency to USD/EUR, defaulting to EUR for anything else."""
    normalized = _normalize_price_currency(currency)
    if not normalized:
        return ""
    return normalized if normalized in DISPLAY_CURRENCIES else "EUR"


def _convert_price_to_usd_or_eur(price: object, currency: object) -> tuple[Optional[float], str]:
    """Leave USD/EUR prices untouched; convert anything else to EUR."""
    numeric_price = pd.to_numeric(price, errors="coerce")
    if pd.isna(numeric_price):
        return None, ""
    normalized_currency = _normalize_price_currency(currency)
    if normalized_currency in DISPLAY_CURRENCIES:
        return float(numeric_price), normalized_currency
    return _convert_price_to_eur(numeric_price, normalized_currency)


def _fetch_fx_rate(from_currency: object, to_currency: object) -> Optional[float]:
    """Return the multiplier to convert an amount in from_currency into to_currency, via EUR."""
    from_normalized = _normalize_price_currency(from_currency)
    to_normalized = _normalize_price_currency(to_currency)
    if not from_normalized or not to_normalized:
        return None
    if from_normalized == to_normalized:
        return 1.0
    from_to_eur = _fetch_fx_to_eur_rate(from_normalized)
    to_to_eur = _fetch_fx_to_eur_rate(to_normalized)
    if not from_to_eur or not to_to_eur:
        return None
    return from_to_eur / to_to_eur


def _convert_price_to_currency(price: object, from_currency: object, to_currency: object) -> Optional[float]:
    numeric_price = pd.to_numeric(price, errors="coerce")
    if pd.isna(numeric_price) or not to_currency:
        return None
    rate = _fetch_fx_rate(from_currency, to_currency)
    if rate is None:
        return None
    return float(numeric_price) * rate


def _current_price_for_kurspotential(
    bid: object, ask: object, native_currency: object, display_currency: str
) -> Optional[float]:
    """Return the Ask price converted to display_currency."""
    numeric = pd.to_numeric(ask, errors="coerce")
    if pd.isna(numeric):
        return None
    return _convert_price_to_currency(float(numeric), native_currency, display_currency)


ONVISTA_PRICE_PLAUSIBILITY_MAX_RATIO = 5.0
# Ask/Bid above this is no real quote. Real spreads of listed stocks and funds
# are a few percent, illiquid small caps reach tens of percent. Below
# ONVISTA_SPREAD_MIN_PRICE the tick size alone makes ratios like 2 normal
# (0.001 / 0.002), so those are not checked.
ONVISTA_MAX_SPREAD_RATIO = 1.5
ONVISTA_SPREAD_MIN_PRICE = 1.0


def _is_onvista_spread_implausible(bid: object, ask: object) -> bool:
    bid_numeric = pd.to_numeric(bid, errors="coerce")
    ask_numeric = pd.to_numeric(ask, errors="coerce")
    if pd.isna(bid_numeric) or pd.isna(ask_numeric) or bid_numeric <= 0 or ask_numeric <= 0:
        return False
    if min(float(bid_numeric), float(ask_numeric)) < ONVISTA_SPREAD_MIN_PRICE:
        return False
    return max(float(bid_numeric), float(ask_numeric)) / min(float(bid_numeric), float(ask_numeric)) > ONVISTA_MAX_SPREAD_RATIO


def _is_onvista_price_plausible(
    onvista_price: object,
    onvista_currency: object,
    reference_price: object,
    reference_currency: object,
) -> bool:
    """Sanity check an onvista Bid/Ask against an independent reference price
    (e.g. Yahoo's current_price). Returns True when there's nothing to compare
    against (so it never blocks a value out of caution alone) and False only
    when both are known and differ by more than ONVISTA_PRICE_PLAUSIBILITY_MAX_RATIO."""
    onvista_numeric = pd.to_numeric(onvista_price, errors="coerce")
    reference_numeric = pd.to_numeric(reference_price, errors="coerce")
    if pd.isna(onvista_numeric) or pd.isna(reference_numeric) or reference_numeric <= 0:
        return True
    converted_reference = _convert_price_to_currency(float(reference_numeric), reference_currency, onvista_currency)
    if converted_reference is None or converted_reference <= 0 or onvista_numeric <= 0:
        return True
    ratio = float(onvista_numeric) / converted_reference
    return (1.0 / ONVISTA_PRICE_PLAUSIBILITY_MAX_RATIO) <= ratio <= ONVISTA_PRICE_PLAUSIBILITY_MAX_RATIO


# Yahoo's analyst target-price field is occasionally corrupted for a specific
# ticker with no secondary source to cross-check against — seen for FME.DE
# (Fresenius Medical Care's Xetra listing), where targetMeanPrice/Low/High
# were all ~200x the actual quote, producing a Kurspotential % in the tens of
# thousands. Legitimate small/micro-cap outliers in this watchlist have
# topped out around a 5x target/current ratio, so 15x leaves generous room
# for real analyst optimism while still catching that kind of bad data.
TARGET_PRICE_PLAUSIBILITY_MAX_RATIO = 15.0


def _is_target_price_plausible(target_price: object, current_price: object) -> bool:
    """Sanity check an analyst target price against the current price. Returns
    True when there's nothing to compare against (so it never blocks a value
    out of caution alone) and False only when both are known and differ by
    more than TARGET_PRICE_PLAUSIBILITY_MAX_RATIO."""
    target_numeric = pd.to_numeric(target_price, errors="coerce")
    current_numeric = pd.to_numeric(current_price, errors="coerce")
    if pd.isna(target_numeric) or pd.isna(current_numeric) or current_numeric <= 0 or target_numeric <= 0:
        return True
    ratio = float(target_numeric) / float(current_numeric)
    return (1.0 / TARGET_PRICE_PLAUSIBILITY_MAX_RATIO) <= ratio <= TARGET_PRICE_PLAUSIBILITY_MAX_RATIO


CHARTMILL_TARGET_CACHE: dict[str, dict] = {}
CHARTMILL_TARGET_RE = re.compile(
    r"average price target is\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]{3})\."
)


def fetch_chartmill_target_data(symbol: str) -> dict:
    """Cross-check source for the analyst target price, used only when Yahoo's
    targetMeanPrice fails _is_target_price_plausible() against the current
    price — Yahoo's field is occasionally corrupted for a specific ticker
    with nothing to cross-check against otherwise (seen for FME.DE, Fresenius
    Medical Care's Xetra listing: targetMeanPrice=8075 EUR against a ~40 EUR
    quote). ChartMill.com keys its analyst-ratings page by the same ticker
    format as our YahooSymbol/equity_ticker (e.g. "FME.DE", "AAPL"); its
    Cloudflare bot-challenge blocks plain requests but passes with
    curl_cffi's browser TLS impersonation — same technique this module
    already relies on for onvista.de.
    """
    symbol = str(symbol or "").strip()
    if not symbol or curl_cffi_requests is None:
        return {}
    if symbol in CHARTMILL_TARGET_CACHE:
        return CHARTMILL_TARGET_CACHE[symbol]

    data: dict = {}
    try:
        session = curl_cffi_requests.Session(impersonate="chrome")
        response = session.get(
            f"https://www.chartmill.com/stock/quote/{symbol}/analyst-ratings", timeout=20
        )
        if response.status_code == 200:
            match = CHARTMILL_TARGET_RE.search(response.text)
            if match:
                data = {
                    "target_mean": float(match.group(1)),
                    "currency": match.group(2).upper(),
                    "source": "chartmill",
                }
    except Exception as exc:
        print(f"    ! ChartMill target lookup failed for {symbol}: {exc}")

    CHARTMILL_TARGET_CACHE[symbol] = data
    return data


def write_xlsx(sheets: dict[str, pd.DataFrame], path: str) -> None:
    used_names: set[str] = set()
    sheet_names: dict[str, str] = {}
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for requested_name, df in sheets.items():
            safe_name = _safe_sheet_name(requested_name, used_names)
            sheet_names[requested_name] = safe_name
            export_df = df.drop(columns=["ISIN_URL"], errors="ignore").copy()
            export_df.to_excel(writer, index=False, sheet_name=safe_name)

    wb = load_workbook(path)

    header_font = Font(name="Arial", bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", start_color="305496")
    header_align = Alignment(horizontal="center", vertical="center")
    body_font = Font(name="Arial")
    widths = {
        "WKN": 9,
        "STOCK_ID": 10,
        "QUANTITY": 10,
        "INSTRUMENT_TYPE": 14,
        "COMMENT": 26,
        "COMMENT2": 22,
        "COMMENT3": 12,
        "ISIN": 14,
        "Name": 30,
        "% Differenz ein Jahr": 12,
        "% Differenz ein Monat": 12,
        "Bid": 11,
        "Ask": 11,
        "Bid Time": 20,
        "Ask Time": 20,
        "% Differenz zum Vortageskurs": 14,
        "DETAIL_SYMBOL": 16,
        "DETAIL_SOURCE": 12,
        "DEV": 10,
        "Kurspotential": 14,
        "Kurspotential %": 14,
    }
    price_cols = {
        "Bid",
        "Ask",
        "Kurspotential",
        "Kurspotential %",
        "% Differenz ein Jahr",
        "% Differenz ein Monat",
        "% Differenz zum Vortageskurs",
    }
    display_number_cols = {"COMMENT2", "Bid", "Ask"}
    date_cols = {"Date"}

    for requested_name, df in sheets.items():
        ws = wb[sheet_names[requested_name]]
        visible_df = df.drop(columns=["ISIN_URL"], errors="ignore")
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align

        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.font = body_font

        for idx, col_name in enumerate(visible_df.columns, start=1):
            letter = get_column_letter(idx)
            ws.column_dimensions[letter].width = widths.get(col_name, 14)
            if col_name in DIFF_REF_COLUMNS or col_name == DIFF_REF_CURRENCY_COLUMN:
                ws.column_dimensions[letter].hidden = True
            if col_name in display_number_cols:
                for cell in ws[letter][1:]:
                    numeric, suffix = _parse_display_number_with_suffix(cell.value)
                    if numeric is None:
                        continue
                    cell.value = numeric
                    cell.number_format = _excel_literal_suffix_format(suffix)
                continue
            fmt = None
            if col_name in price_cols:
                fmt = "#,##0.00"
            elif col_name in date_cols:
                fmt = "dd.mm.yyyy"
            if fmt:
                for cell in ws[letter][1:]:
                    if col_name in date_cols:
                        if cell.value is not None:
                            parsed = pd.to_datetime(cell.value, errors="coerce")
                            if pd.notna(parsed):
                                cell.value = parsed.strftime("%d.%m.%Y")
                    else:
                        cell.number_format = fmt
            if col_name == "COMMENT3":
                for cell in ws[letter][1:]:
                    cell.value = _normalize_comment3_text(cell.value)

        if "ISIN" in visible_df.columns and "ISIN_URL" in df.columns:
            isin_col_idx = visible_df.columns.get_loc("ISIN") + 1
            for row_idx, (_, row) in enumerate(df.iterrows(), start=2):
                isin_value = "" if pd.isna(row.get("ISIN")) else str(row.get("ISIN") or "").strip()
                isin_url = "" if pd.isna(row.get("ISIN_URL")) else str(row.get("ISIN_URL") or "").strip()
                if not isin_value or not isin_url:
                    continue
                cell = ws.cell(row=row_idx, column=isin_col_idx)
                cell.value = isin_value
                cell.hyperlink = isin_url
                cell.style = "Hyperlink"

        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    wb.save(path)


def _format_price_with_timestamp(
    price: object,
    fetched_at: object,
    currency: object = None,
    convert_to_display_currency: bool = False,
    include_currency: bool = True,
) -> str:
    numeric_price = pd.to_numeric(price, errors="coerce")
    if pd.isna(numeric_price):
        return ""

    display_price = float(numeric_price)
    currency_text = "" if pd.isna(currency) else str(currency or "").strip()
    if convert_to_display_currency:
        converted_price, converted_currency = _convert_price_to_usd_or_eur(display_price, currency_text)
        if converted_price is not None:
            display_price = converted_price
            currency_text = converted_currency

    # Penny stocks quote below one cent; two decimals would show 0.005 as 0,01.
    decimals = 2 if abs(display_price) >= 0.1 else 4
    price_text = f"{display_price:.{decimals}f}".replace(".", ",").rstrip("0").rstrip(",")
    if currency_text and include_currency:
        price_text = f"{price_text} {currency_text}"
    return price_text


def _prepare_output_df(df: pd.DataFrame, include_detail_columns: bool, strip_fund_country_from_comment: bool = True) -> pd.DataFrame:
    output_df = df.copy()
    output_df = _drop_blank_output_rows(output_df)
    if strip_fund_country_from_comment and {"COMMENT", "INSTRUMENT_TYPE"}.issubset(output_df.columns):
        fund_like_mask = output_df["INSTRUMENT_TYPE"].fillna("").astype(str).str.lower().isin({"etf", "fund"})
        output_df.loc[fund_like_mask, "COMMENT"] = output_df.loc[fund_like_mask, "COMMENT"].apply(_strip_country_from_comment)
    if {"Bid", "Ask"}.issubset(output_df.columns):
        output_df["Bid"] = output_df.apply(
            lambda row: _format_price_with_timestamp(
                row.get("Bid"),
                row.get("BID_FETCHED_AT"),
                row.get("DEV"),
                convert_to_display_currency=True,
                include_currency=False,
            ),
            axis=1,
        )
        output_df["Ask"] = output_df.apply(
            lambda row: _format_price_with_timestamp(
                row.get("Ask"),
                row.get("ASK_FETCHED_AT"),
                row.get("DEV"),
                convert_to_display_currency=True,
                include_currency=False,
            ),
            axis=1,
        )
    if "DEV" in output_df.columns:
        output_df["DEV"] = output_df["DEV"].apply(
            lambda value: _normalize_display_currency(value) if pd.notna(value) and str(value).strip() else ""
        )
    output_df = output_df.drop(columns=["VOLUMEN_MRD", "TREND", "Laufende Kosten", "onvista_URL", "ENRICH_NOTE", "TREND_TEXT", "MA50", "MA200", "Replikation", "Ertragsverwendung", "_SOURCE_COMMENT"], errors="ignore")
    if not include_detail_columns:
        output_df = output_df.drop(columns=["DETAIL_SOURCE", "DETAIL_SYMBOL"], errors="ignore")
    output_df = _drop_blank_output_columns(output_df)
    return output_df


def _drop_blank_output_columns(df: pd.DataFrame) -> pd.DataFrame:
    keep_internal = {"_IS_SEPARATOR_ROW", "ISIN_URL"}
    if "_IS_SEPARATOR_ROW" in df.columns:
        data_mask = ~df["_IS_SEPARATOR_ROW"].fillna(False)
    else:
        data_mask = pd.Series(True, index=df.index)

    drop_columns = []
    for column in df.columns:
        if column in keep_internal:
            continue
        values = df.loc[data_mask, column] if bool(data_mask.any()) else df[column]
        blank_mask = values.isna() | values.astype("string").str.strip().eq("").fillna(True)
        if bool(blank_mask.all()):
            drop_columns.append(column)
    return df.drop(columns=drop_columns, errors="ignore")


def _drop_blank_output_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    output_df = df.copy()
    if "_IS_SEPARATOR_ROW" in output_df.columns:
        output_df = output_df.loc[~output_df["_IS_SEPARATOR_ROW"].fillna(False)].copy()

    check_columns = [
        column for column in output_df.columns
        if column not in {"_IS_SEPARATOR_ROW", "ISIN_URL"}
    ]
    if not check_columns:
        return output_df

    nonblank_mask = ~output_df[check_columns].apply(
        lambda row: all(pd.isna(value) or str(value).strip() == "" for value in row),
        axis=1,
    )
    return output_df.loc[nonblank_mask].copy()


def _comment3_has_double_bang(value: object) -> bool:
    if pd.isna(value):
        return False
    normalized = re.sub(r"\s+", "", str(value))
    return "11" in normalized


def _normalize_comment3_text(value: object) -> str:
    text = "" if pd.isna(value) else str(value or "")
    return re.sub(r"(?<=[12\-])\s+(?=[12\-])", " ", text)


def _is_positive_export_value(value: object) -> bool:
    numeric = pd.to_numeric(value, errors="coerce")
    return bool(pd.notna(numeric) and float(numeric) > 0)


def _top_list_mask(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=bool, index=df.index)

    source_df = df
    if "_IS_SEPARATOR_ROW" in source_df.columns:
        separator_mask = source_df["_IS_SEPARATOR_ROW"].fillna(False)
    else:
        separator_mask = pd.Series(False, index=source_df.index)

    required_columns = [
        "COMMENT3",
        "% Differenz ein Jahr",
        "% Differenz ein Monat",
        "% Differenz zum Vortageskurs",
    ]
    if any(column not in source_df.columns for column in required_columns):
        return pd.Series(False, index=source_df.index)

    mask = ~separator_mask
    mask &= source_df["COMMENT3"].apply(_comment3_has_double_bang)
    for column in required_columns[1:]:
        mask &= source_df[column].apply(_is_positive_export_value)
    return mask


def _populate_isin_links(df: pd.DataFrame, linked_indexes: Optional[set[object]] = None) -> pd.DataFrame:
    linked_df = df.copy()
    if "ISIN_URL" not in linked_df.columns:
        linked_df["ISIN_URL"] = pd.NA

    eligible_mask = _top_list_mask(linked_df)
    if "INSTRUMENT_TYPE" in linked_df.columns:
        eligible_mask &= linked_df["INSTRUMENT_TYPE"].fillna("").astype(str).str.lower().isin({"equity", "etf", "fund"})
    else:
        eligible_mask &= False

    if linked_indexes is not None:
        eligible_mask &= linked_df.index.to_series().isin(linked_indexes)

    linked_df.loc[:, "ISIN_URL"] = pd.NA
    session = requests.Session()
    eligible_rows = list(linked_df.loc[eligible_mask].iterrows())
    if eligible_rows:
        print(f"  [links] resolving ISIN links for {len(eligible_rows)} Top List row(s)")
    for pos, (idx, row) in enumerate(eligible_rows, start=1):
        isin = str(row.get("ISIN") or "").strip()
        if not isin:
            continue
        wkn = None if pd.isna(row.get("WKN")) else str(row.get("WKN") or "").strip()
        name = None if pd.isna(row.get("Name")) else str(row.get("Name") or "").strip()
        symbol = None if pd.isna(row.get("DETAIL_SYMBOL")) else str(row.get("DETAIL_SYMBOL") or "").strip()
        print(f"    [link {pos}/{len(eligible_rows)}] {isin} {name or ''}".rstrip())
        if not symbol and str(row.get("INSTRUMENT_TYPE") or "").strip().lower() == "equity":
            symbol, _source, _instrument_hint = resolve_equity_ticker(isin, session)
        url = build_instrument_url(session, isin, wkn, name, symbol=symbol)
        linked_df.at[idx, "ISIN_URL"] = url
        print(f"      -> {url}")
    return linked_df


def build_top_list_df(df: pd.DataFrame) -> pd.DataFrame:
    source_df = df.copy()
    return source_df.loc[_top_list_mask(source_df)].copy()


ACTIVE_TRADER_EXPORT_COLUMNS = [
    "WKN",
    "STOCK_ID",
    "QUANTITY",
    "COMMENT",
    "COMMENT2",
    "COMMENT3",
    "ISIN",
    "Name",
    "Bid",
    "BID_FETCHED_AT",
    "Ask",
    "ASK_FETCHED_AT",
    "% Differenz zum Vortageskurs",
    "% Differenz ein Monat",
    "% Differenz ein Jahr",
    "Kurspotential",
    "Kurspotential %",
    "DEV",
    *DIFF_REF_COLUMNS,
    DIFF_BASIS_COLUMN,
    DIFF_REF_CURRENCY_COLUMN,
]
ACTIVE_TRADER_MNEMONIC_ROW = "MNEMONIC:WKN; ;;;;; ;;;;;;;;;;;" + ";" * (len(DIFF_REF_COLUMNS) + 2)
_CSV_COMMA_DECIMAL_COLS = {
    "% Differenz ein Jahr",
    "% Differenz ein Monat",
    "% Differenz zum Vortageskurs",
    "Kurspotential",
    "Kurspotential %",
}


def _prepare_active_trader_export_df(df: pd.DataFrame, *, keep_internal_columns: bool = True) -> pd.DataFrame:
    export_df = df.copy()
    if "WKN" in export_df.columns:
        export_df = export_df.loc[
            export_df["WKN"].apply(lambda value: not pd.isna(value) and str(value).strip() != "")
        ].copy()

    for column in ACTIVE_TRADER_EXPORT_COLUMNS:
        if column not in export_df.columns:
            export_df[column] = ""

    export_df["WKN"] = export_df["WKN"].apply(lambda value: "" if pd.isna(value) else str(value).strip())
    export_df["STOCK_ID"] = export_df["STOCK_ID"].apply(
        lambda value: " " if pd.isna(value) or str(value).strip() == "" else str(value).strip()
    )
    for column in [
        "COMMENT", "COMMENT2", "COMMENT3", "ISIN", "Bid", "Ask",
        "BID_FETCHED_AT", "ASK_FETCHED_AT", "DEV",
    ]:
        export_df[column] = export_df[column].apply(lambda value: "" if pd.isna(value) else str(value).strip())
    export_df["COMMENT3"] = export_df["COMMENT3"].apply(_normalize_comment3_text)

    internal_columns = []
    if keep_internal_columns:
        internal_columns = [column for column in ["_IS_SEPARATOR_ROW", "ISIN_URL"] if column in export_df.columns]
    return export_df[ACTIVE_TRADER_EXPORT_COLUMNS + internal_columns].rename(
        columns={
            "BID_FETCHED_AT": "Bid Time",
            "ASK_FETCHED_AT": "Ask Time",
        }
    ).copy()


def _format_csv_value(column_name: str, value: object) -> str:
    if pd.isna(value):
        return ""
    if column_name in DIFF_REF_COLUMNS:
        numeric = pd.to_numeric(value, errors="coerce")
        if pd.notna(numeric):
            # References stay unrounded enough for sub-cent prices.
            return f"{float(numeric):.6f}".rstrip("0").rstrip(".").replace(".", ",")
    if column_name in _CSV_COMMA_DECIMAL_COLS:
        numeric = pd.to_numeric(value, errors="coerce")
        if pd.notna(numeric):
            return f"{float(numeric):.2f}".replace(".", ",")
    return str(value)


def _escape_excel_formula_text(value: str) -> str:
    return str(value or "").replace('"', '""')


def _format_csv_cell(row: pd.Series, column_name: str) -> str:
    value = row.get(column_name)
    if column_name == "ISIN":
        display_value = _format_csv_value(column_name, value)
        url = "" if pd.isna(row.get("ISIN_URL")) else str(row.get("ISIN_URL") or "").strip()
        if display_value and url:
            return f'=HYPERLINK("{_escape_excel_formula_text(url)}","{_escape_excel_formula_text(display_value)}")'
    return _format_csv_value(column_name, value)


def write_csv_preserving_blank_rows(df: pd.DataFrame, path: str) -> None:
    export_df = df.copy()
    visible_columns = [column for column in export_df.columns if column != "ISIN_URL"]
    if "_IS_SEPARATOR_ROW" in export_df.columns:
        separator_mask = export_df["_IS_SEPARATOR_ROW"].fillna(False)
        export_df = export_df.drop(columns=["_IS_SEPARATOR_ROW"], errors="ignore")
        visible_columns = [column for column in visible_columns if column != "_IS_SEPARATOR_ROW"]
    else:
        separator_mask = pd.Series(False, index=export_df.index)

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=";", lineterminator="\n")
        writer.writerow(visible_columns + [""])
        handle.write(f"{ACTIVE_TRADER_MNEMONIC_ROW}\n")

        for idx, row in export_df.iterrows():
            if bool(separator_mask.loc[idx]):
                handle.write("\n")
                continue

            values = []
            for column_name in visible_columns:
                values.append(_format_csv_value(column_name, row.get(column_name)))
            values.append("")
            writer.writerow(values)


def write_outputs(
    df: pd.DataFrame,
    preferred_xlsx_path: str,
    save_csv: bool = True,
    include_detail_columns: bool = False,
    generate_isin_links: bool = True,
    source_csv_path: Optional[str] = None,
    allow_source_csv_overwrite: bool = False,
    strip_fund_country_from_comment: bool = True,
) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    linked_indexes = set(build_top_list_df(df).index) if generate_isin_links else set()
    output_df = _prepare_output_df(df, include_detail_columns, strip_fund_country_from_comment=strip_fund_country_from_comment)
    if generate_isin_links:
        output_df = _populate_isin_links(output_df, linked_indexes=linked_indexes)
    else:
        output_df = output_df.drop(columns=["ISIN_URL"], errors="ignore")
    output_df = output_df.drop(columns=["INSTRUMENT_TYPE"], errors="ignore")
    output_df = _prepare_active_trader_export_df(output_df, keep_internal_columns=True)
    base_sheet_name = "Merged"
    try:
        write_xlsx(
            {
                base_sheet_name: output_df.drop(columns=["_IS_SEPARATOR_ROW"], errors="ignore"),
            },
            preferred_xlsx_path,
        )
    except PermissionError:
        print(f"    ! output file is in use, skipped: {preferred_xlsx_path}")
        return False, None, None, None

    csv_path = None
    if save_csv:
        csv_df = output_df.copy()
        csv_path = os.path.splitext(preferred_xlsx_path)[0] + ".csv"
        if (
            not allow_source_csv_overwrite
            and source_csv_path
            and os.path.normcase(os.path.abspath(csv_path)) == os.path.normcase(os.path.abspath(source_csv_path))
        ):
            print(f"    ! skipped csv to avoid overwriting input: {csv_path}")
            csv_path = None
        else:
            try:
                write_csv_preserving_blank_rows(csv_df, csv_path)
            except PermissionError:
                print(f"    ! csv file is in use, skipped csv: {csv_path}")
                csv_path = None
    return True, preferred_xlsx_path, csv_path, None


def _comment_subcategory_sort_key(comment: object) -> str:
    """Extract the subcategory text from COMMENT for sorting."""
    if pd.isna(comment):
        return "\xff"
    text = _strip_country_from_comment(comment)
    if not text:
        return "\xff"
    return text.casefold()


def sort_df_by_comment_subcategory(df: pd.DataFrame) -> pd.DataFrame:
    """Sort *df* rows by the subcategory (text after '|') in the COMMENT column.

    Rows without a COMMENT column are returned unchanged.  The sort is stable
    so rows with the same subcategory keep their original relative order.
    """
    comment_col = next(
        (c for c in df.columns if str(c).strip().lower() == "comment"), None
    )
    if comment_col is None:
        return df

    sort_key = df[comment_col].map(_comment_subcategory_sort_key)
    return df.iloc[sort_key.argsort(kind="stable")].reset_index(drop=True)


def sort_xlsx_by_comment_subcategory(input_path: Path, output_path: Path) -> None:
    """Sort an existing XLSX file in place by COMMENT subcategory.

    Reads *input_path* with pandas (losing openpyxl formatting), removes fully
    blank rows, sorts by COMMENT subcategory, and writes to *output_path*.
    Use this only for XLSX files that were *not* produced by write_xlsx (which
    already applies formatting).  For pipeline output, sorting happens before
    write_xlsx so formatting is preserved.
    """
    df = pd.read_excel(input_path)
    blank_mask = df.map(lambda v: pd.isna(v) or (isinstance(v, str) and not v.strip())).all(axis=1)
    df = df.loc[~blank_mask].copy()
    df = sort_df_by_comment_subcategory(df)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(output_path, index=False)


def tag_csv_comments(input_csv: str, output_csv: Optional[str] = None, use_llm_fallback: bool = True) -> str:
    source_label = os.path.basename(input_csv)
    sector = _infer_sector_from_source_label(source_label)
    if not sector:
        known = ", ".join(SECTOR_FILE_HINTS)
        raise ValueError(f"Cannot infer sector from filename: {source_label}. Known sectors: {known}")

    sector_tags = [
        str(row.get("Tag") or "").strip()
        for row in SUBCATEGORY_ROWS
        if str(row.get("Sector") or "").strip() == sector and str(row.get("Tag") or "").strip()
    ]
    if not sector_tags:
        raise ValueError(f"No tags found for sector '{sector}' in {SUBCATEGORIES_PATH}")

    df = read_csv_for_tagging(input_csv)
    before = df["COMMENT"].astype(str).str.strip().replace("nan", "").ne("").sum() if "COMMENT" in df.columns else 0
    update_comment_with_subcategory(
        df,
        source_label,
        use_llm_fallback=use_llm_fallback,
        allow_new_tags=False,
    )
    after = df["COMMENT"].astype(str).str.strip().replace("nan", "").ne("").sum() if "COMMENT" in df.columns else 0

    sort_dataframe_by_subcategories(df, source_label)
    out_path = output_csv or input_csv
    output_df = _drop_blank_output_rows(df.drop(columns=["_SOURCE_COMMENT"], errors="ignore"))
    output_df = _drop_blank_output_columns(output_df)
    output_df = output_df.drop(columns=["INSTRUMENT_TYPE"], errors="ignore")
    write_csv_preserving_blank_rows(output_df, out_path)
    print(f"[tag-only] sector={sector}, tags={len(sector_tags)}, COMMENT filled={after} (was {before})")
    print(f"[tag-only] wrote {out_path}")
    return out_path


def search_yahoo_symbol(query: str, session: requests.Session, allowed_quote_types: Optional[set[str]] = None) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(query, str) or not query.strip():
        return None, None
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = session.get(
                YAHOO_SEARCH_URL,
                params={"q": query, "quotesCount": 8, "newsCount": 0},
                headers=YAHOO_API_HEADERS,
                timeout=10,
            )
            response.raise_for_status()
            quotes = response.json().get("quotes", [])
            for quote in quotes:
                quote_type = str(quote.get("quoteType") or "").upper()
                symbol = quote.get("symbol")
                if not symbol:
                    continue
                if allowed_quote_types is None or quote_type in allowed_quote_types:
                    return _normalize_yahoo_symbol(str(symbol)), quote_type
            return None, None
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
                continue
    if last_error is not None:
        print(f"    ! Yahoo search failed for {query}: {last_error}")
    return None, None


def resolve_equity_ticker(isin: str, session: requests.Session) -> tuple[Optional[str], Optional[str], Optional[str]]:
    symbol, source = _resolve_symbol_via_local_override(isin)
    if symbol:
        _hint_symbol, _exchange, instrument_hint = _resolve_symbol_via_openfigi_with_hint(isin, DEFAULT_OPENFIGI_API_KEY)
        return symbol, source or "LOCAL_OVERRIDE", instrument_hint
    symbol, _exchange, instrument_hint = _resolve_symbol_via_openfigi_with_hint(isin, DEFAULT_OPENFIGI_API_KEY)
    if symbol:
        if _should_refine_openfigi_symbol_with_yahoo(isin, symbol):
            yahoo_symbol, _quote_type = search_yahoo_symbol(isin, session, allowed_quote_types={"EQUITY"})
            if _should_prefer_yahoo_equity_symbol(symbol, yahoo_symbol):
                return yahoo_symbol, "YAHOO_SEARCH", instrument_hint
        return symbol, "OPENFIGI", instrument_hint
    symbol, _quote_type = search_yahoo_symbol(isin, session, allowed_quote_types={"EQUITY"})
    return symbol, "YAHOO_SEARCH" if symbol else None, None


def isin_to_yahoo_ticker(isin: str, session: requests.Session) -> Optional[str]:
    symbol, _source, _instrument_hint = resolve_equity_ticker(isin, session)
    return symbol


def resolve_fund_like_symbol(isin: str, name: str, session: requests.Session) -> tuple[Optional[str], Optional[str]]:
    if isin in MANUAL_FUND_TICKERS:
        return MANUAL_FUND_TICKERS[isin], "MANUAL"
    symbol, source = _resolve_symbol_via_local_override(isin)
    if symbol:
        return symbol, source
    symbol, exch_code = _resolve_symbol_via_openfigi(isin, DEFAULT_OPENFIGI_API_KEY)
    if symbol:
        return symbol, f"OPENFIGI:{exch_code}" if exch_code else "OPENFIGI"
    symbol, source = _resolve_symbol_via_justetf(isin, session)
    if symbol:
        return symbol, source
    symbol, quote_type = search_yahoo_symbol(isin, session, allowed_quote_types=ENRICHABLE_FUND_TYPES)
    if symbol:
        return symbol, quote_type
    if isinstance(name, str) and name.strip():
        symbol, quote_type = search_yahoo_symbol(name, session, allowed_quote_types=ENRICHABLE_FUND_TYPES)
        if symbol:
            return symbol, quote_type
    return None, None


YAHOO_FETCH_MAX_RETRIES = 2
YAHOO_FETCH_RETRY_BACKOFF_SECONDS = 3.0


def fetch_analyst_data(ticker: str) -> dict:
    info = {}
    last_error: Optional[Exception] = None
    for candidate in _yahoo_symbol_candidates(ticker):
        for attempt in range(YAHOO_FETCH_MAX_RETRIES + 1):
            try:
                info = _safe_yf_info(candidate)
                last_error = None
                break
            except Exception as e:
                last_error = e
                if attempt < YAHOO_FETCH_MAX_RETRIES:
                    # Transient failures (rate limiting, timeouts) are common on
                    # long batches; back off briefly and retry before giving up
                    # on this symbol candidate.
                    time.sleep(YAHOO_FETCH_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
        if info:
            break
    if not info and last_error is not None:
        print(f"    ! yfinance fetch failed for {ticker}: {last_error}")
        return {}
    return {
        "quote_type": info.get("quoteType"),
        "currency": info.get("currency"),
        "country": info.get("country"),
        "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "target_mean": info.get("targetMeanPrice"),
        "target_high": info.get("targetHighPrice"),
        "target_low": info.get("targetLowPrice"),
        "analysts": info.get("numberOfAnalystOpinions"),
        "source": "yahoo",
    }


def _instrument_text(row: pd.Series) -> str:
    comment_text = row.get("_SOURCE_COMMENT")
    if pd.isna(comment_text) or not str(comment_text or "").strip():
        comment_text = row.get("COMMENT", "")
    return " ".join(str(value or "") for value in [comment_text, row.get("Name", ""), row.get("STOCK_ID", ""), row.get("WKN", "")]).upper()


def classify_instrument(row: pd.Series, equity_ticker: Optional[str], instrument_hint: Optional[str] = None) -> str:
    text = _instrument_text(row)
    if any(token in text for token in CERTIFICATE_KEYWORDS):
        return "certificate"
    if any(token in text for token in FUND_KEYWORDS):
        return "fund"
    if any(token in text for token in ETF_KEYWORDS):
        return "etf"
    if any(token in text for token in INDEX_KEYWORDS):
        return "index"
    if instrument_hint in {"etf", "fund"}:
        return instrument_hint
    if equity_ticker:
        return "equity"
    return "other"


def normalize_instrument_type(instrument_type: str, quote_type: Optional[str]) -> str:
    quote_key = str(quote_type or "").upper()
    if quote_key == "ETF":
        return "etf"
    if quote_key in {"MUTUALFUND", "FUND", "MANUAL"}:
        return "fund" if instrument_type != "etf" else instrument_type
    return instrument_type


def fetch_fund_market_data_yahoo(symbol: str) -> dict:
    if not symbol:
        return {"fehler": "Kein Yahoo-Symbol"}
    info = {}
    last_error: Optional[Exception] = None
    used_symbol = _normalize_yahoo_symbol(symbol)
    for candidate in _yahoo_symbol_candidates(symbol):
        used_symbol = candidate
        try:
            info = _safe_yf_info(candidate)
            if info:
                break
        except Exception as e:
            last_error = e
            continue
    if not info and last_error is not None:
        return {"fehler": str(last_error)[:80], "detail_source": "yahoo", "detail_symbol": used_symbol}
    return {
        "waehrung": info.get("currency", ""),
        "country": info.get("country") or info.get("domicile"),
        "detail_source": "yahoo",
        "detail_symbol": used_symbol,
        "fehler": None,
    }


def fetch_fund_market_data_eodhd(isin: str, api_key: str, wkn: Optional[str] = None, name: Optional[str] = None) -> dict:
    if not isin or not api_key:
        return {"fehler": "Kein EODHD API Key"}
    try:
        fallback_symbol = None
        fallback_source = None
        fund_url = f"https://eodhd.com/api/fundamentals/{isin}?api_token={api_key}&fmt=json"
        response = requests.get(fund_url, timeout=10)
        data = response.json() if response.status_code == 200 else {}
        general = data.get("General", {}) if isinstance(data, dict) else {}
        ticker_code = general.get("Code", "")
        exchange = general.get("Exchange", "")
        currency = general.get("CurrencyCode", "")
        if not ticker_code or not exchange:
            fallback_symbol, fallback_source = _resolve_eodhd_symbol_via_market_fetcher(isin, wkn=wkn, name=name)
            if not fallback_symbol:
                error_text = f"HTTP {response.status_code}" if response.status_code != 200 else "Kein EODHD Ticker"
                return {"waehrung": currency, "fehler": error_text, "detail_source": "eodhd"}

        return {
            "waehrung": currency,
            "country": general.get("CountryName") or general.get("Country", ""),
            "detail_source": fallback_source or "eodhd",
            "detail_symbol": fallback_symbol or f"{ticker_code}.{exchange}",
            "fehler": None,
        }
    except Exception as e:
        return {"fehler": str(e)[:80], "detail_source": "eodhd"}


def apply_enrichment_columns(df: pd.DataFrame) -> None:
    for column in [
        "INSTRUMENT_TYPE",
        "ISIN_URL",
        "DETAIL_SYMBOL",
        "DETAIL_SOURCE",
        "ENRICH_NOTE",
        "BID_FETCHED_AT",
        "ASK_FETCHED_AT",
        "% Differenz ein Jahr",
        "% Differenz ein Monat",
        "% Differenz zum Vortageskurs",
        *DIFF_REF_COLUMNS,
        DIFF_BASIS_COLUMN,
        DIFF_REF_CURRENCY_COLUMN,
        "Kurspotential",
        "Kurspotential %",
        *ONVISTA_OUTPUT_COLUMNS,
    ]:
        if column not in df.columns:
            df[column] = pd.NA


def apply_blank_comment(df: pd.DataFrame, args: argparse.Namespace) -> None:
    """When --blank-comment is set, force COMMENT empty regardless of whatever
    tagging/preservation logic ran during enrichment."""
    if not getattr(args, "blank_comment", False) or "COMMENT" not in df.columns:
        return
    if "_IS_SEPARATOR_ROW" in df.columns:
        separator_mask = df["_IS_SEPARATOR_ROW"].fillna(False)
    else:
        separator_mask = pd.Series(False, index=df.index)
    df.loc[~separator_mask, "COMMENT"] = ""


def _append_note(existing: object, extra: str) -> str:
    base = "" if pd.isna(existing) else str(existing or "").strip()
    add = str(extra or "").strip()
    if not add:
        return base
    if not base:
        return add
    return f"{base} | {add}"


def _normalize_country_name(country: object) -> str:
    normalized = re.sub(r"\s+", " ", str(country or "").strip())
    if not normalized:
        return ""
    upper = normalized.upper()
    if upper in ISIN_COUNTRY_MAP:
        return upper
    return COUNTRY_NAME_TO_CODE.get(normalized.lower(), normalized)


def _is_country_label(value: object) -> bool:
    text = "" if pd.isna(value) else re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return False
    normalized = _normalize_country_name(text)
    known_codes = {code.upper() for code in ISIN_COUNTRY_MAP.values()}
    known_names = set(COUNTRY_NAME_TO_CODE.keys())
    return normalized.upper() in known_codes or text.lower() in known_names


def _country_from_isin(isin: object) -> str:
    token = str(isin or "").strip().upper()
    if len(token) < 2:
        return ""
    return ISIN_COUNTRY_MAP.get(token[:2], "")


def _country_from_data(data: Optional[dict], isin: object = None) -> str:
    payload = data or {}
    for key in ("country", "domicile", "CountryName", "Country"):
        country = _normalize_country_name(payload.get(key))
        if country:
            return country
    return _country_from_isin(isin)


def _append_country_to_comment(existing: object, country: str) -> str:
    base = "" if pd.isna(existing) else str(existing or "").strip()
    normalized_country = _normalize_country_name(country)
    if not normalized_country:
        return base

    parts = [part.strip() for part in base.split("|") if part.strip()]
    if parts and _is_country_label(parts[0]):
        parts = parts[1:]
    if parts and _is_country_label(parts[-1]):
        parts = parts[:-1]

    if not parts:
        return normalized_country
    return f"{' | '.join(parts)} | {normalized_country}"


def _strip_country_from_comment(value: object) -> str:
    text = "" if pd.isna(value) else str(value or "").strip()
    if not text:
        return ""

    parts = [part.strip() for part in text.split("|") if part.strip()]
    if parts and _is_country_label(parts[0]):
        parts = parts[1:]
    if parts and _is_country_label(parts[-1]):
        parts = parts[:-1]
    if not parts and _is_country_label(text):
        return ""
    return " | ".join(parts) if parts else text


def _tokenize_taxonomy_text(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9&]+", str(text or "").lower()))
    return {token for token in tokens if len(token) >= 3 and token not in TAXONOMY_STOPWORDS}


def _normalize_match_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _company_match_key(name: object) -> str:
    """Normalize company names for cache/LLM matching.

    Broker exports often append share nominal values such as "DL 1",
    "EO -,01", or "O.N.". These suffixes are not part of the company identity
    and otherwise make cached LLM classifications miss.
    """
    normalized = _normalize_match_text("" if pd.isna(name) else str(name or ""))
    tokens = normalized.split()
    if not tokens:
        return ""

    nominal_markers = {
        "aud", "cad", "chf", "dk", "dl", "eo", "eur", "gbp", "hd",
        "ir", "ls", "nk", "rc", "sf", "sk", "usd", "yc", "zy",
    }
    trailing_share_tokens = {"na", "nam", "st", "vz", "vzo"}

    changed = True
    while changed and tokens:
        changed = False

        if len(tokens) >= 2 and tokens[-2:] == ["o", "n"]:
            tokens = tokens[:-2]
            changed = True

        while tokens and tokens[-1] in trailing_share_tokens:
            tokens = tokens[:-1]
            changed = True

        remove_from = None
        start = max(0, len(tokens) - 6)
        for pos in range(len(tokens) - 1, start - 1, -1):
            if tokens[pos] in nominal_markers:
                tail = tokens[pos + 1 :]
                if not tail or all(re.fullmatch(r"\d+", token) for token in tail):
                    remove_from = pos
                    break
        if remove_from is not None:
            tokens = tokens[:remove_from]
            changed = True

        start = max(0, len(tokens) - 6)
        adr_pos = next(
            (pos for pos in range(start, len(tokens)) if tokens[pos] in {"adr", "adrs", "gdr"}),
            None,
        )
        if adr_pos is not None:
            if adr_pos > 0 and tokens[adr_pos - 1] in {"sp", "spon"}:
                adr_pos -= 1
            tokens = tokens[:adr_pos]
            changed = True

    return " ".join(tokens) or normalized


def _subcategory_sectors() -> list[str]:
    seen: set[str] = set()
    sectors: list[str] = []
    for row in SUBCATEGORY_ROWS:
        sector = str(row.get("Sector") or "").strip()
        key = sector.casefold()
        if sector and key not in seen:
            sectors.append(sector)
            seen.add(key)
    return sectors


def _infer_sector_from_source_label(source_label: str) -> str:
    haystack = _normalize_match_text(source_label)
    if not haystack:
        return ""

    padded_haystack = f" {haystack} "
    for sector in sorted(_subcategory_sectors(), key=lambda value: len(_normalize_match_text(value)), reverse=True):
        normalized_sector = _normalize_match_text(sector)
        if normalized_sector and f" {normalized_sector} " in padded_haystack:
            return sector

    for sector, hints in SECTOR_FILE_HINTS.items():
        if any(hint in haystack for hint in hints):
            return sector
    return ""


def _subcategory_keywords(row: dict[str, str]) -> set[str]:
    keywords = set()
    for key in ("Sector", "Tag", "Description"):
        keywords.update(_tokenize_taxonomy_text(row.get(key, "")))
    return keywords


def _infer_subcategory_tag(name: str, comment: object, sector: str) -> str:
    if not SUBCATEGORY_ROWS or not sector:
        return ""

    haystack = f"{str(name or '')} {'' if pd.isna(comment) else str(comment or '')}".lower()
    haystack_normalized = _normalize_match_text(haystack)
    haystack_tokens = _tokenize_taxonomy_text(haystack)
    if not haystack_tokens:
        return ""

    sector_rows = [row for row in SUBCATEGORY_ROWS if str(row.get("Sector") or "").strip() == sector]
    if not sector_rows:
        return ""
    if len(sector_rows) == 1:
        return str(sector_rows[0].get("Tag") or "").strip()

    alias_rows = SECTOR_TAG_ALIASES.get(sector, {})
    padded_haystack = f" {haystack_normalized} "
    alias_candidates: list[tuple[int, str, str]] = []
    for tag, aliases in alias_rows.items():
        for alias in aliases:
            alias_normalized = _normalize_match_text(alias)
            if alias_normalized:
                alias_candidates.append((len(alias_normalized), alias_normalized, tag))
    for _length, alias_normalized, tag in sorted(alias_candidates, key=lambda item: item[0], reverse=True):
        if f" {alias_normalized} " in padded_haystack:
            return tag

    best_label = ""
    best_score = 0
    best_specific = False
    for row in sector_rows:
        tag_keywords = _tokenize_taxonomy_text(row.get("Tag", ""))
        desc_keywords = _subcategory_keywords(row)
        if not desc_keywords:
            continue
        if tag_keywords and haystack_tokens & tag_keywords:
            return str(row["Tag"])

        matching = haystack_tokens & desc_keywords
        score = len(matching)
        # A single match on a specific (>=5 char) token is a strong enough signal
        # (e.g. "tesla", "ameresco", "solaredge" explicitly named in description)
        specific = any(len(t) >= 5 for t in matching)
        if score > best_score or (score == best_score and specific and not best_specific):
            best_score = score
            best_label = str(row["Tag"])
            best_specific = specific

    if best_score >= 2:
        return best_label
    if best_score == 1 and best_specific:
        return best_label
    return ""


def _known_tags_for_sector(sector: str) -> set[str]:
    return {
        str(r.get("Tag") or "").strip()
        for r in SUBCATEGORY_ROWS
        if str(r.get("Sector") or "").strip() == sector and str(r.get("Tag") or "").strip()
    }


def _extract_known_subcategory_from_comment(comment: object, sector: str) -> str:
    if pd.isna(comment) or not sector:
        return ""
    text = str(comment or "").strip()
    if not text:
        return ""

    known_tags = _known_tags_for_sector(sector)
    if not known_tags:
        return ""

    known_by_casefold = {tag.casefold(): tag for tag in known_tags}
    parts = [part.strip() for part in text.split("|") if part.strip()]
    if parts and _is_country_label(parts[0]):
        parts = parts[1:]
    if parts and _is_country_label(parts[-1]):
        parts = parts[:-1]

    candidates = parts or [text]
    for candidate in candidates:
        tag = known_by_casefold.get(candidate.casefold())
        if tag:
            return tag
    return ""


LLM_TAG_CACHE_PATH = SCRIPT_DIR / "llm_tag_cache.json"
_llm_tag_cache: dict[str, str] = {}


def _load_llm_tag_cache() -> None:
    global _llm_tag_cache
    if LLM_TAG_CACHE_PATH.exists():
        try:
            with LLM_TAG_CACHE_PATH.open("r", encoding="utf-8") as fh:
                _llm_tag_cache = json.load(fh)
        except Exception:
            _llm_tag_cache = {}


def _save_llm_tag_cache() -> None:
    try:
        with LLM_TAG_CACHE_PATH.open("w", encoding="utf-8") as fh:
            json.dump(_llm_tag_cache, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _llm_tag_cache_key(sector: str, name: str, allow_new_tags: bool, provider: str, model: str) -> str:
    mode = "new" if allow_new_tags else "known"
    return f"{sector.casefold()}|||{mode}|||{provider.casefold()}|||{model.casefold()}|||{_company_match_key(name)}"


def _legacy_llm_tag_cache_key(sector: str, name: str, allow_new_tags: bool, provider: str, model: str) -> str:
    mode = "new" if allow_new_tags else "known"
    return f"{sector.casefold()}|||{mode}|||{provider.casefold()}|||{model.casefold()}|||{_normalize_match_text(name)}"


def _llm_config() -> dict:
    return dict(LOCAL_CONFIG.get("llm", {}) or {})


def _classification_llm_settings() -> Optional[dict[str, str]]:
    cfg = _llm_config()
    provider = str(
        CLASSIFICATION_PROVIDER_OVERRIDE
        or cfg.get("classification_provider")
        or cfg.get("llm_provider")
        or os.getenv("WATCHLIST_LLM_PROVIDER", "")
        or "deepseek"
    ).strip().lower()
    aliases = {
        "mini-max": "minimax",
        "mini_max": "minimax",
        "deep-seek": "deepseek",
        "deep_seek": "deepseek",
        "local": "ollama",
        "none": "none",
        "off": "none",
    }
    provider = aliases.get(provider, provider)
    model_override = str(CLASSIFICATION_MODEL_OVERRIDE or "").strip()

    if provider == "none":
        return None

    if provider == "ollama":
        return {
            "provider": "ollama",
            "label": "Ollama",
            "api_type": "ollama",
            "api_key": "",
            "base_url": str(cfg.get("ollama_base_url") or "http://localhost:11434").rstrip("/"),
            "model": model_override or str(cfg.get("ollama_model") or "qwen2.5:3b").strip(),
        }

    if provider == "minimax":
        api_key = str(cfg.get("minimax_api_key") or os.getenv("MINIMAX_API_KEY", "")).strip()
        return {
            "provider": "minimax",
            "label": "MiniMax",
            "api_type": "openai",
            "api_key": api_key,
            "base_url": str(cfg.get("minimax_base_url") or "https://api.minimaxi.com/v1").rstrip("/"),
            "model": model_override or str(cfg.get("minimax_model") or "MiniMax-M2.7-highspeed").strip(),
        } if api_key else None

    if provider == "deepseek":
        api_key = str(cfg.get("deepseek_api_key") or os.getenv("DEEPSEEK_API_KEY", "")).strip()
        return {
            "provider": "deepseek",
            "label": "DeepSeek",
            "api_type": "openai",
            "api_key": api_key,
            "base_url": str(cfg.get("deepseek_base_url") or "https://api.deepseek.com/v1").rstrip("/"),
            "model": model_override or str(cfg.get("deepseek_model") or "deepseek-chat").strip(),
        } if api_key else None

    print(f"[LLM] Unsupported classification_provider: {provider}")
    return None


def _llm_classify_batch(
    names: list[str],
    sector: str,
    allow_new_tags: bool = True,
    settings: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Classify company names into subcategory tags via the configured LLM.
    Returns dict: company name -> tag (may include new tags when enabled)."""
    settings = settings or _classification_llm_settings()
    if not settings:
        return {}

    api_key = settings["api_key"]
    base_url = settings["base_url"]
    model = settings["model"]
    label = settings["label"]
    api_type = settings.get("api_type", "openai")
    if not names:
        return {}
    if api_type == "openai" and not api_key:
        return {}

    sector_rows = [r for r in SUBCATEGORY_ROWS if str(r.get("Sector") or "").strip() == sector]
    tag_list = "\n".join(f"- {r['Tag']}: {r.get('Description', '')}" for r in sector_rows)
    company_list = "\n".join(f"- {n}" for n in names)
    tag_instruction = (
        "If no existing tag fits well, propose a concise new tag name (2-4 words)."
        if allow_new_tags
        else "Choose only from the known tag list. Do not create new tags."
    )
    prompt = (
        f"You are a financial analyst classifying companies into sector subcategories.\n"
        f"Sector: {sector}\n\n"
        f"Known subcategory tags:\n{tag_list}\n\n"
        f"For each company, choose the best matching tag from the list above.\n"
        f"{tag_instruction}\n"
        f"Return ONLY a JSON object: {{\"company name\": \"tag name\", ...}}\n"
        f"Use exact existing tag names when they fit. No markdown, no explanations.\n\n"
        f"Companies:\n{company_list}"
    )

    try:
        print(f"[{label}] Classifying {len(names)} companies (sector={sector}) ...")
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a precise financial taxonomy assistant. "
                    "Always return valid JSON only."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        if api_type == "ollama":
            url = f"{base_url}/api/chat"
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": 0.0},
            }
            resp = requests.post(url, json=payload, timeout=180)
            resp.raise_for_status()
            msg = resp.json().get("message") or {}
        else:
            url = f"{base_url}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {
                "model": model,
                "messages": messages,
                "temperature": 0.0,
                "max_tokens": 8000,
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
        content = msg.get("content") or ""
        if not content:
            content = msg.get("reasoning_content") or ""
        if not content:
            print(f"[{label}] Empty response content")
            return {}
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
        content = re.sub(r"```(?:json)?", "", content).strip().rstrip("`")
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end == -1:
            print(f"[{label}] No JSON object found in response, raw content: {content[:300]!r}")
            return {}
        result: dict[str, str] = json.loads(content[start : end + 1])
        print(f"[{label}] Successfully classified {len(result)} companies")
        return result
    except Exception as exc:
        print(f"[{label}] Batch classification failed: {exc}")
        return {}


def _append_new_tags_to_subcategories(sector: str, new_tag_companies: dict[str, list[str]]) -> None:
    """Append newly discovered tags to subcategories.csv under the correct sector."""
    if not new_tag_companies:
        return
    try:
        # Read existing tags from disk (case-insensitive) to prevent duplicates across runs.
        existing_keys: set[str] = set()
        if SUBCATEGORIES_PATH.exists():
            with SUBCATEGORIES_PATH.open("r", encoding="utf-8", newline="") as rfh:
                for row in csv.DictReader(rfh):
                    s = str(row.get("Sector") or "").strip()
                    t = str(row.get("Tag") or "").strip()
                    if s and t:
                        existing_keys.add(f"{s.casefold()}|||{t.casefold()}")

        with SUBCATEGORIES_PATH.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["Sector", "Tag", "Description"])
            for tag, companies in new_tag_companies.items():
                key = f"{sector.casefold()}|||{tag.casefold()}"
                if key in existing_keys:
                    print(f"  [subcategories] Skipping duplicate tag: {sector} / {tag}")
                    continue
                sample = ", ".join(companies[:10])
                writer.writerow({"Sector": sector, "Tag": tag, "Description": f"Key holdings: {sample}."})
                print(f"  [subcategories] Added tag: {sector} / {tag} ({len(companies)} companies)")
                existing_keys.add(key)
    except Exception as exc:
        print(f"  [subcategories] Failed to write new tags: {exc}")


def update_comment_with_subcategory(
    df: pd.DataFrame,
    source_label: str,
    use_llm_fallback: bool = True,
    allow_new_tags: bool = False,
) -> None:
    if "COMMENT" not in df.columns or not SUBCATEGORY_ROWS:
        return

    sector = _infer_sector_from_source_label(source_label)
    if not sector:
        return

    # Phase 1: rule-based matching
    unmatched_indices: list[int] = []
    for idx, row in df.iterrows():
        if bool(row.get("_IS_SEPARATOR_ROW", False)):
            continue
        current = row.get("COMMENT")
        source_comment = row.get("_SOURCE_COMMENT")
        existing_tag = _extract_known_subcategory_from_comment(source_comment, sector)
        if existing_tag:
            df.at[idx, "COMMENT"] = existing_tag
            continue
        comment_for_inference = source_comment if not pd.isna(source_comment) else current
        tag = _infer_subcategory_tag(row.get("Name", ""), comment_for_inference, sector)
        if tag:
            base = "" if pd.isna(current) else str(current or "").strip()
            if base.strip().lower() != tag.strip().lower():
                df.at[idx, "COMMENT"] = tag
        else:
            unmatched_indices.append(idx)

    if not unmatched_indices:
        return

    # Phase 2: configured LLM fallback for rows that rule-based matching couldn't classify
    settings = _classification_llm_settings()
    if not use_llm_fallback or not settings:
        return

    _load_llm_tag_cache()
    known_tags = _known_tags_for_sector(sector)
    provider = settings["provider"]
    model = settings["model"]

    names: list[str] = []
    idx_map: dict[str, int] = {}
    idx_by_company_key: dict[str, int] = {}
    cached_result: dict[str, str] = {}
    for idx in unmatched_indices:
        name = str(df.at[idx, "Name"] or "").strip()
        if name and name not in idx_map:
            company_key = _company_match_key(name)
            cache_key = _llm_tag_cache_key(sector, name, allow_new_tags, provider, model)
            legacy_cache_key = _legacy_llm_tag_cache_key(sector, name, allow_new_tags, provider, model)
            cached_tag = str(_llm_tag_cache.get(cache_key) or _llm_tag_cache.get(legacy_cache_key) or "").strip()
            if cached_tag:
                cached_result[name] = cached_tag
                idx_map[name] = idx
                if company_key:
                    idx_by_company_key.setdefault(company_key, idx)
                    _llm_tag_cache[cache_key] = cached_tag
                continue
            names.append(name)
            idx_map[name] = idx
            if company_key:
                idx_by_company_key.setdefault(company_key, idx)

    llm_result: dict[str, str] = dict(cached_result)
    if not names and not llm_result:
        return

    fresh_result = _llm_classify_batch(names, sector, allow_new_tags=allow_new_tags, settings=settings)
    if fresh_result:
        llm_result.update(fresh_result)
        for name, tag in fresh_result.items():
            clean_tag = str(tag or "").strip()
            if clean_tag:
                _llm_tag_cache[_llm_tag_cache_key(sector, name, allow_new_tags, provider, model)] = clean_tag
        _save_llm_tag_cache()

    new_tag_companies: dict[str, list[str]] = {}
    for name, tag in llm_result.items():
        tag = str(tag or "").strip()
        if not tag:
            continue
        if not allow_new_tags and tag not in known_tags:
            continue
        idx = idx_map.get(name)
        if idx is None:
            idx = idx_by_company_key.get(_company_match_key(name))
        if idx is not None:
            df.at[idx, "COMMENT"] = tag
        if tag not in known_tags:
            new_tag_companies.setdefault(tag, []).append(name)

    if allow_new_tags:
        _append_new_tags_to_subcategories(sector, new_tag_companies)


def _subcategory_order_map(sector: str) -> dict[str, int]:
    if not sector:
        return {}
    order_map: dict[str, int] = {}
    for row in SUBCATEGORY_ROWS:
        row_sector = str(row.get("Sector") or "").strip()
        tag = str(row.get("Tag") or "").strip()
        if row_sector != sector or not tag:
            continue
        if tag not in order_map:
            order_map[tag] = len(order_map)
    return order_map


def _comment_subcategory_tag(value: object, order_map: dict[str, int]) -> str:
    text = "" if pd.isna(value) else str(value or "").strip()
    if not text:
        return ""
    if text in order_map:
        return text

    parts = [part.strip() for part in text.split("|") if part.strip()]
    if parts and _is_country_label(parts[0]):
        parts = parts[1:]
    if parts and _is_country_label(parts[-1]):
        parts = parts[:-1]
    remainder = " | ".join(parts).strip()
    if remainder in order_map:
        return remainder
    for part in reversed(parts):
        if part in order_map:
            return part
    return remainder or text


def sort_dataframe_by_subcategories(df: pd.DataFrame, source_label: str) -> None:
    if "COMMENT" not in df.columns:
        return

    sector = _infer_sector_from_source_label(source_label)
    if not sector:
        return

    order_map = _subcategory_order_map(sector)
    if not order_map:
        return

    if "_IS_SEPARATOR_ROW" in df.columns:
        non_separator_mask = ~df["_IS_SEPARATOR_ROW"].fillna(False)
    else:
        non_separator_mask = pd.Series(True, index=df.index)

    if not bool(non_separator_mask.any()):
        return

    sortable = df.loc[non_separator_mask].copy()
    sortable["_TAG_SORT"] = sortable["COMMENT"].map(_comment_subcategory_sort_key)
    if "Name" in sortable.columns:
        sortable["_NAME_SORT"] = sortable["Name"].astype(str).str.strip().str.lower()
        sortable = sortable.sort_values(by=["_TAG_SORT", "_NAME_SORT"], kind="stable")
        sortable = sortable.drop(columns=["_NAME_SORT"], errors="ignore")
    else:
        sortable = sortable.sort_values(by=["_TAG_SORT"], kind="stable")

    sortable = sortable.drop(columns=["_TAG_SORT"], errors="ignore")
    df.loc[non_separator_mask, sortable.columns] = sortable.to_numpy()


def find_onvista_url(isin: str, session: requests.Session) -> Optional[str]:
    for path in ["etf", "fonds"]:
        direct_url = f"https://www.onvista.de/{path}/{isin}"
        try:
            response = onvista_http.get(session, direct_url, headers=HEADERS, timeout=10, allow_redirects=True)
            if response.status_code == 200 and isin in response.url:
                return response.url
        except onvista_http.OnvistaBlocked:
            return None
        except requests.RequestException:
            pass

    search_url = f"https://www.onvista.de/suche/?searchValue={isin}"
    try:
        response = onvista_http.get(session, search_url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
        for link in soup.find_all("a", href=True):
            href = str(link.get("href") or "")
            if ("/etf/" in href or "/fonds/" in href) and isin in href:
                return "https://www.onvista.de" + href if href.startswith("/") else href
    except requests.RequestException:
        pass

    return None


def _parse_onvista_price(value: object) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"([0-9]+(?:\.[0-9]{3})*(?:,[0-9]+)?|[0-9]+(?:,[0-9]+)?)", text)
    if not match:
        return None
    normalized = match.group(1).replace(".", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def _normalize_onvista_text(value: object) -> str:
    return re.sub(r"[\s\u00ad]+", " ", str(value or "")).strip()


_ONVISTA_PRICE_AFTER_LABEL = re.compile(
    r"\s*([0-9]+(?:\.[0-9]{3})*(?:,[0-9]+)?|[0-9]+(?:,[0-9]+)?)(?![0-9A-Za-z])"
)


def _extract_onvista_price_and_currency_near_label(text: str, label: str) -> tuple[Optional[float], str]:
    """The price (and its currency code) written directly after an occurrence of
    `label`. Tries every occurrence in order and returns the first one that is
    followed by a number.

    onvista repeats "Geld"/"Brief" as bare column headers (once per exchange, and in
    the table of knock-out certificates further down), and a page whose main quote
    block has no "Geld ... EUR" line (a German stock after the exchange's close) has
    only those headers. Looking a few characters ahead for any number read digits of
    the certificate product codes as the Ask ("MK6Z1H" gave 6, "MN74SQ" gave 74), so
    the number has to follow the label immediately, and may not run into letters.
    """
    normalized = _normalize_onvista_text(text)
    if not normalized:
        return None, ""

    for label_match in re.finditer(rf"\b{re.escape(label)}\b", normalized, flags=re.IGNORECASE):
        price_match = _ONVISTA_PRICE_AFTER_LABEL.match(normalized, label_match.end())
        if not price_match:
            continue
        price = _parse_onvista_price(price_match.group(1))
        if price is None:
            continue
        # London lines are quoted in pence, written "GBp" (lower-case p). Read it
        # as GBX, not GBP: upper-casing it would be off by a factor of 100. "ZAc"
        # (South African cents) upper-cases to the ZAC subunit code.
        tail = normalized[price_match.end():price_match.end() + 10]
        currency_match = re.match(r"\s*(GBp|ZAc|[A-Z]{3})\b", tail)
        currency = str(currency_match.group(1) or "").strip() if currency_match else ""
        currency = "GBX" if currency == "GBp" else currency.upper()
        return price, currency
    return None, ""


def _extract_onvista_compact_value(text: str, label: str, stop_labels: tuple[str, ...]) -> str:
    normalized = _normalize_onvista_text(text)
    if not normalized:
        return ""

    pattern = rf"{re.escape(label)}\s*(.+?)(?=(?:{'|'.join(re.escape(item) for item in stop_labels)})\b|$)"
    match = re.search(pattern, normalized, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip()


def _extract_onvista_line_value(
    lines: list[str],
    labels: tuple[str, ...],
    stop_labels: tuple[str, ...],
    require_number: bool = False,
) -> str:
    normalized_lines = [_normalize_onvista_text(line) for line in lines if _normalize_onvista_text(line)]
    normalized_labels = tuple(_normalize_onvista_text(label).lower() for label in labels)
    stop_label_set = {item.lower() for item in stop_labels}

    for index, line in enumerate(normalized_lines):
        lowered_line = line.lower()
        if lowered_line not in normalized_labels:
            continue
        for next_line in normalized_lines[index + 1:index + 5]:
            lowered_next = next_line.lower()
            if lowered_next in normalized_labels:
                continue
            if any(stop_label in lowered_next for stop_label in stop_label_set):
                break
            if require_number and not re.search(r"\d", next_line):
                continue
            return next_line
    return ""


def _extract_onvista_named_value(
    lines: list[str],
    compact_text: str,
    label: str,
    stop_labels: tuple[str, ...],
    require_number: bool = False,
) -> str:
    normalized_label = label.lower()
    normalized_lines = [_normalize_onvista_text(line) for line in lines if _normalize_onvista_text(line)]
    stop_label_set = {item.lower() for item in stop_labels}

    for index, line in enumerate(normalized_lines):
        if normalized_label not in line.lower():
            continue

        same_line_match = re.search(rf"{re.escape(label)}\s*[:\-]?\s*(.+)$", line, flags=re.IGNORECASE)
        if same_line_match:
            candidate = same_line_match.group(1).strip()
            if (
                candidate
                and candidate.lower() != normalized_label
                and (not require_number or re.search(r"\d", candidate))
            ):
                return candidate

        for next_line in normalized_lines[index + 1:index + 5]:
            lowered_next = next_line.lower()
            if lowered_next == normalized_label:
                continue
            if any(stop_label in lowered_next for stop_label in stop_label_set):
                break
            if require_number and not re.search(r"\d", next_line):
                continue
            return next_line

    candidate = _extract_onvista_compact_value(compact_text, label, stop_labels)
    if require_number and not re.search(r"\d", candidate):
        return ""
    return candidate


def extract_onvista_fields(url: str, session: requests.Session, isin: Optional[str] = None) -> dict:
    """With `isin`, result["onvista_page_ok"] says whether the page showed it: onvista answers
    an address it does not know with its search page (status 200), so that is how a cached
    address that went stale is told apart from a page that merely has no quote."""
    result = {
        "onvista_URL": url,
        "onvista_page_ok": None,
        "Laufende Kosten": "",
        "Replikation": "",
        "Ertragsverwendung": "",
        "Bid": None,
        "Ask": None,
        "QUOTE_CURRENCY": "",
        "onvista_error": "",
    }

    try:
        response = onvista_http.get(session, url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text(separator="\n")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        compact_text = soup.get_text(separator=" ")
        if isin:
            result["onvista_page_ok"] = response.status_code == 200 and isin.strip().upper() in compact_text.upper()

        result["Laufende Kosten"] = _extract_onvista_line_value(
            lines,
            ("Laufende Kosten",),
            ("Benchmark", "Replikationsmethode", "Ertragsverwendung", "Morningstar", "Ausgabeaufschlag", "Verwaltungsgebühr", "Fondsvolumen"),
            require_number=True,
        ) or _extract_onvista_named_value(
            lines,
            compact_text,
            "Laufende Kosten",
            ("Benchmark", "Replikationsmethode", "Ertragsverwendung", "Morningstar", "Ausgabeaufschlag", "Verwaltungsgebühr", "Fondsvolumen"),
            require_number=True,
        )
        result["Replikation"] = _extract_onvista_line_value(
            lines,
            ("Replikations methode", "Replikationsmethode", "Replikations"),
            ("Ertragsverwendung", "Ertrags ver wendung", "Morningstar", "Merkmal", "Kosten und Erträge", "Fondsvolumen"),
        ) or _extract_onvista_named_value(
            lines,
            compact_text,
            "Replikationsmethode",
            ("Ertragsverwendung", "Morningstar", "Merkmal", "Kosten und Erträge", "Fondsvolumen"),
        ) or _extract_onvista_named_value(
            lines,
            compact_text,
            "Replikations",
            ("Ertragsverwendung", "Morningstar", "Merkmal", "Kosten und Erträge", "Fondsvolumen"),
        )
        result["Ertragsverwendung"] = _extract_onvista_line_value(
            lines,
            ("Ertragsverwendung", "Ertrags ver wendung"),
            ("Morningstar", "Merkmal", "Kosten und Erträge", "Benchmark", "Replikationsmethode"),
        ) or _extract_onvista_named_value(
            lines,
            compact_text,
            "Ertragsverwendung",
            ("Morningstar", "Merkmal", "Kosten und Erträge", "Benchmark", "Replikationsmethode"),
        ) or _extract_onvista_named_value(
            lines,
            compact_text,
            "Ertrags ver wendung",
            ("Morningstar", "Merkmal", "Kosten und Erträge", "Benchmark", "Replikationsmethode"),
        )

        result["Bid"], bid_currency = _extract_onvista_price_and_currency_near_label(compact_text, "Geld")
        if bid_currency:
            result["QUOTE_CURRENCY"] = bid_currency

        result["Ask"], ask_currency = _extract_onvista_price_and_currency_near_label(compact_text, "Brief")
        if not result["QUOTE_CURRENCY"] and ask_currency:
            result["QUOTE_CURRENCY"] = ask_currency
    except Exception as e:
        result["onvista_error"] = str(e)

    return result


def fetch_onvista_fields(
    isin: str,
    instrument_type: str,
    session: requests.Session,
    wkn: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    result = {column: "" for column in ONVISTA_OUTPUT_COLUMNS}
    result["onvista_error"] = ""
    result["Laufende Kosten"] = ""
    result["Replikation"] = ""
    result["Ertragsverwendung"] = ""
    result["Bid"] = None
    result["Ask"] = None
    result["QUOTE_CURRENCY"] = ""

    def look_up_url() -> Optional[str]:
        if instrument_type in {"etf", "fund"}:
            return find_onvista_url(isin, session) or resolve_onvista_url(session, isin, wkn, name)
        return resolve_onvista_url(session, isin, wkn, name)

    onvista_http.last_block()
    url = onvista_urls.get(isin)
    from_cache = url is not None
    if not url:
        url = look_up_url()
    if not url:
        result["onvista_error"] = onvista_http.last_block() or "onvista URL nicht gefunden"
        return result

    details = extract_onvista_fields(url, session, isin=isin)
    if from_cache and details.get("onvista_page_ok") is False:
        # the remembered address no longer shows this ISIN: look it up again, once
        onvista_urls.forget(isin)
        from_cache = False
        url = look_up_url()
        if not url:
            result["onvista_error"] = onvista_http.last_block() or "onvista URL nicht gefunden"
            return result
        details = extract_onvista_fields(url, session, isin=isin)
    if details.get("onvista_page_ok") and not from_cache:
        onvista_urls.put(isin, url)
    for column in ONVISTA_OUTPUT_COLUMNS:
        result[column] = details.get(column, "")
    result["Laufende Kosten"] = details.get("Laufende Kosten", "")
    result["Replikation"] = details.get("Replikation", "")
    result["Ertragsverwendung"] = details.get("Ertragsverwendung", "")
    result["Bid"] = details.get("Bid")
    result["Ask"] = details.get("Ask")
    result["QUOTE_CURRENCY"] = str(details.get("QUOTE_CURRENCY") or "").strip().upper()
    if details.get("onvista_error"):
        result["onvista_error"] = details["onvista_error"]
    if _is_onvista_spread_implausible(result["Bid"], result["Ask"]):
        # A quote with no real book behind it (pre-market, OTC): Oceaneering
        # showed Bid 27.8 / Ask 55.8 while it traded at 40. Dropping both makes
        # every caller behave as for "no quote": --bid-ask-only keeps the last
        # values, the Top List falls back to Yahoo, a full run leaves them empty.
        result["onvista_error"] = f"implausible spread Bid={result['Bid']} Ask={result['Ask']}"
        result["Bid"] = None
        result["Ask"] = None
    return result


def normalize_update_types(values: Optional[list[str]]) -> set[str]:
    if not values:
        return {"all"}

    normalized = set()
    for value in values:
        for part in str(value).split(","):
            key = part.strip().lower()
            if not key:
                continue
            normalized.add(UPDATE_TYPE_ALIASES.get(key, key))

    invalid = normalized - {"all", "equity", "etf", "fund", "index", "certificate", "other"}
    if invalid:
        raise ValueError(f"Unsupported update type(s): {', '.join(sorted(invalid))}")
    return normalized or {"all"}


def should_update_instrument(instrument_type: str, selected_types: set[str]) -> bool:
    return "all" in selected_types or instrument_type in selected_types


def _new_processing_stats() -> dict:
    stats = {
        "filled_targets": 0,
        "funds_enriched": 0,
        "filtered_skip": 0,
        "non_equity_skip": 0,
        "no_coverage": 0,
        "no_isin": 0,
        "fund_errors": 0,
        "onvista_enriched": 0,
        "onvista_errors": 0,
    }
    for instrument_type in INSTRUMENT_TYPE_SUMMARY_ORDER:
        stats[f"type_{instrument_type}"] = 0
    return stats


def _instrument_type_from_existing_or_text(row: pd.Series) -> str:
    existing_value = row.get("INSTRUMENT_TYPE")
    existing = "" if pd.isna(existing_value) else str(existing_value).strip().lower()
    if existing in set(INSTRUMENT_TYPE_SUMMARY_ORDER) - {"missing_isin"}:
        return existing
    return classify_instrument(row, None)


COMMENT2_NUMBER_RE = re.compile(r"\s*(-?\d+(?:[.,]\d+)?)\s*")


def _comment2_has_target(df: pd.DataFrame, idx: object) -> bool:
    """Whether COMMENT2 holds a target price (a number, possibly followed by the
    "(n=..,src)" source meta) rather than a reason such as "no analyst target",
    "no symbol", "fetch failed" or "type:etf", or nothing at all."""
    text = df.at[idx, "COMMENT2"]
    return bool(re.match(r"\s*-?\d", "" if pd.isna(text) else str(text)))


def _clear_kurspotential(df: pd.DataFrame, idx: object) -> bool:
    """Blank Kurspotential, Kurspotential % and COMMENT3 (which only carries the
    Kurspotential text; the TA signal lives in QUANTITY). Without a target
    price they have nothing to be computed from, and a value left over from an
    earlier target (or earlier run) would sit next to "no analyst target".
    Returns whether anything was there to clear."""
    changed = False
    for column in ("Kurspotential", "Kurspotential %", "COMMENT3"):
        if column not in df.columns:
            continue
        value = df.at[idx, column]
        if pd.notna(value) and str(value).strip() != "":
            changed = True
        df.at[idx, column] = "" if column == "COMMENT3" else float("nan")
    return changed


def _refresh_kurspotential_from_comment2(
    df: pd.DataFrame, idx: object, target_currency: object, ask: object, ask_currency: object
) -> bool:
    """Recompute Kurspotential/Kurspotential %/COMMENT3 for an equity row from
    the target price already stored in COMMENT2 and a freshly fetched Ask, so a
    --bid-ask-only run doesn't leave them stale. Mirrors the full run: both
    prices are converted to EUR and Ask is the current price. Leaves the row
    untouched (returns False) when COMMENT2 isn't a plain number (e.g. "no
    analyst target") or the target looks implausible against the new price."""
    comment2 = df.at[idx, "COMMENT2"]
    match = COMMENT2_NUMBER_RE.fullmatch("" if pd.isna(comment2) else str(comment2))
    if not match:
        return False
    now = datetime.now()
    target_eur = _convert_price_to_eur_at_date(match.group(1).replace(",", "."), target_currency, now)
    current_eur = _convert_price_to_eur_at_date(ask, ask_currency, now)
    if not target_eur or not current_eur or not _is_target_price_plausible(target_eur, current_eur):
        return False
    kurspotential = target_eur - current_eur
    df.at[idx, "Kurspotential"] = kurspotential
    df.at[idx, "Kurspotential %"] = (target_eur / current_eur - 1.0) * 100.0
    df.at[idx, "COMMENT3"] = f"{kurspotential:.2f}".replace(".", ",")
    return True


def _comment2_display_currencies(stored_dev: object, onvista_currency: object) -> tuple[str, str]:
    """(currency COMMENT2 is stored in, currency it has to be in after this refresh), both USD/EUR.

    COMMENT2 follows the DEV the export writes, which is USD or EUR (_prepare_output_df turns
    Bid, Ask and DEV into those), not the currency onvista quoted this time. A stock onvista
    quotes in CHF or pence (Belimo, Segro) has DEV CHF/GBX in memory but EUR in the file; a target
    kept in CHF/pence would be shown as EUR next to an Ask that was converted."""
    stored = "" if pd.isna(stored_dev) else _normalize_display_currency(stored_dev)
    fallback = "" if pd.isna(stored_dev) else stored_dev
    return stored, _normalize_display_currency(onvista_currency or fallback)


def _convert_comment2_currency(df: pd.DataFrame, idx: object, from_currency: object, to_currency: object) -> bool:
    """Re-express a plain-number COMMENT2 target in to_currency. COMMENT2 is in the
    row's DEV, and a refresh overwrites DEV with whatever currency onvista
    quoted this time (EUR on the German listing, USD later), so the target has
    to follow or it would be read as the wrong currency. Leaves the row
    untouched (returns False) when COMMENT2 isn't a plain number or the rate is
    unavailable."""
    text = df.at[idx, "COMMENT2"]
    match = COMMENT2_NUMBER_RE.fullmatch("" if pd.isna(text) else str(text))
    if not match:
        return False
    converted = _convert_price_to_currency(match.group(1).replace(",", "."), from_currency, to_currency)
    if converted is None:
        return False
    df.at[idx, "COMMENT2"] = f"{converted:.2f}".replace(".", ",")
    return True


def _refresh_comment2_from_cache(
    df: pd.DataFrame,
    idx: object,
    entry: Optional[dict],
    stored_currency: object,
    ask: object,
    ask_currency: object,
    include_source_meta: bool,
) -> bool:
    """Replace an equity row's target price (COMMENT2) with the one in the shared
    target cache (see target_cache.py), expressed in the DEV the last full
    run wrote, which is what _refresh_kurspotential_from_comment2 reads it as.
    Leaves the row untouched (returns False) when the cache has no target, the
    target looks implausible against the new Ask (the row may then be using a
    ChartMill fallback chosen by the full run), or nothing changed."""
    if not entry or pd.isna(pd.to_numeric(entry.get("target_mean"), errors="coerce")):
        return False
    text = format_comment2(
        entry,
        display_currency="" if pd.isna(stored_currency) else str(stored_currency),
        include_source_meta=include_source_meta,
    )
    if not text:
        return False
    now = datetime.now()
    target_eur = _convert_price_to_eur_at_date(entry.get("target_mean"), entry.get("currency"), now)
    current_eur = _convert_price_to_eur_at_date(ask, ask_currency, now)
    if not target_eur or not current_eur or not _is_target_price_plausible(target_eur, current_eur):
        return False
    current = df.at[idx, "COMMENT2"]
    if ("" if pd.isna(current) else str(current).strip()) == text:
        return False
    df.at[idx, "COMMENT2"] = text
    return True


def _is_equity_row(instrument_type: str, isin: object, cached_targets: Optional[dict]) -> bool:
    """Whether a row is a stock for the --bid-ask-only refreshes. They cannot read
    the type off the row: INSTRUMENT_TYPE is not persisted, so a plain stock
    classifies as "other" there (a name that says ETF/fund/certificate/index
    still classifies as such). An "other" row whose ISIN has a symbol in the
    shared target cache is one the full run resolved to an equity ticker, which
    is how it decides "equity" too."""
    if instrument_type == "equity":
        return True
    if instrument_type != "other":
        return False
    key = "" if pd.isna(isin) else str(isin).strip().upper()
    entry = (cached_targets or {}).get(key)
    return isinstance(entry, dict) and bool(entry.get("symbol"))


def _quote_against_reference(
    df: pd.DataFrame,
    idx: object,
    instrument_type: str,
    ask: object,
    bid: object,
    quote_currency: object,
    stored_currency: object,
    cached_targets: Optional[dict] = None,
) -> Optional[tuple[float, float, str]]:
    """(quote, reference close, basis) with the quote (Ask, else Bid) converted into
    the currency of the reference close the last full run stored in REF_PREV_CLOSE,
    or None when there is no quote or no usable reference."""
    quote_price, basis = _quote_for_difference(ask, bid)
    if quote_price is None:
        return None
    quote_currency = None if pd.isna(quote_currency) else quote_currency
    stored_currency = None if pd.isna(stored_currency) else stored_currency
    reference = pd.to_numeric(df.at[idx, "REF_PREV_CLOSE"], errors="coerce")
    if pd.isna(reference) or reference == 0:
        return None
    reference = float(reference)

    ref_currency = None
    if DIFF_REF_CURRENCY_COLUMN in df.columns and pd.notna(df.at[idx, DIFF_REF_CURRENCY_COLUMN]):
        ref_currency = _normalize_price_currency(df.at[idx, DIFF_REF_CURRENCY_COLUMN])
    if not ref_currency:
        isin = df.at[idx, "ISIN"] if "ISIN" in df.columns else None
        if _is_equity_row(instrument_type, isin, cached_targets):
            ref_currency = "EUR"
        else:
            ref_currency = _normalize_price_currency(stored_currency)

    if ref_currency == "EUR":
        current_price = _convert_price_to_eur_at_date(quote_price, quote_currency, datetime.now())
    else:
        current_price = (
            _convert_price_to_currency(quote_price, quote_currency, ref_currency)
            if ref_currency and quote_currency
            else quote_price
        )
    if current_price is None:
        return None
    return current_price, reference, basis


# A Bid or Ask this far from the previous close (3x, or a third) is a suspect: the real
# intraday moves of the listed stocks stay well inside it.
QUOTE_VS_CLOSE_MAX_RATIO = 3.0


def _is_far_from_reference(price: float, reference: float) -> bool:
    ratio = _outlier_ratio(price, reference)
    return ratio is not None and ratio >= QUOTE_VS_CLOSE_MAX_RATIO


def _is_garbage_quote_side(price: float, other: Optional[float], reference: float) -> bool:
    """Whether one side of a quote (price, other side, previous close; all in one currency) is no
    price. Being far from the previous close is not enough: the stored reference can be wrong
    too (three US stocks all had 0.366929), and a real quote must not be refused for that. What
    marks the digits of a product code or a time (MN74SQ gave 74 for Siemens at 269, "17:49"
    gave 17) is that they are whole numbers, or that the other side of the same quote is
    where the previous close says it should be. So: a price <= 0, or one far from the previous
    close that is a whole number or contradicts an other side that fits it."""
    if price <= 0:
        return True
    if not _is_far_from_reference(price, reference):
        return False
    if float(price).is_integer():
        return True
    return other is not None and other > 0 and not _is_far_from_reference(other, reference)


def _quote_fits_previous_close(
    df: pd.DataFrame,
    idx: object,
    instrument_type: str,
    price: object,
    other_price: object,
    quote_currency: object,
    stored_currency: object,
    cached_targets: Optional[dict] = None,
) -> bool:
    """False when a fresh Bid or Ask (`price`; `other_price` is the other side) is garbage
    against the stored previous close, see _is_garbage_quote_side. Nothing to compare against
    (no REF_PREV_CLOSE yet, no conversion) passes."""
    numeric = pd.to_numeric(price, errors="coerce")
    if pd.isna(numeric):
        return True
    if numeric <= 0:
        return False
    resolved = _quote_against_reference(
        df, idx, instrument_type, numeric, None, quote_currency, stored_currency, cached_targets
    )
    if resolved is None:
        return True
    converted, reference, _ = resolved
    other = None
    other_numeric = pd.to_numeric(other_price, errors="coerce")
    if pd.notna(other_numeric) and other_numeric > 0:
        other_resolved = _quote_against_reference(
            df, idx, instrument_type, other_numeric, None, quote_currency, stored_currency, cached_targets
        )
        other = other_resolved[0] if other_resolved else None
    return not _is_garbage_quote_side(converted, other, reference)


def _refresh_differences_from_quote(
    df: pd.DataFrame,
    idx: object,
    instrument_type: str,
    ask: object,
    bid: object,
    quote_currency: object,
    stored_currency: object,
    cached_targets: Optional[dict] = None,
) -> bool:
    """Recompute "% Differenz zum Vortageskurs" from a freshly fetched Ask (Bid if
    Ask is missing) and the reference close the last full run stored in
    REF_PREV_CLOSE, so a --bid-ask-only run doesn't leave it stale. Monat and
    Jahr are close-to-close and stay as the last full run computed them.
    The quote is converted into REF_CURRENCY, the currency the full run
    expressed the reference in (EUR for equities, the row's DEV for ETFs/funds).
    Rows from before REF_CURRENCY existed fall back to the instrument type, see
    _is_equity_row. Leaves the row untouched (returns False) when
    there is no quote, no stored reference (the row has not been through a full
    run since the column was introduced) or the quote's scale does not fit the
    reference."""
    resolved = _quote_against_reference(
        df, idx, instrument_type, ask, bid, quote_currency, stored_currency, cached_targets
    )
    if resolved is None:
        return False
    current_price, reference, basis = resolved
    ratio = _outlier_ratio(current_price, reference)
    if ratio is not None and ratio >= PRICE_CURRENT_HISTORY_OUTLIER_RATIO:
        print(f"    ! diff {basis} {current_price:.4g} does not fit reference {reference:.4g}; kept previous values")
        return False

    df.at[idx, "% Differenz zum Vortageskurs"] = _percent_change(current_price, reference)
    df.at[idx, DIFF_BASIS_COLUMN] = basis
    return True


def process_bid_ask_only_dataframe(df: pd.DataFrame, args: argparse.Namespace, source_label: str) -> dict:
    apply_enrichment_columns(df)
    cached_targets = target_cache.load()
    for column in ("Bid", "Ask"):
        if column not in df.columns:
            df[column] = pd.NA
    session = requests.Session()
    stats = _new_processing_stats()

    for idx, row in df.iterrows():
        if bool(row.get("_IS_SEPARATOR_ROW", False)):
            continue

        isin = row.get("ISIN")
        wkn = row.get("WKN")
        name = row.get("Name") if isinstance(row.get("Name"), str) else "?"
        if not isinstance(isin, str) or not isin.strip():
            stats["no_isin"] += 1
            df.at[idx, "INSTRUMENT_TYPE"] = "missing_isin"
            stats["type_missing_isin"] += 1
            continue

        instrument_type = _instrument_type_from_existing_or_text(row)
        df.at[idx, "INSTRUMENT_TYPE"] = instrument_type
        stats[f"type_{instrument_type}"] += 1

        print(f"[{idx + 1:>2}/{len(df)}] {source_label}: bid/ask {isin}  {name[:35]}")
        if not should_update_instrument(instrument_type, args.update_types):
            stats["filtered_skip"] += 1
            df.at[idx, "ENRICH_NOTE"] = f"Skipped by --update-types ({instrument_type})"
            print(f"    - skip by filter ({instrument_type})")
            continue

        onvista_fields = fetch_onvista_fields(isin.strip(), instrument_type, session, wkn=wkn, name=name)
        onvista_currency = str(onvista_fields.get("QUOTE_CURRENCY") or "").strip().upper()
        # COMMENT2 is expressed in the DEV written by the last full run.
        target_currency = df.at[idx, "DEV"]
        # A Bid or Ask that is no price next to the stored previous close (digits of a product
        # code, 2026-10-28) is dropped, that side only; the row keeps its last value.
        fresh = {side: onvista_fields.get(side) for side in ("Bid", "Ask")}
        for side, other in (("Bid", "Ask"), ("Ask", "Bid")):
            price = fresh[side]
            if not _quote_fits_previous_close(
                df, idx, instrument_type, price, fresh[other], onvista_currency or target_currency, target_currency, cached_targets
            ):
                reason = f"{side} {price} rejected: not a price next to the previous close {df.at[idx, 'REF_PREV_CLOSE']}"
                onvista_fields[side] = None
                onvista_fields["onvista_error"] = _append_note(onvista_fields.get("onvista_error"), reason)
                print(f"    ! onvista: {reason}")
        if onvista_currency:
            df.at[idx, "DEV"] = onvista_currency
        if pd.notna(onvista_fields.get("Bid")):
            df.at[idx, "Bid"] = onvista_fields.get("Bid")
            df.at[idx, "BID_FETCHED_AT"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if pd.notna(onvista_fields.get("Ask")):
            df.at[idx, "Ask"] = onvista_fields.get("Ask")
            df.at[idx, "ASK_FETCHED_AT"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if _is_equity_row(instrument_type, isin, cached_targets):
                # COMMENT2 is in DEV, which was just set to the quote currency:
                # bring the stored target along first when that changed it.
                ask_currency = onvista_currency or target_currency
                # COMMENT2 is kept in USD/EUR, the currency the export gives DEV, while Ask
                # keeps the currency onvista quoted it in (the export converts it from there).
                stored_currency, wanted_currency = _comment2_display_currencies(target_currency, onvista_currency)
                comment2_currency = stored_currency or wanted_currency
                if (
                    onvista_currency
                    and stored_currency
                    and stored_currency != wanted_currency
                    and _convert_comment2_currency(df, idx, stored_currency, wanted_currency)
                ):
                    comment2_currency = wanted_currency
                if _refresh_comment2_from_cache(
                    df,
                    idx,
                    target_cache.fresh_entry(cached_targets, isin),
                    comment2_currency,
                    onvista_fields.get("Ask"),
                    ask_currency,
                    bool(getattr(args, "comment2_with_source_meta", False)),
                ):
                    print(f"    -> TMP {df.at[idx, 'COMMENT2']} (from target cache)")
                # Kurspotential % must equal TMP / Ask of the same row. Bring the Ask into the currency
                # COMMENT2 is in first, by the same rate that wrote COMMENT2 and the exported Ask; the
                # at-date lookup _refresh_kurspotential_from_comment2 uses for the Ask can sit a day
                # away (CHF 1.0821 vs 1.0872), which moved the percentage by ~0.5 points.
                potential_ask, potential_ask_currency = onvista_fields.get("Ask"), ask_currency
                ask_in_comment2_currency = (
                    _convert_price_to_currency(potential_ask, ask_currency, comment2_currency)
                    if comment2_currency
                    else None
                )
                if ask_in_comment2_currency is not None:
                    potential_ask, potential_ask_currency = ask_in_comment2_currency, comment2_currency
                if not _comment2_has_target(df, idx):
                    if _clear_kurspotential(df, idx):
                        print("    -> Kurspotential cleared (no target)")
                elif _refresh_kurspotential_from_comment2(
                    df, idx, comment2_currency, potential_ask, potential_ask_currency
                ):
                    print(f"    -> Kurspotential={df.at[idx, 'Kurspotential']:.2f}")
        if pd.notna(onvista_fields.get("Bid")) or pd.notna(onvista_fields.get("Ask")):
            stats["onvista_enriched"] += 1
            print(f"    -> Bid={df.at[idx, 'Bid']} Ask={df.at[idx, 'Ask']}")
            if _refresh_differences_from_quote(
                df,
                idx,
                instrument_type,
                onvista_fields.get("Ask"),
                onvista_fields.get("Bid"),
                onvista_currency or target_currency,
                target_currency,
                cached_targets,
            ):
                print(f"    -> Diff recomputed from {df.at[idx, DIFF_BASIS_COLUMN]}")
        elif onvista_fields.get("onvista_error"):
            stats["onvista_errors"] += 1
            df.at[idx, "ENRICH_NOTE"] = _append_note(df.at[idx, "ENRICH_NOTE"], f"onvista: {onvista_fields['onvista_error']}")
            print(f"    ! onvista: {onvista_fields['onvista_error']}")
        else:
            stats["onvista_errors"] += 1
            df.at[idx, "ENRICH_NOTE"] = _append_note(df.at[idx, "ENRICH_NOTE"], "onvista: no bid/ask")
            print("    ! onvista: no bid/ask")
        time.sleep(args.sleep)

    return stats


def process_stock_target(args: argparse.Namespace, session: requests.Session, isin: str, wkn: Optional[str], name: str, equity_ticker: Optional[str] = None) -> dict:
    equity_ticker = equity_ticker or isin_to_yahoo_ticker(isin, session)
    if not equity_ticker:
        return {"equity_ticker": None, "data": {}, "no_equity": True}

    data = fetch_analyst_data(equity_ticker)
    time.sleep(args.sleep)
    return {"equity_ticker": equity_ticker, "data": data, "no_equity": False}


def process_fund_like(args: argparse.Namespace, session: requests.Session, isin: str, name: str, wkn: Optional[str] = None) -> dict:
    symbol, quote_type = resolve_fund_like_symbol(isin, name, session)
    use_eodhd_first = args.fund_data_source in {"eodhd", "eodhd_then_yahoo"}
    use_yahoo_fallback = args.fund_data_source in {"yahoo", "eodhd_then_yahoo"}

    if use_eodhd_first and args.eodhd_api_key:
        data = fetch_fund_market_data_eodhd(isin, args.eodhd_api_key, wkn=wkn, name=name)
        if not data.get("fehler"):
            detail_symbol = data.get("detail_symbol")
            detail_source = str(data.get("detail_source") or "")
            market_ta_symbol = None
            if detail_source.startswith("market_fetcher:"):
                market_ta_symbol, _market_source = _resolve_market_fetcher_symbol(isin, wkn=wkn, name=name)
            return {
                "symbol": detail_symbol,
                "ta_symbol": market_ta_symbol or symbol or detail_symbol,
                "quote_type": quote_type or "EODHD",
                "data": data,
            }
    if not symbol:
        if use_yahoo_fallback:
            return {"symbol": None, "ta_symbol": None, "quote_type": quote_type, "data": {"fehler": "Kein ETF/Fund-Symbol gefunden"}}
        return {"symbol": None, "ta_symbol": None, "quote_type": quote_type, "data": {"fehler": "EODHD fehlgeschlagen und kein Yahoo-Symbol gefunden"}}

    if use_yahoo_fallback:
        data = fetch_fund_market_data_yahoo(symbol)
        return {"symbol": symbol, "ta_symbol": symbol, "quote_type": quote_type, "data": data}

    return {"symbol": symbol, "ta_symbol": symbol, "quote_type": quote_type, "data": {"fehler": "EODHD fehlgeschlagen"}}


def process_dataframe(df: pd.DataFrame, args: argparse.Namespace, source_label: str) -> dict:
    apply_enrichment_columns(df)
    cached_targets = target_cache.load()
    if "QUANTITY" not in df.columns:
        df["QUANTITY"] = pd.Series("", index=df.index, dtype=object)
    else:
        df["QUANTITY"] = df["QUANTITY"].astype(object)
        if "_IS_SEPARATOR_ROW" in df.columns:
            data_mask = ~df["_IS_SEPARATOR_ROW"].fillna(False)
            df.loc[data_mask, "QUANTITY"] = ""
        else:
            df["QUANTITY"] = ""
    skip_comment_country = bool(getattr(args, "skip_subcategories", False))
    if not skip_comment_country:
        update_comment_with_subcategory(df, source_label, allow_new_tags=bool(getattr(args, "allow_new_tags", False)))
        sort_dataframe_by_subcategories(df, source_label)

    session = requests.Session()
    stats = {
        "filled_targets": 0,
        "funds_enriched": 0,
        "filtered_skip": 0,
        "non_equity_skip": 0,
        "no_coverage": 0,
        "no_isin": 0,
        "fund_errors": 0,
        "onvista_enriched": 0,
        "onvista_errors": 0,
    }
    for instrument_type in INSTRUMENT_TYPE_SUMMARY_ORDER:
        stats[f"type_{instrument_type}"] = 0

    yahoo_batch_size = max(0, int(getattr(args, "yahoo_batch_size", 0) or 0))
    yahoo_batch_cooldown = max(0.0, float(getattr(args, "yahoo_batch_cooldown", 0.0) or 0.0))
    if len(df) <= YAHOO_BATCH_MIN_ROWS:
        # Small files don't burst Yahoo hard enough to need cooldown pauses;
        # only files above this size get the batching behavior at all.
        yahoo_batch_size = 0
    equity_rows_since_cooldown = 0

    for idx, row in df.iterrows():
        if bool(row.get("_IS_SEPARATOR_ROW", False)):
            continue

        isin = row.get("ISIN")
        wkn = row.get("WKN")
        name = row.get("Name") if isinstance(row.get("Name"), str) else "?"
        if not isinstance(isin, str) or not isin.strip():
            stats["no_isin"] += 1
            df.at[idx, "INSTRUMENT_TYPE"] = "missing_isin"
            stats["type_missing_isin"] += 1
            continue

        print(f"[{idx + 1:>2}/{len(df)}] {source_label}: {isin}  {name[:35]}")
        equity_ticker, equity_source, instrument_hint = resolve_equity_ticker(isin, session)
        instrument_type = classify_instrument(row, equity_ticker, instrument_hint=instrument_hint)
        df.at[idx, "INSTRUMENT_TYPE"] = instrument_type
        if instrument_type not in {"etf", "fund"} and not skip_comment_country:
            df.at[idx, "COMMENT"] = _append_country_to_comment(df.at[idx, "COMMENT"], _country_from_isin(isin))
        stats[f"type_{instrument_type}"] += 1

        if not should_update_instrument(instrument_type, args.update_types):
            stats["filtered_skip"] += 1
            df.at[idx, "ENRICH_NOTE"] = f"Skipped by --update-types ({instrument_type})"
            print(f"    - skip by filter ({instrument_type})")
            continue

        if instrument_type == "equity":
            stock_result = process_stock_target(args, session, isin, wkn, name, equity_ticker=equity_ticker)

            # If OPENFIGI returned a symbol that Yahoo does not recognise as EQUITY,
            # fall back to a direct Yahoo ISIN search before giving up.
            if (
                equity_source == "OPENFIGI"
                and not stock_result.get("no_equity")
                and str(stock_result["data"].get("quote_type") or "").upper() not in {"EQUITY"}
            ):
                bad_qt = str(stock_result["data"].get("quote_type") or "no data").lower()
                print(f"    -> OPENFIGI symbol {equity_ticker!r} invalid on Yahoo (quote_type={bad_qt}), retrying via Yahoo ISIN search")
                fallback_sym, _ = search_yahoo_symbol(isin, session, allowed_quote_types={"EQUITY"})
                if not fallback_sym and isinstance(name, str) and name.strip():
                    fallback_sym, _ = search_yahoo_symbol(name, session, allowed_quote_types={"EQUITY"})
                if fallback_sym and fallback_sym != equity_ticker:
                    equity_ticker = fallback_sym
                    equity_source = "YAHOO_SEARCH"
                    stock_result = process_stock_target(args, session, isin, wkn, name, equity_ticker=equity_ticker)
                    print(f"    -> retried with Yahoo symbol {equity_ticker!r}")

            # The target price (COMMENT2/Kurspotential) comes from the shared cache
            # that refresh_targets.py fills at 10:00 and 16:30, so it matches every
            # other list; the live lookup is only the fallback for ISINs not in it.
            cached_target = target_cache.fresh_entry(cached_targets, isin)
            if cached_target and not stock_result.get("no_equity"):
                stock_result["data"] = target_cache.apply_to_data(stock_result["data"], cached_target)
            df.at[idx, "DETAIL_SYMBOL"] = equity_ticker
            df.at[idx, "DETAIL_SOURCE"] = equity_source
            if equity_source not in {"LOCAL_OVERRIDE", "MANUAL", None}:
                _record_symbol_candidate(isin, name, instrument_type, equity_ticker, equity_source)
            tm = stock_result["data"].get("target_mean")
            if tm is None:
                # Determine and record the concrete failure reason in COMMENT2.
                if stock_result.get("no_equity") or not equity_ticker:
                    _reason = "no symbol"
                else:
                    _qt = str(stock_result["data"].get("quote_type") or "").upper()
                    if _qt and _qt != "EQUITY":
                        _reason = f"type:{_qt.lower()}"
                    elif not stock_result["data"]:
                        _reason = "fetch failed"
                    else:
                        _reason = "no analyst target"
                df.at[idx, "COMMENT2"] = _reason
                _clear_kurspotential(df, idx)
                print(f"    -> no target: {_reason}")
                stats["no_coverage"] += 1
            else:
                df.at[idx, "DETAIL_SOURCE"] = stock_result["data"].get("source")
                stats["filled_targets"] += 1
            stock_currency = str(stock_result["data"].get("currency") or "").strip()
            if stock_currency:
                df.at[idx, "DEV"] = stock_currency
            if not skip_comment_country:
                df.at[idx, "COMMENT"] = _append_country_to_comment(
                    df.at[idx, "COMMENT"],
                    _country_from_data(stock_result.get("data"), isin=isin),
                )
            onvista_fields = fetch_onvista_fields(isin, instrument_type, session, wkn=wkn, name=name)
            onvista_currency = str(onvista_fields.get("QUOTE_CURRENCY") or "").strip().upper()
            # Sanity-check onvista's Bid/Ask against Yahoo's current_price before
            # trusting them: a scraping slip (e.g. picking up an unrelated number
            # from a multi-exchange listing page) should not silently corrupt
            # Bid/Ask/Kurspotential with an implausible value.
            reference_price = stock_result["data"].get("current_price")
            reference_currency = stock_result["data"].get("currency")
            bid_plausible = _is_onvista_price_plausible(
                onvista_fields.get("Bid"), onvista_currency, reference_price, reference_currency
            )
            ask_plausible = _is_onvista_price_plausible(
                onvista_fields.get("Ask"), onvista_currency, reference_price, reference_currency
            )
            if onvista_currency and (bid_plausible or ask_plausible):
                df.at[idx, "DEV"] = onvista_currency
            if pd.notna(onvista_fields.get("Bid")) and bid_plausible:
                df.at[idx, "Bid"] = onvista_fields.get("Bid")
                df.at[idx, "BID_FETCHED_AT"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            elif pd.notna(onvista_fields.get("Bid")):
                df.at[idx, "ENRICH_NOTE"] = _append_note(
                    df.at[idx, "ENRICH_NOTE"],
                    f"onvista Bid {onvista_fields.get('Bid')} {onvista_currency} weicht stark vom Referenzkurs ab, ignoriert",
                )
            if pd.notna(onvista_fields.get("Ask")) and ask_plausible:
                df.at[idx, "Ask"] = onvista_fields.get("Ask")
                df.at[idx, "ASK_FETCHED_AT"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            elif pd.notna(onvista_fields.get("Ask")):
                df.at[idx, "ENRICH_NOTE"] = _append_note(
                    df.at[idx, "ENRICH_NOTE"],
                    f"onvista Ask {onvista_fields.get('Ask')} {onvista_currency} weicht stark vom Referenzkurs ab, ignoriert",
                )
            # Format COMMENT2 only now that DEV is finalized above, so the target
            # price is expressed in the same currency as Bid/Ask instead of drifting
            # from its own quote source's currency.
            if tm is not None:
                row_display_currency = _normalize_display_currency(df.at[idx, "DEV"])
                df.at[idx, "COMMENT2"] = format_comment2(
                    stock_result["data"],
                    display_currency=row_display_currency,
                    include_source_meta=args.comment2_with_source_meta,
                )
                print(f"    -> target {df.at[idx, 'COMMENT2']}")
                # Kurspotential/Kurspotential % (equity only): target price vs. the
                # current Bid/Ask mid, both resolved to the same display currency.
                potential_date = datetime.now()
                target_price_eur = _convert_price_to_eur_at_date(
                    stock_result["data"].get("target_mean"),
                    stock_result["data"].get("currency"),
                    potential_date,
                )
                current_price_eur = _convert_price_to_eur_at_date(
                    onvista_fields.get("Ask"), onvista_currency, potential_date
                )
                if current_price_eur is None:
                    current_price_eur = _convert_price_to_eur_at_date(
                        stock_result["data"].get("current_price"),
                        stock_result["data"].get("currency"),
                        potential_date,
                    )
                if target_price_eur is not None and current_price_eur and not _is_target_price_plausible(target_price_eur, current_price_eur):
                    # Yahoo's target looks corrupted — cross-check ChartMill
                    # before giving up on Kurspotential for this row.
                    yahoo_target_price = target_price_eur
                    chartmill_data = fetch_chartmill_target_data(equity_ticker)
                    chartmill_target = _convert_price_to_eur_at_date(
                        chartmill_data.get("target_mean"), chartmill_data.get("currency"), potential_date
                    )
                    if chartmill_target is not None and _is_target_price_plausible(chartmill_target, current_price_eur):
                        target_price_eur = chartmill_target
                        # COMMENT2 (shown as "TMP" in the dashboard) must
                        # reflect the same source Kurspotential was actually
                        # computed from, or the two columns contradict each
                        # other on the same row.
                        df.at[idx, "COMMENT2"] = format_comment2(
                            chartmill_data,
                            display_currency=row_display_currency,
                            include_source_meta=args.comment2_with_source_meta,
                        )
                        df.at[idx, "ENRICH_NOTE"] = _append_note(
                            df.at[idx, "ENRICH_NOTE"],
                            f"Yahoo-Zielkurs {yahoo_target_price:.2f} EUR unplausibel, "
                            f"Kurspotential nutzt ChartMill-Kursziel {chartmill_target:.2f} EUR",
                        )
                    else:
                        target_price_eur = None
                        df.at[idx, "ENRICH_NOTE"] = _append_note(
                            df.at[idx, "ENRICH_NOTE"],
                            f"Zielkurs {yahoo_target_price:.2f} EUR weicht stark vom aktuellen EUR-Kurs ab, Kurspotential ignoriert",
                        )
                if target_price_eur is not None and current_price_eur:
                    kurspotential = target_price_eur - current_price_eur
                    df.at[idx, "Kurspotential"] = kurspotential
                    df.at[idx, "Kurspotential %"] = (target_price_eur / current_price_eur - 1.0) * 100.0
                    df.at[idx, "COMMENT3"] = f"{kurspotential:.2f}".replace(".", ",")
                else:
                    # No usable target or price: don't leave an older value behind.
                    _clear_kurspotential(df, idx)
            difference_values = _compute_difference_columns(
                equity_ticker,
                isin=isin,
                eodhd_key=args.eodhd_api_key,
                wkn=wkn,
                name=name,
                prefer_eodhd=True,
                # Only quotes that passed the plausibility check above.
                bid=onvista_fields.get("Bid") if bid_plausible else None,
                ask=onvista_fields.get("Ask") if ask_plausible else None,
                fallback_current_price=stock_result["data"].get("current_price"),
                history_currency=stock_result["data"].get("currency"),
                quote_currency=onvista_currency or stock_result["data"].get("currency"),
            )
            for column_name, column_value in difference_values.items():
                df.at[idx, column_name] = column_value
            if onvista_fields.get("onvista_error"):
                stats["onvista_errors"] += 1
                df.at[idx, "ENRICH_NOTE"] = _append_note(df.at[idx, "ENRICH_NOTE"], f"onvista: {onvista_fields['onvista_error']}")
            elif onvista_fields.get("onvista_URL"):
                stats["onvista_enriched"] += 1
            if not getattr(args, "skip_ta", False):
                _apply_ta_signal(
                    df,
                    idx,
                    equity_ticker,
                    isin=isin,
                    eodhd_key=args.eodhd_api_key,
                    wkn=wkn,
                    name=name,
                    prefer_eodhd=True,
                )
            equity_rows_since_cooldown += 1
            if yahoo_batch_size and equity_rows_since_cooldown >= yahoo_batch_size:
                print(f"    -- pausing {yahoo_batch_cooldown:.0f}s after {equity_rows_since_cooldown} equity rows to avoid rate limiting --")
                time.sleep(yahoo_batch_cooldown)
                equity_rows_since_cooldown = 0
            continue

        stats["non_equity_skip"] += 1
        if instrument_type in {"index", "certificate"}:
            df.at[idx, "ENRICH_NOTE"] = f"Skipped {instrument_type}"
            print(f"    - skip {instrument_type}")
            continue
        if instrument_type not in {"etf", "fund", "other"}:
            df.at[idx, "ENRICH_NOTE"] = "Skipped unsupported instrument type"
            print("    - skip unsupported instrument type")
            continue

        fund_result = process_fund_like(args, session, isin, name, wkn=wkn)
        fund_data = fund_result["data"]
        instrument_type = normalize_instrument_type(instrument_type, fund_result.get("quote_type"))
        df.at[idx, "INSTRUMENT_TYPE"] = instrument_type
        if instrument_type in {"etf", "fund"} and not skip_comment_country:
            df.at[idx, "COMMENT"] = _strip_country_from_comment(df.at[idx, "COMMENT"])
        df.at[idx, "DETAIL_SYMBOL"] = fund_result.get("symbol")
        df.at[idx, "DETAIL_SOURCE"] = fund_data.get("detail_source") or fund_result.get("quote_type")
        df.at[idx, "ENRICH_NOTE"] = fund_data.get("fehler")
        if fund_data.get("fehler"):
            stats["fund_errors"] += 1
            print(f"    - fund enrich failed: {fund_data.get('fehler')}")
        else:
            stats["funds_enriched"] += 1
            print(f"    -> fund source={df.at[idx, 'DETAIL_SOURCE']}, symbol={df.at[idx, 'DETAIL_SYMBOL']}")
        fund_currency = str(fund_data.get("waehrung") or "").strip()
        if fund_currency:
            df.at[idx, "DEV"] = fund_currency
        if instrument_type not in {"etf", "fund"} and not skip_comment_country:
            df.at[idx, "COMMENT"] = _append_country_to_comment(
                df.at[idx, "COMMENT"],
                _country_from_data(fund_data, isin=isin),
            )
        if not getattr(args, "skip_ta", False):
            ta_symbol = fund_result.get("ta_symbol")
            print(f"    -> TA symbol {ta_symbol or '-'}")
            if not ta_symbol:
                df.at[idx, "ENRICH_NOTE"] = _append_note(
                    df.at[idx, "ENRICH_NOTE"],
                    "TA symbol missing",
                )

        candidate_source = fund_result.get("quote_type") or fund_data.get("detail_source") or ""
        candidate_note = fund_data.get("fehler") or ""
        if candidate_source not in {"LOCAL_OVERRIDE", "MANUAL"}:
            _record_symbol_candidate(
                isin,
                name,
                instrument_type,
                fund_result.get("ta_symbol") or fund_result.get("symbol"),
                candidate_source,
                note=candidate_note,
            )

        onvista_fields = fetch_onvista_fields(isin, instrument_type, session, wkn=wkn, name=name)
        for column in ONVISTA_OUTPUT_COLUMNS:
            df.at[idx, column] = onvista_fields.get(column, "")
        onvista_currency = str(onvista_fields.get("QUOTE_CURRENCY") or "").strip().upper()
        if onvista_currency:
            df.at[idx, "DEV"] = onvista_currency
        if pd.notna(onvista_fields.get("Bid")):
            df.at[idx, "Bid"] = onvista_fields.get("Bid")
            df.at[idx, "BID_FETCHED_AT"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if pd.notna(onvista_fields.get("Ask")):
            df.at[idx, "Ask"] = onvista_fields.get("Ask")
            df.at[idx, "ASK_FETCHED_AT"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        difference_values = _compute_difference_columns(
            fund_result.get("ta_symbol") or fund_result.get("symbol") or "",
            isin=isin,
            eodhd_key=args.eodhd_api_key,
            wkn=wkn,
            name=name,
            prefer_eodhd=args.fund_data_source != "yahoo",
            bid=onvista_fields.get("Bid"),
            ask=onvista_fields.get("Ask"),
            convert_to_eur=False,
            quote_currency=onvista_currency,
            display_currency=df.at[idx, "DEV"] if pd.notna(df.at[idx, "DEV"]) else None,
        )
        if all(difference_values[column] is None for column in DIFF_PCT_COLUMNS):
            # OpenFIGI often yields exchange-internal codes (e.g. DJGTEEX.DE) that
            # Yahoo cannot serve history for; retry once with the symbol Yahoo's
            # own ISIN search resolves, like the equity path does.
            first_symbol = fund_result.get("ta_symbol") or fund_result.get("symbol") or ""
            retry_symbol, _retry_qt = search_yahoo_symbol(isin, session, allowed_quote_types=ENRICHABLE_FUND_TYPES)
            if retry_symbol and retry_symbol != first_symbol:
                print(f"    -> diff retry via Yahoo ISIN search symbol {retry_symbol!r} (was {first_symbol!r})")
                difference_values = _compute_difference_columns(
                    retry_symbol,
                    isin=isin,
                    eodhd_key=args.eodhd_api_key,
                    wkn=wkn,
                    name=name,
                    prefer_eodhd=False,
                    bid=onvista_fields.get("Bid"),
                    ask=onvista_fields.get("Ask"),
                    convert_to_eur=False,
                    quote_currency=onvista_currency,
                    display_currency=df.at[idx, "DEV"] if pd.notna(df.at[idx, "DEV"]) else None,
                )
        for column_name, column_value in difference_values.items():
            df.at[idx, column_name] = column_value
        if instrument_type in {"etf", "fund"}:
            etf_comment2 = format_etf_comment2(onvista_fields.get("Laufende Kosten"))
            if etf_comment2:
                df.at[idx, "COMMENT2"] = etf_comment2
        if onvista_fields.get("onvista_error"):
            stats["onvista_errors"] += 1
            df.at[idx, "ENRICH_NOTE"] = _append_note(df.at[idx, "ENRICH_NOTE"], f"onvista: {onvista_fields['onvista_error']}")
        elif onvista_fields.get("onvista_URL"):
            stats["onvista_enriched"] += 1
        if not getattr(args, "skip_ta", False) and fund_result.get("ta_symbol"):
            _apply_ta_signal(df, idx, fund_result["ta_symbol"], isin=isin, eodhd_key=args.eodhd_api_key, wkn=wkn, name=name)
        time.sleep(args.sleep)

    if not getattr(args, "skip_subcategories", False):
        sort_dataframe_by_subcategories(df, source_label)
    _persist_symbol_candidates()
    return stats


def build_output_path(input_csv: str, output_dir: Optional[str], output_xlsx: Optional[str]) -> str:
    if output_xlsx:
        return output_xlsx
    input_base = os.path.splitext(os.path.basename(input_csv))[0]
    final_dir = output_dir or os.path.dirname(input_csv)
    return os.path.join(final_dir, f"{input_base}.xlsx")


def format_runtime(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def print_summary(total_rows: int, stats: dict, saved_xlsx: Optional[str], saved_csv: Optional[str], saved_top_csv: Optional[str]) -> None:
    print()
    print("Summary")
    print("-------")
    print(f"  Total rows           : {total_rows}")
    print("  Instrument types     :")
    for instrument_type in INSTRUMENT_TYPE_SUMMARY_ORDER:
        label = instrument_type.ljust(12)
        print(f"    {label}: {stats[f'type_{instrument_type}']}")
    print(f"  Filled stock target  : {stats['filled_targets']}")
    print(f"  Enriched ETF/Fds     : {stats['funds_enriched']}")
    print(f"  onvista enriched     : {stats['onvista_enriched']}")
    print(f"  onvista errors       : {stats['onvista_errors']}")
    print(f"  Skipped by filter    : {stats['filtered_skip']}")
    print(f"  Non-equity rows      : {stats['non_equity_skip']}")
    print(f"  No analyst coverage  : {stats['no_coverage']}")
    print(f"  Fund enrich errors   : {stats['fund_errors']}")
    print(f"  No/invalid ISIN      : {stats['no_isin']}")
    if saved_xlsx:
        print(f"  Saved to             : {saved_xlsx}")
    if saved_csv:
        print(f"  Additional CSV saved : {saved_csv}")
    if saved_top_csv:
        print(f"  Top CSV saved        : {saved_top_csv}")


def _is_generated_top_csv(path: str) -> bool:
    return Path(path).suffix.lower() == ".csv" and Path(path).stem.lower().endswith("_top")


def collect_batch_csv_files(batch_folder: str) -> list[str]:
    csv_files = sorted(glob.glob(os.path.join(batch_folder, "*.csv")))
    generated_top_files = [path for path in csv_files if _is_generated_top_csv(path)]
    if generated_top_files:
        print(f"Skipping {len(generated_top_files)} generated *_top.csv file(s)")
    return [path for path in csv_files if path not in generated_top_files]


def process_batch_csv_file(csv_file: str, args: argparse.Namespace) -> bool:
    output_path = build_output_path(csv_file, args.output_dir, None)
    print(f"Processing {csv_file} -> {output_path}")
    try:
        df = read_activetrader_csv(
            csv_file,
            clear_comments=not args.bid_ask_only and not args.skip_subcategories,
            clear_comment3=not args.bid_ask_only,
        )
    except Exception as exc:
        print(f"  ! skipped unreadable CSV: {exc}")
        return False
    try:
        if args.bid_ask_only:
            stats = process_bid_ask_only_dataframe(df, args, os.path.basename(csv_file))
        else:
            stats = process_dataframe(df, args, os.path.basename(csv_file))
    except Exception as exc:
        print(f"  ! processing failed: {_format_http_error(exc)}")
        return False
    apply_blank_comment(df, args)
    ok, final_xlsx, final_csv, final_top_csv = write_outputs(
        df,
        output_path,
        save_csv=True,
        include_detail_columns=args.show_detail_columns,
        generate_isin_links=not args.skip_isin_links and not args.bid_ask_only,
        source_csv_path=csv_file,
        allow_source_csv_overwrite=args.bid_ask_only,
        strip_fund_country_from_comment=not args.skip_subcategories,
    )
    if not ok:
        return False
    print_summary(len(df), stats, final_xlsx, final_csv, final_top_csv)
    return True


def main() -> None:
    global USE_LOCAL_OVERRIDES, CLASSIFICATION_PROVIDER_OVERRIDE, CLASSIFICATION_MODEL_OVERRIDE
    parser = argparse.ArgumentParser()
    parser.add_argument("input_csv", nargs="?", help="Path to ActiveTrader CSV export")
    parser.add_argument("output_xlsx", nargs="?", help="Optional output XLSX path")
    parser.add_argument("--batch-folder", help="Folder containing CSV files to process")
    parser.add_argument("--batch-workers", type=int, default=1, help="Number of CSV files to process in parallel in --batch-folder mode")
    parser.add_argument("--output-dir", help="Folder to save output files")
    parser.add_argument("--tag-only", action="store_true", help="Only fill COMMENT with subcategory tags inferred from filename/subcategories.csv")
    parser.add_argument("--tag-output", help="CSV output path for --tag-only (default: overwrite input CSV)")
    parser.add_argument("--tag-no-llm", action="store_true", help="Disable LLM fallback in --tag-only mode")
    parser.add_argument("--allow-new-tags", action="store_true", help="Allow the configured LLM to create new subcategory tags during normal enrichment")
    parser.add_argument("--skip-subcategories", action="store_true", help="Skip COMMENT subcategory tagging and subcategory-based sorting during normal enrichment")
    comment_group = parser.add_mutually_exclusive_group()
    comment_group.add_argument(
        "--blank-comment",
        dest="blank_comment",
        action="store_true",
        help="Force COMMENT to be empty in the output, regardless of tagging/preservation logic",
    )
    comment_group.add_argument(
        "--keep-comment",
        dest="blank_comment",
        action="store_false",
        help="Let COMMENT be filled/preserved by the normal tagging logic (default)",
    )
    parser.add_argument(
        "--classification-provider",
        choices=["config", "ollama", "deepseek", "minimax", "none"],
        default="config",
        help="LLM provider for COMMENT subcategory fallback (default: config.yaml)",
    )
    parser.add_argument(
        "--classification-model",
        help="Temporarily override the configured model for the selected classification provider",
    )
    parser.add_argument("--promote-candidates-to-overrides", action="store_true", help="Promote high-confidence entries from isin_symbol_candidates.yaml into isin_symbol_overrides.yaml and exit")
    parser.add_argument("--sort-xlsx", type=Path, metavar="XLSX", help="Sort an existing XLSX file by COMMENT subcategory in-place and exit")
    parser.add_argument("--sort-xlsx-output", type=Path, metavar="OUT", help="Write sorted result to OUT instead of overwriting the input (only with --sort-xlsx)")
    parser.add_argument("--sleep", type=float, default=0.3, help="Seconds between remote calls")
    parser.add_argument("--yahoo-batch-size", type=int, default=80, help="Pause for --yahoo-batch-cooldown after this many equity rows in a row. 0 disables batching.")
    parser.add_argument("--yahoo-batch-cooldown", type=float, default=10.0, help="Seconds to pause after each --yahoo-batch-size equity rows")
    parser.add_argument("--comment2-with-source-meta", action="store_true", help="Append analyst count and source abbreviation to COMMENT2")
    parser.add_argument("--bid-ask-only", action="store_true", help="Only refresh Bid/Ask from onvista; preserve comments, target prices, returns, and TA")
    detail_group = parser.add_mutually_exclusive_group()
    detail_group.add_argument(
        "--show-detail-columns",
        dest="show_detail_columns",
        action="store_true",
        help="Include DETAIL_SYMBOL and DETAIL_SOURCE in exported CSV/XLSX",
    )
    detail_group.add_argument(
        "--hide-detail-columns",
        dest="show_detail_columns",
        action="store_false",
        help="Hide DETAIL_SYMBOL and DETAIL_SOURCE in exported CSV/XLSX",
    )
    parser.add_argument(
        "--update-types",
        nargs="+",
        help="Which instrument types to update: equity etf fund index certificate other, or all",
    )
    parser.add_argument(
        "--fund-data-source",
        choices=["yahoo", "eodhd", "eodhd_then_yahoo"],
        default="eodhd_then_yahoo",
        help="ETF/Fund enrichment source (default: EODHD first, then Yahoo fallback)",
    )
    override_group = parser.add_mutually_exclusive_group()
    override_group.add_argument(
        "--use-local-overrides",
        dest="use_local_overrides",
        action="store_true",
        help="Use symbols from isin_symbol_overrides.yaml during resolution",
    )
    override_group.add_argument(
        "--ignore-local-overrides",
        dest="use_local_overrides",
        action="store_false",
        help="Ignore symbols from isin_symbol_overrides.yaml during resolution",
    )
    parser.add_argument("--eodhd-api-key", default=DEFAULT_EODHD_API_KEY, help="EODHD API key for ETF/Fund enrichment")
    parser.add_argument("--skip-ta", dest="skip_ta", action="store_true", help="Skip MACD+Stochastic signal computation for COMMENT3")
    parser.add_argument("--skip-isin-links", action="store_true", help="Skip resolving ISIN web links for XLSX/CSV hyperlinks")
    parser.set_defaults(show_detail_columns=False, use_local_overrides=False, blank_comment=False)
    args = parser.parse_args()
    USE_LOCAL_OVERRIDES = bool(args.use_local_overrides)
    CLASSIFICATION_PROVIDER_OVERRIDE = "" if args.classification_provider == "config" else args.classification_provider
    CLASSIFICATION_MODEL_OVERRIDE = str(args.classification_model or "").strip()

    try:
        args.update_types = normalize_update_types(args.update_types)
    except ValueError as e:
        parser.error(str(e))
    if args.batch_workers < 1:
        parser.error("--batch-workers must be at least 1")

    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)

    if args.promote_candidates_to_overrides:
        promoted = promote_symbol_candidates_to_overrides()
        if promoted:
            print(f"Promoted {len(promoted)} candidate(s) into {SYMBOL_OVERRIDES_PATH.name}:")
            for item in promoted:
                print(f"  {item}")
        else:
            print("No promotable candidates found.")
        return

    if args.sort_xlsx:
        input_xlsx = args.sort_xlsx.resolve()
        out_xlsx = args.sort_xlsx_output.resolve() if args.sort_xlsx_output else input_xlsx
        sort_xlsx_by_comment_subcategory(input_xlsx, out_xlsx)
        print(f"Sorted file written to: {out_xlsx}")
        return

    if args.tag_only:
        if args.batch_folder:
            csv_files = collect_batch_csv_files(args.batch_folder)
            if not csv_files:
                print(f"No input CSV files found in folder: {args.batch_folder}")
                sys.exit(1)
            if args.tag_output and len(csv_files) > 1:
                parser.error("--tag-output can only be used with a single input CSV")
            for csv_file in csv_files:
                output_csv = (
                    os.path.join(args.output_dir, os.path.basename(csv_file))
                    if args.output_dir else None
                )
                tag_csv_comments(csv_file, output_csv=output_csv, use_llm_fallback=not args.tag_no_llm)
            return

        if not args.input_csv:
            parser.error("input_csv is required for --tag-only unless --batch-folder is used")
        tag_csv_comments(args.input_csv, output_csv=args.tag_output, use_llm_fallback=not args.tag_no_llm)
        return

    if not args.input_csv and not args.batch_folder:
        parser.error("input_csv is required unless --batch-folder is used")

    if args.batch_folder:
        csv_files = collect_batch_csv_files(args.batch_folder)
        if not csv_files:
            print(f"No input CSV files found in folder: {args.batch_folder}")
            sys.exit(1)
        if args.batch_workers == 1:
            for csv_file in csv_files:
                process_batch_csv_file(csv_file, args)
        else:
            worker_count = min(args.batch_workers, len(csv_files))
            print(f"Processing {len(csv_files)} CSV file(s) with {worker_count} worker(s)")
            succeeded = 0
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(process_batch_csv_file, csv_file, args): csv_file
                    for csv_file in csv_files
                }
                for future in as_completed(futures):
                    csv_file = futures[future]
                    try:
                        if future.result():
                            succeeded += 1
                    except Exception as exc:
                        print(f"  ! processing failed for {csv_file}: {_format_http_error(exc)}")
            print(f"Batch complete: {succeeded}/{len(csv_files)} CSV file(s) processed successfully")
        sys.exit(0)

    output_path = build_output_path(args.input_csv, args.output_dir, args.output_xlsx)
    print(f"Auto output filename: {output_path}")
    df = read_activetrader_csv(
        args.input_csv,
        clear_comments=not args.bid_ask_only and not args.skip_subcategories,
        clear_comment3=not args.bid_ask_only,
    )
    if args.bid_ask_only:
        stats = process_bid_ask_only_dataframe(df, args, os.path.basename(args.input_csv))
    else:
        stats = process_dataframe(df, args, os.path.basename(args.input_csv))
    apply_blank_comment(df, args)
    ok, final_xlsx, final_csv, final_top_csv = write_outputs(
        df,
        output_path,
        save_csv=True,
        include_detail_columns=args.show_detail_columns,
        generate_isin_links=not args.skip_isin_links and not args.bid_ask_only,
        source_csv_path=args.input_csv,
        allow_source_csv_overwrite=args.bid_ask_only,
        strip_fund_country_from_comment=not args.skip_subcategories,
    )
    if not ok:
        print_summary(len(df), stats, None, None, None)
        sys.exit(1)
    print_summary(len(df), stats, final_xlsx, final_csv, final_top_csv)


if __name__ == "__main__":
    started_at = time.perf_counter()
    try:
        main()
    finally:
        print()
        print(f"Total runtime: {format_runtime(time.perf_counter() - started_at)}")
