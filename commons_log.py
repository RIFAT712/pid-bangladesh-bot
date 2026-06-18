# commons_log.py
# Writes per-run upload results to the bot's log page on Wikimedia Commons
# and converts the processing DataFrame to a wikitable for human review.

from datetime import datetime

import pandas as pd
import pywikibot

from config import logger


def excel_to_wikitable(df):
    """Convert pandas DataFrame to wikitable format"""
    wikitable = '{| class="wikitable sortable"\n'

    # Headers
    wikitable += (
        '! Unique ID !! Date !! Image URL !! !! OCR Text !! Status !! '
        'Translation !! Trans Status !! Title !! Title Status !! '
        'Data Entry !! PIDDateData Status !! Description !! Upload Status\n'
    )

    for idx in range(len(df)):
        wikitable += '|-\n'
        for col in range(min(14, df.shape[1])):
            cell_value = str(df.iat[idx, col]) if pd.notna(df.iat[idx, col]) else ""

            # Column 0 (Unique ID) — add [[File:title|100px]] thumbnail
            if col == 0:
                title_value = str(df.iat[idx, 8]) if pd.notna(df.iat[idx, 8]) else ""
                if title_value:
                    cell_value = f"[[File:{title_value}|100px]] {cell_value}"
                cell_value = cell_value.replace('|', '{{!}}').replace('\n', '<br>')

            # Column 12 (Description) — wrap in <nowiki> and strip leading apostrophe
            elif col == 12:
                if cell_value.startswith("'"):
                    cell_value = cell_value[1:]
                cell_value = f"<nowiki>{cell_value}</nowiki>"

            else:
                cell_value = cell_value.replace('|', '{{!}}').replace('\n', '<br>')

            wikitable += f'| {cell_value}\n'

    wikitable += '|}'
    return wikitable


def log_to_commons(site, df=None, success_count=0, failed_count=0, total_rows=0):
    """Append processing results to the bot's monthly log page on Wikimedia Commons."""
    try:
        current_date = datetime.now()
        month_name = current_date.strftime("%B")   # e.g. "June"
        year = current_date.strftime("%Y")
        page_title = f"User:PID-Bangladesh-UploadBot/Log/{month_name} {year}"

        page = pywikibot.Page(site, page_title)
        timestamp = current_date.strftime("%Y-%m-%d %H:%M:%S UTC")

        if df is None:
            log_entry = f"\n\n{timestamp} \nBot run completed. No new images found.\n"
        else:
            wikitable = excel_to_wikitable(df)
            log_entry = f"\n\n== {timestamp} ==\n"
            log_entry += f"Processed {total_rows} images. "
            log_entry += f"Successful uploads: {success_count}, Failed: {failed_count}\n\n"
            log_entry += wikitable + "\n"

        if page.exists():
            page.text = page.text + log_entry
        else:
            page.text = f"Upload Log for {month_name} {year} =\n" + log_entry

        page.save(summary="Bot log update")
        logger.info(f"Successfully logged to {page_title}")
        return True

    except Exception as e:
        logger.error(f"Error logging to Commons: {str(e)}")
        return False
