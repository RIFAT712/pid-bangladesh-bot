# scraper.py
# Scrapes the PID website (pressinform.gov.bd) and builds the work queue
# for the image upload pipeline. Outputs an Excel file of new images.

import hashlib
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook

import config


def normalize_url(url):
    """Normalize URL for comparison"""
    url = re.sub(r'^https?://', '', url)
    url = url.replace('pressinform.portal.gov.bd', 'pressinform.gov.bd')
    url = unquote(url)
    url = url.replace('%20', ' ')
    return url


def generate_unique_id(img_url, date_str, counter):
    """Generate a unique identifier for each entry"""
    url_hash = hashlib.md5(img_url.encode()).hexdigest()[:8]
    date_part = ""
    if date_str:
        match = re.search(r'(\d{4})-(\d{2})-(\d{2})', date_str)
        if match:
            date_part = f"{match.group(1)}{match.group(2)}{match.group(3)}_"
    unique_id = f"PID_{date_part}{url_hash}_{counter:04d}"
    return unique_id


def fetch_wikimedia_data(year):
    """Fetch data from Wikimedia Module:PIDDateData for given year"""
    headers = {
        'User-Agent': 'PressInformScraper/1.0 Python/requests'
    }

    urls_to_try = [
        f"https://commons.wikimedia.org/w/index.php?title=Module:PIDDateData/{year}&action=raw",
        f"https://commons.wikimedia.org/wiki/Module:PIDDateData/{year}?action=raw",
        f"https://commons.wikimedia.org/w/api.php?action=query&titles=Module:PIDDateData/{year}&prop=revisions&rvprop=content&format=json&formatversion=2"
    ]

    for url in urls_to_try:
        try:
            print(f"Trying URL: {url}")
            response = requests.get(url, headers=headers, timeout=10)
            print(f"Status code: {response.status_code}")

            if response.status_code == 200:
                content = response.text

                if 'api.php' in url:
                    data = json.loads(content)
                    pages = data.get('query', {}).get('pages', [])
                    if pages and len(pages) > 0:
                        page_data = pages[0]
                        if 'revisions' in page_data and len(page_data['revisions']) > 0:
                            content = page_data['revisions'][0]['content']
                        else:
                            continue
                    else:
                        continue

                if len(content) < 50:
                    continue

                urls = set()
                checksums = set()
                pattern = r'\["(http[^"]+)"\]'
                matches = re.findall(pattern, content)
                print(f"Found {len(matches)} URLs in {year} module")

                for match in matches:
                    normalized = normalize_url(match)
                    urls.add(normalized)

                # Extract checksums — backwards compatible with both formats:
                # Old format: ["url"] = "date"
                # New format: ["url"] = {date="date", checksum="hash"}
                checksum_pattern = r'\["[^"]+"\]\s*=\s*\{[^}]*checksum\s*=\s*"([^"]+)"'
                for cs in re.findall(checksum_pattern, content):
                    checksums.add(cs)
                print(f"Found {len(checksums)} checksums in {year} module")

                if len(urls) > 0:
                    return urls, checksums
        except Exception as e:
            print(f"Error with URL {url}: {e}")
            continue

    print(f"Could not fetch Wikimedia data for {year}")
    return set(), set()


def convert_bengali_date_to_english(bengali_date_text):
    """Convert Bengali date to English yyyy-mm-dd hh:mm:ss format"""
    # Bengali to English digit mapping
    bengali_digits = {'০': '0', '১': '1', '২': '2', '৩': '3', '৪': '4',
                      '৫': '5', '৬': '6', '৭': '7', '৮': '8', '৯': '9'}

    # Bengali to English month mapping
    bengali_months = {
        'জানুয়ারী': '01', 'জানুয়ারি': '01',
        'ফেব্রুয়ারী': '02', 'ফেব্রুয়ারি': '02',
        'মার্চ': '03',
        'এপ্রিল': '04',
        'মে': '05',
        'জুন': '06',
        'জুলাই': '07',
        'আগস্ট': '08',
        'সেপ্টেম্বর': '09',
        'অক্টোবর': '10',
        'নভেম্বর': '11',
        'ডিসেম্বর': '12'
    }

    try:
        # Extract date from text like "বৃহস্পতিবার, ৮ জানুয়ারী, ২০২৬ এ ০৯:৪৩ PM"
        # Pattern: day, date month, year এ time AM/PM
        match = re.search(
            r'([০-৯\d]+)\s+([^\s,]+),?\s+([০-৯\d]+)\s+এ\s+([০-৯\d]+):([০-৯\d]+)\s+(AM|PM)', bengali_date_text)

        if not match:
            return ""

        day = match.group(1)
        month_bengali = match.group(2)
        year = match.group(3)
        hour = match.group(4)
        minute = match.group(5)
        am_pm = match.group(6)

        # Convert Bengali digits to English
        day_en = ''.join(bengali_digits.get(c, c) for c in day)
        year_en = ''.join(bengali_digits.get(c, c) for c in year)
        hour_en = ''.join(bengali_digits.get(c, c) for c in hour)
        minute_en = ''.join(bengali_digits.get(c, c) for c in minute)

        # Convert month
        month_en = bengali_months.get(month_bengali, '01')

        # Convert to 24-hour format
        hour_int = int(hour_en)
        if am_pm == 'PM' and hour_int != 12:
            hour_int += 12
        elif am_pm == 'AM' and hour_int == 12:
            hour_int = 0

        # Format as yyyy-mm-dd hh:mm:ss
        formatted_date = f"{year_en}-{month_en.zfill(2)}-{day_en.zfill(2)} {str(hour_int).zfill(2)}:{minute_en.zfill(2)}:00"

        return formatted_date

    except Exception as e:
        print(f"Error converting Bengali date: {e}")
        return ""


