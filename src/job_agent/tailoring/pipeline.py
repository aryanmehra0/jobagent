"""Phase 4: dynamic resume tailoring pipeline coordinator.

Orchestrates:

1. Profile loading and fact-seal verification.
2. Ingestion of the qualified jobs from Phase 3.
3. Bullet rewriting behind the anti-hallucination integrity gate.
4. Single-column ATS PDF compilation via Typst.
5. Delta store updates and a manifest recording what the integrity gate did.

The manifest records every restored and every rejected metric, so a tailored PDF
can be audited after the fact without re-running the LLM.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.table import Table

from job_agent.config.schema import EvaluatedJob, TailoredResumeRecord
from job_agent.config.settings import settings
from job_agent.runtime import check_cancelled, exclusive_run, invalidate_after
from job_agent.intake.validator import load_and_verify_profile
from job_agent.sourcing.delta_store import DeltaStore
from job_agent.tailoring.compiler import TypstResumeCompiler
from job_agent.tailoring.rewriter import ResumeTailorer

console = Console()


class ResumeTailoringPipeline:
    """Coordinator for dynamic ATS resume tailoring and Typst compilation."""

    def __init__(
        self,
        tailorer: Optional[ResumeTailorer] = None,
        compiler: Optional[TypstResumeCompiler] = None,
        delta_store: Optional[DeltaStore] = None,
    ):
        self.tailorer = tailorer or ResumeTailorer()
        self.compiler = compiler or TypstResumeCompiler()
        self.delta_store = delta_store or DeltaStore()

    @exclusive_run
    def run_tailoring(
        self,
        profile_path: Optional[Path] = None,
        qualified_jobs_path: Optional[Path] = None,
        specific_job_id: Optional[str] = None,
        limit: Optional[int] = None,
        mode: Optional[str] = None,
        country: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Tailor and compile a bespoke resume for each qualified target job."""
        selected_mode = mode or ("regional" if country else settings.tailoring_mode)
        if selected_mode not in {"auto", "faithful", "generated", "regional"}:
            raise ValueError("Resume mode must be auto, faithful, generated or regional.")
        profile_file = profile_path or settings.profile_path
        qualified_file = qualified_jobs_path or (settings.outputs_dir / "qualified_jobs.json")

        console.print("\n[bold cyan]=== Phase 4: dynamic resume tailoring (Typst) ===[/bold cyan]")

        profile, is_valid = load_and_verify_profile(profile_file)
        if not is_valid:
            # The integrity gate compares rewrites against the profile's locked
            # metrics; if those were tampered with, the gate is checking the wrong
            # reference and cannot be relied on.
            raise ValueError(
                "Refusing to tailor: the profile's fact seal does not verify, so the "
                "anti-hallucination gate cannot be trusted. Re-run 'python main.py intake' "
                "to re-seal the profile from the source resume."
            )

        qualified = self._load_qualified(qualified_file)
        invalidate_after("tailor", qualified_file.parent)

        if specific_job_id:
            qualified = [item for item in qualified if item.job.id == specific_job_id]
            if not qualified:
                console.print(f"[bold red]Job ID '{specific_job_id}' is not in the qualified list.[/bold red]")
                return []

        # Highest scoring roles first, so a `--limit` run tailors the best matches.
        qualified.sort(key=lambda item: item.evaluation.fit_score, reverse=True)
        if limit is not None:
            qualified = qualified[:limit]

        if not qualified:
            manifest_path = self.compiler.output_dir / "manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text("[]", encoding="utf-8")
            console.print("[yellow]No qualified jobs to tailor for. Run 'python main.py evaluate' first.[/yellow]")
            return []

        console.print(f"Targeting [bold green]{len(qualified)}[/bold green] qualified jobs.\n")

        if country and selected_mode in {"auto", "faithful"}:
            raise ValueError("Country overrides require regional or generated mode.")
        source_pdf = self._faithful_source(profile, selected_mode) if selected_mode in {"auto", "faithful"} else None
        if selected_mode == "faithful" and source_pdf is None:
            raise ValueError("Faithful mode requires the original readable PDF.")
        if source_pdf is not None:
            console.print(
                f"[cyan]Tailoring your own resume ([bold]{source_pdf.name}[/bold]): content, fonts, links and "
                "layout are kept exactly; only the order of bullets, projects and skill lines changes "
                "per job, and every PDF is validated against the original.[/cyan]\n"
            )
        checkpoint_key = self._checkpoint_key(profile) + f"|{selected_mode}|{country or ''}"
        resumed = self._load_checkpoint(checkpoint_key)
        if resumed:
            console.print(
                f"[cyan]Resuming an interrupted batch: {len(resumed)} verified resume(s) "
                "will not be tailored again.[/cyan]\n"
            )

        records: List[TailoredResumeRecord] = []
        for index, evaluated in enumerate(qualified, start=1):
            check_cancelled()
            job = evaluated.job
            console.print(
                f"[{index}/{len(qualified)}] {job.title} @ {job.company} "
                f"(fit {evaluated.evaluation.fit_score:.1f}/10)"
            )
            prior = resumed.get(job.id)
            if prior is not None:
                console.print(f"  [dim]Reused {Path(prior.pdf_path).name} (hash verified)[/dim]")
                self.delta_store.update_status(job.id, "tailored")
                records.append(prior)
                self._save_checkpoint(checkpoint_key, records)
                continue
            if source_pdf is not None:
                record = self._tailor_faithfully(source_pdf, profile, job, evaluated.evaluation.fit_score)
                if record is not None:
                    self.delta_store.update_status(job.id, "tailored")
                    records.append(record)
                    self._save_checkpoint(checkpoint_key, records)
                    continue
            try:
                if selected_mode == "regional":
                    # Regional mode preserves exact source prose. Its relevance
                    # ordering needs no generative call or provider cooldown.
                    tailored = self.tailorer.generate_tailored_profile_data(profile, job, use_llm=False)
                else:
                    tailored = self.tailorer.generate_tailored_profile_data(profile, job)
                from job_agent.tailoring.regional import regional_policy
                tailored["regional"] = regional_policy(job, country)
                pdf_path = self.compiler.compile_resume(tailored, job_id=job.id)
            except Exception as exc:
                if settings.llm_strict:
                    raise
                # A Typst failure on one job should not cost the whole batch.
                console.print(f"  [red]Tailoring failed for {job.company}: {exc}[/red]")
                continue

            self.delta_store.update_status(job.id, "tailored")
            integrity = tailored.get("integrity", {})
            records.append(
                TailoredResumeRecord(
                    job_id=job.id,
                    title=job.title,
                    company=job.company,
                    score=evaluated.evaluation.fit_score,
                    pdf_path=str(pdf_path),
                    json_path=str(self.compiler.output_dir / f"tailored_{job.id}.json"),
                    profile_hash=profile.profile_hash,
                    pdf_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
                    mode="regional" if selected_mode == "regional" else "generated",
                    target_country=tailored["regional"]["country"],
                    regional=tailored["regional"],
                    validation_passed=True,
                    validation_summary=f"ATS checks passed; {tailored['regional']['country']}; {tailored['regional']['paper']}",
                    restored_metrics=integrity.get("restored_metrics", []),
                    dropped_fabrications=integrity.get("dropped_fabrications", []),
                )
            )
            # Recorded after every resume, so an error on a later job — a rate
            # limit, a Typst failure — cannot orphan the ones already compiled.
            self._save_checkpoint(checkpoint_key, records)

        results = [record.model_dump() for record in records]
        manifest_path = self.compiler.output_dir / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        self._clear_checkpoint()
        self._archive_foreign_resumes(profile)

        self._render_summary(records, manifest_path)
        return results

    # --- Faithful tailoring ----------------------------------------------------

    def _faithful_source(self, profile: Any, mode: Optional[str] = None) -> Optional[Path]:
        """The uploaded resume PDF that every tailored resume is made from.

        When the candidate uploaded a PDF, it is always the source: a layout the
        engine cannot reorder safely still yields the exact original, never a
        resume generated from a template. A generated resume is used only when
        no PDF was uploaded (a Word file, for example).
        """
        from job_agent.intake.preferences import SAMPLE_RESUME_NAME, choose_resume

        mode = (mode or settings.tailoring_mode or "auto").strip().lower()
        if mode == "generated":
            return None
        name = profile.source_document or ""
        chosen = choose_resume()
        if name.lower() == SAMPLE_RESUME_NAME and chosen is not None and chosen.name.lower() != SAMPLE_RESUME_NAME:
            raise ValueError(
                f"Your profile was built from the demo resume, but your own resume ({chosen.name}) is uploaded. "
                "Run intake on it first, so resumes are tailored from yours: python main.py intake "
                f"--resume \"{chosen}\""
            )
        path = settings.raw_resumes_dir / name if name else None
        usable = path is not None and path.suffix.lower() == ".pdf" and path.is_file()
        if usable:
            try:
                import fitz

                with fitz.open(str(path)) as doc:
                    usable = len(doc) > 0 and bool(doc[0].get_text().strip())
            except Exception:
                usable = False
        if not usable and mode == "faithful":
            raise ValueError(
                "TAILORING_MODE=faithful needs the uploaded resume as a readable PDF in "
                f"{settings.raw_resumes_dir}; found {name or 'no source document'}."
            )
        return path if usable else None

    def _tailor_faithfully(self, source_pdf: Path, profile: Any, job: Any,
                           score: float) -> Optional[TailoredResumeRecord]:
        """Reorder the candidate's own PDF for one job and record the validation."""
        from job_agent.tailoring.faithful import summarize, tailor_pdf

        pdf_path = self.compiler.output_dir / f"resume_{job.id}.pdf"
        json_path = self.compiler.output_dir / f"tailored_{job.id}.json"
        try:
            result = tailor_pdf(source_pdf, job, pdf_path)
        except Exception as exc:
            # Your own resume, unchanged, beats a generated one.
            import shutil

            from job_agent.tailoring.faithful import FaithfulResult, Plan, read_layout, validate

            shutil.copyfile(source_pdf, pdf_path)
            validation = validate(read_layout(source_pdf), Plan(groups=[], skipped=[]), pdf_path)
            result = FaithfulResult(pdf_path, False, validation, [],
                                    [f"Could not reorder ({exc.__class__.__name__}); your original resume is used."])

        summary = summarize(result)
        for change in result.changes:
            console.print(f"  [green]•[/green] {change}")
        for note in result.notes:
            console.print(f"  [dim]{note}[/dim]")
        style = "green" if result.validation.get("passed") else "red"
        console.print(f"  [{style}]Validation: {summary}[/{style}]")

        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps({
            "mode": "faithful",
            "source_resume": source_pdf.name,
            "target_job": {"id": job.id, "title": job.title, "company": job.company, "job_url": job.job_url},
            "changes": result.changes,
            "notes": result.notes,
            "validation": result.validation,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

        return TailoredResumeRecord(
            job_id=job.id,
            title=job.title,
            company=job.company,
            score=score,
            pdf_path=str(pdf_path),
            json_path=str(json_path),
            profile_hash=profile.profile_hash,
            pdf_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
            mode="faithful",
            changes=result.changes,
            validation_passed=bool(result.validation.get("passed")),
            validation_summary=summary,
            validation=result.validation,
        )

    @exclusive_run
    def rebuild_existing(self, profile_path: Optional[Path] = None) -> Dict[str, Any]:
        """Re-tailor every job that already has a resume listed, from the original PDF.

        Covers the current batch and earlier ones still named in the jobs sheet
        or the tracker, so resumes built by an older method, or deleted since,
        are replaced by validated faithful versions. The job description for
        each is looked up in the current artifacts and the history archives.
        """
        import csv as csv_module
        import glob

        from job_agent.config.schema import JobPosting

        profile, is_valid = load_and_verify_profile(profile_path or settings.profile_path)
        if not is_valid:
            raise ValueError("The profile's fact seal does not verify. Re-run intake first.")
        source_pdf = self._faithful_source(profile)
        if source_pdf is None:
            raise ValueError(
                "Your uploaded resume could not be used for faithful tailoring. It must be a PDF in "
                f"{settings.raw_resumes_dir} named in the profile (currently: {profile.source_document})."
            )

        out = settings.outputs_dir
        manifest_path = self.compiler.output_dir / "manifest.json"
        manifest = []
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except ValueError:
                manifest = []

        wanted: Dict[str, float] = {}
        for entry in manifest:
            if isinstance(entry, dict) and entry.get("job_id"):
                wanted[entry["job_id"]] = float(entry.get("score") or 0.0)
        csv_path = out / "jobs_master.csv"
        if csv_path.is_file():
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv_module.DictReader(handle):
                    if row.get("Tailored Resume") and row.get("Job ID"):
                        wanted.setdefault(row["Job ID"], float(row.get("Fit Score") or 0.0))
        try:
            from job_agent.tracking.tracker import JOB_ID_COLUMN, RESUME_COLUMN
            import openpyxl

            if settings.tracker_path.is_file():
                sheet = openpyxl.load_workbook(str(settings.tracker_path), read_only=True).active
                for row in sheet.iter_rows(min_row=2, values_only=True):
                    job_id = row[JOB_ID_COLUMN - 1] if len(row) >= JOB_ID_COLUMN else None
                    resume = row[RESUME_COLUMN - 1] if len(row) >= RESUME_COLUMN else None
                    if job_id and resume and ".pdf" in str(resume):
                        score = row[3] if len(row) > 3 else None
                        try:
                            wanted.setdefault(str(job_id), float(score))
                        except (TypeError, ValueError):
                            wanted.setdefault(str(job_id), 0.0)
        except Exception:
            pass

        postings: Dict[str, Any] = {}
        pattern_roots = [out] + sorted((out / "history").glob("*"), reverse=True) if (out / "history").is_dir() else [out]
        for root in pattern_roots:
            for name in ("qualified_jobs.json", "evaluated_jobs.json", "scraped_jobs.json"):
                path = root / name
                if not path.is_file():
                    continue
                try:
                    items = json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                for item in items if isinstance(items, list) else []:
                    raw = item.get("job") if isinstance(item, dict) and "job" in item else item
                    try:
                        job = JobPosting.model_validate(raw)
                    except Exception:
                        continue
                    if job.id in wanted and job.id not in postings:
                        postings[job.id] = job
                        score = (item.get("evaluation") or {}).get("fit_score") if isinstance(item, dict) else None
                        if score is not None and not wanted[job.id]:
                            wanted[job.id] = float(score)

        console.print(
            f"\n[bold cyan]Rebuilding {len(postings)} tailored resume(s) from {source_pdf.name}[/bold cyan]"
        )
        rebuilt: List[TailoredResumeRecord] = []
        for index, (job_id, job) in enumerate(postings.items(), start=1):
            check_cancelled()
            console.print(f"[{index}/{len(postings)}] {job.title} @ {job.company}")
            record = self._tailor_faithfully(source_pdf, profile, job, min(max(wanted[job_id], 0.0), 10.0))
            if record is not None:
                rebuilt.append(record)

        by_id = {entry["job_id"]: entry for entry in manifest if isinstance(entry, dict) and entry.get("job_id")}
        for record in rebuilt:
            by_id[record.job_id] = record.model_dump()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(list(by_id.values()), indent=2), encoding="utf-8")
        self._archive_foreign_resumes(profile)

        missing = sorted(set(wanted) - set(postings))
        self._render_summary(rebuilt, manifest_path)
        if missing:
            console.print(
                f"[yellow]{len(missing)} job(s) could not be rebuilt: their job description is no longer "
                f"on disk ({', '.join(missing[:6])}{'…' if len(missing) > 6 else ''}).[/yellow]"
            )
        return {
            "rebuilt": len(rebuilt),
            "passed": sum(1 for record in rebuilt if record.validation_passed),
            "missing": missing,
        }

    def _archive_foreign_resumes(self, profile: Any) -> List[str]:
        """Move resumes that are not the candidate's out of the output folder.

        Leftovers from a demo profile (another person's name at the top) would
        otherwise sit beside the real resumes and could be opened or attached
        by mistake. They are archived to history, not deleted.
        """
        import re
        import time

        try:
            import fitz
        except ImportError:
            return []

        folder = self.compiler.output_dir
        expected = re.sub(r"\s+", " ", (profile.contact.full_name or "")).strip().casefold()
        if not expected or not folder.is_dir():
            return []
        # Only meaningful when the uploaded resume itself shows the name as text;
        # a name drawn as an image would otherwise mark every resume as foreign.
        source = settings.raw_resumes_dir / (profile.source_document or "")
        try:
            with fitz.open(str(source)) as doc:
                if expected not in re.sub(r"\s+", " ", doc[0].get_text()).casefold():
                    return []
        except Exception:
            return []
        moved: List[str] = []
        archive = settings.outputs_dir / "history" / f"{time.time_ns()}_foreign_resumes"
        for pdf in sorted(folder.glob("*.pdf")):
            try:
                with fitz.open(str(pdf)) as doc:
                    text = re.sub(r"\s+", " ", doc[0].get_text()).casefold() if len(doc) else ""
            except Exception:
                continue
            if expected in text:
                continue
            archive.mkdir(parents=True, exist_ok=True)
            pdf.replace(archive / pdf.name)
            stale = folder / f"tailored_{pdf.stem.replace('resume_', '')}.json"
            if stale.is_file():
                stale.replace(archive / stale.name)
            moved.append(pdf.name)
        if moved:
            console.print(f"[yellow]Moved {len(moved)} resume(s) that are not {profile.contact.full_name}'s "
                          f"to {archive}.[/yellow]")
        return moved

    # --- Checkpointing --------------------------------------------------------

    CHECKPOINT_NAME = "tailoring_checkpoint.json"

    @property
    def _checkpoint_path(self) -> Path:
        return self.compiler.output_dir / self.CHECKPOINT_NAME

    def _checkpoint_key(self, profile: Any) -> str:
        """A compiled resume is reusable only for the same profile and tailorer."""
        tailorer = getattr(self.tailorer, "provider", "")
        # Bumped whenever what a tailored resume contains changes, so resumes
        # built by an older version are rebuilt rather than reused.
        version = f"4-{settings.tailoring_mode}"
        return hashlib.sha256(f"{profile.compute_profile_hash()}|{tailorer}|{version}".encode("utf-8")).hexdigest()

    def _load_checkpoint(self, key: str) -> Dict[str, TailoredResumeRecord]:
        """Resumes from an interrupted batch whose PDFs are still exactly as compiled.

        Each PDF is re-hashed before reuse. A file that was edited, replaced or
        deleted since the interrupted run is rebuilt rather than trusted, because
        the integrity record describes the original bytes, not whatever is on
        disk now.
        """
        path = self._checkpoint_path
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if payload.get("key") != key:
            self._clear_checkpoint()
            return {}

        verified: Dict[str, TailoredResumeRecord] = {}
        for item in payload.get("records", []):
            try:
                record = TailoredResumeRecord(**item)
            except Exception:
                continue
            pdf = Path(record.pdf_path)
            if not pdf.is_file() or not record.pdf_sha256:
                continue
            if hashlib.sha256(pdf.read_bytes()).hexdigest() != record.pdf_sha256:
                continue
            audit_path = pdf.with_suffix(".ats.json")
            try:
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if audit.get("passed") is not True:
                continue
            verified[record.job_id] = record
        return verified

    def _save_checkpoint(self, key: str, records: List[TailoredResumeRecord]) -> None:
        """Record progress atomically, so a crash mid-write cannot corrupt it."""
        path = self._checkpoint_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"key": key, "records": [record.model_dump() for record in records]}),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _clear_checkpoint(self) -> None:
        self._checkpoint_path.unlink(missing_ok=True)

    @staticmethod
    def _load_qualified(qualified_file: Path) -> List[EvaluatedJob]:
        """Load the qualified jobs produced by Phase 3."""
        if not qualified_file.exists():
            raise FileNotFoundError(
                f"Qualified jobs file not found: {qualified_file}. Run 'python main.py evaluate' first."
            )
        try:
            raw = json.loads(qualified_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{qualified_file} is not valid JSON: {exc}") from exc

        items: List[EvaluatedJob] = []
        for entry in raw:
            try:
                items.append(EvaluatedJob(**entry))
            except Exception as exc:
                console.print(f"[dim]Skipping malformed qualified entry: {exc}[/dim]")
        return items

    @staticmethod
    def _render_summary(records: List[TailoredResumeRecord], manifest_path: Path) -> None:
        """Print the compilation summary, including anti-hallucination activity."""
        table = Table(title="Tailored resume compilation", show_header=True, header_style="bold magenta")
        table.add_column("Company", style="cyan")
        table.add_column("Role", style="white")
        table.add_column("Fit", justify="center", style="green")
        table.add_column("Checks", style="yellow")
        table.add_column("PDF", style="yellow")

        for record in records:
            if record.mode == "faithful":
                check = record.validation_summary or ""
            else:
                notes = []
                if record.restored_metrics:
                    notes.append(f"{len(record.restored_metrics)} restored")
                if record.dropped_fabrications:
                    notes.append(f"{len(record.dropped_fabrications)} fabrication(s) blocked")
                check = "integrity gate: " + (", ".join(notes) or "clean")
            table.add_row(
                record.company,
                record.title,
                f"{record.score:.1f}/10",
                check,
                Path(record.pdf_path).name,
            )

        console.print(table)
        console.print(f"\n[bold green]Phase 4 complete.[/bold green] {len(records)} tailored PDF(s).")
        console.print(f"Manifest: [yellow]{manifest_path}[/yellow]\n")
