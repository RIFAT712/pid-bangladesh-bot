# test_processor_cv.py
# Runs the real ImageProcessor.find_white_separator() against a sample of live
# PID archive images and writes annotated debug output + a summary report.
#
# Usage:
#   python test_processor_cv.py [--pages N] [--page-start P] [--out DIR]
#
# Options:
#   --pages N       Number of archive pages to scrape  (default: 3)
#   --page-start P  First archive page number to fetch (default: 665)
#   --out DIR       Output directory                   (default: test_output)

import argparse
import math
import os
import sys
import time
import warnings
from datetime import datetime

import cv2
import numpy as np
import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")  # suppress SSL warnings


# ── Constants ─────────────────────────────────────────────────────────────────
HEADERS = {"User-Agent": "Mozilla/5.0 (PID-test-script)"}
REQUEST_TIMEOUT = 15
PASS_COLORS = {
    "pass1":   (0,   200,  0),    # green   — strict white
    "pass2":   (0,   165, 255),   # orange  — relaxed off-white
    "pass3":   (255,   0, 255),   # magenta — gradient edge fallback
    "failed":  (0,    0, 255),    # red     — no separator found
}
PASS_LABELS = {
    "pass1":  "Pass 1 (strict white >240/95%)",
    "pass2":  "Pass 2 (off-white >220/90%)",
    "pass3":  "Pass 3 (gradient edge fallback)",
    "failed": "FAILED — no separator",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    return url.replace("pressinform.portal.gov.bd", "pressinform.gov.bd")


def fetch_image_urls(page: int) -> list[str]:
    """Return all thumbnail image URLs from one PID archive page."""
    url = (
        f"https://pressinform.gov.bd/pages/daily-photos"
        f"?archived=true&page={page}&page_size=100"
    )
    try:
        resp = requests.get(url, headers=HEADERS, verify=False, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")
        table = soup.find("table", id="noticeTable")
        if not table:
            print(f"  [page {page}] WARNING: no #noticeTable found")
            return []
        return [
            normalize_url(img["src"])
            for img in table.find_all("img")
            if img.get("src")
        ]
    except Exception as exc:
        print(f"  [page {page}] ERROR fetching page: {exc}")
        return []


def download_cv2(url: str):
    """Download image bytes and decode with OpenCV. Returns (img_cv, error)."""
    try:
        resp = requests.get(url, headers=HEADERS, verify=False, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        arr = np.asarray(bytearray(resp.content), dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None, "cv2.imdecode returned None"
        return img, None
    except Exception as exc:
        return None, str(exc)


def detect_pass(image) -> tuple[str, int, int]:
    """
    Run the three-pass algorithm step-by-step and return which pass fired.

    Returns:
        (pass_name, separator_row, offset)
        pass_name is one of 'pass1', 'pass2', 'pass3', 'failed'
    """
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    start_row = int(height * 0.35)
    end_row   = int(height * 0.95)

    bright_240 = np.sum(gray > 240, axis=1) / width
    bright_220 = np.sum(gray > 220, axis=1) / width

    MIN_RUN = max(3, int(height / 300))
    offset  = max(2, int(round((3 / math.log(3100 / 670)) * math.log(height / 670))))

    def _scan(bg_pct, threshold_pct, s, e):
        run_start, run_len = -1, 0
        for y in range(s, e):
            if bg_pct[y] > threshold_pct:
                if run_start == -1:
                    run_start = y
                run_len += 1
                if run_len >= MIN_RUN:
                    return run_start
            else:
                run_start, run_len = -1, 0
        return -1

    sep = _scan(bright_240, 0.95, start_row, end_row)
    if sep != -1:
        return "pass1", max(0, sep - offset), offset

    sep = _scan(bright_220, 0.90, start_row, end_row)
    if sep != -1:
        return "pass2", max(0, sep - offset), offset

    # Pass 3 — gradient edge
    blurred    = cv2.GaussianBlur(gray, (5, 5), 0)
    sobel      = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)
    row_energy = np.abs(sobel).sum(axis=1)
    fb_start   = int(height * 0.50)
    slice_     = row_energy[fb_start:end_row]
    if slice_.size > 0:
        best = int(np.argmax(slice_))
        sep  = fb_start + best
        if row_energy[sep] > slice_.mean() * 2.5:
            return "pass3", max(0, sep - offset), offset

    return "failed", -1, offset


def annotate_debug(image, sep_row: int, pass_name: str, img_idx: int) -> np.ndarray:
    """Draw the separator line and a pass label on a copy of the image."""
    debug = image.copy()
    h, w  = debug.shape[:2]
    color = PASS_COLORS[pass_name]
    label = f"#{img_idx}  {PASS_LABELS[pass_name]}"

    if sep_row != -1:
        cv2.line(debug, (0, sep_row), (w, sep_row), color, 3)
        cv2.putText(
            debug, f"row {sep_row}", (10, max(sep_row - 8, 20)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv2.LINE_AA
        )

    # Semi-transparent banner at the top
    banner_h = 36
    overlay  = debug.copy()
    cv2.rectangle(overlay, (0, 0), (w, banner_h), (30, 30, 30), -1)
    cv2.addWeighted(overlay, 0.6, debug, 0.4, 0, debug)
    cv2.putText(
        debug, label, (8, 24),
        cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2, cv2.LINE_AA
    )
    return debug


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Test PID separator detection")
    parser.add_argument("--pages",      type=int, default=3,              help="Number of archive pages to test")
    parser.add_argument("--page-start", type=int, default=665,            help="First archive page number")
    parser.add_argument("--out",        type=str, default="test_output",  help="Output directory")
    args = parser.parse_args()

    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)

    print(f"\nFetching URLs from {args.pages} page(s) starting at page {args.page_start}...")
    all_urls: list[tuple[int, str]] = []   # (page_number, url)
    for p in range(args.page_start, args.page_start + args.pages):
        urls = fetch_image_urls(p)
        print(f"  Page {p}: {len(urls)} images found")
        for u in urls:
            all_urls.append((p, u))
        time.sleep(0.3)   # be polite

    total = len(all_urls)
    print(f"\nTotal images to test: {total}\n{'='*60}")

    # ── Per-image processing ──────────────────────────────────────────────────
    counters = {"pass1": 0, "pass2": 0, "pass3": 0, "failed": 0, "download_error": 0}
    report_lines: list[str] = []

    for idx, (page, url) in enumerate(all_urls):
        print(f"[{idx+1:>3}/{total}] page={page}  {url}")

        img, err = download_cv2(url)
        if err:
            print(f"         ✗ Download error: {err}")
            counters["download_error"] += 1
            report_lines.append(f"{idx+1:>3}  DOWNLOAD_ERROR  page={page}  {url}\n    {err}")
            continue

        h, w = img.shape[:2]
        t0 = time.perf_counter()
        pass_name, sep_row, offset = detect_pass(img)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        counters[pass_name] += 1
        status = "✓" if pass_name != "failed" else "✗"
        print(f"         {status} {PASS_LABELS[pass_name]}  row={sep_row}  size={w}×{h}  {elapsed_ms:.1f}ms")

        # Write debug annotated image
        debug = annotate_debug(img, sep_row, pass_name, idx + 1)
        cv2.imwrite(os.path.join(out_dir, f"{idx+1:03d}_debug.jpg"), debug)

        # Write cropped sections if separator was found
        if sep_row != -1:
            photo = img[:sep_row, :]
            text  = img[sep_row:, :]
            if photo.size > 0:
                cv2.imwrite(os.path.join(out_dir, f"{idx+1:03d}_photo.jpg"), photo)
            if text.size > 0:
                cv2.imwrite(os.path.join(out_dir, f"{idx+1:03d}_text.jpg"),  text)

        report_lines.append(
            f"{idx+1:>3}  {pass_name:<8}  row={sep_row:<6}  {elapsed_ms:>6.1f}ms"
            f"  size={w}x{h}  page={page}  {url}"
        )

    # ── Summary ───────────────────────────────────────────────────────────────
    processed = total - counters["download_error"]
    found     = counters["pass1"] + counters["pass2"] + counters["pass3"]
    rate      = (found / processed * 100) if processed else 0

    summary = f"""
{'='*60}
PID Separator Detection — Test Report
Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Pages     : {args.page_start} → {args.page_start + args.pages - 1}
{'='*60}
Total images   : {total}
Download errors: {counters['download_error']}
Successfully processed: {processed}

Detection results:
  Pass 1 (strict white  >240 / 95%) : {counters['pass1']:>4}  ({counters['pass1']/processed*100 if processed else 0:.1f}%)
  Pass 2 (off-white     >220 / 90%) : {counters['pass2']:>4}  ({counters['pass2']/processed*100 if processed else 0:.1f}%)
  Pass 3 (gradient edge fallback)   : {counters['pass3']:>4}  ({counters['pass3']/processed*100 if processed else 0:.1f}%)
  Failed (no separator found)       : {counters['failed']:>4}  ({counters['failed']/processed*100 if processed else 0:.1f}%)

Overall detection rate: {found}/{processed} = {rate:.1f}%
{'='*60}

Per-image log:
{'─'*60}
"""

    print(summary)

    report_path = os.path.join(out_dir, "test_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(summary)
        f.write("\n".join(report_lines))
        f.write("\n")

    print(f"Output saved to: {os.path.abspath(out_dir)}/")
    print(f"Report saved to: {os.path.abspath(report_path)}")


if __name__ == "__main__":
    main()
