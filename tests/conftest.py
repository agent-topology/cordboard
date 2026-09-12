from pathlib import Path
import os

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Same credential recipe as upstream v0.1.0-beta.1 bindings/python/tests/test_api.py.
# Private key is base64 of "SYNTHETIC_ONLY_NOT_A_REAL_KEY", never a usable key.
CREDENTIAL = "ghp_" + "SYNTHETICREVOKED00000000000000000000"
PRIVATE_KEY = ("-----BEGIN PRIVATE KEY-----\n"
               "U1lOVEhFVElDX09OTFlfTk9UX0FfUkVBTF9LRVk=\n"
               "-----END PRIVATE KEY-----")


@pytest.fixture
def cli():
    path = Path(os.environ.get("REDACT_SECRET_CLI", ROOT / ".tools/redact-secret-0.1.0-beta.1-aarch64-apple-darwin"))
    if not path.is_file():
        pytest.fail("pinned redact-secret CLI required; see docs/archive.md")
    return str(path.resolve())
