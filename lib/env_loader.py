"""Environment variable loader for OpenMontage.

Loads .env file and provides typed access to environment configuration.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


def load_env(project_root: Optional[Path] = None) -> None:
    """Load .env file from project root.

    Values that start with ``dummy-`` are placeholders in the checkout template
    and are not applied, so tools stay unavailable until a real key is pasted.
    """
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    for key, value in list(os.environ.items()):
        if isinstance(value, str) and value.startswith("dummy-"):
            os.environ.pop(key, None)


def get_env(key: str, default: Optional[str] = None) -> Optional[str]:
    """Get an environment variable with optional default."""
    return os.environ.get(key, default)


def require_env(key: str) -> str:
    """Get a required environment variable. Raises if missing."""
    value = os.environ.get(key)
    if value is None:
        raise EnvironmentError(f"Required environment variable {key!r} is not set")
    return value
