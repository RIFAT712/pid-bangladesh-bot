# main.py
# Pipeline orchestrator for the PID Image Processor & Uploader.
# All heavy logic lives in the individual modules; this file wires them together.
#
# Execution order:
#   0. Boot checks (pywikibot credentials, infrastructure)
#   1. Scrape pressinform.gov.bd  →  Excel work queue
#   2. For each image:
#       2a. Archive to Wayback Machine
#       2b. Download + separator detection + OCR  (image_processor)
#       2c. Translate Bengali  →  English  (translator)
#       2d. Generate Wikimedia filename  (translator)
#       2e. Upload to Commons  (uploader)
#       2f. Update Module:PIDDateData  (uploader)
#   3. Log results to Commons  (commons_log)

import os
import sys
from time import sleep
import concurrent.futures

import pandas as pd
from flask import Flask
from google import genai
from google.cloud import translate_v2 as translate

# ── Project modules ───────────────────────────────────────────────────────────
import config
import credentials
import wayback
from commons_log import log_to_commons
from image_processor import ImageProcessor
from scraper import scrape_data
from translator import (
    apply_translation_replacements,
    generate_title,
    load_translation_replacements,
    translate_text,
)
from uploader import (
    ensure_pid_infrastructure,
    initialize_pywikibot,
    update_pid_date_data,
    upload_to_commons,
)

logger = config.logger


