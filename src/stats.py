# stats.py
# Daily totals for the panel's Stats page: what each run uploaded, why photos
# failed, how the Gemini tiers were used, and how big the review backlog is.
# Written by the bot at the end of every run into $TOOL_DATA_DIR, because the
# panel's other sources (run_state's last 200 runs, the Commons logs) either
# forget or never recorded these.

import json
import os
from datetime import datetime, timezone

import config
from config import logger

CAUSES = ('download', 'ocr', 'translation', 'title', 'upload', 'other')
COMMONS_API = 'https://commons.wikimedia.org/w/api.php'
REVIEW_SEARCH = 'hastemplate:"Auto-translated PID English description"'
UNCATEGORISED = 'Category:Press Information Department images without category'


def failure_cause(row):
    """Which step failed this row, or None if it didn't fail."""
    ocr = row.get('ocr_status', '') or ''
    upload = row.get('upload_status', '') or ''
    if upload == 'Success' or upload.startswith('Skipped') or ocr == 'No URL':
        return None
    low = ocr.lower()
    if '404' in low or 'download' in low or 'image processing error' in low:
        return 'download'
    if ocr.startswith('OCR failed') or ocr == 'No text detected':
        return 'ocr'
    if (row.get('translation_status') or '').startswith('Error'):
        return 'translation'
    if (row.get('filename_status') or '').startswith('Error'):
        return 'title'
    if upload.startswith('Failed'):
        return 'upload'
    return 'other'


def load(path=None):
    try:
        with open(path or config.STATS_PATH, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _empty_day():
    return {'runs': 0, 'uploaded': 0, 'duplicates': 0,
            'failed': {c: 0 for c in CAUSES},
            'gemini': {'free': 0, 'paid': 0, 'free_limit': 0},
            'backlog': None}


def record_run(uploaded, duplicates, failed, gemini, backlog, path=None, today=None):
    """Add one run to today's totals. Never raises: stats must not fail a run."""
    path = path or config.STATS_PATH
    today = today or datetime.now(timezone.utc).date().isoformat()
    try:
        data = load(path)
        day = _empty_day()
        old = data.get(today)
        if isinstance(old, dict):
            for k in ('runs', 'uploaded', 'duplicates'):
                day[k] = int(old.get(k, 0) or 0)
            day['failed'] = {c: int((old.get('failed') or {}).get(c, 0) or 0) for c in CAUSES}
            day['gemini'] = {k: int((old.get('gemini') or {}).get(k, 0) or 0) for k in day['gemini']}
            day['backlog'] = old.get('backlog')
        day['runs'] += 1
        day['uploaded'] += uploaded
        day['duplicates'] += duplicates
        for c, n in (failed or {}).items():
            day['failed'][c if c in CAUSES else 'other'] += n
        for k in day['gemini']:
            day['gemini'][k] += (gemini or {}).get(k, 0)
        if backlog:
            day['backlog'] = backlog
        data[today] = day
        tmp = f'{path}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"Could not record daily stats: {e}")


def fetch_backlog():
    """{'review': n, 'uncategorised': n} from Commons, or None if it can't say."""
    try:
        s = config.http_session(retries=2)
        headers = {'User-Agent': 'PID-Bangladesh-UploadBot stats'}
        review = s.get(COMMONS_API, timeout=15, headers=headers, params={
            'action': 'query', 'list': 'search', 'srsearch': REVIEW_SEARCH,
            'srnamespace': 6, 'srlimit': 1, 'srinfo': 'totalhits', 'format': 'json',
            'formatversion': 2}).json()['query']['searchinfo']['totalhits']
        pages = s.get(COMMONS_API, timeout=15, headers=headers, params={
            'action': 'query', 'prop': 'categoryinfo', 'titles': UNCATEGORISED,
            'format': 'json', 'formatversion': 2}).json()['query']['pages']
        return {'review': int(review),
                'uncategorised': int(pages[0].get('categoryinfo', {}).get('files', 0))}
    except Exception as e:
        logger.warning(f"Could not fetch review backlog: {e}")
        return None
