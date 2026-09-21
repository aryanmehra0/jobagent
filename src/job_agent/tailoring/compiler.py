"""Typst Resume Compiler Bridge.

Compiles tailored JSON resume data into a single-column, ATS-friendly PDF
in under 100 milliseconds using the Rust-backed Typst compilation engine.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Dict, Any, Optional
import typst
from rich.console import Console

from job_agent.config.settings import settings

console = Console()


class TypstResumeCompiler:
    """High-speed Typst PDF compiler bridge."""

    def __init__(self, template_path: Optional[Path] = None, output_dir: Optional[Path] = None):
        self.template_path = template_path or (settings.templates_dir / "resume.typ")
        self.output_dir = output_dir or (settings.outputs_dir / "tailored_resumes")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def compile_resume(
        self,
        tailored_data: Dict[str, Any],
        job_id: Optional[str] = None,
    ) -> Path:
        """Serialize tailored data and compile to PDF via Typst in milliseconds."""
        target_id = job_id or tailored_data.get("target_job", {}).get("id", "candidate")
        json_file = self.output_dir / f"tailored_{target_id}.json"
        pdf_file = self.output_dir / f"resume_{target_id}.pdf"
        typ_entry_file = self.output_dir / f"_entry_{target_id}.typ"

        # 1. Save tailored JSON data
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(tailored_data, f, indent=2)

        # 2. Prepare dynamic Typst entry file referencing this specific JSON data
        # Typst loads relative to the .typ file; using filename avoids Windows drive letter issues
        json_filename = json_file.name

        # Read template body and substitute data loading
        template_content = self.template_path.read_text(encoding="utf-8")
        
        # Replace the data_file loading line with the relative path to our tailored JSON
        custom_typ = template_content.replace(
            '#let data_file = sys.inputs.at("data_file", default: "resume_data.json")',
            f'#let data_file = "{json_filename}"',
        )
        typ_entry_file.write_text(custom_typ, encoding="utf-8")

        # 3. Benchmark and compile PDF with Typst
        start_time = time.perf_counter()
        try:
            typst.compile(str(typ_entry_file), output=str(pdf_file))
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            console.print(
                f"[bold green]✓ Typst Compiled ATS PDF in {elapsed_ms:.1f}ms[/bold green] -> [yellow]{pdf_file}[/yellow]"
            )
        except Exception as e:
            console.print(f"[bold red]Typst compilation error:[/bold red] {e}")
            raise
        finally:
            # Clean up ephemeral entry typ file
            if typ_entry_file.exists():
                try:
                    typ_entry_file.unlink()
                except Exception:
                    pass

        # 4. Verify file output
        if not pdf_file.exists() or pdf_file.stat().st_size == 0:
            raise RuntimeError(f"Typst compilation produced empty or missing file: {pdf_file}")

        # A successful PDF write does not prove an ATS can read it. Validate the
        # compiled bytes before the application phase is allowed to consume them.
        report = self.validate_ats_pdf(pdf_file, tailored_data)
        report_file = pdf_file.with_suffix(".ats.json")
        report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")

        return pdf_file

    @staticmethod
    def _search_key(value: Any) -> str:
        text = unicodedata.normalize("NFKD", str(value or "")).casefold()
        return re.sub(r"[^a-z0-9]+", "", text)

    def validate_ats_pdf(self, pdf_file: Path, tailored_data: Dict[str, Any]) -> Dict[str, Any]:
        """Fail closed when a compiled resume is blank, unreadable, or loses source facts."""
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_file))
        page_text = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(page_text)
        searchable = self._search_key(text)
        failures = []

        if len(searchable) < 150:
            failures.append("the PDF contains too little searchable text")
        if len(reader.pages) > 3:
            failures.append(f"the resume is {len(reader.pages)} pages (maximum 3)")

        required = [
            tailored_data.get("contact", {}).get("full_name"),
            tailored_data.get("contact", {}).get("email"),
        ]
        required.extend(exp.get("company") for exp in tailored_data.get("experience", []))
        missing = [str(value) for value in required if value and self._search_key(value) not in searchable]
        if missing:
            failures.append("missing identity/experience text: " + ", ".join(missing[:5]))

        # Tailoring is restricted to exact source bullets. Confirm the PDF still
        # carries them after layout and font rendering, allowing whitespace and
        # punctuation changes introduced by PDF extraction.
        bullets = [
            bullet
            for exp in tailored_data.get("experience", [])
            for bullet in exp.get("description_bullets", [])
            if bullet
        ]
        missing_bullets = [bullet for bullet in bullets if self._search_key(bullet) not in searchable]
        if missing_bullets:
            failures.append(f"{len(missing_bullets)} source achievement(s) are missing from extracted text")

        report = {
            "passed": not failures,
            "page_count": len(reader.pages),
            "searchable_characters": len(text.strip()),
            "source_bullets_checked": len(bullets),
            "failures": failures,
        }
        if failures:
            raise RuntimeError("ATS PDF validation failed: " + "; ".join(failures))
        return report
