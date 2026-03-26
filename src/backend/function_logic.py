"""
DownloadParisSalesFn - B2B Paris Sales Report Downloader

Downloads weekly sales data (CSV) from the B2B Paris portal using
Browserbase for remote browser automation (Selenium).

Credentials:
- Browserbase: Platform-level secrets from AWS Secrets Manager (chask/browserbase)
- B2B Paris: Widget parameters (per-organization, stored in AWS Secrets Manager)
"""

import io
import json
import logging
import time
import zipfile
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

import requests
from browserbase import Browserbase
from selenium import webdriver
from selenium.webdriver.remote.remote_connection import RemoteConnection

from chask_foundation.backend.models import OrchestrationEvent
from chask_foundation.configs.utils import get_secret
from api.files_requests import files_api_manager
from api.widget_resolver import WidgetParamResolver

logger = logging.getLogger()
logger.setLevel(logging.INFO)

B2B_PORTAL = "https://www.cenconlineb2b.com/ParisCL/BBRe-commerce/main"


class BrowserbaseRemoteConnection(RemoteConnection):
    """Custom RemoteConnection that adds Browserbase signing key header."""

    def __init__(self, remote_server_addr: str, signing_key: str):
        super().__init__(remote_server_addr)
        self._signing_key = signing_key

    def get_remote_connection_headers(self, parsed_url, keep_alive=False):
        headers = super().get_remote_connection_headers(parsed_url, keep_alive)
        headers.update({'x-bb-signing-key': self._signing_key})
        return headers


