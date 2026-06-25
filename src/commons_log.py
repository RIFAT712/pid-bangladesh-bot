# commons_log.py
# Writes per-run upload results to the bot's log page on Wikimedia Commons
# as a JSON file for easier tracking of failures and successes.

import json
from datetime import datetime
import pandas as pd
import pywikibot
from config import logger

def dataframe_to_dict_list(df):
    """Convert pandas DataFrame to a list of dictionaries for JSON logging"""
    results = []
    for idx in range(len(df)):
        row_dict = {}
        for col_name in df.columns:
            val = df.at[idx, col_name]
            row_dict[col_name] = str(val) if pd.notna(val) else ""
        results.append(row_dict)
    return results

def log_to_commons(site, df=None, success_count=0, failed_count=0, total_rows=0):
    """Append processing results to the bot's monthly JSON log page on Wikimedia Commons."""
    try:
        current_date = datetime.now()
        month_name = current_date.strftime("%B")   # e.g. "June"
        year = current_date.strftime("%Y")
        
        page_title = f"User:PID-Bangladesh-UploadBot/Log/{month_name}_{year}.json"

        page = pywikibot.Page(site, page_title)
        timestamp = current_date.strftime("%Y-%m-%d %H:%M:%S UTC")

        run_data = {
            "timestamp": timestamp,
            "total_processed": total_rows,
            "success_count": success_count,
            "failed_count": failed_count,
            "items": []
        }

        if df is not None and not df.empty:
            run_data["items"] = dataframe_to_dict_list(df)

        existing_data = []
        if page.exists():
            text = page.text
            try:
                existing_data = json.loads(text)
                if not isinstance(existing_data, list):
                    existing_data = []
            except json.JSONDecodeError:
                existing_data = []

        existing_data.append(run_data)

        # .json pages automatically render as JSON, so no syntaxhighlight is needed or allowed
        json_str = json.dumps(existing_data, indent=4, ensure_ascii=False)
        page.text = json_str
        page.save(summary="Bot log update (JSON format)", bot=True)
        
        logger.info(f"Successfully logged to {page_title}")
        return True

    except Exception as e:
        logger.error(f"Error logging to Commons: {str(e)}")
        return False
