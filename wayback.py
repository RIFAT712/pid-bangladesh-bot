# wayback.py
# All Wayback Machine / Internet Archive operations:
#   - Persistent pending-archive queue (wayback_pending.json)
#   - Save Page Now v2 authenticated archiving
#   - Unauthenticated fallback archiving
#   - Retrieval of archived snapshots for 404 recovery
#   - Retry loop for URLs that failed in previous runs

import json
import os
import time
import threading

import requests
from urllib.parse import quote

import config
from config import retry_on_failure

session = requests.Session()
session.trust_env = False  # Do not pick up HTTP_PROXY / HTTPS_PROXY env vars; connect directly
# Use a short connect timeout so failures are detected instantly.
# (read timeout is kept long for slow SPN2 polling responses)
CONNECT_TIMEOUT = 8   # seconds

_queue_lock = threading.Lock()

# ── Persistent queue helpers ──────────────────────────────────────────────────

def _load_wayback_queue():
    """Load the persistent pending-archive queue from disk."""
    if not os.path.exists(config.WAYBACK_QUEUE_PATH):
        return []
    try:
        with open(config.WAYBACK_QUEUE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Warning: could not read wayback queue: {e}")
        return []


def _save_wayback_queue(queue):
    """Persist the pending-archive queue to disk."""
    try:
        with open(config.WAYBACK_QUEUE_PATH, 'w', encoding='utf-8') as f:
            json.dump(queue, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Warning: could not save wayback queue: {e}")


def _enqueue_wayback(url):
    """Add a URL to the persistent queue if not already present."""
    with _queue_lock:
        queue = _load_wayback_queue()
        if url not in queue:
            queue.append(url)
            _save_wayback_queue(queue)
            print(f"  Queued for next run: {url}")


def _dequeue_wayback(url):
    """Remove a successfully archived URL from the queue."""
    with _queue_lock:
        queue = _load_wayback_queue()
        if url in queue:
            queue.remove(url)
            _save_wayback_queue(queue)


# ── Core Wayback operations ───────────────────────────────────────────────────

@retry_on_failure(max_attempts=10, delay=2)
def get_wayback_url(url):
    """Get the oldest archived version from Wayback Machine (used for 404 fallback)"""
    try:
        encoded_url = quote(url, safe='')
        api_url = f"http://archive.org/wayback/available?url={encoded_url}"

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = session.get(api_url, headers=headers, timeout=30)
        response.raise_for_status()

        data = response.json()

        if data.get('archived_snapshots') and data['archived_snapshots'].get('closest'):
            wayback_url = data['archived_snapshots']['closest']['url']

            cdx_url = f"http://web.archive.org/cdx/search/cdx?url={encoded_url}&limit=1&output=json"
            cdx_response = session.get(cdx_url, headers=headers, timeout=30)

            if cdx_response.status_code == 200:
                cdx_data = cdx_response.json()
                if len(cdx_data) > 1:
                    timestamp = cdx_data[1][1]
                    original_url = cdx_data[1][2]
                    oldest_url = f"http://web.archive.org/web/{timestamp}/{original_url}"
                    return oldest_url, None

            return wayback_url, None
        else:
            return None, "No archived version found"

    except Exception as e:
        return None, f"Wayback Machine error: {str(e)}"


def archive_to_wayback(url, _enqueue_on_fail=True):
    """Submit URL to the Save Page Now v2 API for Wayback Machine archiving.
    Uses Internet Archive S3-like credentials (ia.key) when available.
    Falls back to unauthenticated request if keys are missing.
    On failure, adds the URL to wayback_pending.json for retry on the next run
    (unless _enqueue_on_fail=False, used internally by the retry loop).
    Returns True if the archive was confirmed created, False otherwise.
    """
    # web.archive.org is not reachable from Toolforge Kubernetes pods.
    # Silently queue the URL for processing on a local machine instead.
    if config.TOOL_DATA_DIR:
        if _enqueue_on_fail:
            _enqueue_wayback(url)
        return False

    print(f"Archiving to Wayback Machine: {url}")
    success = False
    try:
        headers = {
            'User-Agent': 'PID-Bangladesh-UploadBot/2.0',
            'Accept': 'application/json',
        }
        # Authenticated path — uses SPN2 API with IA S3-like keys
        if config.IA_KEYS.get('access') and config.IA_KEYS.get('secret'):
            headers['Authorization'] = f"LOW {config.IA_KEYS['access']}:{config.IA_KEYS['secret']}"
            data = {'url': url, 'capture_all': '1'}
            response = session.post(
                'https://web.archive.org/save',
                headers=headers, data=data, timeout=(CONNECT_TIMEOUT, 120))
            try:
                resp_json = response.json()
            except Exception:
                resp_json = {}

            if response.status_code == 200 and resp_json.get('job_id'):
                job_id = resp_json['job_id']
                print(f"SPN2 job submitted: {job_id} — polling for result...")
                # Poll for job completion (up to ~90s, 18 × 5 s)
                timed_out = True
                for _ in range(18):
                    time.sleep(5)
                    try:
                        status_r = session.get(
                            f'https://web.archive.org/save/status/{job_id}',
                            headers=headers, timeout=(CONNECT_TIMEOUT, 30))
                        status = status_r.json()
                    except Exception:
                        continue
                    s = status.get('status', '')
                    if s == 'success':
                        archived = 'https://web.archive.org/web/' + status.get('timestamp', '') + '/' + url
                        print(f"Successfully archived: {archived}")
                        _dequeue_wayback(url)  # remove from retry queue if it was pending
                        success = True
                        timed_out = False
                        break
                    elif s == 'error':
                        print(f"  SPN2 error for {url}: {status.get('exception', status)}")
                        timed_out = False
                        break
                    # status == 'pending' — keep polling
                if timed_out:
                    print(f"  WARNING: SPN2 job timed out (>90 s) for: {url}")
                    print(f"  URL will be retried on the next run via wayback_pending.json")
            elif response.status_code == 523:
                # Cloudflare 523 = IA cannot reach the origin server
                print(f"  WARNING: Wayback Machine could not reach the origin server for: {url}")
                print(f"  (HTTP 523 – the host may be blocking IA crawlers)")
            else:
                print(f"  SPN2 API error {response.status_code} for {url}: {resp_json}")
        else:
            # Unauthenticated fallback — GET request which browsers use
            # NOTE: This is unreliable; configure ia.key for guaranteed archiving
            session.get(
                f'https://web.archive.org/save/{url}',
                headers={'User-Agent': 'PID-Bangladesh-UploadBot/2.0'},
                timeout=(CONNECT_TIMEOUT, 60), allow_redirects=True)
            # Verify it was actually archived by querying the availability API
            time.sleep(5)
            check = session.get(
                f'https://archive.org/wayback/available?url={url}',
                timeout=(CONNECT_TIMEOUT, 15))
            snapshots = check.json().get('archived_snapshots', {})
            if snapshots:
                print(f"Confirmed archived (unauthenticated): {snapshots.get('closest', {}).get('url', '')}")
                _dequeue_wayback(url)
                success = True
            else:
                print(f"Warning: Wayback snapshot not confirmed for {url}")
                print(f"Tip: Add Internet Archive S3 keys to ia.key for reliable archiving.")
    except Exception as e:
        print(f"Wayback archiving error for {url}: {e}")

    if not success and _enqueue_on_fail:
        print(f"  Wayback Machine unavailable or failed — saving to queue for next run.")
        _enqueue_wayback(url)

    return success


from concurrent.futures import ThreadPoolExecutor, as_completed

def retry_wayback_queue():
    """At the start of each run, retry all URLs left in wayback_pending.json.
    Successfully archived URLs are removed from the queue.
    On Toolforge, web.archive.org is unreachable — just report the queue size.
    """
    queue = _load_wayback_queue()
    if not queue:
        print("Wayback queue is empty — nothing to retry.")
        return
    if config.TOOL_DATA_DIR:
        print(f"Wayback queue has {len(queue)} URL(s) pending. "
              f"Run locally to flush (web.archive.org not reachable from Toolforge).")
        return
    print(f"\nRetrying {len(queue)} pending Wayback Machine archive(s) from previous runs (in parallel)...")
    still_pending = []
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_url = {executor.submit(archive_to_wayback, url, False): url for url in queue}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                success = future.result()
                if success:
                    print(f"  Retry succeeded: {url}")
                else:
                    print(f"  Still failing, keeping in queue: {url}")
                    still_pending.append(url)
            except Exception as exc:
                print(f"  Retry generated an exception for {url}: {exc}")
                still_pending.append(url)

    _save_wayback_queue(still_pending)
    print(f"Wayback retry done. {len(queue) - len(still_pending)} succeeded, {len(still_pending)} still pending.")
