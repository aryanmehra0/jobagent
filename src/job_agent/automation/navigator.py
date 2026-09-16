"""DOM-Based Perception and Form Navigation Engine.

Defaults strictly to DOM-only perception (use_vision=False) for speed and cost efficiency,
enforcing wait_until='networkidle' wait strategies across dynamic JavaScript applications.
"""

from __future__ import annotations

import time
from typing import List, Dict, Any, Optional
from playwright.sync_api import Page, Locator
from rich.console import Console

from job_agent.config.settings import settings

console = Console()


class DOMNavigator:
    """Navigates application DOM and extracts accessible interactive form elements."""

    def __init__(self, page: Page):
        self.page = page
        self.wait_strategy = settings.playwright_wait_strategy  # "networkidle"

    def navigate_to_url(self, url: str) -> bool:
        """Navigate to an application portal, synchronizing on the configured load state.

        Falls back to `domcontentloaded` when the strict wait times out: single-page
        application portals often keep a websocket or poller open, so `networkidle`
        never fires even though the form is fully rendered and interactive.
        """
        console.print(f"[cyan]Navigating to:[/cyan] {url}")
        try:
            self.page.goto(url, wait_until=self.wait_strategy, timeout=35000)
            return True
        except Exception as e:
            console.print(
                f"[yellow]{self.wait_strategy} wait did not settle ({e.__class__.__name__}); "
                "falling back to domcontentloaded.[/yellow]"
            )
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                return True
            except Exception as final_err:
                console.print(f"[red]Failed to reach page: {final_err}[/red]")
                return False

    def wait_for_idle(self, timeout: int = 5000) -> None:
        """Wait for pending network activity and DOM updates to settle.

        A timeout is not an error here: the caller re-scans the DOM either way, and
        a portal that never goes idle would otherwise abort a valid application.
        """
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass

    def scan_form_fields(self) -> List[Dict[str, Any]]:
        """Extract interactive form fields from accessibility tree and DOM."""
        fields: List[Dict[str, Any]] = []

        # Find all inputs, selects, textareas
        elements = self.page.locator("input, select, textarea").all()

        for el in elements:
            try:
                if not el.is_visible():
                    # Keep file inputs even if hidden (often styled invisibly)
                    input_type = el.get_attribute("type") or ""
                    if input_type.lower() != "file":
                        continue

                tag_name = el.evaluate("el => el.tagName.toLowerCase()")
                el_type = (el.get_attribute("type") or "text").lower()
                el_id = el.get_attribute("id") or ""
                el_name = el.get_attribute("name") or ""
                el_placeholder = el.get_attribute("placeholder") or ""
                aria_label = el.get_attribute("aria-label") or ""

                # Find associated label text
                label_text = ""
                if el_id:
                    label_el = self.page.locator(f"label[for='{el_id}']").first
                    if label_el.count() > 0:
                        label_text = label_el.inner_text().strip()

                if not label_text:
                    # Check parent label or preceding text
                    label_text = el.evaluate(
                        """el => {
                            let parent = el.closest('label');
                            if (parent) return parent.innerText.trim();
                            let prev = el.previousElementSibling;
                            if (prev && (prev.tagName === 'LABEL' || prev.tagName === 'SPAN')) return prev.innerText.trim();
                            return '';
                        }"""
                    )

                field_descriptor = {
                    "locator": el,
                    "tag": tag_name,
                    "type": el_type,
                    "id": el_id,
                    "name": el_name,
                    "label": label_text or aria_label or el_placeholder or el_name or el_id,
                    "placeholder": el_placeholder,
                    "aria_label": aria_label,
                }
                fields.append(field_descriptor)
            except Exception:
                continue

        return fields

    def find_file_upload_input(self) -> Optional[Locator]:
        """Locate file upload element for submitting tailored PDF resume."""
        file_inputs = self.page.locator("input[type='file']").all()
        if file_inputs:
            return file_inputs[0]
        return None

    def find_action_button(self, action_type: str = "submit") -> Optional[Locator]:
        """Locate navigation buttons ('Submit', 'Next', 'Continue', 'Apply')."""
        if action_type == "submit":
            patterns = [
                "button:has-text('Submit')",
                "button:has-text('Submit Application')",
                "button:has-text('Apply')",
                "input[type='submit']",
                "button[type='submit']",
            ]
        elif action_type == "next":
            patterns = [
                "button:has-text('Next')",
                "button:has-text('Continue')",
                "button:has-text('Save & Continue')",
                "button:has-text('Proceed')",
            ]
        else:
            patterns = [f"button:has-text('{action_type}')"]

        for pattern in patterns:
            try:
                btn = self.page.locator(pattern).first
                if btn.count() > 0 and btn.is_visible():
                    return btn
            except Exception:
                pass
        return None

    def detect_submission_success(self) -> bool:
        """Check for confirmation indicators on the page."""
        success_signals = [
            "application submitted",
            "thank you for applying",
            "your application has been received",
            "application received",
            "successfully submitted",
            "thanks for your interest",
        ]
        try:
            body_text = self.page.inner_text("body", timeout=1500).lower()
            return any(sig in body_text for sig in success_signals)
        except Exception:
            return False

