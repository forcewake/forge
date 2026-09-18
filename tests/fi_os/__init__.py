"""Real-OS-process failure-injection helpers (R20).

Everything here is test-only infrastructure for
``tests/test_failure_injection_os.py``: real ``python -m forge.worker``
subprocesses, a canned-response LiteLLM HTTP stub and a fake GitLab HTTP
server, both reachable over 127.0.0.1 so the worker processes use the REAL
``LLMClient`` / ``GitLabClient`` wire paths. Nothing in this package runs in
production.
"""
