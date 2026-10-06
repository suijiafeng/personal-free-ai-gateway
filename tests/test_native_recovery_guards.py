"""Offline guards, not native process rehearsal evidence."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.verify_backup_restore import RecoveryVerificationError
from tests.verify_native_recovery import NativeProcess, require, restored_dsn


def test_restore_database_is_fixed_sibling_of_exact_fixture():
    assert restored_dsn("postgresql://gateway_test@127.0.0.1:6543/gateway_test") == "postgresql://gateway_test@127.0.0.1:6543/gateway_restore_test"


@pytest.mark.parametrize("dsn", [
    "postgresql://gateway_test@production:6543/gateway_test",
    "postgresql://gateway_test@127.0.0.1:6543/gateway",
    "postgresql://gateway_test:secret@127.0.0.1:6543/gateway_test",
    "postgresql://gateway_test@127.0.0.1:6543/gateway_test?host=production",
])
def test_native_rehearsal_rejects_other_targets(dsn):
    with pytest.raises(RecoveryVerificationError):
        restored_dsn(dsn)


def test_failed_rehearsal_does_not_claim_success():
    with pytest.raises(RecoveryVerificationError, match="stage; details suppressed"):
        require(False, "stage")


def test_http_failure_omits_credentials_and_exception_text():
    import httpx
    proxy = NativeProcess(Path("unused"), "unused", Path("unused"), Path("unused"))
    proxy.client = MagicMock()
    proxy.client.request.side_effect = httpx.ConnectError("synthetic-secret-that-must-not-escape")
    with pytest.raises(RecoveryVerificationError) as error:
        proxy.request("GET", "/v1/models", "synthetic-secret")
    assert "synthetic-secret" not in str(error.value)
