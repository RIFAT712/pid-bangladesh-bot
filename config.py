# config.py
# Central configuration, constants, logging setup, IPv4 enforcement,
# and shared utility functions for the PID Image Processor & Uploader bot.

import hashlib
import logging
import os
import socket
import sys
import warnings
from functools import wraps
from time import sleep

import urllib3.util.connection as urllib3_cn

warnings.filterwarnings('ignore')

# ── UTF-8 stdout/stderr (Windows Bengali support) ─────────────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr = open(sys.stderr.fileno(), mode='w', encoding='utf-8', buffering=1)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ── IPv4 enforcement (avoids K8s / Toolforge IPv6 issues) ─────────────────────
def allowed_gai_family():
    """Force IPv4 connections only"""
    return socket.AF_INET

urllib3_cn.allowed_gai_family = allowed_gai_family
print("Forced IPv4 connections to avoid K8s networking issues")

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# $TOOL_DATA_DIR is set by the Toolforge Build Service container to
# /data/project/<toolname>. $HOME inside the container points to /app,
# so credential files must be looked up via TOOL_DATA_DIR, not SCRIPT_DIR.
TOOL_DATA_DIR = os.environ.get('TOOL_DATA_DIR', '')   # '' means local dev

# Prefer TOOL_DATA_DIR (Toolforge Build Service), fall back to SCRIPT_DIR (local)
CREDS_DIR = TOOL_DATA_DIR if TOOL_DATA_DIR else SCRIPT_DIR


def find_pywikibot_config(filename):
    """Search for a pywikibot config file in common locations"""
    search_paths = [
        # Toolforge Build Service: credentials live in $TOOL_DATA_DIR
        os.path.join(CREDS_DIR, filename),
        # Same directory as main.py (local dev)
        os.path.join(SCRIPT_DIR, filename),
        os.path.expanduser(f'~/pywikibot/{filename}'),   # ~/pywikibot/
        os.path.expanduser(f'~/.pywikibot/{filename}'),  # ~/.pywikibot/
        # Current working directory
        os.path.join(os.getcwd(), filename),
    ]
    for path in search_paths:
        if os.path.exists(path):
            return path
    return os.path.join(CREDS_DIR, filename)  # Fallback


USER_CONFIG_PATH = find_pywikibot_config('user-config.py')
PASSWORD_FILE_PATH = find_pywikibot_config('user-password.py')

# ── AI / API settings ─────────────────────────────────────────────────────────
VERTEX_LOCATION = "global"
PRIMARY_MODEL = "gemini-3.1-flash-lite"
FALLBACK_MODEL = "gemini-3.5-flash"

# Credential files: $TOOL_DATA_DIR on Toolforge, SCRIPT_DIR locally
GEMINI_CONFIG_PATH = os.path.join(CREDS_DIR, 'gemini.key')   # AI Studio free API key
IA_KEY_PATH = os.path.join(CREDS_DIR, 'ia.key')              # Internet Archive S3-like keys
WAYBACK_QUEUE_PATH = os.path.join(CREDS_DIR, 'wayback_pending.json')  # Persistent retry queue

MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF = 60.0

# ── Mutable shared state (populated at runtime by credentials module) ─────────
GOOGLE_CREDENTIALS = None            # set by credentials.load_credentials()
IA_KEYS = {'access': None, 'secret': None}  # set by credentials.load_ia_keys()

# ── Google Drive OCR ──────────────────────────────────────────────────────────
DRIVE_TOKEN_PATH = os.path.join(SCRIPT_DIR, 'drive_token.json')
DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive.file']

# ── Prompt templates ──────────────────────────────────────────────────────────
TRANSLATION_PROMPT = (
    'Translate the following Bengali text into English in enclyclopedic style. '
    'You may rearrange words or sentences for clarity, but retain all information. '
    'Do not add or omit anything. Only output the translation text and not a single else. '
    'Do not say description or Bengali text in your answer. do not have any bengali text in your answer just give me the translation, no options and no explanations. '
    'Text: "{text}"'
)

TITLE_PROMPT = (
    'Convert this image description (below) into a single Wikimedia Commons\u2013compliant filename (do NOT add the \u201cFile:\u201d prefix, or wikitext, or Title:, do not add filename extention). Follow Wikimedia Commons file naming guidelines: be descriptive, specific, precise, concise and neutral; include date as YYYY-MM-DD if present; avoid photographer/source-only names. Remove any political bias or references to previous governments and strip flattering/propagandistic/honorific language. Output ONLY the filename (no explanation), Regular Case, remove illegal filesystem characters but KEEP spaces and comma and hyphen, keep \u2264240 bytes, and do not add filename extention. '
    'Text: "{text}"'
)

# ── Shared utilities ──────────────────────────────────────────────────────────

def compute_checksum(raw_bytes):
    """Compute MD5 checksum of raw image bytes for duplicate detection"""
    return hashlib.md5(raw_bytes).hexdigest()


def retry_on_failure(max_attempts=10, delay=2):
    """Decorator to retry function on failure"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    result = func(*args, **kwargs)
                    if isinstance(result, tuple) and len(result) == 2:
                        data, error = result
                        if error is None or "Retrieved from Wayback Machine" in str(error):
                            return result
                        if attempt < max_attempts - 1:
                            print(
                                f"Attempt {attempt + 1} failed, retrying in {delay}s...")
                            sleep(delay)
                            continue
                    return result
                except Exception as e:
                    if attempt < max_attempts - 1:
                        print(
                            f"Attempt {attempt + 1} failed: {str(e)}, retrying in {delay}s...")
                        sleep(delay)
                    else:
                        if hasattr(func, '__name__') and 'ocr' in func.__name__.lower():
                            return f"OCR Error: {str(e)}"
                        return None, f"Error after {max_attempts} attempts: {str(e)}"
            return result
        return wrapper
    return decorator
