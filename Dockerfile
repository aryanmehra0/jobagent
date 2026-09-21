FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ANONYMIZED_TELEMETRY=false \
    POSTHOG_DISABLED=1

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./

RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY config ./config
COPY templates ./templates
COPY data/raw_resumes/sample_resume.pdf ./data/raw_resumes/sample_resume.pdf
COPY main.py ./
COPY .gitignore ./

RUN python -m pip install --no-cache-dir --no-deps -e . \
    && python -m playwright install chromium

RUN mkdir -p data/outputs data/profiles data/browser_profile data/raw_resumes

VOLUME ["/app/data", "/app/config"]

CMD ["python", "main.py", "doctor"]
