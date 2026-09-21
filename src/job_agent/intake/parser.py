"""Candidate resume intake and PDF parsing.

Extraction ladder, best first:

1. **LlamaParse** cloud parser, when `LLAMA_CLOUD_API_KEY` is set (best layout fidelity).
2. **Local text extraction** via pdfplumber, falling back to pypdf.
3. **Structured extraction** through an LLM (OpenAI or Anthropic), whose JSON is
   validated against `CandidateProfile` and then *fact-checked against the source
   text* before it is accepted.
4. **Deterministic extraction** (`src.intake.heuristic`) when no API key is
   configured or the LLM output fails validation.

Step 3's fact check is the important one: an LLM that invents "$2.5M in savings"
would otherwise have that number sealed as an immutable locked fact and printed on
every tailored resume. Metrics that do not appear in the resume text are rejected.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console

from job_agent.config.schema import CandidateProfile
from job_agent.config.settings import settings
from job_agent.runtime import exclusive_run, invalidate_after
from job_agent.intake.heuristic import ResumeParseError, build_profile_dict
from job_agent.intake.validator import validate_and_save_profile

console = Console()


SYSTEM_EXTRACTION_PROMPT = """You are an expert resume parsing engine for an ATS system.
Your mission is to extract the candidate's career data from the provided resume text into a strict, structured JSON format.

CRITICAL INTEGRITY & ANTI-HALLUCINATION RULES:
1. Only extract facts, metrics, and dates that appear explicitly in the resume.
2. DO NOT invent, exaggerate, or infer metrics, company names, degrees, certifications, or dates.
3. If a field is not present in the resume, use null (or an empty list). NEVER substitute a placeholder or example value.
4. Every quantifiable achievement (e.g., "$2.5M", "42% increase", "100k RPS", "team of 8") MUST be preserved verbatim in locked_facts with its category ("metric", "scale", "revenue", "deployment", or "tenure").
5. Copy metric values character-for-character from the resume. Do not round, reformat, or convert units.
6. Only record work authorization details that the resume states explicitly. Otherwise set requires_sponsorship=null, visa_status=null, authorized_countries=[], and citizenship=[]. Residence is not work authorization.
7. Compute total professional experience in years from the start and end dates you extracted.

