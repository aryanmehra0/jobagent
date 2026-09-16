"""Persistent Chromium Browser Session Manager.

Maintains persistent browser profiles across sessions (retaining cookies,
session tokens, and localStorage), applies stealth evasions, and strictly
disables telemetry to protect candidate data privacy.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from playwright.sync_api import sync_playwright, BrowserContext, Page, Playwright
from rich.console import Console

from job_agent.config.settings import settings

console = Console()


class BrowserSessionManager:
    """Manages persistent anti-detect Chromium sessions with Playwright."""

    def __init__(
        self,
        user_data_dir: Optional[Path] = None,
        headless: Optional[bool] = None,
        proxy_url: Optional[str] = None,
    ):
        self.user_data_dir = user_data_dir or settings.browser_profile_dir
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless if headless is not None else settings.playwright_headless
        self.proxy_url = proxy_url or settings.residential_proxy_url

        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None

    def start(self) -> BrowserContext:
        """Launch persistent Chromium context with anti-detect flags and stealth."""
        # Enforce telemetry protection
        os.environ["ANONYMIZED_TELEMETRY"] = "false"
        os.environ["POSTHOG_DISABLED"] = "1"

        if self._context is not None:
            return self._context

        self._playwright = sync_playwright().start()

        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--start-maximized",
        ]

        proxy_dict = None
        if self.proxy_url:
            proxy_dict = {"server": self.proxy_url}

        console.print(f"[dim]Launching persistent Chromium context at: {self.user_data_dir}[/dim]")
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            headless=self.headless,
            args=args,
            viewport=None,  # Maximize viewport
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            proxy=proxy_dict,
            locale="en-US",
            timezone_id="America/New_York",
            bypass_csp=True,
        )

        return self._context

    def _apply_stealth(self, page: Page) -> bool:
        """Apply stealth evasions (WebGL, navigator.webdriver, permissions) to a page.

        playwright-stealth 2.x exposes a `Stealth` class; 1.x exposed a
        `stealth_sync` function. Importing the bare name `stealth` from 2.x yields
        the *module*, so calling it raises TypeError and the evasions silently never
        apply. Both APIs are handled explicitly here, and a failure is reported
        rather than swallowed, since running unmasked changes how sites treat you.
        """
        try:
            from playwright_stealth import Stealth

            Stealth().apply_stealth_sync(page)
            return True
        except ImportError:
            pass
        except Exception as exc:
            console.print(f"[yellow]Stealth evasions could not be applied: {exc}[/yellow]")
            return False

        try:
            from playwright_stealth import stealth_sync

            stealth_sync(page)
            return True
        except Exception as exc:
            console.print(
                f"[yellow]Stealth evasions unavailable ({exc.__class__.__name__}); "
                "continuing without them. Install playwright-stealth>=2.0 for best results.[/yellow]"
            )
            return False

    def new_stealth_page(self) -> Page:
        """Create a new page with stealth evasions applied and sane default timeouts."""
        context = self.start()
        page = context.new_page()
        self._apply_stealth(page)
        page.set_default_timeout(20000)
        page.set_default_navigation_timeout(30000)
        return page

    def __enter__(self) -> BrowserSessionManager:
        """Support `with BrowserSessionManager() as session:` so the browser always closes."""
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Gracefully terminate context and Playwright instance."""
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None

        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

