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
  AUTOCNIC_ALLOW_ANONYMOUS_REPOS
                               1 = accept any repo even without AUTOCNIC_BASIC_AUTH
                               (default 0: a server without a password only runs the
                               bundled demos, because analyzing a repo runs its code)
  AUTOCNIC_GIT_HOSTS           comma-separated hosts repos may be cloned from when local
                               paths are disabled (default github.com,gitlab.com,bitbucket.org;
                               "*" = any)
  AUTOCNIC_MAX_TRIALS          upper bound on n_trials_per_edge per analyze (default 1000)
  AUTOCNIC_MAX_LIVE_SESSIONS   live sessions (one worker process each) kept before the
                               oldest is evicted; its record stays on disk, read-only (default 8)
  AUTOCNIC_SESSION_IDLE_MINUTES  live sessions unused this long are evicted (default 60)
  AUTOCNIC_MAX_STORED_SESSIONS session records kept on disk; oldest are deleted (default 200)
  AUTOCNIC_MAX_CONCURRENT_ANALYSES  analyses allowed to run at once (default 2)
  AUTOCNIC_ANALYZE_TIMEOUT     seconds a project gets to import + run discovery (default 120)
  AUTOCNIC_UPDATE_TIMEOUT      seconds a project gets to process one update (default 30)
  AUTOCNIC_WORKER_MEMORY_MB    address-space limit per project process, Linux only (default 768)
  AUTOCNIC_MAX_REPO_MB         largest clone accepted (default 100)
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


def _list(name: str, default: str) -> list:
    return [x.strip().lower() for x in os.environ.get(name, default).split(",") if x.strip()]


HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
DATA_DIR = os.path.abspath(os.environ.get("AUTOCNIC_DATA_DIR", BACKEND_DIR))
ALLOW_LOCAL_PATHS = _bool("AUTOCNIC_ALLOW_LOCAL_PATHS", True)
BASIC_AUTH = os.environ.get("AUTOCNIC_BASIC_AUTH", "").strip()
if BASIC_AUTH and ":" not in BASIC_AUTH:
    raise SystemExit("AUTOCNIC_BASIC_AUTH must look like user:password")
# Local mode trusts whoever runs it; on a server, arbitrary repos need a password.
REPOS_ENABLED = ALLOW_LOCAL_PATHS or bool(BASIC_AUTH) or _bool("AUTOCNIC_ALLOW_ANONYMOUS_REPOS", False)
GIT_HOSTS = _list("AUTOCNIC_GIT_HOSTS", "github.com,gitlab.com,bitbucket.org")
MAX_TRIALS = int(os.environ.get("AUTOCNIC_MAX_TRIALS", "1000"))
MAX_LIVE_SESSIONS = int(os.environ.get("AUTOCNIC_MAX_LIVE_SESSIONS", "8"))
SESSION_IDLE_SECONDS = 60 * int(os.environ.get("AUTOCNIC_SESSION_IDLE_MINUTES", "60"))
MAX_STORED_SESSIONS = int(os.environ.get("AUTOCNIC_MAX_STORED_SESSIONS", "200"))
MAX_CONCURRENT_ANALYSES = int(os.environ.get("AUTOCNIC_MAX_CONCURRENT_ANALYSES", "2"))
ANALYZE_TIMEOUT = int(os.environ.get("AUTOCNIC_ANALYZE_TIMEOUT", "120"))
UPDATE_TIMEOUT = int(os.environ.get("AUTOCNIC_UPDATE_TIMEOUT", "30"))
WORKER_MEMORY_MB = int(os.environ.get("AUTOCNIC_WORKER_MEMORY_MB", "768"))
MAX_REPO_MB = int(os.environ.get("AUTOCNIC_MAX_REPO_MB", "100"))
CLONE_TIMEOUT = int(os.environ.get("AUTOCNIC_CLONE_TIMEOUT", "60"))
CORS_ORIGINS = [o.strip() for o in os.environ.get("AUTOCNIC_CORS_ORIGINS", "").split(",") if o.strip()]