Output ONLY a single valid JSON object adhering to this structure:
{
  "contact": {
    "full_name": "...",
    "email": "...",
    "phone": "... or null",
    "location": "... or null",
    "linkedin_url": "... or null",
    "github_url": "... or null",
    "portfolio_url": "... or null"
  },
  "summary": "...",
  "work_authorization": {
    "citizenship": [],
    "current_country": "...",
    "authorized_countries": [],
    "requires_sponsorship": null,
    "visa_status": null
  },
  "education": [
    {
      "institution": "...",
      "degree": "...",
      "field_of_study": "...",
      "start_date": "YYYY",
      "end_date": "YYYY",
      "gpa": "... or null",
      "honors": []
    }
  ],
  "experience": [
    {
      "company": "...",
      "title": "...",
      "location": "... or null",
      "start_date": "YYYY-MM",
      "end_date": "YYYY-MM or Present",
      "is_current": false,
      "description_bullets": ["..."],
      "locked_facts": [
        {"category": "metric", "statement": "...", "metric_value": "..."}
      ]
    }
  ],
  "skills": {
    "languages": [],
    "frameworks": [],
    "developer_tools": [],
    "cloud_devops": [],
    "domain_knowledge": []
  },
  "projects": [
    {"title": "...", "role": "... or null", "technologies": [], "description": "...", "link": "... or null", "locked_facts": []}
  ],
  "certifications": [
    {"name": "...", "issuer": "...", "issue_date": "... or null", "credential_url": "... or null"}
  ],
  "years_of_experience": 4.0
}
"""

# Extraction quality floor. An LLM that returns a profile with no roles and no
# skills has failed, even if the JSON validates, so we fall back instead.
MIN_USEFUL_SECTIONS = 1

# Resume formats the agent can read. PDF is preferred because it preserves the
# layout the parser reasons about; .docx is accepted because it is what most
# people actually have.
SUPPORTED_RESUME_SUFFIXES = (".pdf", ".docx", ".txt", ".md")


class ResumeParser:
    """Multi-engine PDF resume parser."""

    def __init__(self, provider: Optional[str] = None):
        self.provider = (provider or settings.default_llm_provider or "").lower()

    # --- Stage 1: raw text -----------------------------------------------------

    def extract_text(self, document_path: Path) -> str:
        """Extract text from a resume in any supported format.

        Dispatches on the file extension: `.pdf` through pdfplumber (with a pypdf
        fallback), `.docx` through python-docx, and `.txt`/`.md` read directly.
        """
        if not document_path.exists():
            raise FileNotFoundError(f"Resume not found: {document_path}")

        suffix = document_path.suffix.lower()
        if suffix == ".pdf":
            return self.extract_text_from_pdf(document_path)
        if suffix == ".docx":
            return self._extract_from_docx(document_path)
        if suffix in (".txt", ".md"):
            text = document_path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                raise ValueError(f"{document_path.name} is empty.")
            return text
        if suffix == ".doc":
            raise ValueError(
                f"{document_path.name} is in the old .doc format, which cannot be read directly. "
                "Open it and save as .docx or export to PDF."
            )
        raise ValueError(
            f"Unsupported resume format '{suffix or document_path.name}'. "
            f"Supported: {', '.join(SUPPORTED_RESUME_SUFFIXES)}."
        )

    def extract_text_from_pdf(self, pdf_path: Path) -> str:
        """Extract text from a local PDF, preferring pdfplumber and falling back to pypdf."""
        if not pdf_path.exists():
            raise FileNotFoundError(f"Resume PDF not found: {pdf_path}")

        text = self._extract_with_pdfplumber(pdf_path) or self._extract_with_pypdf(pdf_path)

        if not text:
            raise ValueError(
                f"No extractable text found in PDF: {pdf_path.name}. "
                "This is usually a scanned or image-only PDF. Run it through OCR, "
                "or export a text-based PDF from the original document."
            )
        return text

    @staticmethod
    def _extract_from_docx(docx_path: Path) -> str:
        """Extract text from a Word .docx file.

        Table cells are included: a great many resume templates lay the whole
        document out in an invisible table, and reading only paragraphs would
        return almost nothing for those.
        """
        try:
            import docx
        except ImportError as exc:
            raise ValueError(
                "Reading .docx resumes needs python-docx. Install it with "
                "'pip install python-docx', or export your resume to PDF."
            ) from exc

        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        document = docx.Document(str(docx_path))
        lines: List[str] = []

        # Walk the body in document order. `document.paragraphs` and
        # `document.tables` are separate sequences, so reading them one after the
        # other moves every table to the end — which put role headers after the
        # skills section and left the experience section empty.
        for child in document.element.body.iterchildren():
            if child.tag == qn("w:p"):
                lines.append(Paragraph(child, document).text)
            elif child.tag == qn("w:tbl"):
                for row in Table(child, document).rows:
                    # One row becomes one line, so a right-aligned date stays
                    # beside the employer it belongs to.
                    cells: List[str] = []
                    for cell in row.cells:
                        value = cell.text.strip().replace("\n", " ")
                        # Word repeats a cell object across merged spans.
                        if value and (not cells or cells[-1] != value):
                            cells.append(value)
                    if cells:
                        lines.append("  ".join(cells))

        text = "\n".join(line.strip() for line in lines if line and line.strip())
        if not text:
            raise ValueError(
                f"No text found in {docx_path.name}. If the content is inside text "
                "boxes or images, export the document to PDF instead."
            )
        return text

    @staticmethod
    def _extract_with_pdfplumber(pdf_path: Path) -> str:
        """Extract page text with pdfplumber, returning '' if it is unavailable or fails.

        Uses column-aware extraction so a sidebar template is read column by
        column rather than line by line across the gutter.
        """
        try:
            import pdfplumber
        except ImportError:
            return ""
        try:
            from job_agent.intake.layout import extract_page_lines

            pages = []
            with pdfplumber.open(pdf_path) as pdf:
                for index, page in enumerate(pdf.pages, start=1):
                    lines = extract_page_lines(page)
                    if lines:
                        pages.append(f"--- PAGE {index} ---\n" + "\n".join(lines))
            return "\n\n".join(pages).strip()
        except Exception as exc:
            console.print(f"[yellow]pdfplumber extraction warning: {exc}. Trying pypdf...[/yellow]")
            return ""

    @staticmethod
    def _extract_with_pypdf(pdf_path: Path) -> str:
        """Extract page text with pypdf."""
        try:
            import pypdf

            pages = []
            reader = pypdf.PdfReader(str(pdf_path))
            for index, page in enumerate(reader.pages, start=1):
                page_text = page.extract_text()
                if page_text:
                    pages.append(f"--- PAGE {index} ---\n{page_text}")
            return "\n\n".join(pages).strip()
        except Exception as exc:
            raise RuntimeError(f"Failed to extract text from PDF: {exc}") from exc

    def parse_with_llamaparse(self, pdf_path: Path) -> Optional[str]:
        """Parse via LlamaParse when an API key is present, else return None."""
        if not settings.llama_cloud_api_key:
            return None
        try:
            console.print("[cyan]Attempting document extraction with LlamaParse...[/cyan]")
            from llama_parse import LlamaParse

            parser = LlamaParse(api_key=settings.llama_cloud_api_key, result_type="markdown", verbose=False)
            documents = parser.load_data(str(pdf_path))
            if documents:
                return "\n\n".join(doc.text for doc in documents)
        except Exception as exc:
            console.print(f"[yellow]LlamaParse unavailable: {exc}. Falling back to local extractor.[/yellow]")
        return None

    # --- Stage 2: structured extraction ---------------------------------------

    def _call_openai(self, resume_text: str) -> Dict[str, Any]:
        """Extract structured JSON via the OpenAI Chat Completions API in JSON mode."""
        from openai import OpenAI

        if self.provider == "openai_compatible":
            if not settings.openai_compatible_api_key or not settings.openai_compatible_base_url:
                raise ValueError("OPENAI_COMPATIBLE_API_KEY and OPENAI_COMPATIBLE_BASE_URL are required")
            client = OpenAI(
                api_key=settings.openai_compatible_api_key,
                base_url=settings.openai_compatible_base_url,
            )
            model = settings.openai_compatible_model
        else:
            if not settings.openai_api_key:
                raise ValueError("OPENAI_API_KEY is not configured in .env")
            client = OpenAI(api_key=settings.openai_api_key)
            model = settings.llm_intake_model
        response = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            temperature=0.0,
            messages=[
                {"role": "system", "content": SYSTEM_EXTRACTION_PROMPT},
                {"role": "user", "content": f"Here is the raw resume text:\n\n{resume_text}"},
            ],
        )
        return json.loads(response.choices[0].message.content)

    def _call_anthropic(self, resume_text: str) -> Dict[str, Any]:
        """Extract structured JSON via the Anthropic Messages API."""
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is not configured in .env")

        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=8000,
            temperature=0.0,
            system=SYSTEM_EXTRACTION_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Extract the structured profile JSON from this resume text. "
                        f"Return ONLY valid JSON:\n\n{resume_text}"
                    ),
                }
            ],
        )
        return _extract_json_object(response.content[0].text)

    def _llm_extract(self, resume_text: str) -> Optional[Dict[str, Any]]:
        """Run the configured LLM extractor, or return None if none is usable."""
        if self.provider == "groq":
            from job_agent.llm import groq_complete
            from pydantic import ValidationError
            prompt = resume_text + "\nPreserve year-only dates as YYYY strings. Copy all prose verbatim."
            baseline: Optional[Dict[str, Any]] = None
            try:
                baseline = build_profile_dict(resume_text, source_document="resume")
                prompt += "\nSource-derived draft to verify and complete (keep its valid field names and date formats):\n" + json.dumps(baseline)
            except ResumeParseError:
                pass
            for attempt in range(2):
                data = groq_complete(SYSTEM_EXTRACTION_PROMPT, prompt, max_tokens=8000)
                # Reconcile against the source *before* validating. Validating
                # the raw draft first meant a date the model omitted failed the
                # whole intake, even though the resume text supplied it — and the
                # retry spent a second model call asking for a fact already known.
                if baseline:
                    _reconcile_with_source(data, baseline)
                try:
                    CandidateProfile(**data)
                    return data
                except ValidationError as exc:
                    fields = [".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in exc.errors()]
                    if attempt:
                        raise ValueError("Groq extraction failed validation: " + "; ".join(fields)) from None
                    prompt += "\nCorrect these validation problems using only source evidence: " + "; ".join(fields)
        if self.provider == "openai" and settings.openai_api_key:
            console.print(f"[cyan]Executing LLM parsing with OpenAI ({settings.llm_intake_model})...[/cyan]")
            return self._call_openai(resume_text)
        if (
            self.provider == "openai_compatible"
            and settings.openai_compatible_api_key
            and settings.openai_compatible_base_url
        ):
            console.print(
                f"[cyan]Executing LLM parsing with OpenAI-compatible API "
                f"({settings.openai_compatible_model})...[/cyan]"
            )
            return self._call_openai(resume_text)
        if self.provider == "anthropic" and settings.anthropic_api_key:
            console.print(f"[cyan]Executing LLM parsing with Anthropic ({settings.anthropic_model})...[/cyan]")
            return self._call_anthropic(resume_text)
        return None

    # --- Orchestration --------------------------------------------------------

    @exclusive_run
    def parse(self, pdf_path: Path, output_path: Optional[Path] = None) -> CandidateProfile:
        """Run the full intake pipeline: PDF to sealed, fact-checked profile.json."""
        target_output = output_path or settings.profile_path
        console.print(f"[bold cyan]Starting candidate intake for:[/bold cyan] {pdf_path}")

        raw_text = self.parse_with_llamaparse(pdf_path) or self.extract_text(pdf_path)
        console.print(f"[green]OK[/green] Extracted {len(raw_text)} characters from resume.")

        profile_dict: Optional[Dict[str, Any]] = None
        method = "deterministic"

        try:
            llm_output = self._llm_extract(raw_text)
        except Exception as exc:
            if settings.llm_strict and self.provider not in ("offline", "none"):
                raise
            console.print(f"[yellow]LLM extraction failed: {exc}. Falling back to deterministic parser.[/yellow]")
            llm_output = None

        if llm_output is not None:
            if _is_useful_extraction(llm_output):
                profile_dict = llm_output
                method = f"{self.provider}:{settings.model_for('intake', self.provider)}"
            else:
                if settings.llm_strict:
                    raise ValueError("LLM extraction returned an unusable profile; intake stopped.")
                console.print(
                    "[yellow]LLM returned an empty or unusable profile. "
                    "Falling back to the deterministic parser.[/yellow]"
                )

        if profile_dict is None:
            console.print(
                "[yellow]Running the offline deterministic resume extractor "
                "(no LLM key configured, or LLM extraction was rejected).[/yellow]"
            )
            profile_dict = build_profile_dict(raw_text, source_document=pdf_path.name)

        profile_dict.setdefault("source_document", pdf_path.name)
        profile_dict["extraction_method"] = method

        # Groq drafts were already reconciled against the source inside
        # `_llm_extract`, before validation.

        # Validate, fact-check against the source text, seal, and persist.
        profile = validate_and_save_profile(profile_dict, target_output, source_text=raw_text)
        if target_output.resolve() == settings.profile_path.resolve():
            # Country, sponsorship and salary are not on a resume; the ones the
            # candidate saved earlier carry over to the new profile.
            from job_agent.intake.preferences import reapply_saved_preferences

            profile = reapply_saved_preferences(profile, target_output)
            invalidate_after("intake", settings.outputs_dir)
        import hashlib
        target_output.with_suffix(".source.json").write_text(json.dumps({
            "source_sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
            "profile_hash": profile.profile_hash,
        }, indent=2), encoding="utf-8")
        return profile


def _match_source_role(role: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find the one source-parsed role an LLM-extracted role corresponds to.

    Tries progressively looser matches and accepts a match only when it is
    unique, so an ambiguous pairing leaves the draft untouched rather than
    grafting one role's dates onto another. Containment handles the common case
    of a model trimming or expanding an employer name ("ScaleFlow" versus
    "ScaleFlow Technologies").
    """
    company = str(role.get("company") or "").casefold().strip()
    title = str(role.get("title") or "").casefold().strip()
    if not company:
        return None

    def related(left: str, right: str) -> bool:
        return bool(left) and bool(right) and (left in right or right in left)

    rules = (
        lambda item: item["company"].casefold() == company and item["title"].casefold() == title,
        lambda item: related(item["company"].casefold(), company) and related(item["title"].casefold(), title),
        lambda item: related(item["company"].casefold(), company),
    )
    for rule in rules:
        matches = [item for item in candidates if rule(item)]
        if len(matches) == 1:
            return matches[0]
    return None


