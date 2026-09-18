"""Test-suite defaults: force deterministic, offline-friendly settings
regardless of the outer shell environment."""

import os
import warnings

warnings.filterwarnings(
    "ignore",
    message=r"Using `httpx` with `starlette\.testclient` is deprecated.*",
    category=DeprecationWarning,
)

os.environ["PAPERPILOT_RETRIEVAL_EMBEDDING"] = "hash"
os.environ["PAPERPILOT_CHAT_LLM"] = "0"
os.environ["PAPERPILOT_JUDGE_LLM"] = "0"
os.environ["PAPERPILOT_ROUTER_LLM"] = "0"
os.environ.setdefault("PAPERPILOT_TEST_OFFLINE", "1")

if os.getenv("PAPERPILOT_TEST_OFFLINE", "1") != "0":
    from .offline_guard import install_offline_test_guard

    _restore_offline_test_guard = install_offline_test_guard()