class FunctionBackend:
    """
    Backend for downloading weekly sales data from B2B Paris portal.

    Uses Browserbase remote browser to navigate the Vaadin-based portal,
    generate a sales report, and download the resulting CSV file.
    """

    def __init__(self, orchestration_event: OrchestrationEvent):
        self.orchestration_event = orchestration_event
        self.verbose = False
        self.driver: Optional[webdriver.Remote] = None
        self._session_id: Optional[str] = None
        self._browserbase_api_key: Optional[str] = None
        logger.info(f"Initialized FunctionBackend for org: {orchestration_event.organization.organization_id}")

    def process_request(self) -> str:
        tool_args = self._extract_tool_args()
        self.verbose = tool_args.get("verbose", False)

        bb_api_key, bb_project_id = self._get_browserbase_credentials()
        self._browserbase_api_key = bb_api_key

        resolver = WidgetParamResolver(self.orchestration_event)
        widget_data = self.orchestration_event.extra_params.get("widget_data", {})
        b2b_user, b2b_pass = resolver.resolve_positional(widget_data, count=2)

        if not b2b_user or not b2b_pass:
            raise ValueError(
                "Missing required B2B Paris credentials. "
                "Please configure b2b_paris_user and b2b_paris_pass in widget settings."
            )

        try:
            session = self._create_browserbase_session(bb_api_key, bb_project_id)
            driver = self._connect_to_session(session)
            self.driver = driver

            try:
                self._login(driver, b2b_user, b2b_pass)
                self._wait_vaadin(driver)
                self._navigate_to_informe_ventas(driver)
                self._generate_report(driver)
                self._wait_for_report_data(driver)
                file_bytes = self._download_report(driver)

                if not file_bytes:
                    raise ValueError("Failed to download sales report file")

                file_data = self._upload_to_chask(file_bytes)

                return (
                    f"B2B Paris sales report downloaded successfully!\n\n"
                    f"Download: {file_data.get('file_url', 'URL not available')}"
                )
            finally:
                if driver:
                    driver.quit()

        except Exception as e:
            logger.error(f"B2B Paris scraper failed: {e}", exc_info=True)
            raise

    # ── Browserbase setup ──────────────────────────────────────────────

    def _create_browserbase_session(self, api_key: str, project_id: str):
        self._log("Creating Browserbase session...")
        bb = Browserbase(api_key=api_key)
        session = bb.sessions.create(project_id=project_id)
        self._session_id = session.id
        self._log(f"Session created: {session.id}")
        self._log(f"Live view: https://www.browserbase.com/sessions/{session.id}")
        return session

    def _connect_to_session(self, session) -> webdriver.Remote:
        self._log("Connecting to Browserbase with Selenium...")
        custom_conn = BrowserbaseRemoteConnection(
            session.selenium_remote_url,
            session.signing_key
        )
        options = webdriver.ChromeOptions()
        driver = webdriver.Remote(custom_conn, options=options)
        self._log("Connected to remote browser")
        return driver

    # ── Login ──────────────────────────────────────────────────────────

    def _login(self, driver: webdriver.Remote, username: str, password: str) -> None:
        """Navigate to B2B portal, handle Cencosud SSO login."""
        self._log("Navigating to B2B Paris portal...")
        driver.get(B2B_PORTAL)
        time.sleep(2)

        self._log("Filling SSO credentials...")
        driver.execute_script("""
            const u = document.querySelector('#username');
            const p = document.querySelector('#password');
            if (u) { u.value = arguments[0]; u.dispatchEvent(new Event('input', {bubbles: true})); }
            if (p) { p.value = arguments[1]; p.dispatchEvent(new Event('input', {bubbles: true})); }
        """, username, password)
        time.sleep(1)

        driver.execute_script("document.querySelector('#kc-login').click();")

        # Wait for redirect back to portal
        for _ in range(20):
            time.sleep(3)
            title = driver.title.lower()
            if "paris" in title and "oops" not in title:
                self._log("Login successful")
                return

        raise TimeoutError("Login failed: portal did not load after SSO")

    # ── Vaadin navigation ──────────────────────────────────────────────

    def _wait_vaadin(self, driver: webdriver.Remote, timeout: int = 60) -> None:
        """Wait for Vaadin framework to fully load (~40s)."""
        self._log("Waiting for Vaadin to load...")
        for i in range(timeout // 3):
            time.sleep(3)
            count = driver.execute_script(
                "return document.querySelectorAll('[class*=\"v-\"]').length"
            )
            if count > 50:
                self._log(f"Vaadin ready ({count} elements)")
                return
        raise TimeoutError("Vaadin did not load within timeout")

    def _navigate_to_informe_ventas(self, driver: webdriver.Remote) -> None:
        """Click Comercial > Informe de Ventas in the Vaadin menu."""
        self._log("Navigating: Comercial > Informe de Ventas...")
        time.sleep(2)

        clicked = driver.execute_script("""
            const captions = document.querySelectorAll('.v-menubar-menuitem-caption');
            for (const cap of captions) {
                if (cap.textContent.trim() === 'Comercial') {
                    cap.closest('.v-menubar-menuitem').click();
                    return true;
                }
            }
            return false;
        """)
        if not clicked:
            raise ValueError("Could not find 'Comercial' menu item")

        time.sleep(2)

        clicked = driver.execute_script("""
            const popup = document.querySelector('.v-menubar-popup');
            if (!popup) return false;
            const items = popup.querySelectorAll('.v-menubar-menuitem-caption');
            for (const item of items) {
                if (item.textContent.trim() === 'Informe de Ventas') {
                    item.closest('.v-menubar-menuitem').click();
                    return true;
                }
            }
            return false;
        """)
        if not clicked:
            raise ValueError("Could not find 'Informe de Ventas' submenu item")

        self._log("Waiting for report form to load...")
        time.sleep(5)

    # ── Report generation ──────────────────────────────────────────────

    def _generate_report(self, driver: webdriver.Remote) -> None:
        """Click 'Generar Informe' button."""
        self._log("Clicking 'Generar Informe'...")
        clicked = driver.execute_script("""
            const buttons = document.querySelectorAll('.v-button');
            for (const btn of buttons) {
                if (btn.textContent.trim().includes('Generar Informe')) {
                    btn.click();
                    return true;
                }
            }
            return false;
        """)
        if not clicked:
            raise ValueError("Could not find 'Generar Informe' button")

    def _wait_for_report_data(self, driver: webdriver.Remote, timeout: int = 60) -> None:
        """Poll until grid rows appear."""
        self._log("Waiting for report data to load...")
        for i in range(timeout // 3):
            time.sleep(3)
            rows = driver.execute_script(
                "return document.querySelectorAll('.v-grid-body .v-grid-row').length"
            )
            if rows > 0:
                self._log(f"Report loaded ({rows} rows visible)")
                return
        raise TimeoutError("Report data did not load within timeout")

    # ── Download flow ──────────────────────────────────────────────────

    def _download_report(self, driver: webdriver.Remote) -> Optional[bytes]:
        """Execute the full download flow: toolbar > popup > modal > link."""

        # Step 1: Click download toolbar button
        self._log("Opening download popup...")
        driver.execute_script("""
            const buttons = document.querySelectorAll('.v-button.toolbar-button');
            for (const btn of buttons) {
                const img = btn.querySelector('img.v-icon');
                if (img && img.src.includes('DownloadPrimary')) { btn.click(); return; }
            }
        """)
        time.sleep(2)

        # Step 2: Click "Producto-Local" option
        self._log("Selecting 'Producto-Local' download option...")
        driver.execute_script("""
            const popup = document.querySelector('.v-popupview-popup .popupContent');
            if (!popup) return;
            const buttons = popup.querySelectorAll('.v-button');
            for (const btn of buttons) {
                if (btn.textContent.trim().includes('Producto-Local')) { btn.click(); return; }
            }
        """)
        time.sleep(3)

        # Step 3: Modal "Descarga de Datos Fuente de Ventas" — set dates and click Descargar
        self._log("Setting date range in download modal...")
        today = datetime.now()
        desde = (today - timedelta(days=7)).strftime("%d-%m-%Y")
        hasta = today.strftime("%d-%m-%Y")

        # Select first radio (custom date range) and set dates
        driver.execute_script("""
            const win = document.querySelector('.v-window');
            if (!win) return;
            const radios = win.querySelectorAll('input[type="radio"]');
            if (radios[0]) radios[0].click();
        """)
        time.sleep(1)

        driver.execute_script("""
            const win = document.querySelector('.v-window');
            const dateInputs = win.querySelectorAll('.v-textfield.v-datefield-textfield');
            for (let i = 0; i < 2; i++) {
                const input = dateInputs[i];
                input.removeAttribute('disabled');
                input.value = i === 0 ? arguments[0] : arguments[1];
                input.dispatchEvent(new Event('change', { bubbles: true }));
                input.dispatchEvent(new Event('blur', { bubbles: true }));
            }
        """, desde, hasta)
        time.sleep(1)

        self._log(f"Date range: {desde} to {hasta}")

        # Click "Descargar"
        self._log("Clicking 'Descargar'...")
        driver.execute_script("""
            const win = document.querySelector('.v-window');
            if (!win) return;
            const buttons = win.querySelectorAll('.v-button');
            for (const btn of buttons) {
                if (btn.textContent.trim().includes('Descargar')) { btn.click(); return; }
            }
        """)

        # Step 4: Wait for "Descargar Archivo" dialog with download link
        self._log("Waiting for file generation...")
        link_href = self._wait_for_download_link(driver)

        if not link_href:
            self._log("No download link found, falling back to Browserbase Downloads API")
            file_bytes = self._wait_for_browserbase_download()
            if file_bytes:
                self._validate_download_content(file_bytes)
            return file_bytes

        # Step 5: Download the file via requests with authenticated cookies
        self._log("Downloading file from link...")
        cookies = {c['name']: c['value'] for c in driver.get_cookies()}
        response = requests.get(link_href, cookies=cookies, timeout=120)
        if response.status_code != 200:
            raise ValueError(f"File download failed with status {response.status_code}")

        file_bytes = response.content
        self._log(f"Downloaded {len(file_bytes)} bytes")

        # Validate content — if HTML received, fall back to browser click + Browserbase API
        try:
            self._validate_download_content(file_bytes)
        except ValueError as e:
            self._log(f"Direct download invalid: {e}")
            self._log("Falling back to browser click + Browserbase Downloads API...")
            driver.execute_script("""
                const win = document.querySelector('.v-window');
                if (win) { const a = win.querySelector('a[href]'); if (a) a.click(); }
            """)
            file_bytes_fallback = self._wait_for_browserbase_download()
            if file_bytes_fallback:
                self._validate_download_content(file_bytes_fallback)
                return self._extract_from_zip_if_needed(file_bytes_fallback)
            raise

        return self._extract_from_zip_if_needed(file_bytes)

    def _wait_for_download_link(self, driver: webdriver.Remote, timeout: int = 180) -> Optional[str]:
        """Poll for the 'Descargar Archivo' dialog containing a download link."""
        for i in range(timeout // 2):
            time.sleep(2)

            link_info = driver.execute_script("""
                const windows = document.querySelectorAll('.v-window');
                for (const win of windows) {
                    const header = win.querySelector('.v-window-header');
                    if (header && header.textContent.trim().includes('Descargar Archivo')) {
                        const links = win.querySelectorAll('a');
                        if (links.length > 0) {
                            return { found: true, href: links[0].href };
                        }
                    }
                }
                return { found: false };
            """)

            if link_info and link_info.get("found"):
                self._log("Download link found!")
                return link_info["href"]

            if i > 0 and i % 15 == 0:
                elapsed = i * 2
                self._log(f"Still waiting for file generation... ({elapsed}s elapsed)")

        self._log("Timeout waiting for download link")
        return None

    def _wait_for_browserbase_download(self, timeout: int = 180) -> Optional[bytes]:
        """Fallback: poll Browserbase Downloads API for the file."""
        if not self._session_id or not self._browserbase_api_key:
            return None

        self._log("Polling Browserbase Downloads API...")
        downloads_url = f"https://www.browserbase.com/v1/sessions/{self._session_id}/downloads"
        headers = {"x-bb-api-key": self._browserbase_api_key}

        start = time.time()
        while time.time() - start < timeout:
            try:
                resp = requests.get(downloads_url, headers=headers, timeout=30)
                if resp.status_code == 200 and len(resp.content) > 100:
                    self._log(f"Downloads API returned {len(resp.content)} bytes")
                    return self._extract_from_zip_if_needed(resp.content)
            except requests.RequestException as e:
                self._log(f"Downloads API request failed: {e}")

            time.sleep(5)

        self._log("Browserbase download timeout reached")
        return None

    # ── Content validation ─────────────────────────────────────────────

    def _validate_download_content(self, content: bytes) -> None:
        """Validate that downloaded content is actual CSV/report data, not an HTML login page."""
        if not content:
            raise ValueError(
                "El archivo descargado está vacío. "
                "Intente ejecutar la función nuevamente."
            )

        # Detect HTML content (login page redirect when cookies are missing)
        header = content[:500]
        if b'<!DOCTYPE html' in header or b'<html' in header:
            title = ""
            try:
                decoded = header.decode('utf-8', errors='ignore')
                import re
                title_match = re.search(r'<title[^>]*>(.*?)</title>', decoded, re.DOTALL | re.IGNORECASE)
                if title_match:
                    title = title_match.group(1).strip()
            except Exception:
                pass

            raise ValueError(
                f"El servidor devolvió una página HTML en lugar del archivo de ventas. "
                f"{'Título: ' + title + '. ' if title else ''}"
                f"Esto ocurre cuando la sesión no está autenticada o ha expirado. "
                f"Tamaño recibido: {len(content):,} bytes. "
                f"Intente ejecutar la función nuevamente."
            )

        # Warn on suspiciously small files (but don't fail — small reports are possible)
        MIN_EXPECTED_SIZE = 100_000  # 100KB
        if len(content) < MIN_EXPECTED_SIZE:
            self._log(
                f"WARNING: Downloaded file is only {len(content):,} bytes "
                f"(expected > {MIN_EXPECTED_SIZE:,})"
            )

    # ── File handling ──────────────────────────────────────────────────

    def _extract_from_zip_if_needed(self, file_bytes: bytes) -> bytes:
        """If the file is a ZIP, extract the first CSV/file from it."""
        try:
            zip_buffer = io.BytesIO(file_bytes)
            with zipfile.ZipFile(zip_buffer, 'r') as zf:
                file_list = zf.namelist()
                self._log(f"ZIP contains: {file_list}")

                csv_files = [f for f in file_list if f.lower().endswith('.csv')]
                target = csv_files[0] if csv_files else file_list[0] if file_list else None

                if target:
                    self._log(f"Extracting: {target}")
                    return zf.read(target)

                self._log("ZIP archive is empty")
                return file_bytes
        except zipfile.BadZipFile:
            self._log("Not a ZIP file, using raw content")
            self._validate_download_content(file_bytes)
            return file_bytes

    def _upload_to_chask(self, file_bytes: bytes) -> Dict[str, Any]:
        """Upload file to Chask storage."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"paris_ventas_{timestamp}.csv"
        self._log(f"Uploading {filename} ({len(file_bytes)} bytes)...")

        file_obj = io.BytesIO(file_bytes)
        file_obj.name = filename

        result = files_api_manager.call(
            "upload_file",
            file=file_obj,
            orchestration_session_uuids=[
                self.orchestration_event.orchestration_session_uuid
            ] if self.orchestration_event.orchestration_session_uuid else None,
            internal_orchestration_session_uuid=self.orchestration_event.internal_orchestration_session_uuid,
            shared=False,
            access_token=self.orchestration_event.access_token,
            organization_id=self.orchestration_event.organization.organization_id,
        )

        if hasattr(result, "status_code") and result.status_code != 200:
            raise ValueError(f"File upload failed: {result.status_code}")

        file_data = result.json() if hasattr(result, "json") else result
        self._log(f"File uploaded: {file_data.get('file_url', 'URL not available')}")
        return file_data

    # ── Helpers ────────────────────────────────────────────────────────

    def _log(self, message: str) -> None:
        if self.verbose:
            logger.info(message)

    def _get_browserbase_credentials(self) -> Tuple[str, str]:
        try:
            secret_value = get_secret('chask/browserbase', MODE='PRODUCTION')
            secrets = json.loads(secret_value)
            api_key = secrets.get('BROWSERBASE_API_KEY')
            project_id = secrets.get('BROWSERBASE_PROJECT_ID')
            if not api_key or not project_id:
                raise ValueError(
                    "Browserbase credentials incomplete in AWS Secrets Manager. "
                    "Ensure BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID are set."
                )
            self._log("Browserbase credentials retrieved")
            return api_key, project_id
        except Exception as e:
            logger.error(f"Failed to retrieve Browserbase credentials: {e}")
            raise ValueError(f"Failed to retrieve Browserbase credentials: {e}")

    def _extract_tool_args(self) -> Dict[str, Any]:
        extra_params = self.orchestration_event.extra_params or {}
        tool_calls = extra_params.get("tool_calls", [])
        if not tool_calls:
            return {}
        return tool_calls[0].get("args", {})