def fetch_detail_date(detail_href):
    """Fetch date from a detail page, with retries. Returns date string or empty string."""
    detail_url = f"https://pressinform.gov.bd{detail_href}"
    detail_max_retries = 10
    for detail_attempt in range(detail_max_retries):
        try:
            detail_response = requests.get(
                detail_url, timeout=10, verify=False)
            if detail_response.status_code == 200:
                detail_soup = BeautifulSoup(
                    detail_response.content, 'html.parser')
                # Try div.content-update-block first, then any <p> containing Bengali date pattern
                date_element = detail_soup.find(
                    'div', class_='content-update-block')
                if not date_element:
                    # Fallback: find a <p> tag containing the Bengali date pattern (এ + AM/PM)
                    for p in detail_soup.find_all('p'):
                        if 'এ' in p.get_text() and ('AM' in p.get_text() or 'PM' in p.get_text()):
                            date_element = p
                            break
                if date_element:
                    date_text = date_element.get_text()
                    print(f"Date text found: {date_text.strip()}")
                    result = convert_bengali_date_to_english(date_text)
                    if result:
                        return result
                    else:
                        print(
                            f"Date conversion failed for text: {date_text.strip()}")
                        return ""
                else:
                    print(f"No date found on detail page: {detail_url}")
                return ""
            else:
                print(
                    f"Failed to fetch detail page (attempt {detail_attempt + 1}/{detail_max_retries}): {detail_url} - Status {detail_response.status_code}")
                if detail_attempt < detail_max_retries - 1:
                    time.sleep(2 ** detail_attempt)
        except Exception as e:
            print(
                f"Error fetching detail page (attempt {detail_attempt + 1}/{detail_max_retries}) {detail_url}: {e}")
            if detail_attempt < detail_max_retries - 1:
                time.sleep(2 ** detail_attempt)
    return ""


