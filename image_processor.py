# image_processor.py
# Downloads images, detects the horizontal white separator between the
# photograph and the Bengali text band, crops sections, and runs OCR
# via the Google Drive API.

import math
import os
import re
import tempfile
from io import BytesIO

import cv2
import numpy as np
import requests
from PIL import Image, ImageFile, ImageOps
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as gdrive_build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

import config
import wayback
from config import compute_checksum, retry_on_failure


class ImageProcessor:
    def __init__(self):
        self._drive_service = None

    def initialize_vision_client(self):
        """Initialize Google Drive OCR using OAuth2 user credentials"""
        try:
            if not os.path.exists(config.DRIVE_TOKEN_PATH):
                raise RuntimeError(
                    "drive_token.json not found. Run generate_token.py first.")
            creds = Credentials.from_authorized_user_file(
                config.DRIVE_TOKEN_PATH, config.DRIVE_SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(GoogleAuthRequest())
                with open(config.DRIVE_TOKEN_PATH, 'w') as f:
                    f.write(creds.to_json())
            self._drive_service = gdrive_build(
                'drive', 'v3', credentials=creds)
            return True, "Drive OCR initialized with user OAuth2 credentials"
        except Exception as e:
            return False, f"Failed to initialize Drive OCR: {str(e)}"

    def download_image(self, url):
        """Download image from URL and return image with its extension.
        Falls back to Wayback Machine on 404.
        """
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            response = requests.get(
                url, headers=headers, timeout=30, verify=False)
            response.raise_for_status()

            ImageFile.LOAD_TRUNCATED_IMAGES = True

            raw_bytes = response.content
            img_pil = Image.open(BytesIO(raw_bytes))

            # Store EXIF data before any processing
            exif_data = img_pil.info.get('exif', None)
            img_pil = ImageOps.exif_transpose(img_pil)

            # Convert CMYK to RGB if needed
            if img_pil.mode == 'CMYK':
                img_pil = img_pil.convert('RGB')
            elif img_pil.mode not in ('RGB', 'L', 'RGBA'):
                # Convert any other color mode to RGB
                img_pil = img_pil.convert('RGB')

            # Detect image format
            img_format = img_pil.format.lower() if img_pil.format else 'jpg'
            if img_format == 'jpeg':
                img_format = 'jpg'

            # Convert PIL to numpy array
            img_np = np.array(img_pil)

            # Convert to OpenCV BGR format
            if len(img_np.shape) == 2:
                # Grayscale
                img_cv = cv2.cvtColor(img_np, cv2.COLOR_GRAY2BGR)
            elif len(img_np.shape) == 3:
                if img_np.shape[2] == 4:
                    # RGBA
                    img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGBA2BGR)
                elif img_np.shape[2] == 3:
                    # RGB — convert to BGR for OpenCV
                    img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
                else:
                    img_cv = img_np
            else:
                return None, None, None, None, "Invalid image format"

            return img_cv, img_format, exif_data, raw_bytes, None

        except requests.exceptions.RequestException as e:
            if "404" in str(e) or (hasattr(e, 'response') and e.response is not None and e.response.status_code == 404):
                wayback_url, wayback_error = wayback.get_wayback_url(url)
                if wayback_url:
                    try:
                        headers = {
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                        }
                        response = requests.get(
                            wayback_url, headers=headers, timeout=30, verify=False)
                        response.raise_for_status()

                        ImageFile.LOAD_TRUNCATED_IMAGES = True

                        raw_bytes = response.content
                        img_pil = Image.open(BytesIO(raw_bytes))

                        # Store EXIF data before any processing
                        exif_data = img_pil.info.get('exif', None)
                        img_pil = ImageOps.exif_transpose(img_pil)
                        # Convert CMYK to RGB if needed
                        if img_pil.mode == 'CMYK':
                            img_pil = img_pil.convert('RGB')
                        elif img_pil.mode not in ('RGB', 'L', 'RGBA'):
                            img_pil = img_pil.convert('RGB')

                        # Detect image format
                        img_format = img_pil.format.lower() if img_pil.format else 'jpg'
                        if img_format == 'jpeg':
                            img_format = 'jpg'

                        img_np = np.array(img_pil)

                        if len(img_np.shape) == 2:
                            img_cv = cv2.cvtColor(img_np, cv2.COLOR_GRAY2BGR)
                        elif len(img_np.shape) == 3:
                            if img_np.shape[2] == 4:
                                img_cv = cv2.cvtColor(
                                    img_np, cv2.COLOR_RGBA2BGR)
                            elif img_np.shape[2] == 3:
                                img_cv = cv2.cvtColor(
                                    img_np, cv2.COLOR_RGB2BGR)
                            else:
                                img_cv = img_np
                        else:
                            return None, None, None, None, "Invalid image format"

                        return img_cv, img_format, exif_data, raw_bytes, "Retrieved from Wayback Machine"

                    except Exception as wb_e:
                        return None, None, None, None, f"404 error - Wayback Machine also failed: {str(wb_e)}"
                else:
                    return None, None, None, None, f"404 error - {wayback_error}"
            return None, None, None, None, f"Download failed: {str(e)}"
        except Exception as e:
            return None, None, None, None, f"Image processing error: {str(e)}"

    def find_white_separator(self, image):
        """Find separator by scanning vertical columns and horizontal lines"""
        height, width = image.shape[:2]
        start_row = int(height * 0.4)

        first_columns = list(range(1, 5))
        last_columns = list(range(width-5, width-1))
        all_columns = first_columns + last_columns

        column_heights = {}
        column_colors = {}

        for col in all_columns:
            column_height = -1
            color_samples = []

            for y in range(height-6, start_row-1, -1):
                pixel = image[y, col]

                if len(color_samples) == 0:
                    color_samples.append(pixel)
                    column_height = y
                else:
                    avg_color = np.mean(color_samples, axis=0)
                    color_diff = np.abs(pixel.astype(np.float32) - avg_color)
                    max_allowed_diff = 255 * 0.02
                    is_matching = np.all(color_diff <= max_allowed_diff)

                    if is_matching:
                        color_samples.append(pixel)
                        column_height = y
                    else:
                        if len(color_samples) > 0:
                            column_heights[col] = height - 1 - y
                            column_colors[col] = np.mean(color_samples, axis=0)
                        break

            if column_height != -1 and col not in column_heights:
                column_heights[col] = height - 1 - start_row
                if len(color_samples) > 0:
                    column_colors[col] = np.mean(color_samples, axis=0)

        if not column_heights:
            return -1, False

        min_uniform_top = height
        for col, col_height in column_heights.items():
            uniform_top_row = height - col_height
            min_uniform_top = min(min_uniform_top, uniform_top_row)

        valid_lines = []

        for first_col in first_columns:
            for last_col in last_columns:
                if first_col in column_heights and last_col in column_heights:
                    height_diff = abs(
                        column_heights[first_col] - column_heights[last_col])
                    if height_diff > 4:
                        continue

                    first_uniform_top = height - column_heights[first_col]
                    last_uniform_top = height - column_heights[last_col]
                    scan_row = max(first_uniform_top, last_uniform_top)

                    if scan_row >= start_row and scan_row < height:
                        row_pixels = image[scan_row, first_col:last_col+1]

                        if len(row_pixels) > 0:
                            line_avg_color = np.mean(row_pixels, axis=0)
                            color_diffs = np.abs(row_pixels.astype(
                                np.float32) - line_avg_color)
                            max_allowed_diff = 255 * 0.02
                            matching_pixels = np.all(
                                color_diffs <= max_allowed_diff, axis=1)
                            matching_percentage = np.sum(
                                matching_pixels) / len(row_pixels)

                            if matching_percentage >= 0.98:
                                valid_lines.append(scan_row)

        if valid_lines:
            cutoff_row = min(valid_lines)
        elif column_heights:
            cutoff_row = min_uniform_top
        else:
            return -1, False

        offset = round(2 + 3/math.log(3100/670) * math.log(height/670))
        if offset < 2:
            offset = 2

        separator_row = cutoff_row - offset

        height_38_percent = int(height * 0.38)
        height_42_percent = int(height * 0.42)

        needs_fallback = (
            separator_row == -1) or (height_38_percent <= cutoff_row <= height_42_percent)

        if needs_fallback:
            fallback_start_row = int(height * 0.75)
            fallback_separator = self.find_separator_fallback(
                image, fallback_start_row)
            if fallback_separator != -1:
                separator_row = fallback_separator
                return separator_row, True, offset

        return separator_row, False, offset

    def find_separator_fallback(self, image, start_row):
        """Fallback method to find separator"""
        height, width = image.shape[:2]

        fallback_consecutive_similar_lines = 0
        fallback_separator_row = -1

        fallback_required_lines = round(
            (4 / math.log(3100 / 670)) * math.log(height / 670) + 5)
        if fallback_required_lines <= 1:
            fallback_required_lines = 2

        white_color = np.array([255, 255, 255], dtype=np.uint8)
        fbf9fa_color = np.array([250, 249, 251], dtype=np.uint8)
        color_tolerance = 255 * 0.02

        for y_fallback in range(start_row, height):
            row_fallback = image[y_fallback]

            white_diff = np.abs(row_fallback.astype(
                np.float32) - white_color.astype(np.float32))
            white_matching = np.all(white_diff <= color_tolerance, axis=1)

            fbf9fa_diff = np.abs(row_fallback.astype(
                np.float32) - fbf9fa_color.astype(np.float32))
            fbf9fa_matching = np.all(fbf9fa_diff <= color_tolerance, axis=1)

            matching_pixels = white_matching | fbf9fa_matching
            matching_percentage = np.sum(matching_pixels) / width

            if matching_percentage >= 0.98:
                fallback_consecutive_similar_lines += 1
                if fallback_consecutive_similar_lines >= fallback_required_lines:
                    if fallback_consecutive_similar_lines >= 10:
                        fallback_separator_row_y_offset = fallback_consecutive_similar_lines + 5
                    elif fallback_consecutive_similar_lines in (1, 2, 3):
                        fallback_separator_row_y_offset = 2
                    else:
                        fallback_separator_row_y_offset = fallback_consecutive_similar_lines + 5

                    fallback_separator_row = y_fallback - fallback_separator_row_y_offset
                    break
            else:
                fallback_consecutive_similar_lines = 0

        return fallback_separator_row

    def crop_side_whitespace(self, image):
        """Crop white or fbf9fa colored sections from left and right sides"""
        height, width = image.shape[:2]

        white_color = np.array([255, 255, 255], dtype=np.uint8)
        fbf9fa_color = np.array([250, 249, 251], dtype=np.uint8)
        color_tolerance = 255 * 0.02

        left_crop = 0
        for x in range(width):
            column = image[:, x]

            white_diff = np.abs(column.astype(np.float32) -
                                white_color.astype(np.float32))
            white_matching = np.all(white_diff <= color_tolerance, axis=1)

            fbf9fa_diff = np.abs(column.astype(
                np.float32) - fbf9fa_color.astype(np.float32))
            fbf9fa_matching = np.all(fbf9fa_diff <= color_tolerance, axis=1)

            matching_pixels = white_matching | fbf9fa_matching
            matching_percentage = np.sum(matching_pixels) / height

            if matching_percentage >= 0.98:
                left_crop = x + 1
            else:
                break

        right_crop = width
        for x in range(width-1, -1, -1):
            column = image[:, x]

            white_diff = np.abs(column.astype(np.float32) -
                                white_color.astype(np.float32))
            white_matching = np.all(white_diff <= color_tolerance, axis=1)

            fbf9fa_diff = np.abs(column.astype(
                np.float32) - fbf9fa_color.astype(np.float32))
            fbf9fa_matching = np.all(fbf9fa_diff <= color_tolerance, axis=1)

            matching_pixels = white_matching | fbf9fa_matching
            matching_percentage = np.sum(matching_pixels) / height

            if matching_percentage >= 0.98:
                right_crop = x
            else:
                break

        expansion = int(round((4 / math.log(3100 / 670))
                        * math.log(height / 670) + 5))
        left_expanded = max(0, left_crop - expansion)
        right_expanded = min(width, right_crop + expansion)

        if left_expanded < right_expanded:
            return image[:, left_expanded:right_expanded]
        else:
            return image

    def crop_image_sections(self, image, separator_row, apply_side_crop=False):
        """Split image into photo section and text section"""
        if apply_side_crop:
            image = self.crop_side_whitespace(image)

        if separator_row == -1:
            return None, image

        photo_section = image[:separator_row, :]
        text_section = image[separator_row:, :]

        if photo_section is None or photo_section.size == 0 or photo_section.shape[0] < 1:
            return None, image

        return photo_section, text_section

    def clean_ocr_text(self, text):
        """Clean OCR text with find and replace operations"""
        if not text or text.startswith("OCR Error"):
            return text

        text = text.replace('|', '।')
        text = text.replace('। পিআইডি', '।')
        text = text.replace('।পিআইডি', '।')
        text = text.replace(' - পিআইডি', '')
        text = text.replace(' -পিআইডি', '')
        text = text.replace('- পিআইডি', '')
        text = text.replace('-পিআইডি', '')
        text = text.replace(' \ufeff________________ ', '')
        text = text.replace('________________', '')
        text = text.replace('  ', ' ')
        text = text.replace('  ', ' ')

        return text

    @retry_on_failure(max_attempts=10, delay=2)
    def perform_ocr(self, image):
        """Perform OCR using Google Drive API (free, replaces Vision API)"""
        file_id = None
        temp_path = None
        try:
            # Save image section to a temp file
            temp_fd, temp_path = tempfile.mkstemp(suffix='.png')
            os.close(temp_fd)
            cv2.imwrite(temp_path, image)

            # Upload image to Drive as a Google Doc — Drive OCRs it automatically
            file_metadata = {
                'name': 'ocr_temp.png',
                'mimeType': 'application/vnd.google-apps.document',
            }
            media = MediaIoBaseUpload(
                open(temp_path, 'rb'),
                mimetype='image/png',
                resumable=False
            )
            uploaded = self._drive_service.files().create(
                body=file_metadata,
                media_body=media,
                ocrLanguage='bn',   # Bengali hint — improves accuracy
                fields='id'
            ).execute()
            file_id = uploaded.get('id')

            # Export the OCR'd Google Doc as plain text
            request = self._drive_service.files().export_media(
                fileId=file_id,
                mimeType='text/plain'
            )
            text_buffer = BytesIO()
            downloader = MediaIoBaseDownload(text_buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

            raw_text = text_buffer.getvalue().decode('utf-8', errors='replace')

            # Strip the Drive separator line that appears in exported docs
            raw_text = raw_text.replace('________________\n\n', '')
            raw_text = re.sub(r'\s+', ' ', raw_text).strip()
            cleaned_text = self.clean_ocr_text(raw_text)
            return cleaned_text

        except Exception as e:
            return f"OCR Error: {str(e)}"

        finally:
            # Always delete the local temp file
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception as del_e:
                    print(
                        f"Warning: could not delete local temp file {temp_path}: {del_e}")
            # Always delete the temp file from Drive
            if file_id:
                try:
                    self._drive_service.files().delete(fileId=file_id).execute()
                except Exception as del_e:
                    print(
                        f"Warning: could not delete temp Drive file {file_id}: {del_e}")

    def process_image(self, row_index, image_url, wikimedia_checksums=None):
        """Process a single image - download, split, OCR"""
        result = {
            'image': None,
            'format': 'jpg',
            'exif': None,
            'ocr_text': '',
            'status': '',
            'checksum': '',
            'is_duplicate': False
        }

        try:
            if not image_url or image_url == 'nan':
                result['status'] = 'No URL provided'
                return result

            print(f"Row {row_index}: Downloading image...")
            image, img_format, exif_data, raw_bytes, error = self.download_image(
                image_url)
            if error:
                if "404" in error:
                    result['status'] = error
                elif "Wayback Machine" in error:
                    result['status'] = "Retrieved from archive"
                else:
                    result['status'] = error
                return result

            result['format'] = img_format
            result['exif'] = exif_data

            # Checksum duplicate check — runs BEFORE OCR/AI (saves quota)
            if raw_bytes:
                checksum = compute_checksum(raw_bytes)
                result['checksum'] = checksum
                if wikimedia_checksums and checksum in wikimedia_checksums:
                    print(
                        f"Row {row_index}: Duplicate image detected via checksum — skipping OCR/AI")
                    result['status'] = 'Duplicate (checksum match)'
                    result['is_duplicate'] = True
                    result['image'] = image
                    return result
            else:
                result['checksum'] = ''

            print(f"Row {row_index}: Finding separator...")
            separator_row, fallback_used, separator_offset = self.find_white_separator(
                image)

            photo_section, text_section = self.crop_image_sections(
                image, separator_row, apply_side_crop=fallback_used)

            if photo_section is None or separator_row == -1:
                result['status'] = 'No separator found - using full image'
                result['image'] = image

                print(
                    f"Row {row_index}: Performing OCR on bottom 40% of image...")
                height_fallback = image.shape[0]
                ocr_section = image[int(height_fallback * 0.60):, :]
                ocr_text = self.perform_ocr(ocr_section)
                result['ocr_text'] = ocr_text

                if ocr_text.startswith("OCR Error"):
                    result['status'] = 'OCR failed'
                elif not ocr_text:
                    result['status'] = 'No text detected'
                else:
                    result['status'] = 'Success - full image'
            else:
                result['image'] = photo_section

                print(
                    f"Row {row_index}: Performing OCR on text section (trimmed by {1 * separator_offset}px)...")
                trim_top = min(2 * separator_offset, text_section.shape[0] - 1)
                ocr_section = text_section[trim_top:, :]
                ocr_text = self.perform_ocr(ocr_section)
                result['ocr_text'] = ocr_text

                if ocr_text.startswith("OCR Error"):
                    result['status'] = 'OCR failed'
                elif not ocr_text:
                    result['status'] = 'No text detected'
                else:
                    result['status'] = 'Success'

            print(f"Row {row_index}: Image processing completed")

        except Exception as e:
            result['status'] = f"Error: {str(e)}"
            print(f"Row {row_index}: Error - {str(e)}")

        return result
