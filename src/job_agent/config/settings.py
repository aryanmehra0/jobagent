"""Settings and environment management with built-in privacy safeguards.

Values come from `.env` (or the process environment) and are validated on load, so
a typo like `MIN_MATCH_SCORE=seven` fails at startup with a clear message rather
than halfway through a scraping sweep.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

# Ensure safe UTF-8 output on Windows PowerShell / Command Prompt.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Base project directory.
BASE_DIR = Path(__file__).resolve().parents[3]

# Pre-load .env file if it exists.
dotenv_path = BASE_DIR / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path)

# Enforce telemetry shutdown immediately and silence the HF symlink warning on Windows.
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["POSTHOG_DISABLED"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

SUPPORTED_PROVIDERS = ("openai", "anthropic", "groq", "openai_compatible", "none")


class Settings(BaseSettings):
    """Global configuration for the autonomous job search agent."""

    model_config = SettingsConfigDict(
        env_file=str(dotenv_path),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Privacy & telemetry safeguard ---
    anonymized_telemetry: bool = Field(
        default=False,
        validation_alias="ANONYMIZED_TELEMETRY",
        description="Strictly disable external telemetry tracking",
    )

    # --- LLM configuration ---
    default_llm_provider: str = Field(default="openai", validation_alias="DEFAULT_LLM_PROVIDER")
    openai_api_key: Optional[str] = Field(default=None, validation_alias="OPENAI_API_KEY")
    anthropic_api_key: Optional[str] = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    groq_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="GROQ_API_KEY", repr=False)
    groq_api_keys: SecretStr = Field(default=SecretStr(""), validation_alias="GROQ_API_KEYS", repr=False)
    groq_model: str = Field(default="openai/gpt-oss-120b", validation_alias="GROQ_MODEL")
    # Groq's free tier caps tokens per day per model. When the main model's
    # daily allowance is used up, requests move to this model (which has its
    # own allowance) instead of stopping the run. Empty disables the fallback.
    groq_fallback_model: str = Field(default="", validation_alias="GROQ_FALLBACK_MODEL")
    groq_timeout: float = Field(default=45, ge=1, le=120, validation_alias="GROQ_TIMEOUT")
    openai_compatible_api_key: Optional[str] = Field(default=None, validation_alias="OPENAI_COMPATIBLE_API_KEY")
    openai_compatible_base_url: Optional[str] = Field(default=None, validation_alias="OPENAI_COMPATIBLE_BASE_URL")
    openai_compatible_model: str = Field(default="gpt-4o-mini", validation_alias="OPENAI_COMPATIBLE_MODEL")
    llm_strict: bool = Field(default=False, validation_alias="LLM_STRICT")

    llm_intake_model: str = Field(default="gpt-4o", validation_alias="LLM_INTAKE_MODEL")
    llm_rerank_model: str = Field(default="gpt-4o", validation_alias="LLM_RERANK_MODEL")
    llm_tailor_model: str = Field(default="gpt-4o", validation_alias="LLM_TAILOR_MODEL")
    anthropic_model: str = Field(default="claude-sonnet-5", validation_alias="ANTHROPIC_MODEL")

    # --- Document parsing ---
    llama_cloud_api_key: Optional[str] = Field(default=None, validation_alias="LLAMA_CLOUD_API_KEY")

    # --- Omnichannel sourcing & proxies ---
    residential_proxy_url: Optional[str] = Field(default=None, validation_alias="RESIDENTIAL_PROXY_URL")

    # --- Contact discovery ---
    hunter_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="HUNTER_API_KEY", repr=False)

    # --- Hosted / database configuration ---
    database_url: Optional[str] = Field(default=None, validation_alias="DATABASE_URL")

    # --- Semantic evaluation ---
    semantic_embedding_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        validation_alias="SEMANTIC_EMBEDDING_MODEL",
    )
    min_match_score: float = Field(default=7.0, ge=0.0, le=10.0, validation_alias="MIN_MATCH_SCORE")
    tier1_threshold: float = Field(default=0.15, ge=0.0, le=1.0, validation_alias="TIER1_THRESHOLD")

    # --- Browser automation safeguards ---
    use_vision: bool = Field(default=False, validation_alias="USE_VISION")
    # "auto": tailor the candidate's own PDF when its layout can be read, and
    # generate a resume from the profile otherwise. "faithful" requires the
    # original PDF; "generated" always builds from the profile.
    tailoring_mode: str = Field(default="auto", validation_alias="TAILORING_MODE")
    playwright_headless: bool = Field(default=False, validation_alias="PLAYWRIGHT_HEADLESS")
    playwright_wait_strategy: str = Field(default="networkidle", validation_alias="PLAYWRIGHT_WAIT_STRATEGY")
    max_application_steps: int = Field(default=25, ge=1, le=200, validation_alias="MAX_APPLICATION_STEPS")
    capsolver_api_key: Optional[str] = Field(default=None, validation_alias="CAPSOLVER_API_KEY")
    require_apply_confirmation: bool = Field(
        default=True,
        validation_alias="REQUIRE_APPLY_CONFIRMATION",
        description="Pause for explicit consent before the first live (non-dry-run) submission",
    )

    # --- Core directories and paths ---
    base_dir: Path = BASE_DIR
    data_dir: Path = BASE_DIR / "data"
    raw_resumes_dir: Path = BASE_DIR / "data" / "raw_resumes"
    profiles_dir: Path = BASE_DIR / "data" / "profiles"
    browser_profile_dir: Path = BASE_DIR / "data" / "browser_profile"
    outputs_dir: Path = BASE_DIR / "data" / "outputs"
    templates_dir: Path = BASE_DIR / "templates"

    profile_path: Path = BASE_DIR / "data" / "profiles" / "profile.json"
    searches_path: Path = (
        BASE_DIR / "config" / "searches.yaml"
        if (BASE_DIR / "config" / "searches.yaml").exists()
        else BASE_DIR / "searches.yaml"
    )
    tracker_path: Path = BASE_DIR / "data" / "outputs" / "applications_tracker.xlsx"

    @field_validator("default_llm_provider", mode="before")
    @classmethod
    def _validate_provider(cls, value: object) -> str:
        """Reject an unrecognized provider instead of silently disabling every LLM stage."""
        provider = str(value or "openai").strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"DEFAULT_LLM_PROVIDER must be one of {', '.join(SUPPORTED_PROVIDERS)}, got {value!r}"
            )
        return provider

    @field_validator("playwright_wait_strategy", mode="before")
    @classmethod
    def _validate_wait_strategy(cls, value: object) -> str:
        """Only Playwright's own load states are valid here."""
        strategy = str(value or "networkidle").strip().lower()
        valid = {"load", "domcontentloaded", "networkidle", "commit"}
        if strategy not in valid:
            raise ValueError(
                f"PLAYWRIGHT_WAIT_STRATEGY must be one of {', '.join(sorted(valid))}, got {value!r}"
            )
        return strategy

    @field_validator(
        "openai_api_key", "anthropic_api_key", "openai_compatible_api_key",
        "llama_cloud_api_key", "capsolver_api_key", mode="before"
    )
    @classmethod
    def _blank_placeholder_keys(cls, value: object) -> Optional[str]:
        """Treat an unedited `.env.example` placeholder as 'not configured'.

        Otherwise the literal string `your_openai_api_key_here` reaches the API
        client and produces a confusing 401 instead of a clean fallback.
        """
        text = str(value or "").strip()
        if not text or text.lower().startswith("your_") or text.lower().endswith("_here"):
            return None
        return text

    @field_validator("residential_proxy_url", mode="before")
    @classmethod
    def _blank_empty_proxy(cls, value: object) -> Optional[str]:
        text = str(value or "").strip()
        return text or None

    @field_validator("database_url", mode="before")
    @classmethod
    def _blank_empty_database_url(cls, value: object) -> Optional[str]:
        text = str(value or "").strip()
        return text or None

    @field_validator("openai_compatible_base_url", mode="before")
    @classmethod
    def _blank_empty_base_url(cls, value: object) -> Optional[str]:
        text = str(value or "").strip()
        return text or None

    # --- Derived helpers ------------------------------------------------------

    @property
    def groq_keys(self) -> list[str]:
        value = self.groq_api_key.get_secret_value() + "," + self.groq_api_keys.get_secret_value()
        return list(dict.fromkeys(k for k in re.split(r"[,;\s]+", value) if k and not k.startswith("your_")))

    def model_for(self, stage: str, provider: Optional[str] = None) -> str:
        provider = provider or self.active_provider
        if provider == "groq":
            return self.groq_model
        if provider == "anthropic":
            return self.anthropic_model
        if provider == "openai_compatible":
            return self.openai_compatible_model
        return getattr(self, f"llm_{stage}_model")

    @property
    def active_provider(self) -> str:
        """The provider that actually has a usable key, or 'none'.

        Every LLM-backed stage checks this instead of testing keys itself, so the
        fallback behaviour is identical across phases.
        """
        if self.default_llm_provider == "openai" and self.openai_api_key:
            return "openai"
        if self.default_llm_provider == "anthropic" and self.anthropic_api_key:
            return "anthropic"
        if self.default_llm_provider == "groq" and self.groq_keys:
            return "groq"
        if (
            self.default_llm_provider == "openai_compatible"
            and self.openai_compatible_api_key
            and self.openai_compatible_base_url
        ):
            return "openai_compatible"
        return "none"

    def ensure_directories(self) -> None:
        """Create the directories the pipeline writes into."""
        for path in (
            self.data_dir,
            self.raw_resumes_dir,
            self.profiles_dir,
            self.browser_profile_dir,
            self.outputs_dir,
            self.templates_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_directories()
