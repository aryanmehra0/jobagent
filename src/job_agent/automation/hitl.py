"""Human-in-the-Loop (HITL) & CAPTCHA Handling Module.

Detects bot challenges (Cloudflare Turnstile, reCAPTCHA, hCaptcha, MFA prompts)
and orchestrates automated resolution via CapSolver or pauses execution with a
terminal prompt for manual human intervention before resuming the workflow.
"""

from __future__ import annotations

import time
from typing import Optional
from playwright.sync_api import Page
from rich.console import Console
from rich.panel import Panel

from job_agent.config.settings import settings

console = Console()

CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='turnstile']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='arkoselabs']",
    ".g-recaptcha",
    ".cf-turnstile",
    ".h-captcha",
    "#captcha",
    "[name='cf-turnstile-response']",
]

MFA_PATTERNS = [
    "verification code",
    "enter code sent",
    "two-factor",
    "security code",
    "verify your identity",
]


class ChallengeHandler:
    """Detects and resolves bot mitigation challenges and authentication walls."""

    def __init__(self, capsolver_api_key: Optional[str] = None):
        self.capsolver_key = capsolver_api_key or settings.capsolver_api_key

    def detect_challenge(self, page: Page) -> Optional[str]:
        """Check whether the active page is obstructed by a CAPTCHA or MFA challenge."""
        # 1. Check DOM selectors
        for sel in CAPTCHA_SELECTORS:
            try:
                elem = page.locator(sel).first
                if elem.is_visible(timeout=500):
                    return f"CAPTCHA Challenge ({sel})"
            except Exception:
                pass

        # 2. Check text content for MFA
        try:
            body_text = page.inner_text("body", timeout=500).lower()
            for pattern in MFA_PATTERNS:
                if pattern in body_text:
                    return f"MFA/Verification Challenge ('{pattern}')"
        except Exception:
            pass

        return None

    def _solve_with_capsolver(self, page: Page, challenge_type: str) -> bool:
        """Automated CAPTCHA resolution via CapSolver.

        Not implemented: dispatching a solve task needs the site key and challenge
        type for each provider, and returning a fake success would cause the agent
        to submit an application behind an unsolved challenge. Until it is built,
        every challenge routes to the human-in-the-loop pause, which is correct if
        slower. Returns False always.
        """
        if self.capsolver_key:
            console.print(
                f"[yellow]CAPSOLVER_API_KEY is set, but automated solving of "
                f"{challenge_type} is not implemented; pausing for you instead.[/yellow]"
            )
        return False

    def trigger_hitl_pause(self, page: Page, challenge_description: str) -> bool:
        """Pause automated execution and prompt user to solve the challenge in the browser."""
        console.print(
            Panel.fit(
                f"[bold red]⚠ HUMAN-IN-THE-LOOP (HITL) REQUIRED[/bold red]\n\n"
                f"[yellow]{challenge_description}[/yellow] detected on:\n[cyan]{page.url}[/cyan]\n\n"
                f"1. Switch to the open Chromium browser window.\n"
                f"2. Manually solve the CAPTCHA or complete the verification challenge.\n"
                f"3. Return to this terminal and press [bold green]ENTER[/bold green] to continue.",
                border_style="red",
            )
        )

        try:
            input("\nPress ENTER once you have cleared the challenge in the browser...")
            # Wait for network activity to settle after human clearance
            page.wait_for_load_state("networkidle", timeout=10000)
            console.print("[bold green]✓ Resuming autonomous execution...[/bold green]")
            return True
        except Exception as e:
            console.print(f"[yellow]HITL continuation warning: {e}[/yellow]")
            return True

    def handle_if_challenged(self, page: Page) -> bool:
        """Inspect page and resolve challenge if encountered. Returns True if page is clear."""
        challenge = self.detect_challenge(page)
        if not challenge:
            return True

        console.print(f"[bold yellow]Detected: {challenge}[/bold yellow]")

        # 1. Try CapSolver if configured
        if self._solve_with_capsolver(page, challenge):
            console.print("[bold green]✓ Automated challenge resolution succeeded![/bold green]")
            return True

        # 2. Fall back to Human-in-the-Loop pause
        return self.trigger_hitl_pause(page, challenge)

