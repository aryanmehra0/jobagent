"""Phase 3: two-tier semantic evaluation pipeline coordinator.

Orchestrates:

1. Candidate profile loading and fact-seal verification.
2. Tier 1 dense-embedding cosine pre-filter (MiniLM, with a TF-IDF fallback).
3. Tier 2 sliding-window LLM judge re-ranking.
4. Segregation of qualified roles for Phase 4 tailoring.

Tier 1 exists to keep Tier 2 cheap: the embedding pass is free and local, so only
plausibly relevant postings ever reach a paid LLM call.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rich.console import Console
from rich.table import Table

from job_agent.config.schema import CandidateProfile, EvaluatedJob, EvaluationScore, JobPosting
from job_agent.config.settings import settings
from job_agent.runtime import check_cancelled, exclusive_run, invalidate_after
from job_agent.evaluation.embedder import SemanticEmbedder
from job_agent.evaluation.reranker import LLMReranker
from job_agent.intake.validator import load_and_verify_profile
from job_agent.sourcing.delta_store import DeltaStore

console = Console()


class SemanticEvaluationPipeline:
    """End-to-end two-tier evaluation pipeline."""

    def __init__(
        self,
        embedder: Optional[SemanticEmbedder] = None,
        reranker: Optional[LLMReranker] = None,
        delta_store: Optional[DeltaStore] = None,
    ):
        self.embedder = embedder or SemanticEmbedder()
        self.reranker = reranker or LLMReranker()
        self.delta_store = delta_store or DeltaStore()

    @exclusive_run
    def run_evaluation(
        self,
        profile_path: Optional[Path] = None,
        jobs_path: Optional[Path] = None,
        tier1_threshold: Optional[float] = None,
        output_dir: Optional[Path] = None,
        limit: Optional[int] = None,
    ) -> Tuple[List[EvaluatedJob], List[EvaluatedJob]]:
        """Run the full two-tier evaluation over the sourced job pool.

        Args:
            limit: Cap on how many Tier 1 survivors reach the LLM judge, which
                bounds the cost of a large sweep.

        Returns:
            (all evaluated jobs, jobs that met the fit threshold)
        """
        profile_file = profile_path or settings.profile_path
        jobs_file = jobs_path or (settings.outputs_dir / "scraped_jobs.json")
        out_dir = output_dir or settings.outputs_dir
        threshold = settings.tier1_threshold if tier1_threshold is None else tier1_threshold

        console.print("\n[bold cyan]=== Phase 3: semantic evaluation ===[/bold cyan]")

        profile, is_valid = load_and_verify_profile(profile_file)
        if not is_valid:
            raise ValueError("Profile integrity failed. Re-run intake before evaluation.")

        jobs = self._load_jobs(jobs_file)
        input_count = len(jobs)
        missing_descriptions = [job.id for job in jobs if not (job.description or "").strip()]
        jobs = [job for job in jobs if (job.description or "").strip()]
        progress = {"input_jobs": input_count, "missing_descriptions": len(missing_descriptions),
                    "below_similarity_gate": 0, "deferred_by_limit": 0, "scored": 0,
                    "qualified": 0, "failed": 0, "complete": False}
        def save_progress():
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / "evaluation_progress.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(progress, indent=2), encoding="utf-8")
            temporary.replace(target)
        save_progress()
        self._write_outputs(out_dir, [], [])
        console.print(f"Loaded [bold]{len(jobs)}[/bold] discovered listings for evaluation.")
        if not jobs:
            progress["complete"] = True
            save_progress()
            self._write_outputs(out_dir, [], [])
            console.print("[yellow]No jobs to evaluate. Run 'python main.py source' first.[/yellow]")
            return [], []

        # --- Tier 1 -----------------------------------------------------------
        console.print(f"\n[cyan]Tier 1 dense embedding filter (threshold >= {threshold})...[/cyan]")
        tier1_passed = self.embedder.filter_and_rank(profile=profile, jobs=jobs, threshold=threshold)
        passed_ids = {job.id for job, _ in tier1_passed}
        for job in jobs:
            if job.id not in passed_ids:
                self.delta_store.update_status(job.id, "prefilter_rejected")
        progress["below_similarity_gate"] = len(jobs) - len(tier1_passed)
        console.print(f"  Passed Tier 1: [bold green]{len(tier1_passed)}[/bold green] of {len(jobs)}")

        if not tier1_passed:
            console.print(
                "[yellow]Nothing cleared the Tier 1 gate.[/yellow] Lower it with "
                "'--threshold 0.05', or broaden target_domains in searches.yaml."
            )

        if limit is not None and len(tier1_passed) > limit:
            progress["deferred_by_limit"] = len(tier1_passed) - limit
            console.print(f"  [dim]Limiting Tier 2 to the top {limit} by similarity.[/dim]")
            tier1_passed = tier1_passed[:limit]
        save_progress()

        # --- Tier 2 -----------------------------------------------------------
        console.print(f"\n[cyan]Tier 2 LLM re-ranker over {len(tier1_passed)} candidate roles...[/cyan]")
        evaluated_all: List[EvaluatedJob] = []
        qualified: List[EvaluatedJob] = []

        checkpoint_key = self._checkpoint_key(profile)
        resumed = self._load_checkpoint(out_dir, checkpoint_key)
        if resumed:
            console.print(
                f"  [cyan]Resuming an interrupted evaluation: {len(resumed)} job(s) already scored "
                "will not be sent to the judge again.[/cyan]"
            )

        for index, (job, similarity) in enumerate(tier1_passed, start=1):
            # Scores so far are already in the checkpoint, so stopping here loses
            # nothing and the next run resumes from this job.
            check_cancelled()
            prior = resumed.get(job.id)
            if prior is not None:
                score = prior.evaluation
                console.print(f"  [{index}/{len(tier1_passed)}] [dim]Reused score for {job.title} @ {job.company}[/dim]")
            else:
                console.print(f"  [{index}/{len(tier1_passed)}] Scoring: [bold]{job.title}[/bold] @ {job.company}")
                try:
                    score = self.reranker.evaluate_job(profile, job, similarity)
                except Exception as exc:
                    if settings.llm_strict:
                        # Scores already paid for are kept in the checkpoint, so
                        # re-running after a rate limit continues from here.
                        raise
                    # One bad posting must not abort the run; record it as unscored.
                    console.print(f"    [red]Evaluation failed: {exc}. Skipping this listing.[/red]")
                    progress["failed"] += 1
                    save_progress()
                    continue

            evaluated_all.append(EvaluatedJob(job=job, evaluation=score))

            if score.passed_threshold:
                qualified.append(evaluated_all[-1])
                self.delta_store.update_status(job.id, "qualified")
                if prior is None:
                    console.print(f"    [bold green]Qualified[/bold green] fit {score.fit_score:.1f}/10")
            else:
                self.delta_store.update_status(job.id, "evaluated_rejected")
                if prior is None:
                    console.print(f"    [dim]Below {score.threshold_used:g}: fit {score.fit_score:.1f}/10[/dim]")
            self._write_outputs(out_dir, evaluated_all, qualified)
            progress.update(scored=len(evaluated_all), qualified=len(qualified))
            save_progress()
            self._save_checkpoint(out_dir, checkpoint_key, evaluated_all)

        self._write_outputs(out_dir, evaluated_all, qualified)
        progress["complete"] = True
        save_progress()
        # The batch finished, so its results are final. Leaving the checkpoint
        # would let a later, deliberate re-evaluation silently reuse stale scores.
        self._clear_checkpoint(out_dir)
        self._render_summary(evaluated_all, qualified, out_dir)
        return evaluated_all, qualified

    # --- Checkpointing --------------------------------------------------------

    CHECKPOINT_NAME = "evaluation_checkpoint.json"

    def _checkpoint_key(self, profile: CandidateProfile) -> str:
        """Identify the conditions a score was computed under.

        A score is reusable only for the same profile content, the same judge,
        and the same fit threshold. The whole-profile hash is used rather than
        the fact seal: an edited skills list changes every score without changing
        a single locked fact.
        """
        judge = f"{getattr(self.reranker, 'provider', '')}:{getattr(self.reranker, 'min_score', '')}"
        material = f"{profile.compute_profile_hash()}|{judge}|{settings.min_match_score}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _load_checkpoint(self, out_dir: Path, key: str) -> Dict[str, EvaluatedJob]:
        """Scores from an interrupted run under identical conditions, keyed by job ID."""
        path = out_dir / self.CHECKPOINT_NAME
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if payload.get("key") != key:
            # The profile, judge or threshold changed; these scores do not apply.
            self._clear_checkpoint(out_dir)
            return {}

        restored: Dict[str, EvaluatedJob] = {}
        for item in payload.get("results", []):
            try:
                evaluated = EvaluatedJob(**item)
            except Exception:
                continue
            restored[evaluated.job.id] = evaluated
        return restored

    def _save_checkpoint(self, out_dir: Path, key: str, results: List[EvaluatedJob]) -> None:
        """Record progress atomically, so a crash mid-write cannot corrupt it."""
        path = out_dir / self.CHECKPOINT_NAME
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"key": key, "results": [item.model_dump() for item in results]}),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _clear_checkpoint(self, out_dir: Path) -> None:
        (out_dir / self.CHECKPOINT_NAME).unlink(missing_ok=True)

    # --- Helpers --------------------------------------------------------------

    @staticmethod
    def _load_jobs(jobs_file: Path) -> List[JobPosting]:
        """Load and validate sourced postings, skipping any that no longer validate."""
        if not jobs_file.exists():
            raise FileNotFoundError(
                f"Scraped jobs file not found: {jobs_file}. Run 'python main.py source' first."
            )
        try:
            raw = json.loads(jobs_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{jobs_file} is not valid JSON: {exc}") from exc

        jobs: List[JobPosting] = []
        for item in raw:
            try:
                jobs.append(JobPosting(**item))
            except Exception as exc:
                console.print(f"[dim]Skipping malformed listing in {jobs_file.name}: {exc}[/dim]")
        return jobs

    @staticmethod
    def _write_outputs(
        out_dir: Path,
        evaluated_all: List[EvaluatedJob],
        qualified: List[EvaluatedJob],
    ) -> None:
        """Persist the full evaluation and the qualified subset."""
        out_dir.mkdir(parents=True, exist_ok=True)
        invalidate_after("evaluate", out_dir)
        (out_dir / "evaluated_jobs.json").write_text(
            json.dumps([item.model_dump() for item in evaluated_all], indent=2), encoding="utf-8"
        )
        (out_dir / "qualified_jobs.json").write_text(
            json.dumps([item.model_dump() for item in qualified], indent=2), encoding="utf-8"
        )

    @staticmethod
    def _render_summary(
        evaluated_all: List[EvaluatedJob],
        qualified: List[EvaluatedJob],
        out_dir: Path,
    ) -> None:
        """Print the top scored roles and where the artifacts landed."""
        table = Table(title="Semantic evaluation results", show_header=True, header_style="bold magenta")
        table.add_column("Company / Title", style="cyan", width=34)
        table.add_column("Tier 1", justify="center", width=8)
        table.add_column("Tier 2", justify="center", width=9)
        table.add_column("Status", width=14)
        table.add_column("Key matching skills", style="white")

        ranked = sorted(evaluated_all, key=lambda item: item.evaluation.fit_score, reverse=True)
        for item in ranked[:10]:
            status = (
                "[bold green]QUALIFIED[/bold green]"
                if item.evaluation.passed_threshold
                else "[dim]FILTERED[/dim]"
            )
            table.add_row(
                f"{item.job.company}\n{item.job.title}",
                f"{item.evaluation.embedding_similarity:.2f}",
                f"{item.evaluation.fit_score:.1f}/10",
                status,
                ", ".join(item.evaluation.matching_skills[:3]) or "-",
            )

        console.print(table)
        console.print("\n[bold green]Evaluation complete.[/bold green]")
        console.print(f"  Evaluated: {len(evaluated_all)} -> [yellow]{out_dir / 'evaluated_jobs.json'}[/yellow]")
        console.print(
            f"  Qualified: [bold green]{len(qualified)}[/bold green] -> "
            f"[yellow]{out_dir / 'qualified_jobs.json'}[/yellow]\n"
        )
