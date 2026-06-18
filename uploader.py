# uploader.py
# Pywikibot-based upload pipeline:
#   - Pywikibot initialisation and login
#   - Image upload to Wikimedia Commons
#   - Module:PIDDateData update
#   - Pre-upload category / module infrastructure check

import os
import tempfile
import traceback
from datetime import datetime
from time import sleep

import cv2
import pywikibot
from PIL import Image
from pywikibot import FilePage
from pywikibot.exceptions import UploadError

from config import logger
import config


def initialize_pywikibot():
    """Initialise Pywikibot and log in to Wikimedia Commons.
    Returns (site, FilePage) on success, None on failure.
    """
    try:
        if not os.path.exists(config.USER_CONFIG_PATH):
            logger.error(f"Config file not found: {config.USER_CONFIG_PATH}")
            return None
        if not os.path.exists(config.PASSWORD_FILE_PATH):
            logger.error(f"Password file not found: {config.PASSWORD_FILE_PATH}")
            return None

        site = pywikibot.Site('commons', 'commons')
        site.login()

        logger.info("Successfully logged in to Wikimedia Commons")
        return site, FilePage

    except Exception as e:
        logger.error(f"Failed to initialize Pywikibot: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return None


def upload_to_commons(site, FilePage, image, target_filename, img_format, exif_data, description, max_attempts=10):
    """Upload an OpenCV image to Wikimedia Commons.
    Returns (success: bool, error_message: str).
    """
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f'.{img_format}')
    try:
        # Convert OpenCV BGR → PIL RGB, preserving EXIF
        img_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        img_pil = Image.fromarray(img_rgb)

        save_kwargs = {}
        if exif_data:
            save_kwargs['exif'] = exif_data

        if img_format == 'png':
            img_pil.save(temp_file.name, 'PNG', optimize=True, **save_kwargs)
        elif img_format == 'jpg':
            img_pil.save(temp_file.name, 'JPEG', quality=95, **save_kwargs)
        else:
            img_pil.save(temp_file.name, **save_kwargs)

        for attempt in range(max_attempts):
            try:
                file_page = FilePage(site, f'File:{target_filename}')

                if file_page.exists():
                    logger.info(f"File already exists: {target_filename}")
                    return False, 'File already exists'

                logger.info(f"Uploading {target_filename} (attempt {attempt + 1}/{max_attempts})")

                success = file_page.upload(
                    source=temp_file.name,
                    comment="Pypan 0.1.1a0",
                    text=description,
                    ignore_warnings=True,
                )

                if success:
                    logger.info(f"Successfully uploaded {target_filename}")
                    return True, ''
                else:
                    logger.warning(f"Upload failed — server response for {target_filename}")

            except UploadError as e:
                logger.warning(f"Upload warning for {target_filename}: {str(e)}")

            except Exception as e:
                logger.error(f"Error uploading {target_filename}: {str(e)}")

            if attempt < max_attempts - 1:
                logger.info("Waiting 10 seconds before retry...")
                sleep(10)

        return False, 'Max attempts reached'

    finally:
        try:
            os.unlink(temp_file.name)
        except Exception:
            pass


def update_pid_date_data(site, data_entry):
    """Append a new entry to Module:PIDDateData/<current year> on Wikimedia Commons."""
    try:
        current_year = datetime.now().year
        page_title = f"Module:PIDDateData/{current_year}"
        page = pywikibot.Page(site, page_title)

        if not page.exists():
            logger.error(f"Page does not exist: {page_title}")
            return False

        page_text = page.text
        last_brace_index = page_text.rfind('}')

        if last_brace_index == -1:
            logger.error("Could not find closing brace in page")
            return False

        new_entry = f"    {data_entry}\n"
        updated_text = page_text[:last_brace_index] + new_entry + page_text[last_brace_index:]

        page.text = updated_text
        page.save(summary="added another image")

        logger.info(f"Successfully updated {page_title}")
        return True

    except Exception as e:
        logger.error(f"Error updating PIDDateData: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False


def ensure_pid_infrastructure(site):
    """Ensure all required categories and Module:PIDDateData/<year> exist for today."""
    now = datetime.now()
    year = now.year
    month = now.month

    y1 = str(year)[:3]                      # e.g. "202"
    y2 = str(year)[3:]                      # e.g. "6"
    month_padded = str(month).zfill(2)      # e.g. "03"
    month_name = now.strftime("%B")         # e.g. "March"
    date_str = now.strftime("%Y-%m-%d")     # e.g. "2026-03-09"

    pages_to_ensure = [
        (
            f"Category:Bangladesh photographs taken on {date_str}",
            "{{World photos}}"
        ),
        (
            f"Category:{month_name} {year} Bangladesh photographs",
            "{{Countryphotomonth}}"
        ),
        (
            f"Category:{month_name} {year} in Bangladesh",
            "{{{{Monthbyyearbangladesh|{y1}|{y2}|{month}}}}}".format(
                y1=y1, y2=y2, month=month)
        ),
        (
            f"Category:{year} in Bangladesh",
            "{{{{Bangladeshyear|{y1}|{y2}}}}}\n{{{{Countries of Asia|prefix=:Category:{year} in }}}}}}\n{{{{Wikidata Infobox}}}}".format(
                y1=y1, y2=y2, year=year)
        ),
        (
            f"Category:{month_name} {year} in Asia",
            "{{{{Asiamonthyear|{year}|{month_name}}}}}\n{{{{Wikidata Infobox}}}}".format(
                year=year, month_name=month_name)
        ),
        (
            f"Category:{month_name} {year} by country",
            "{{{{Monthbycountryyear|{y1}|{y2}|{month_padded}}}}}\n{{{{Wikidata Infobox}}}}".format(
                y1=y1, y2=y2, month_padded=month_padded)
        ),
        (
            f"Category:{year} photographs of Bangladesh",
            "{{{{Bangladesh-photoyear|{y1}|{y2}}}}}".format(y1=y1, y2=y2)
        ),
        (
            f"Category:PID-BD images from {month_name} {year}",
            "{{PID-BD image category navigation}}"
        ),
        (
            f"Category:PID-BD images from {year}",
            f"[[Category:Press Information Department images|{year}]]\n"
            f"[[Category:{year} in Bangladesh]]"
        ),
    ]

    for title, content in pages_to_ensure:
        try:
            page = pywikibot.Page(site, title)
            if not page.exists():
                page.text = content
                page.save(summary="Creating category for PID uploads")
                logger.info(f"Created: {title}")
            else:
                logger.info(f"Already exists: {title}")
        except Exception as e:
            logger.error(f"Error creating {title}: {e}")

    # Ensure Module:PIDDateData/<year> exists
    module_title = f"Module:PIDDateData/{year}"
    try:
        module_page = pywikibot.Page(site, module_title)
        if not module_page.exists():
            module_page.text = "return {\n\n\n}"
            module_page.save(summary="Creating PIDDateData module for new year")
            logger.info(f"Created: {module_title}")
        else:
            logger.info(f"Already exists: {module_title}")
    except Exception as e:
        logger.error(f"Error creating {module_title}: {e}")
