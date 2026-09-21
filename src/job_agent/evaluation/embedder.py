"""Tier 1 Semantic Evaluation: Lightweight Vector Embedding Filter.

Calculates cosine similarity between candidate profile representation and scraped
job postings using lightweight transformer embeddings (or TF-IDF fallback)
to rapidly weed out blatantly irrelevant listings before running LLM scoring.
"""

from __future__ import annotations

from typing import List, Tuple, Optional
import numpy as np
from rich.console import Console

from job_agent.config.settings import settings
from job_agent.config.schema import CandidateProfile, JobPosting

console = Console()


class SemanticEmbedder:
    """Lightweight vector embedding evaluator for Tier 1 candidate-job matching."""

    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or settings.semantic_embedding_model
        self._encoder = None
        self._use_fallback = False

    def _get_encoder(self):
        """Lazy loader for sentence-transformers model with TF-IDF fallback."""
        if self._use_fallback:
            return None
        if self._encoder is not None:
            return self._encoder

        try:
            from sentence_transformers import SentenceTransformer
            console.print(f"[dim]Loading embedding model: {self.model_name}...[/dim]")
            self._encoder = SentenceTransformer(self.model_name)
            return self._encoder
        except Exception as e:
            console.print(f"[yellow]SentenceTransformer loading note: {e}. Utilizing fast TF-IDF embedding engine.[/yellow]")
            self._use_fallback = True
            return None

    def candidate_to_text(self, profile: CandidateProfile) -> str:
        """Create a dense semantic text representation of the candidate profile.

        Job titles are repeated ahead of the prose because a posting's strongest
        signal is its title, and title-to-title similarity is what separates a
        near-miss role from an unrelated one.
        """
        skills_summary = (
            f"Languages: {', '.join(profile.skills.languages)}. "
            f"Frameworks: {', '.join(profile.skills.frameworks)}. "
            f"Developer Tools: {', '.join(profile.skills.developer_tools)}. "
            f"Cloud & DevOps: {', '.join(profile.skills.cloud_devops)}. "
            f"Domains: {', '.join(profile.skills.domain_knowledge)}."
        )

        titles = [exp.title for exp in profile.experience[:4]]
        exp_snippets = [
            f"{exp.title} at {exp.company}: {' '.join(exp.description_bullets[:2])}"
            for exp in profile.experience[:3]
        ]

        return (
            f"{'. '.join(titles)}.\n"
            f"{profile.summary}\n"
            f"Experience: {profile.years_of_experience:g} years.\n"
            f"Skills: {skills_summary}\n"
            f"Roles: {' | '.join(exp_snippets)}"
        )

    def job_to_text(self, job: JobPosting) -> str:
        """Standardize a job posting into dense text for embedding.

        The description is trimmed to bound memory and encode time; the title and
        company carry most of the signal and are always included in full.
        """
        # ATS feeds often begin with company boilerplate. Put requirements first
        # so transformer truncation and TF-IDF both see the actual role criteria.
        import re
        match = re.search(r"minimum requirements|what you.bring|qualifications|requirements|who you are",
                          job.description, flags=re.IGNORECASE)
        start = match.start() if match else 0
        context = job.description[start:start + 5000]
        return f"{job.title} at {job.company}. Location: {job.location}. {context}"

    def compute_similarity(self, candidate_text: str, job_texts: List[str]) -> List[float]:
        """Compute cosine similarity scores between candidate vector and job vectors."""
        if not job_texts:
            return []

        encoder = self._get_encoder()

        if not self._use_fallback and encoder is not None:
            try:
                candidate_vec = encoder.encode([candidate_text], normalize_embeddings=True)
                job_vecs = encoder.encode(job_texts, normalize_embeddings=True)
                # Dot product of normalized vectors = cosine similarity
                scores = np.dot(job_vecs, candidate_vec.T).flatten()
                return [float(max(0.0, min(1.0, s))) for s in scores]
            except Exception as e:
                console.print(f"[yellow]Transformer encode failed: {e}. Switching to TF-IDF vectorizer.[/yellow]")

        # Fallback: TfidfVectorizer
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        all_corpus = [candidate_text] + job_texts
        vectorizer = TfidfVectorizer(stop_words="english", max_features=1000)
        tfidf_matrix = vectorizer.fit_transform(all_corpus)
        cand_mat = tfidf_matrix[0:1]
        jobs_mat = tfidf_matrix[1:]

        sim_scores = cosine_similarity(cand_mat, jobs_mat).flatten()
        return [float(max(0.0, min(1.0, s))) for s in sim_scores]

    def filter_and_rank(
        self,
        profile: CandidateProfile,
        jobs: List[JobPosting],
        threshold: float = 0.20,
        top_k: Optional[int] = None,
    ) -> List[Tuple[JobPosting, float]]:
        """Filter out jobs scoring below similarity threshold, returned in descending order."""
        if not jobs:
            return []

        cand_text = self.candidate_to_text(profile)
        job_texts = [self.job_to_text(j) for j in jobs]
        scores = self.compute_similarity(cand_text, job_texts)

        ranked: List[Tuple[JobPosting, float]] = []
        for job, score in zip(jobs, scores):
            if score >= threshold:
                ranked.append((job, round(score, 4)))

        # Sort descending by score
        ranked.sort(key=lambda x: x[1], reverse=True)

        if top_k is not None:
            ranked = ranked[:top_k]

        return ranked

