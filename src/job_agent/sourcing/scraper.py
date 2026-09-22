"""Omnichannel job sourcing engine.

Coordinates scraping across LinkedIn, Indeed, Glassdoor, and ZipRecruiter via
`python-jobspy`, folds in direct ATS endpoint feeds, enforces residential proxy
rotation, and filters out previously seen postings via the delta store.

Every constraint in `searches.yaml` is applied here, and the summary reports how
many postings each filter removed, so a sweep that returns nothing tells you which
constraint was responsible instead of just returning an empty list.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.table import Table

from job_agent.automation.routing import resolve_apply_url
from job_agent.config.normalize import clean_text, strip_html
from job_agent.contacts.extract import job_post_contacts
from job_agent.config.schema import CandidateProfile, JobPosting, SearchParameters
from job_agent.config.settings import settings
from job_agent.runtime import RunCancelled, check_cancelled, exclusive_run, invalidate_after
from job_agent.sourcing.ats_direct import ATSDirectIngestion
from job_agent.sourcing.delta_store import DeltaStore
from job_agent.sourcing.proxy_manager import ProxyManager
from job_agent.sourcing.public_feeds import PublicFeedIngestion

console = Console()

# JobSpy caps out well before this; requesting more just slows the sweep down.
MAX_RESULTS_PER_REQUEST = 100

# The name JobSpy gives each board's logger (`jobspy.util.create_logger`).
JOBSPY_LOGGER_NAMES = {
    "linkedin": "LinkedIn", "indeed": "Indeed", "glassdoor": "Glassdoor",
    "zip_recruiter": "ZipRecruiter", "google": "Google", "bayt": "Bayt",
    "naukri": "Naukri", "bdjobs": "BDJobs",
}

# Sources that list every open role regardless of age.
DIRECT_SOURCES = ("greenhouse", "lever", "ashby", "remotive", "arbeitnow", "jobicy")

# Log messages that mean a board refused the request outright, as opposed to
# simply having no matching jobs.
_BLOCK_MARKERS = ("403", "forbidden", "cf-waf", "blocked", "captcha", "429", "too many requests")


@contextmanager
def tolerate_unknown_countries():
    """Stop one foreign listing from discarding a whole JobSpy result page.

    JobSpy parses each LinkedIn listing's location with `Country.from_string`,
    which raises on any country outside its fixed list. A single listing in, say,
    Sri Lanka therefore aborted the query and lost every other result. For the
    duration of a scrape an unknown country resolves to `WORLDWIDE` instead; the
    listing keeps its city and region, and JobSpy's behaviour is restored after.
    """
    try:
        from jobspy.model import Country
    except Exception:
        yield
        return

    original = Country.__dict__["from_string"]
    strict = Country.from_string

    def lenient(cls, country_str):
        try:
            return strict(country_str)
        except ValueError:
            return cls.WORLDWIDE

    Country.from_string = classmethod(lenient)
    try:
        yield
    finally:
        Country.from_string = original


class _BlockDetector(logging.Handler):
    """Collects JobSpy log records that report a board refusing a request.

    JobSpy logs a 403 from Glassdoor or ZipRecruiter and returns an empty result
    rather than raising, so an exception handler never sees the block.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if any(marker in message.lower() for marker in _BLOCK_MARKERS):
            self.messages.append(message)


