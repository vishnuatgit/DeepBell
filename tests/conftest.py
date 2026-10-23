"""Point the app at temporary directories and disable background threads before import."""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="deepbell-tests-")
os.environ["DEEPBELL_DATA_DIR"] = os.path.join(_tmp, "data")
os.environ["DEEPBELL_BACKUP_DIR"] = os.path.join(_tmp, "backups")
os.environ["DEEPBELL_REPORTS_DIR"] = os.path.join(_tmp, "reports")
os.environ["DEEPBELL_ENV_FILE"] = os.path.join(_tmp, ".env")
os.environ["DEEPBELL_DISABLE_WORKERS"] = "1"
# An empty value stops python-dotenv from loading a real key from a local .env file.
os.environ["GOOGLE_API_KEY"] = ""
