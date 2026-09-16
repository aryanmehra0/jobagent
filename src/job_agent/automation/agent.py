"""Autonomous browser application agent.

Drives a persistent Playwright session through an application portal: DOM-only
perception (no vision model), `networkidle` synchronization, form auto-fill,
tailored PDF upload, and a human-in-the-loop pause on CAPTCHA or MFA.

Safeguards, all of which exist because this code submits real applications under
the candidate's name:

* A step ceiling (`max_steps`, default 25) bounds any loop.
* Three consecutive steps with no successful interaction aborts the attempt.
* A submit button is clicked at most once per attempt, so a page that does not
  show a confirmation banner cannot be submitted repeatedly.
* Dry-run mode never opens a browser.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

from rich.console import Console

from job_agent.config.schema import ApplicationOutcome, CandidateProfile, JobPosting
from job_agent.config.settings import settings
from job_agent.automation.browser_session import BrowserSessionManager
from job_agent.automation.form_filler import FormFiller
from job_agent.automation.hitl import ChallengeHandler
from job_agent.automation.navigator import DOMNavigator
from job_agent.sourcing.delta_store import DeltaStore

console = Console()

MAX_CONSECUTIVE_ERRORS = 3


class AutoApplyAgent:
    """Autonomous job application agent."""

    def __init__(
        self,
        session_manager: Optional[BrowserSessionManager] = None,
        max_steps: Optional[int] = None,
        delta_store: Optional[DeltaStore] = None,
    ):
        self._session_manager = session_manager
        self.max_steps = max_steps or settings.max_application_steps
        self.challenge_handler = ChallengeHandler()
        self.delta_store = delta_store or DeltaStore()

    @property
    def session_mgr(self) -> BrowserSessionManager:
        """The browser session, created lazily.

        Deferring creation means a dry run never touches Playwright, so the agent
        can be exercised on a machine with no browser installed.
        """
        if self._session_manager is None:
            self._session_manager = BrowserSessionManager()
        return self._session_manager

    def close(self) -> None:
        """Shut down the browser session if one was opened."""
        if self._session_manager is not None:
            self._session_manager.close()

    def apply_to_job(
        self,
        profile: CandidateProfile,
        job: JobPosting,
        pdf_resume_path: Path,
        dry_run: bool = False,
        fit_score: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Run the auto-apply sequence for a single posting.

        Returns a validated `ApplicationOutcome` as a dict.
        """
        console.print(f"\n[bold cyan]=== Auto-apply: {job.title} @ {job.company} ===[/bold cyan]")
        console.print(f"Portal : {job.job_url}")
        console.print(f"Resume : {pdf_resume_path}")

        if dry_run:
            console.print("[yellow]Dry run: no browser launched, nothing submitted.[/yellow]")
            return ApplicationOutcome(
                job_id=job.id,
                title=job.title,
                company=job.company,
                job_url=job.job_url,
                status="dry_run",
                steps_taken=0,
                fit_score=fit_score,
                error=None,
                pdf_path=str(pdf_resume_path),
            ).model_dump()

        if not Path(pdf_resume_path).exists():
            return self._outcome(
                job, "failed", 0, fit_score, f"Tailored resume not found: {pdf_resume_path}", pdf_resume_path
            )

        page = self.session_mgr.new_stealth_page()
        navigator = DOMNavigator(page)
        filler = FormFiller(profile, job)

        steps = 0
        consecutive_errors = 0
        submitted = False
        submit_attempted = False
        error_message: Optional[str] = None

        try:
            if not navigator.navigate_to_url(job.job_url):
                raise RuntimeError(f"Could not reach the application portal: {job.job_url}")

            while steps < self.max_steps:
                steps += 1
                console.print(f"[dim]Step {steps}/{self.max_steps}: inspecting page...[/dim]")

                self.challenge_handler.handle_if_challenged(page)

                if navigator.detect_submission_success():
                    console.print("[bold green]Submission confirmed on page.[/bold green]")
                    submitted = True
                    break

                fields = navigator.scan_form_fields()
                console.print(f"  - {len(fields)} form element(s) detected.")

                filled_any = False
                for field in fields:
                    try:
                        if filler.fill_field(field, pdf_resume_path=Path(pdf_resume_path)):
                            filled_any = True
                    except Exception as exc:
                        console.print(f"[dim]Fill error: {exc}[/dim]")

                consecutive_errors = 0 if filled_any else consecutive_errors + 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    error_message = f"Aborted after {MAX_CONSECUTIVE_ERRORS} steps with no successful interaction."
                    console.print(f"[yellow]{error_message}[/yellow]")
                    break

                # Submit once. Clicking a submit button on every iteration risks
                # filing duplicate applications when the portal shows no banner.
                if not submit_attempted:
                    submit_button = navigator.find_action_button("submit")
                    if submit_button:
                        console.print("  - Submitting application...")
                        submit_attempted = True
                        submit_button.click()
                        navigator.wait_for_idle(timeout=8000)
                        if navigator.detect_submission_success():
                            submitted = True
                            break
                        continue

                next_button = navigator.find_action_button("next")
                if next_button:
                    console.print("  - Multi-page application; advancing...")
                    next_button.click()
                    navigator.wait_for_idle(timeout=5000)
                    continue

                time.sleep(2)
                if navigator.detect_submission_success():
                    submitted = True
                if not submitted and error_message is None:
                    error_message = (
                        "Form filled but no submit/next control was found; the portal likely "
                        "needs manual completion."
                    )
                break

            if steps >= self.max_steps and not submitted and error_message is None:
                error_message = f"Reached the {self.max_steps}-step ceiling without a confirmation."

        except Exception as exc:
            error_message = str(exc)
            console.print(f"[red]Auto-apply error: {error_message}[/red]")
        finally:
            if filler.skipped_fields:
                console.print(
                    f"[yellow]{len(filler.skipped_fields)} field(s) left blank for lack of profile data:[/yellow] "
                    + "; ".join(filler.skipped_fields[:5])
                )
            try:
                page.close()
            except Exception:
                pass

        return self._outcome(
            job,
            "applied" if submitted else "failed",
            steps,
            fit_score,
            error_message,
            pdf_resume_path,
        )

    def _outcome(
        self,
        job: JobPosting,
        status: str,
        steps: int,
        fit_score: Optional[float],
        error: Optional[str],
        pdf_path: Path | str,
    ) -> Dict[str, Any]:
        """Record the terminal status in the delta store and return it as a dict."""
        self.delta_store.update_status(job.id, status)
        return ApplicationOutcome(
            job_id=job.id,
            title=job.title,
            company=job.company,
            job_url=job.job_url,
            status=status,
            steps_taken=steps,
            fit_score=fit_score,
            error=error,
            pdf_path=str(pdf_path),
        ).model_dump()