class OmnichannelScraper:
    """Omnichannel job sourcing coordinator."""

    def __init__(
        self,
        search_params: Optional[SearchParameters] = None,
        proxy_manager: Optional[ProxyManager] = None,
        delta_store: Optional[DeltaStore] = None,
        ats_feeder: Optional[ATSDirectIngestion] = None,
        public_feeder: Optional[PublicFeedIngestion] = None,
        candidate_profile: Optional[CandidateProfile] = None,
    ):
        self.params = search_params or SearchParameters()
        self.proxy_mgr = proxy_manager or ProxyManager(proxy_source=self.params.proxy_url)
        self.delta_store = delta_store or DeltaStore()
        self.ats_feeder = ats_feeder or ATSDirectIngestion()
        self.public_feeder = public_feeder or PublicFeedIngestion()
        self.candidate_profile = candidate_profile
        # Counts why postings were discarded, reported at the end of the sweep.
        self.filter_stats: Counter = Counter()
        # Boards that refused a request this sweep. Retrying them for every
        # domain and location only adds latency and further blocks.
        self.blocked_boards: Dict[str, str] = {}
        self.board_errors: Dict[str, str] = {}

    def is_board_blocked(self, board: str) -> bool:
        """Whether a board has refused this sweep's requests."""
        return board in self.blocked_boards

    @staticmethod
    def configuration_warnings(params: SearchParameters) -> List[str]:
        """Settings that will make a sweep slow or empty, detectable before it starts."""
        from job_agent.config.schema import CITY_COUNTRIES

        warnings: List[str] = []
        countries = {CITY_COUNTRIES[city] for city in params.locations if city in CITY_COUNTRIES}
        boards_using_country = {"indeed", "glassdoor"} & set(params.job_boards)
        if len(countries) == 1:
            country = next(iter(countries))
            if boards_using_country and params.country_indeed != country:
                boards = " and ".join(sorted(board.title() for board in boards_using_country))
                warnings.append(
                    f"country_indeed is '{params.country_indeed}' but your locations are in "
                    f"{country}. {boards} will search the wrong national site and find nothing "
                    f"for those cities. Set country_indeed to '{country}'."
                )
            if country != "usa" and "zip_recruiter" in params.job_boards:
                warnings.append(
                    "ZipRecruiter only lists jobs in the US and Canada, so it returns nothing "
                    f"for {country} locations. Consider removing it from job_boards."
                )

        if params.min_salary and countries == {"india"} and params.salary_currency == "USD":
            warnings.append(
                f"min_salary {params.min_salary:,} is being treated as USD. If you meant "
                "rupees, set salary_currency to INR."
            )

        cities = [location for location in params.locations if location.casefold() != "remote"]
        if params.selected_work_modes == ["remote"] and cities:
            warnings.append(
                "Only remote roles are selected. Searching specific cities "
                f"({', '.join(cities)}) as well mostly re-finds the same remote listings."
            )
        return warnings

    # --- Normalization --------------------------------------------------------

    def _normalize_jobspy_df(self, df: Any, default_source: str = "jobspy") -> List[JobPosting]:
        """Convert a JobSpy DataFrame into validated `JobPosting` models.

        Rows that fail validation (no URL, blank title) are counted and skipped
        rather than aborting the sweep, since one malformed row on one board should
        not cost the results from every other board.
        """
        import pandas as pd

        postings: List[JobPosting] = []
        if df is None or getattr(df, "empty", True):
            return postings

        for _, row in df.iterrows():
            def value(key: str) -> Any:
                if key not in row:
                    return None
                cell = row[key]
                return None if pd.isna(cell) else cell

            title = clean_text(value("title"))
            company = clean_text(value("company"))
            job_url = clean_text(value("job_url")) or clean_text(value("job_url_direct"))

            if not title or not company or not job_url:
                self.filter_stats["incomplete_row"] += 1
                continue

            description = value("description")
            # JobSpy returns markdown or HTML depending on the board.
            description_text = strip_html(description) if description else ""

            # The board listing usually needs a login; JobSpy often also knows the
            # employer's own application page and website, which were discarded.
            direct_url = clean_text(value("job_url_direct")) or None
            apply_url = resolve_apply_url(job_url, direct_url)
            company_website = clean_text(value("company_url_direct")) or None
            contacts = job_post_contacts(description_text, value("emails"))
            work_hint = clean_text(value("work_from_home_type")).casefold()
            work_mode = (
                "hybrid" if "hybrid" in work_hint
                else "remote" if any(token in work_hint for token in ("remote", "home", "wfh"))
                else "onsite" if any(token in work_hint for token in ("office", "onsite", "on-site"))
                else None
            )

            try:
                postings.append(
                    JobPosting(
                        id=JobPosting.create_id(job_url, company, title),
                        title=title,
                        company=company,
                        location=clean_text(value("location")) or "Remote",
                        job_url=job_url,
                        description=description_text,
                        date_posted=clean_text(value("date_posted")) or None,
                        is_remote=bool(value("is_remote")) if value("is_remote") is not None else False,
                        work_mode=work_mode,
                        salary_min=_as_float(value("min_amount")),
                        salary_max=_as_float(value("max_amount")),
                        salary_currency=clean_text(value("currency")) or "USD",
                        job_type=clean_text(value("job_type")) or None,
                        source=clean_text(value("site")) or default_source,
                        apply_url=apply_url,
                        company_website=company_website,
                        contacts=contacts,
                    )
                )
            except Exception as exc:  # pydantic ValidationError and friends
                self.filter_stats["invalid_row"] += 1
                console.print(f"[dim]Skipped malformed listing from {default_source}: {exc}[/dim]")

        return postings

    # --- Board scraping -------------------------------------------------------

    def scrape_job_boards(self) -> List[JobPosting]:
        """Scrape every configured board for every domain and location.

        Boards are queried one at a time so each keeps its own sticky proxy session
        and a block on one board cannot cost the results of the others.
        """
        if not self.params.job_boards:
            return []
        import jobspy

        results: List[JobPosting] = []
        boards = self.params.job_boards
        locations = self.params.locations or ["Remote"]
        per_request = min(self.params.max_results_per_board, MAX_RESULTS_PER_REQUEST)

        for domain in self.params.target_domains:
            queried_locationless: set[str] = set()
            for location in locations:
                console.print(
                    f"[bold cyan]Sourcing:[/bold cyan] '{domain}' in '{location}' "
                    f"across [yellow]{', '.join(boards)}[/yellow]"
                )
                for board in boards:
                    # Stop takes effect before the next request; the one in flight
                    # finishes, since JobSpy offers no way to abort it cleanly.
                    check_cancelled()
                    if self.is_board_blocked(board):
                        continue
                    # Bayt currently ignores location. Querying it once for every
                    # city repeats the same network request and increases blocking.
                    if board == "bayt" and board in queried_locationless:
                        continue
                    if board == "bayt":
                        queried_locationless.add(board)
                    results.extend(
                        self._scrape_single_board(
                            jobspy, board=board, domain=domain, location=location, results_wanted=per_request
                        )
                    )

        return results

    def _scrape_single_board(
        self,
        jobspy: Any,
        *,
        board: str,
        domain: str,
        location: str,
        results_wanted: int,
    ) -> List[JobPosting]:
        """Query one board once, rotating the proxy and retrying on a rate limit."""
        for attempt in (1, 2):
            proxy = self.proxy_mgr.get_proxy_for_board(board)
            detector = _BlockDetector()
            # JobSpy loggers set `propagate = False`, and "JobSpy:Glassdoor" is not a
            # child of "JobSpy" (logger hierarchy is dot-separated), so the handler
            # must attach to the board's own logger. That also keeps attribution
            # exact: a LinkedIn warning can never mark Indeed as blocked.
            jobspy_logger = logging.getLogger(f"JobSpy:{JOBSPY_LOGGER_NAMES.get(board, board)}")
            jobspy_logger.addHandler(detector)
            try:
                console.print(f"  - {board} (connection: {'configured proxy' if proxy else 'direct'})...")
                with tolerate_unknown_countries():
                    df = jobspy.scrape_jobs(
                        site_name=[board],
                        search_term=domain,
                        google_search_term=(f"{domain} jobs in {location}" if board == "google" else None),
                        location=location,
                        # JobSpy only has a remote-only switch. Mixed, hybrid and
                        # onsite choices are filtered after normalization.
                        is_remote=self.params.selected_work_modes == ["remote"],
                        results_wanted=results_wanted,
                        hours_old=self.params.hours_old,
                        country_indeed=self.params.country_indeed,
                        linkedin_fetch_description=False,
                        proxies=[proxy] if proxy else None,
                        description_format="markdown",
                        verbose=0,
                    )
                postings = self._normalize_jobspy_df(df, default_source=board)
                if detector.messages and not postings:
                    if self.proxy_mgr.has_proxies and attempt == 1:
                        self.proxy_mgr.mark_proxy_failed(board, proxy)
                        console.print(f"    [yellow]{board} refused the request; rotating proxy and retrying...[/yellow]")
                        continue
                    self.blocked_boards[board] = detector.messages[0][:160]
                    console.print(
                        f"    [yellow]{board} is blocking these requests (HTTP 403). Skipping it for "
                        "the rest of this sweep.[/yellow]"
                    )
                    return []
                console.print(f"    [green]Found {len(postings)} listings on {board}[/green]")
                return postings
            except Exception as exc:
                message = str(exc)
                self.board_errors[board] = message[:160]
                rate_limited = any(token in message.lower() for token in _BLOCK_MARKERS)
                if rate_limited and attempt == 1 and self.proxy_mgr.has_proxies:
                    console.print(f"    [yellow]{board} rate-limited; rotating proxy and retrying...[/yellow]")
                    self.proxy_mgr.mark_proxy_failed(board, proxy)
                    continue
                console.print(f"    [yellow]Warning on {board}: {message[:160]}[/yellow]")
                if rate_limited:
                    self.proxy_mgr.mark_proxy_failed(board, proxy)
                    self.blocked_boards[board] = message[:160]
                return []
            finally:
                jobspy_logger.removeHandler(detector)
        return []

    # --- Filtering ------------------------------------------------------------

    def _passes_filters(self, job: JobPosting) -> bool:
        """Apply the `searches.yaml` constraints the boards do not enforce themselves.

        Unknown values never disqualify a posting: a listing with no salary band is
        kept, because most listings omit one and dropping them would discard the
        majority of real opportunities.
        """
        if job.source not in DIRECT_SOURCES:
            from job_agent.sourcing.relevance import title_matches

            # Company feeds are matched by title when fetched; boards are not.
            if not title_matches(job.title, self.params.target_domains):
                self.filter_stats["off_target"] += 1
                return False

        if job.work_mode not in self.params.selected_work_modes:
            reason = "not_remote" if self.params.selected_work_modes == ["remote"] else "work_mode"
            self.filter_stats[reason] += 1
            return False

        if job.work_mode in {"onsite", "hybrid"} and self.params.onsite_countries:
            from job_agent.config.schema import location_in_countries

            # A configured eligibility country list is authoritative for roles
            # that require physical presence. An unknown location cannot be
            # confirmed eligible, so it is excluded from automatic processing.
            eligible = location_in_countries(job.location, self.params.onsite_countries)
            if eligible is not True:
                self.filter_stats["outside_onsite_countries"] += 1
                return False

        if job.work_mode == "remote" and self.candidate_profile is not None:
            auth = self.candidate_profile.work_authorization
            from job_agent.tailoring.regional import remote_eligibility
            eligibility, _ = remote_eligibility(job, auth.current_country)
            if eligibility == "Location restricted":
                self.filter_stats["outside_remote_eligibility"] += 1
                return False
            if auth.remote_worldwide is False:
                from job_agent.config.schema import location_in_countries

                allowed = list(auth.authorized_countries)
                if allowed and location_in_countries(job.location, allowed) is False:
                    self.filter_stats["outside_remote_eligibility"] += 1
                    return False

        if self.params.min_salary is not None and self._same_currency(job):
            # Compare against the top of the band: a range of 120k-200k satisfies a
            # 175k floor, even though its lower bound does not.
            ceiling = job.salary_max if job.salary_max is not None else job.salary_min
            if ceiling is not None and ceiling < self.params.min_salary:
                self.filter_stats["below_min_salary"] += 1
                return False

        age_hours = job.age_hours()
        if age_hours is not None and age_hours > self.params.hours_old:
            self.filter_stats["too_old"] += 1
            return False
        if age_hours is None and job.source in DIRECT_SOURCES:
            # Job boards apply the time window server-side, so an undated board
            # listing is still inside it. A company feed returns every open role,
            # so an undated one there could be months old.
            self.filter_stats["undated"] += 1
            return False

        return True

    def _same_currency(self, job: JobPosting) -> bool:
        """Whether a posting's salary can be compared with the configured floor.

        A floor of 800,000 rupees says nothing about a band of $120k-$150k, but
        the numbers were compared directly and the USD listing was rejected.
        Postings in another currency are kept: an unknown is never a reason to
        discard a real opportunity.
        """
        return (job.salary_currency or "").upper() == self.params.salary_currency

    @staticmethod
    def _deduplicate(jobs: List[JobPosting]) -> Tuple[List[JobPosting], int]:
        """Collapse postings for the same role within a single sweep.

        The same role appears on several boards and under several search terms,
        with a different URL and so a different ID on each. Postings are grouped
        by ID and by company-plus-title fingerprint; the copy kept is the one most
        useful to apply with — an application form first, then contact emails,
        then the fuller description — with the others' emails merged into it.
        """
        from job_agent.automation.routing import route_application

        def usefulness(job: JobPosting):
            return (route_application(job).automatable, bool(job.contacts), len(job.description))

        groups: Dict[str, List[JobPosting]] = {}
        key_of: Dict[str, str] = {}
        for job in jobs:
            key = key_of.get(job.id) or job.fingerprint()
            key_of[job.id] = key
            groups.setdefault(key, []).append(job)

        kept: List[JobPosting] = []
        duplicates = 0
        for group in groups.values():
            duplicates += len(group) - 1
            best = max(group, key=usefulness)
            contacts = [contact.model_dump() for job in group for contact in job.contacts]
            if len(group) > 1 and contacts:
                best = JobPosting.model_validate({**best.model_dump(), "contacts": contacts,
                                                  "company_website": best.company_website or next(
                                                      (j.company_website for j in group if j.company_website), None)})
            kept.append(best)
        return kept, duplicates

    # --- Orchestration --------------------------------------------------------

    @exclusive_run
    def run_sourcing_pipeline(
        self,
        include_ats_direct: bool = True,
        output_file: Optional[Path] = None,
    ) -> List[JobPosting]:
        """Execute a full omnichannel sourcing sweep.

        Boards and ATS feeds are queried, results are filtered and de-duplicated
        within the sweep and against the delta store, and the surviving novel
        postings are written to disk for Phase 3.
        """
        target_output = output_file or (settings.outputs_dir / "scraped_jobs.json")
        self.filter_stats.clear()

        if self.candidate_profile is None and settings.profile_path.is_file():
            try:
                from job_agent.intake.validator import load_and_verify_profile

                profile, valid = load_and_verify_profile(settings.profile_path)
                if valid:
                    self.candidate_profile = profile
            except Exception:
                # Sourcing remains usable before intake. Eligibility unknowns are
                # kept and the evaluation phase will flag them for review.
                pass

        console.print("[bold cyan]=== Omnichannel sourcing sweep started ===[/bold cyan]")
        console.print(f"Target domains : {', '.join(self.params.target_domains)}")
        console.print(f"Locations      : {', '.join(self.params.locations)}")
        console.print(f"Temporal window: {self.params.hours_old} hours")
        if self.params.min_salary:
            console.print(f"Minimum salary : {self.params.min_salary:,} {self.params.salary_currency}")
        self.blocked_boards.clear()
        self.board_errors.clear()
        for warning in self.configuration_warnings(self.params):
            console.print(f"[bold yellow]Check your settings:[/bold yellow] {warning}")

        aggregated: List[JobPosting] = []
        source_errors: Dict[str, str] = {}

        try:
            aggregated.extend(self.scrape_job_boards())
        except RunCancelled:
            # Nothing is written and nothing marked seen: a half sweep recorded as
            # seen would hide those jobs from every future sweep.
            raise
        except ImportError:
            source_errors["job_boards"] = "python-jobspy is not installed"
            console.print(
                "[red]python-jobspy is not installed.[/red] Install it with: pip install python-jobspy"
            )
        except Exception as exc:
            source_errors["job_boards"] = str(exc)[:160]
            console.print(f"[red]JobSpy scraping error: {exc}[/red]")

        if include_ats_direct:
            console.print("[cyan]Querying direct ATS endpoints (Greenhouse, Lever, Ashby)...[/cyan]")
            try:
                ats_jobs = self.ats_feeder.scrape_configured_ats(
                    companies=self.params.ats_companies,
                    search_params=self.params,
                )
                console.print(f"[green]Direct ATS feeds provided {len(ats_jobs)} matching listings[/green]")
                aggregated.extend(ats_jobs)
            except RunCancelled:
                raise
            except Exception as exc:
                source_errors["ats"] = str(exc)[:160]
                console.print(f"[yellow]Direct ATS query issue: {exc}[/yellow]")

        if self.params.public_sources:
            console.print(
                f"[cyan]Querying public job APIs ({', '.join(self.params.public_sources)})...[/cyan]"
            )
            try:
                aggregated.extend(self.public_feeder.scrape_configured(self.params))
            except RunCancelled:
                raise
            except Exception as exc:
                source_errors["public_feeds"] = str(exc)[:160]
                console.print(f"[yellow]Public job API issue: {str(exc)[:160]}[/yellow]")

        raw_count = len(aggregated)

        filtered = [job for job in aggregated if self._passes_filters(job)]
        deduped, in_sweep_duplicates = self._deduplicate(filtered)
        unseen = self.delta_store.filter_unseen(deduped)
        previously_seen = len(deduped) - len(unseen)

        self._print_summary(raw_count, in_sweep_duplicates, previously_seen, unseen)

        from job_agent.sourcing.details import enrich_job_details
        unseen, detail_report = enrich_job_details(
            unseen, proxy=self.proxy_mgr.get_proxy_for_board("linkedin"))
        if detail_report["requested"]:
            console.print(f"[cyan]Descriptions: {detail_report['fetched']}/{detail_report['requested']} thin job descriptions fetched.[/cyan]")

        if unseen and self.params.find_contacts:
            unseen = self._find_contacts(unseen)

        backlog = self._unevaluated_backlog(target_output, {job.id for job in unseen})
        if backlog:
            backlog, backlog_details = enrich_job_details(
                backlog, proxy=self.proxy_mgr.get_proxy_for_board("linkedin"))
            for key, value in backlog_details.items():
                detail_report[key] += value
            console.print(
                f"[cyan]Carried over {len(backlog)} job(s) from the previous sweep that were never "
                "evaluated and are still inside the time window.[/cyan]"
            )

        if unseen:
            self.delta_store.mark_many_seen(unseen, status="scraped")

        target_output.parent.mkdir(parents=True, exist_ok=True)
        # This is the observed shortlist, including already-seen jobs. The
        # processing queue below remains deduplicated so repeat runs cannot apply twice.
        enriched = {job.id: job for job in unseen + backlog}
        latest = [enriched.get(job.id, job) for job in deduped]
        (target_output.parent / "latest_jobs.json").write_text(
            json.dumps([job.model_dump() for job in latest], indent=2), encoding="utf-8")
        target_output.write_text(
            json.dumps([job.model_dump() for job in unseen + backlog], indent=2), encoding="utf-8"
        )
        from collections import Counter
        from job_agent.config.normalize import utc_now_iso
        counts = Counter(job.source for job in aggregated)
        coverage = {
            "checked_at": utc_now_iso(), "raw_listings": raw_count, "new_jobs": len(unseen),
            "backlog_jobs": len(backlog), "latest_matching_jobs": len(latest),
            "previously_seen": previously_seen, "duplicates": in_sweep_duplicates + previously_seen,
            "filtered": dict(self.filter_stats), "errors": source_errors,
            "description_fetch": detail_report,
            "boards": {board: {"status": "blocked" if board in self.blocked_boards else
                                "failed" if "job_boards" in source_errors or board in self.board_errors else "completed",
                                "matching_listings": counts.get(board, 0),
                                "detail": self.blocked_boards.get(board, self.board_errors.get(board, ""))}
                       for board in self.params.job_boards},
            "public_feeds": getattr(self.public_feeder, "report", {}),
            "ats": {"requested": bool(include_ats_direct), "configured_boards": self.params.ats_companies,
                    "endpoints": getattr(self.ats_feeder, "report", {}),
                    "matching_listings": {name: counts.get(name, 0) for name in ("greenhouse", "lever", "ashby")},
                    "detail": "Counts do not prove every configured employer endpoint was reachable."},
            "limitations": "Coverage is limited to configured sources and accessible listings; remote does not mean worldwide eligibility.",
        }
        checks = list(coverage["boards"].values()) + list(coverage["public_feeds"].values()) + list(coverage["ats"]["endpoints"].values())
        failures = sum(item.get("status") in {"blocked", "failed", "partial"} for item in checks)
        successes = sum(item.get("status") in {"completed", "ok", "partial"} for item in checks)
        coverage["status"] = "failed" if failures and not successes and not aggregated else (
            "partial" if failures or source_errors else "ok")
        coverage["hours_old"] = self.params.hours_old
        (target_output.parent / "source_coverage.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")
        if unseen or backlog:
            invalidate_after("source", target_output.parent)
        console.print(f"[bold green]Sourced jobs written to:[/bold green] [yellow]{target_output}[/yellow]\n")

        return unseen + backlog

    def _unevaluated_backlog(self, previous_output: Path, exclude: set) -> List[JobPosting]:
        """Jobs the previous sweep found that evaluation never reached.

        A sweep marks every novel job as seen, so it is never sourced again. When
        evaluation is capped (`--limit`) or stopped, the jobs it did not reach
        would otherwise be lost for good the moment the next sweep overwrites
        the file. They are kept while still relevant and inside the time window.
        """
        if not previous_output.is_file():
            return []
        try:
            previous = [JobPosting.model_validate(item)
                        for item in json.loads(previous_output.read_text(encoding="utf-8"))]
        except Exception:
            return []
        statuses = self.delta_store.statuses([job.id for job in previous])
        stats_before = self.filter_stats.copy()
        backlog = [
            job for job in previous
            if job.id not in exclude
            and statuses.get(job.id) == "scraped"
            and self._within_window_since_discovery(job)
            and self._passes_filters(job)
        ]
        # Re-filtering the backlog must not inflate this sweep's funnel.
        self.filter_stats = stats_before
        return backlog

    def _within_window_since_discovery(self, job: JobPosting) -> bool:
        from job_agent.config.normalize import parse_posting_timestamp
        from datetime import datetime, timezone

        if job.age_hours() is not None:
            return True  # `_passes_filters` checks the real posting age.
        found = parse_posting_timestamp(job.discovered_at)
        return found is not None and (datetime.now(timezone.utc) - found).total_seconds() / 3600 <= self.params.hours_old

    def _find_contacts(self, jobs: List[JobPosting]) -> List[JobPosting]:
        """Add published contact emails from employer websites and, if keyed, Hunter.io.

        A lookup failure costs only the emails, never the sweep.
        """
        from job_agent.contacts.finder import HunterClient, enrich_contacts

        key = settings.hunter_api_key.get_secret_value().strip()
        console.print(
            "[cyan]Looking up published contact emails (job posts, company websites"
            + (", Hunter.io" if key else "") + ")...[/cyan]"
        )
        try:
            enriched, stats = enrich_contacts(jobs, hunter=HunterClient(key) if key else None)
        except RunCancelled:
            raise
        except Exception as exc:
            console.print(f"[yellow]Contact lookup issue: {exc}[/yellow]")
            return jobs
        console.print(
            f"[green]{stats['with_email']} of {len(jobs)} job(s) have a contact email[/green] "
            f"[dim](job post: {stats['from_post']}, company site: {stats['from_site']}, "
            f"Hunter.io: {stats['from_hunter']})[/dim]"
        )
        return enriched

    def _print_summary(
        self,
        raw_count: int,
        in_sweep_duplicates: int,
        previously_seen: int,
        unseen: List[JobPosting],
    ) -> None:
        """Render a funnel showing where every discovered posting went."""
        table = Table(title="Sourcing funnel", show_header=True, header_style="bold magenta")
        table.add_column("Stage", style="cyan")
        table.add_column("Count", justify="right", style="white")

        table.add_row("Raw listings discovered", str(raw_count))
        labels = {
            "not_remote": "Dropped: not remote",
            "below_min_salary": "Dropped: below minimum salary",
            "too_old": "Dropped: older than freshness window",
            "undated": "Dropped: company feed listing with no posting date",
            "off_target": "Dropped: title is not one of the target roles",
            "incomplete_row": "Dropped: missing title/company/URL",
            "invalid_row": "Dropped: failed schema validation",
        }
        for key, label in labels.items():
            if self.filter_stats.get(key):
                table.add_row(label, f"-{self.filter_stats[key]}")
        if in_sweep_duplicates:
            table.add_row("Dropped: duplicates within this sweep", f"-{in_sweep_duplicates}")
        if previously_seen:
            table.add_row("Dropped: already in delta store", f"-{previously_seen}")
        table.add_row("[bold green]Novel jobs ready for evaluation[/bold green]", f"[bold green]{len(unseen)}[/bold green]")

        console.print(table)

        missing_descriptions = sum(1 for job in unseen if len(job.description) < 50)
        if missing_descriptions:
            console.print(
                f"[yellow]Note:[/yellow] {missing_descriptions} listing(s) have little or no description text; "
                "their semantic match scores will be less reliable."
            )


def _as_float(value: Any) -> Optional[float]:
    """Coerce a DataFrame cell to a non-negative float, or None if it is not numeric."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
