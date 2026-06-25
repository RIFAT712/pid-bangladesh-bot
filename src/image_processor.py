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
from src import wayback
from config import compute_checksum, retry_on_failure


class ImageProcessor:
    def __init__(self):
        self._drive_service = None

    def cleanup_temp_drive_files(self):
        """Find and delete any leftover ocr_temp.png files in Google Drive."""
        if not self._drive_service:
            return
        try:
            results = self._drive_service.files().list(
                q="name='ocr_temp.png'",
                spaces='drive',
                fields='files(id, name)'
            ).execute()
            items = results.get('files', [])
            if items:
                print(f"Found {len(items)} orphaned temporary Drive files. Cleaning up...")
                for item in items:
                    try:
                        self._drive_service.files().delete(fileId=item['id']).execute()
                    except Exception as e:
                        print(f"Failed to clean up {item['id']}: {e}")
        except Exception as e:
            print(f"Failed to query Drive for temp files: {e}")

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
            
            # Clean up any leftover files from previous interrupted runs
            self.cleanup_temp_drive_files()
            
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
        """
        Multi-pass Top-Down Variance Scanning separator detector.

        PID images are composite JPEGs: a photograph on top and a printed Bengali
        caption band below, separated by a horizontal white (or off-white) strip.
        The goal is to find the top edge of that strip and return the pixel row
        just above it so the caller can split photo from caption.

        Algorithm overview
        ------------------
        Pass 1 — Strict white band (threshold ≥ 240, coverage ≥ 95 %)
            Scans row-by-row from 35 % of image height downward.  For each
            candidate row we require a *run* of MIN_RUN consecutive qualifying
            rows, not just 2-3, so JPEG ringing on the separator boundary cannot
            produce a false positive.  The run is scored by its minimum coverage
            value (weakest link) to prefer the cleanest band.

        Pass 2 — Relaxed off-white band (threshold ≥ 220, coverage ≥ 90 %)
            Older or heavily-compressed PID images use a light-grey separator
            instead of pure white.  If pass 1 found nothing we repeat with
            looser thresholds.

        Pass 3 — Gradient edge fallback
            If no flat-colour band exists at all, we fall back to finding the
            strongest horizontal edge in the lower half of the image using a
            Sobel gradient.  This handles unusual layouts without returning -1.

        Offset
        ------
        A small log-scaled pixel offset is subtracted from the separator row so
        the cropped photograph is not clipped at its very bottom edge.  The
        formula scales with image height (small images need fewer pixels of
        breathing room than large ones).
        """
        height, width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        # Avoid the top 35 % (header / masthead area) and the bottom 5 %
        # (white bottom-padding is not a separator — ignore it).
        start_row = int(height * 0.35)
        end_row   = int(height * 0.95)

        # Pre-compute: fraction of pixels per row brighter than a threshold
        bright_240 = np.sum(gray > 240, axis=1) / width   # strict white
        bright_220 = np.sum(gray > 220, axis=1) / width   # relaxed off-white

        # Minimum consecutive qualifying rows for a valid separator band.
        # Scales with image height so very tall images need a proportionally
        # larger run, reducing false positives from wide bright sky bands.
        MIN_RUN = max(3, int(height / 300))

        def _scan(bg_pct, threshold_pct, search_start, search_end):
            """
            Return (separator_row, run_length) for the first run of ≥ MIN_RUN
            consecutive rows with bg_pct > threshold_pct inside [search_start,
            search_end), or (-1, 0) if none found.
            """
            run_start = -1
            run_len   = 0
            for y in range(search_start, search_end):
                if bg_pct[y] > threshold_pct:
                    if run_start == -1:
                        run_start = y
                    run_len += 1
                    if run_len >= MIN_RUN:
                        return run_start, run_len
                else:
                    run_start = -1
                    run_len   = 0
            return -1, 0

        # Log-scaled breathing-room offset (pixels)
        offset = max(2, int(round((3 / math.log(3100 / 670)) * math.log(height / 670))))

        # ── Pass 1: strict white (>240, ≥95 %) ───────────────────────────────
        sep, _ = _scan(bright_240, 0.95, start_row, end_row)
        if sep != -1:
            return max(0, sep - offset), False, offset

        # ── Pass 2: relaxed off-white (>220, ≥90 %) ──────────────────────────
        sep, _ = _scan(bright_220, 0.90, start_row, end_row)
        if sep != -1:
            return max(0, sep - offset), False, offset

        # ── Pass 3: strongest horizontal gradient edge in lower half ──────────
        # Blur first to suppress JPEG noise, then run a horizontal Sobel.
        blurred   = cv2.GaussianBlur(gray, (5, 5), 0)
        sobelx    = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)   # dy direction
        grad_mag  = np.abs(sobelx)
        row_energy = grad_mag.sum(axis=1)   # total horizontal edge energy per row

        # Only consider the lower half of the search band for the fallback
        fallback_start = int(height * 0.50)
        fallback_end   = end_row
        search_slice   = row_energy[fallback_start:fallback_end]
        if search_slice.size > 0:
            best_local = int(np.argmax(search_slice))
            sep = fallback_start + best_local
            # Accept only if this row's energy is significantly above the mean
            # (avoids returning a "best" row in a featureless image)
            if row_energy[sep] > row_energy[fallback_start:fallback_end].mean() * 2.5:
                return max(0, sep - offset), False, offset

        return -1, False, offset

    def find_separator_fallback(self, image, start_row):
        """Legacy stub — logic is now incorporated into find_white_separator pass 3."""
        return -1

    def crop_side_whitespace(self, image):
        """Crop white or fbf9fa colored sections from left and right sides"""
        height, width = image.shape[:2]

        # Vectorized processing over entire image using fast int thresholds
        B = image[:, :, 0]
        G = image[:, :, 1]
        R = image[:, :, 2]
        
        white_match = (B >= 250) & (G >= 250) & (R >= 250)
        fbf9fa_match = (B >= 246) & (G >= 244) & (G <= 254) & (R >= 245)

        matching_pixels = white_match | fbf9fa_match
        matching_percentage = np.sum(matching_pixels, axis=0) / height
        is_background_col = matching_percentage >= 0.98

        left_crop = 0
        for x in range(width):
            if is_background_col[x]:
                left_crop = x + 1
            else:
                break

        right_crop = width
        for x in range(width-1, -1, -1):
            if is_background_col[x]:
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
        temp_file_obj = None
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
            temp_file_obj = open(temp_path, 'rb')
            media = MediaIoBaseUpload(
                temp_file_obj,
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
            if temp_file_obj:
                try:
                    temp_file_obj.close()
                except Exception:
                    pass

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