def _reconcile_with_source(profile_dict: Dict[str, Any], baseline: Dict[str, Any]) -> None:
    """Overwrite source-verifiable fields of an LLM draft with the source's own values.

    Dates, bullets and locked facts are copied from the deterministic parse of
    the same resume text, so the model can neither manufacture month precision
    nor rewrite an achievement. Nothing here is invented: every value comes from
    the document. A role with no unique source match is left as the model wrote
    it, and validation then reports any gap honestly.
    """
    source_roles = baseline.get("experience", [])
    for role in profile_dict.get("experience") or []:
        if not isinstance(role, dict):
            continue
        match = _match_source_role(role, source_roles)
        if match:
            for field in ("start_date", "end_date", "description_bullets", "locked_facts"):
                if field in match:
                    role[field] = match[field]

    skills = profile_dict.get("skills")
    if not isinstance(skills, dict):
        skills = profile_dict["skills"] = {}
    for group, values in (baseline.get("skills") or {}).items():
        existing = skills.get(group) or []
        skills[group] = list(dict.fromkeys(list(existing) + list(values)))

    profile_dict["work_authorization"] = baseline["work_authorization"]
    profile_dict["years_of_experience"] = baseline["years_of_experience"]


def _extract_json_object(content: str) -> Dict[str, Any]:
    """Pull a JSON object out of a model response that may be fenced or prose-wrapped."""
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content)
    if fenced:
        return json.loads(fenced.group(1).strip())
    try:
        return json.loads(content.strip())
    except json.JSONDecodeError:
        # Last resort: the outermost brace-delimited span.
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(content[start : end + 1])


def _is_useful_extraction(data: Dict[str, Any]) -> bool:
    """Whether an extraction carries enough substance to be worth keeping.

    Guards against a well-formed but empty response, which would otherwise seal a
    profile containing nothing but a name.
    """
    if not isinstance(data, dict) or not data.get("contact"):
        return False
    populated = sum(
        1
        for key in ("experience", "education", "projects", "certifications")
        if data.get(key)
    )
    skills = data.get("skills") or {}
    if isinstance(skills, dict) and any(skills.values()):
        populated += 1
    return populated >= MIN_USEFUL_SECTIONS


__all__ = ["ResumeParser", "ResumeParseError", "SYSTEM_EXTRACTION_PROMPT"]