# ── Main pipeline ─────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("PID Image Processor & Uploader")
    print("=" * 60)
    print()

    # ── 0a. Pywikibot config sanity check ─────────────────────────────────────
    if not os.path.exists(config.USER_CONFIG_PATH):
        print(f"ERROR: Config file not found: {config.USER_CONFIG_PATH}")
        print("Please create user-config.py in the same directory as this script")
        sys.exit(1)

    if not os.path.exists(config.PASSWORD_FILE_PATH):
        print(f"ERROR: Password file not found: {config.PASSWORD_FILE_PATH}")
        print("Please create user-password.py in the same directory as this script")
        sys.exit(1)

    # ── 0b. Early Pywikibot login + infrastructure ────────────────────────────
    print("\nInitializing Pywikibot for pre-scrape checks...")
    _early_result = initialize_pywikibot()
    if _early_result is None:
        print("Error: Failed to initialize Pywikibot")
        sys.exit(1)
    _early_site, _ = _early_result
    print("\nChecking and creating categories/modules before scraping...")
    ensure_pid_infrastructure(_early_site)

    # ── 0c. Pre-translation replacement table ─────────────────────────────────
    _translation_replacements = load_translation_replacements()

    # ── 0d. Internet Archive keys + pending Wayback queue ─────────────────────
    print("\nLoading Internet Archive S3 keys for Save Page Now...")
    credentials.load_ia_keys()

    print("\nChecking Wayback Machine pending queue...")
    wayback.retry_wayback_queue()

    # ── Step 1: Scrape ────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("STEP 1: Scraping data from pressinform.gov.bd")
    print("=" * 60)
    excel_file, wikimedia_checksums = scrape_data()

    # ── Load Google credentials ───────────────────────────────────────────────
    print("\nLoading Google credentials...")
    credentials.load_credentials()
    print("Setting up Google credentials...")
    creds_path = credentials.setup_credentials()

    try:
        # ── Initialise API clients ────────────────────────────────────────────
        print("Initializing Google Cloud clients...")
        image_processor = ImageProcessor()
        success, message = image_processor.initialize_vision_client()
        if not success:
            print(f"Error: {message}")
            sys.exit(1)
        print(message)

        print("Loading Gemini AI Studio key (free secondary account)...")
        gemini_api_key = credentials.load_gemini_api_key()
        genai_client = genai.Client(api_key=gemini_api_key)
        translate_client = translate.Client()
        print("Gemini (AI Studio) and Translate clients initialized")

        print("\nInitializing Pywikibot...")
        result = initialize_pywikibot()
        if result is None:
            print("Error: Failed to initialize Pywikibot")
            sys.exit(1)
        site, FilePage = result

        # ── No new images? ────────────────────────────────────────────────────
        if excel_file is None:
            print("\nNo new images found. Logging to Commons...")
            if log_to_commons(site, df=None):
                print("Log entry created on Commons.")
            else:
                print("Warning: Failed to log to Commons.")
            return

        # ── Load work queue ───────────────────────────────────────────────────
        print(f"\nLoading Excel file: {excel_file}")
        df = pd.read_excel(excel_file, header=None)
        total_rows = len(df)
        print(f"Total rows to process: {total_rows}")

        while df.shape[1] < 14:
            df[df.shape[1]] = ""

        success_count = 0
        failed_count = 0

        # ── Per-image loop ────────────────────────────────────────────────────
        wayback_executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
        for idx in range(total_rows):
            print(f"\n{'='*60}")
            print(f"Processing row {idx + 1}/{total_rows}")
            print(f"{'='*60}")

            try:
                unique_id = str(df.iat[idx, 0]) if pd.notna(
                    df.iat[idx, 0]) else f"image_{idx}"
                date_str = str(df.iat[idx, 1]) if pd.notna(
                    df.iat[idx, 1]) else ""
                image_url = str(df.iat[idx, 2]) if pd.notna(
                    df.iat[idx, 2]) else ""
                detail_url = str(df.iat[idx, 3]) if pd.notna(
                    df.iat[idx, 3]) else ""

                if not image_url or image_url == 'nan':
                    print(f"Row {idx + 1}: No URL, skipping")
                    df.iat[idx, 5] = "No URL"
                    df.to_excel(excel_file, index=False, header=False)
                    continue

                # Step 1.5 — Archive source URLs to Wayback Machine (Async)
                print(f"\nSTEP 1.5: Archiving to Wayback Machine (Async)...")
                wayback_executor.submit(wayback.archive_to_wayback, image_url)
                if detail_url:
                    wayback_executor.submit(wayback.archive_to_wayback, detail_url)

                # Step 2 — Download + OCR
                print(f"\nSTEP 2: Processing image...")
                result = image_processor.process_image(
                    idx + 1, image_url, wikimedia_checksums)

                df.iat[idx, 4] = result['ocr_text']   # Column E: OCR text
                df.iat[idx, 5] = result['status']      # Column F: Status
                df.to_excel(excel_file, index=False, header=False)

                # Checksum duplicate — already on Commons under a different URL
                if result.get('is_duplicate'):
                    print(
                        f"Row {idx + 1}: Checksum match — registering URL in module, skipping upload")
                    dup_checksum = result.get('checksum', '')
                    dup_entry = f'        ["{image_url}"] = {{date="{date_str}", checksum="{dup_checksum}"}},'
                    df.iat[idx, 10] = dup_entry
                    df.iat[idx, 13] = "Skipped (checksum duplicate)"
                    if update_pid_date_data(site, dup_entry):
                        df.iat[idx, 11] = "Success (dup)"
                        success_count += 1
                    else:
                        df.iat[idx, 11] = "Failed"
                        failed_count += 1
                    df.to_excel(excel_file, index=False, header=False)
                    continue

                if result['image'] is None or \
                        result['status'].startswith('Error') or \
                        result['status'].startswith('OCR failed'):
                    print(f"Row {idx + 1}: Image processing failed")
                    failed_count += 1
                    continue

                img_format = result.get('format', 'jpg')

                # Step 3 — Translate
                print(f"\nSTEP 3: Translating text...")
                bengali_text_raw = result['ocr_text']
                print(f"Sanitized OCR Data: {bengali_text_raw}")
                bengali_text = apply_translation_replacements(
                    bengali_text_raw, _translation_replacements)
                print(f"After pre-translation replacements: {bengali_text}")
                translation, trans_status = translate_text(
                    genai_client, translate_client, bengali_text, idx + 1)
                print(f"Translation Data: {translation}")

                df.iat[idx, 6] = translation     # Column G
                df.iat[idx, 7] = trans_status    # Column H
                df.to_excel(excel_file, index=False, header=False)

                if trans_status != "Success":
                    print(f"Row {idx + 1}: Translation failed")
                    failed_count += 1
                    continue

                # Step 4 — Generate filename
                print(f"\nSTEP 4: Generating title...")
                title, title_status = generate_title(
                    genai_client, translation, date_str, idx + 1, img_format)
                print(f"Full Title Data (with extension): {title}")

                df.iat[idx, 8] = title          # Column I
                df.iat[idx, 9] = title_status   # Column J
                df.to_excel(excel_file, index=False, header=False)

                if title_status != "Success":
                    print(f"Row {idx + 1}: Title generation failed")
                    failed_count += 1
                    continue

                # Step 5 — Prepare description
                print(f"\nSTEP 5: Preparing metadata...")
                img_checksum = result.get('checksum', '')
                data_entry = (
                    f'        ["{image_url}"] = '
                    f'{{date="{date_str}", checksum="{img_checksum}"}},'
                )
                df.iat[idx, 10] = data_entry  # Column K

                description = f'''=={{{{int:filedesc}}}}==
{{{{Information
 |description = {{{{bn|1={bengali_text_raw.strip().lstrip('\ufeff').strip()}}}}}{{{{en|1={translation.strip()}{{{{Auto-translated PID English description}}}}}}}}
 |date = {{{{Date-PID|{date_str}}}}}
 |source = {{{{Source-PID | url={image_url}}}}}
 |author = {{{{Institution:Press Information Department}}}}
 |permission =
 |other versions =
}}}}
=={{{{int:license-header}}}}==
{{{{PD-BDGov-PID}}}}
[[Category: Uploaded with pypan]]'''

                df.iat[idx, 12] = "'" + description  # Column M
                df.to_excel(excel_file, index=False, header=False)

                # Step 6 — Upload
                print(f"\nSTEP 6: Uploading to Wikimedia Commons...")
                upload_success, upload_error = upload_to_commons(
                    site, FilePage, result['image'], title, img_format,
                    result.get('exif'), description
                )

                if upload_success:
                    df.iat[idx, 13] = "Success"   # Column N
                    success_count += 1
                    print(f"Row {idx + 1}: Upload successful")
                    sleep(5)

                    print(f"Row {idx + 1}: Updating PIDDateData...")
                    if update_pid_date_data(site, data_entry):
                        df.iat[idx, 11] = "Success"
                        print(f"Row {idx + 1}: PIDDateData updated")
                    else:
                        df.iat[idx, 11] = "Failed"
                        print(f"Row {idx + 1}: PIDDateData update failed")
                else:
                    df.iat[idx, 13] = f"Failed: {upload_error}"
                    failed_count += 1
                    print(f"Row {idx + 1}: Upload failed - {upload_error}")

                df.to_excel(excel_file, index=False, header=False)

            except Exception as e:
                logger.error(f"Error processing row {idx + 1}: {str(e)}")
                df.iat[idx, 13] = f"Error: {str(e)}"
                failed_count += 1
                df.to_excel(excel_file, index=False, header=False)

        # ── Final save + Commons log ──────────────────────────────────────────
        df.to_excel(excel_file, index=False, header=False)

        print("\nWaiting for pending Wayback Machine archives to complete...")
        wayback_executor.shutdown(wait=True)
        print("All Wayback Machine archives completed.")

        print("\nLogging results to Wikimedia Commons...")
        if log_to_commons(site, df, success_count, failed_count, total_rows):
            try:
                os.unlink(excel_file)
                print(f"Excel file deleted: {excel_file}")
            except Exception as e:
                print(f"Warning: Could not delete Excel file: {e}")

        print("\n" + "=" * 60)
        print("PROCESSING COMPLETED")
        print("=" * 60)
        print(f"Total rows processed: {total_rows}")
        print(f"Successful uploads:   {success_count}")
        print(f"Failed uploads:       {failed_count}")
        print(f"Results saved to:     {excel_file}")
        print("=" * 60)

    finally:
        # Always clean up the temp credentials file
        try:
            os.unlink(creds_path)
        except Exception:
            pass


# ── Toolforge entry points ────────────────────────────────────────────────────

def run_as_job():
    """Entry point for Toolforge background jobs."""
    main()


def _continuous_loop():
    print("Starting continuous execution mode. Will check for new images every hour.")
    while True:
        try:
            main()
        except Exception as e:
            logger.error(f"Critical error in main execution: {e}")
            print(f"Critical error in main execution: {e}")
        
        print("\n" + "=" * 60)
        print("Run completed. Sleeping for 1 hour (3600 seconds) before next check...")
        print("=" * 60)
        sleep(3600)

if __name__ == "__main__":
    if "--web" in sys.argv or os.environ.get('TOOLFORGE_WEBSERVICE'):
        import threading
        
        # Start the scraper loop in a background thread so the web server can run
        scraper_thread = threading.Thread(target=_continuous_loop, daemon=True)
        scraper_thread.start()

        app = Flask(__name__)

        @app.route('/')
        def home():
            return "PID Image Processor is running continuously in the background. Check logs for updates."

        @app.route('/health')
        def health():
            return {'status': 'healthy', 'scraper_thread_alive': scraper_thread.is_alive()}

        port = int(os.environ.get("PORT", 8000))
        app.run(host='0.0.0.0', port=port)
    else:
        _continuous_loop()