def scrape_page(page_num, wikimedia_urls, hard_stop_url):
    """Scrape a single page (page_size=50) row by row.
    Returns (results, hard_stop_hit) where results is a list of (img_url, detail_href) tuples
    for images not yet in Wikimedia, and hard_stop_hit is True if the hard stop URL was encountered.
    Date fetching is deferred — only done for images that need uploading.
    """
    url = f"https://pressinform.gov.bd/pages/daily-photos?archived=true&page={page_num}&page_size=50"
    print(f"Scraping page {page_num} (50 items)...")

    max_retries = 10
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=10, verify=False)
            if response.status_code != 200:
                print(
                    f"Failed to fetch page {page_num} (status {response.status_code})")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                    continue
                return [], False

            soup = BeautifulSoup(response.content, 'html.parser')
            table = soup.find('table', id='noticeTable')

            if not table:
                print(f"No table found on page {page_num}")
                return [], False

            results = []
            hard_stop_hit = False
            total_images_seen = 0
            rows = table.find(
                'tbody', class_='table-tbody').find_all('tr', class_='table-tr')

            for row in rows:
                # Skip the search input row
                if 'toggle-hidden' in row.get('class', []):
                    continue

                # Find image TD
                img_td = row.find('td', {'data-column': 'file'})
                if not img_td:
                    continue

                # Get ALL img tags in this TD
                img_tags = img_td.find_all('img')
                if not img_tags:
                    continue

                detail_link = row.find(
                    'a', href=lambda x: x and '/pages/daily-photos/' in x and x != '#')
                detail_href = detail_link['href'] if detail_link else None

                # Collect new image URLs for this row, stopping at hard stop
                for img_tag in img_tags:
                    if not img_tag.get('src'):
                        continue
                    img_url = img_tag['src']
                    normalized_url = normalize_url(img_url)
                    if hard_stop_url in normalized_url:
                        print(f"\n{'='*60}")
                        print("HARD STOP: Reached the specified stopping point")
                        print(f"Image URL: {img_url}")
                        print(f"{'='*60}")
                        hard_stop_hit = True
                        break  # Do not include this image or any after it in this row
                    total_images_seen += 1
                    # Only keep images not already in Wikimedia
                    if normalized_url not in wikimedia_urls:
                        results.append((img_url, detail_href))
                    else:
                        print(f"Skipping (already in Wikimedia): {img_url}")

                if hard_stop_hit:
                    break  # Stop processing further rows on this page

            # If page had no images at all (not just all-uploaded), treat as end of content
            if total_images_seen == 0 and not hard_stop_hit:
                print(
                    f"Page {page_num}: no images found at all, end of content")
                return None, False  # None signals "end of content" vs [] which means "all uploaded"

            return results, hard_stop_hit

        except Exception as e:
            wait_time = 2 ** attempt
            print(
                f"Error scraping page {page_num} (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                print(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                print(f"Failed after {max_retries} attempts")
                return [], False

    return [], False


def scrape_data():
    """Scrape data from pressinform.gov.bd"""
    output_dir = os.path.expanduser('~/output')
    os.makedirs(output_dir, exist_ok=True)

    current_year = datetime.now().year
    previous_year = current_year - 1

    print(f"Current year: {current_year}")
    print("Fetching Wikimedia data...")
    wikimedia_urls, wikimedia_checksums = fetch_wikimedia_data(current_year)
    print(f"Loaded {len(wikimedia_urls)} URLs from {current_year}")
    prev_year_urls, prev_year_checksums = fetch_wikimedia_data(previous_year)
    print(f"Loaded {len(prev_year_urls)} URLs from {previous_year}")
    wikimedia_urls.update(prev_year_urls)
    wikimedia_checksums.update(prev_year_checksums)
    print(
        f"Total URLs from Wikimedia: {len(wikimedia_urls)}, checksums: {len(wikimedia_checksums)}")

    wb = Workbook()
    ws = wb.active

    fully_uploaded_pages = 0
    page_num = 1
    entry_counter = 1

    # Hard stop URL - if this image is encountered, stop scraping
    HARD_STOP_URL = "objectstorage.ap-dcc-gazipur-1.oraclecloud15.com/n/axvjbnqprylg/b/V2Ministry/o/office-pressinform/2024/12/ec18321a25e844ab9503b7b704aafb34.jpg"

    while fully_uploaded_pages < 3:
        new_items, hard_stop_hit = scrape_page(
            page_num, wikimedia_urls, HARD_STOP_URL)

        if new_items is None:
            print(
                f"Page {page_num}: no images found, end of content. Stopping scraper.")
            break

        if hard_stop_hit and not new_items:
            print(
                f"Hard stop reached on page {page_num} with no new items. Stopping scraper.")
            break

        if not new_items and not hard_stop_hit:
            # scrape_page filters out already-uploaded images, so empty means fully uploaded page
            fully_uploaded_pages += 1
            print(
                f"Page {page_num}: all items already uploaded ({fully_uploaded_pages}/3 fully-uploaded pages)")
        else:
            fully_uploaded_pages = 0
            # Fetch dates only for new images
            for img_url, detail_href in new_items:
                if detail_href:
                    date = fetch_detail_date(detail_href)
                    time.sleep(0.5)
                else:
                    date = ""
                    print(f"No detail link for image: {img_url}")
                unique_id = generate_unique_id(img_url, date, entry_counter)
                detail_url = f"https://pressinform.gov.bd{detail_href}" if detail_href else ""
                print(f"Adding: {unique_id} | {date} | {img_url}")
                ws.append([unique_id, date, img_url, detail_url])
                entry_counter += 1
            print(f"Page {page_num}: {len(new_items)} new items added")

        if hard_stop_hit:
            print(f"Hard stop reached on page {page_num}. Stopping scraper.")
            break

        if fully_uploaded_pages >= 3:
            print(f"\nStopping scraper: 3 consecutive fully-uploaded pages of 50.")
            break

        page_num += 1
        time.sleep(1)

    # Check if any new entries were added
    if entry_counter == 1:  # No new entries found
        print("\nNo new images found. Skipping Excel file creation.")
        return None, wikimedia_checksums

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(
        output_dir, f"pressinform_photos_{timestamp}.xlsx")
    wb.save(output_file)
    print(f"\nData saved to {output_file}")
    print(f"Total rows written: {ws.max_row}")

    return output_file, wikimedia_checksums
