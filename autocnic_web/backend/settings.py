"""
settings.py
-----------
Runtime configuration, read once from environment variables. Defaults suit a
local run (`python main.py`); the Dockerfile overrides the ones a public
deployment needs.

  HOST / PORT                  bind address (default 0.0.0.0:8000)
  AUTOCNIC_DATA_DIR            where clones and the cert store live (default: backend/)
  AUTOCNIC_ALLOW_LOCAL_PATHS   1 = accept local folder paths and file:// URLs (default 1;
                               set 0 on a server, or anyone can read its filesystem)
  AUTOCNIC_BASIC_AUTH          "user:password" -- if set, every page and API call
                               requires HTTP Basic auth (except /healthz)
  AUTOCNIC_MAX_TRIALS          upper bound on n_trials_per_edge per analyze (default 1000)
  AUTOCNIC_MAX_LIVE_SESSIONS   in-memory sessions kept before the oldest is evicted
                               (its record stays on disk, read-only) (default 20)
  AUTOCNIC_CLONE_TIMEOUT       seconds before git clone is abandoned (default 60)
  AUTOCNIC_CORS_ORIGINS        comma-separated allowed origins (default: none; the
                               frontend is same-origin)
"""

import os

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
DATA_DIR = os.path.abspath(os.environ.get("AUTOCNIC_DATA_DIR", BACKEND_DIR))
ALLOW_LOCAL_PATHS = _bool("AUTOCNIC_ALLOW_LOCAL_PATHS", True)
BASIC_AUTH = os.environ.get("AUTOCNIC_BASIC_AUTH", "").strip()
MAX_TRIALS = int(os.environ.get("AUTOCNIC_MAX_TRIALS", "1000"))
MAX_LIVE_SESSIONS = int(os.environ.get("AUTOCNIC_MAX_LIVE_SESSIONS", "20"))
CLONE_TIMEOUT = int(os.environ.get("AUTOCNIC_CLONE_TIMEOUT", "60"))
CORS_ORIGINS = [o.strip() for o in os.environ.get("AUTOCNIC_CORS_ORIGINS", "").split(",") if o.strip()]
