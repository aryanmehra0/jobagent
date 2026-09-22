"""Run tests with provider secrets removed and local .env loading disabled."""
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
environment = {key: value for key, value in os.environ.items()
               if not any(word in key.upper() for word in ('API_KEY', 'APP_PASSWORD', 'DATABASE_URL', 'HOSTED_API_TOKEN'))}
environment.update(JOB_AGENT_LOAD_DOTENV='0', DEFAULT_LLM_PROVIDER='none')
raise SystemExit(subprocess.call([sys.executable, '-m', 'pytest', '-ra', *sys.argv[1:]], cwd=root, env=environment))
