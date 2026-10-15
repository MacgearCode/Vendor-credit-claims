"""Point every test at a throwaway SQLite file before any app module is imported.

app/db.py binds its engine at import time, so this has to run first — and it
keeps tests that import app.main from creating tables in the live data/app.db.
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="reporting-tests-")
os.environ["APP_DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["APP_PASSWORD"] = ""          # no legacy shared login in tests
os.environ.pop("REPORTING_DB_URL", None)  # tests never reach the real warehouse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
