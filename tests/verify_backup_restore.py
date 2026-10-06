"""Verify same-version recovery only inside the disposable PostgreSQL fixture.

This helper is deliberately not a production restore command. Its only writable
DB target is gateway_restore_test in the runner's own temporary PostgreSQL data
directory. No database is dropped or overwritten. Raw rows and command output
are never returned, printed, or incorporated in errors. The dump is temporary.

Unit checks (no database connection):
    .venv/bin/python -m unittest tests.verify_backup_restore -v
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

SOURCE_DATABASE = "gateway_test"
RESTORE_DATABASE = "gateway_restore_test"
FIXTURE_USER = "gateway_test"
SCHEMAS = ("public", "gateway_ext")
MIGRATIONS = "public._prisma_migrations"
OBSERVATIONS = "gateway_ext.observations"
ACTIVE_KEYS = "public.LiteLLM_VerificationToken"
DELETED_KEYS = "public.LiteLLM_DeletedVerificationToken"


class RecoveryVerificationError(RuntimeError):
    """A sanitized failure that never contains SQL data or subprocess output."""


def _source_port(source_dsn: str) -> int:
    """Accept one explicit fixture URI, with no libpq parameter overrides."""
    try:
        parsed = urlsplit(source_dsn)
        port = parsed.port
        valid = (
            parsed.scheme in ("postgresql", "postgres")
            and parsed.hostname == "127.0.0.1"
            and parsed.username == FIXTURE_USER
            and parsed.password is None
            and parsed.path == "/" + SOURCE_DATABASE
            and not parsed.query
            and not parsed.fragment
            and port is not None
            and 0 < port <= 65535
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise RecoveryVerificationError(
            "Recovery verification requires an explicit password-free loopback "
            "gateway_test fixture URI with the gateway_test user and port."
        ) from None
    return port


def _run_binary(binary: Path, arguments: list[str], env: dict[str, str]) -> str:
    try:
        result = subprocess.run(
            [str(binary), *arguments], env=env, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RecoveryVerificationError(
            f"Recovery verification could not complete {binary.name}; output suppressed."
        ) from None
    if result.returncode:
        raise RecoveryVerificationError(
            f"Recovery verification failed at {binary.name}; output suppressed."
        ) from None
    return result.stdout


def _connect(port: int, database: str, temp_dir: Path):
    return psycopg.connect(
        host="127.0.0.1", hostaddr="127.0.0.1", port=port,
        dbname=database, user=FIXTURE_USER, password="",
        sslmode="disable", gssencmode="disable", connect_timeout=5,
        passfile=str(temp_dir / "unused-fixture-password-file"),
        client_encoding="UTF8", application_name="gateway-recovery-test",
        options="-c timezone=UTC -c datestyle=ISO -c extra_float_digits=3",
        autocommit=True,
    )


def _assert_fixture(connection, temp_dir: Path, expected_database: str) -> str:
    row = connection.execute(
        "SELECT current_database(), current_user, host(inet_server_addr()), "
        "current_setting('data_directory'), current_setting('server_version'), "
        "current_setting('server_encoding')"
    ).fetchone()
    if (
        not row
        or row[0] != expected_database
        or row[1] != FIXTURE_USER
        or row[2] != "127.0.0.1"
        or Path(row[3]).resolve() != temp_dir / "data"
        or not (temp_dir / "data" / "PG_VERSION").is_file()
        or row[5] != "UTF8"
        or not re.fullmatch(r"17\.\d+(?:\s.*)?", row[4])
    ):
        raise RecoveryVerificationError(
            "Refusing recovery verification outside the runner's UTF8 PostgreSQL 17 fixture."
        )
    return row[4]


def _table_manifest(connection) -> dict[str, dict[str, object]]:
    tables = connection.execute(
        "SELECT n.nspname, c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p') "
        "ORDER BY n.nspname COLLATE \"C\", c.relname COLLATE \"C\"",
        (list(SCHEMAS),),
    ).fetchall()
    manifest = {}
    for schema, name in tables:
        # SHA-256 is calculated inside PostgreSQL; only row hashes leave the DB.
        # Sorting hashes preserves duplicates but ignores physical row order.
        query = sql.SQL(
            "SELECT row_digest FROM (SELECT "
            "encode(sha256(convert_to(to_jsonb(t)::text, 'UTF8')), 'hex') "
            "AS row_digest FROM {} AS t) AS hashes "
            "ORDER BY row_digest COLLATE \"C\""
        ).format(sql.Identifier(schema, name))
        digest = hashlib.sha256()
        count = 0
        with connection.cursor(name="recovery_digest") as cursor:
            cursor.execute(query)
            for (row_digest,) in cursor:
                digest.update(bytes.fromhex(row_digest))
                count += 1
        manifest[f"{schema}.{name}"] = {"rows": count, "sha256": digest.hexdigest()}
    return manifest


def _migration_counts(connection) -> dict[str, int]:
    total, finished, unfinished = connection.execute(
        'SELECT count(*), count(*) FILTER (WHERE finished_at IS NOT NULL '
        'AND rolled_back_at IS NULL), count(*) FILTER (WHERE finished_at IS NULL '
        'AND rolled_back_at IS NULL) FROM public."_prisma_migrations"'
    ).fetchone()
    return {"total": total, "finished": finished, "unfinished": unfinished}


def _validate_source(manifest, migrations):
    for name in (MIGRATIONS, OBSERVATIONS, ACTIVE_KEYS, DELETED_KEYS):
        if name not in manifest:
            raise RecoveryVerificationError("Required fixture recovery tables are missing.")
    if not migrations["finished"] or migrations["unfinished"]:
        raise RecoveryVerificationError("The fixture has no completed migration baseline or has unfinished migrations.")
    if not manifest[OBSERVATIONS]["rows"]:
        raise RecoveryVerificationError("The fixture must contain observations to prove observation recovery.")


def _same_manifest(source, restored):
    if source != restored:
        raise RecoveryVerificationError("Restored table names, row counts, or content digests differ from the backup snapshot.")


def verify_backup_restore(
    source_dsn: str, postgres_bin: str | Path, temp_dir: str | Path,
) -> dict[str, object]:
    """Dump the test DB, restore a fresh sibling DB, and return safe evidence.

    Call only after the native Proxy tests have finished. ``temp_dir`` must be
    the existing runner-created fixture root containing its ``data`` directory.
    The restored DB intentionally remains in that disposable server until the
    runner shuts it down and deletes its temporary directory. This function does
    not touch any pre-existing gateway_restore_test database, even on failure.
    """
    port = _source_port(source_dsn)
    temporary_root = Path(temp_dir).resolve(strict=True)
    if not temporary_root.is_dir() or not (temporary_root / "data").is_dir():
        raise RecoveryVerificationError("The disposable fixture root is missing its data directory.")
    binaries = {name: Path(postgres_bin).resolve() / name for name in ("pg_dump", "createdb", "pg_restore")}
    if any(not path.is_file() or not os.access(path, os.X_OK) for path in binaries.values()):
        raise RecoveryVerificationError("Official pg_dump, createdb, and pg_restore binaries are required.")
    # Environment variables must not override the loopback target or leak a real
    # password into this isolated trust-authenticated fixture process.
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env.update(PGCLIENTENCODING="UTF8", PGCONNECT_TIMEOUT="5", PGSSLMODE="disable", PGGSSENCMODE="disable", PGPASSFILE=str(temporary_root / "unused-fixture-password-file"))
    connection_args = ["--host=127.0.0.1", f"--port={port}", f"--username={FIXTURE_USER}", "--no-password"]
    started = time.monotonic()
    try:
        with _connect(port, SOURCE_DATABASE, temporary_root) as source:
            server_version = _assert_fixture(source, temporary_root, SOURCE_DATABASE)
            if source.execute("SELECT 1 FROM pg_database WHERE datname = %s", (RESTORE_DATABASE,)).fetchone():
                raise RecoveryVerificationError("The fixed restore database already exists; refusing to overwrite it.")
            versions = {}
            for name, binary in binaries.items():
                match = re.search(r"\b(PostgreSQL)\) (\d+\.\d+)", _run_binary(binary, ["--version"], env))
                if not match or match.group(2) != server_version.split()[0]:
                    raise RecoveryVerificationError("Recovery binaries must match the fixture's exact PostgreSQL version.")
                versions[name] = match.group(2)
            source.autocommit = False
            source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            snapshot = source.execute("SELECT pg_export_snapshot()").fetchone()[0]
            source_manifest = _table_manifest(source)
            source_migrations = _migration_counts(source)
            _validate_source(source_manifest, source_migrations)
            with tempfile.TemporaryDirectory(prefix="recovery-", dir=temporary_root) as work:
                fd, archive_name = tempfile.mkstemp(prefix="native-", suffix=".dump", dir=work)
                os.close(fd)  # mkstemp creates a private 0600 file.
                _run_binary(binaries["pg_dump"], [
                    *connection_args, f"--dbname={SOURCE_DATABASE}", "--format=custom",
                    f"--snapshot={snapshot}", f"--file={archive_name}",
                ], env)
                archive_bytes = Path(archive_name).stat().st_size
                if archive_bytes == 0:
                    raise RecoveryVerificationError("The custom-format fixture backup is empty.")
                _run_binary(binaries["createdb"], [
                    *connection_args, f"--maintenance-db={SOURCE_DATABASE}",
                    "--template=template0", "--encoding=UTF8", RESTORE_DATABASE,
                ], env)
                # Real restore failure: a late duplicate table forces pg_restore to
                # abort after earlier DDL. Its single transaction must retain only
                # this pre-existing sentinel and undo all earlier restore writes.
                with _connect(port, RESTORE_DATABASE, temporary_root) as trial:
                    _assert_fixture(trial, temporary_root, RESTORE_DATABASE)
                    trial.execute('CREATE TABLE public."_prisma_migrations" (fixture_sentinel integer)')
                failed_restore_rolled_back = False
                try:
                    _run_binary(binaries["pg_restore"], [
                        *connection_args, f"--dbname={RESTORE_DATABASE}",
                        "--exit-on-error", "--single-transaction", archive_name,
                    ], env)
                except RecoveryVerificationError:
                    with _connect(port, RESTORE_DATABASE, temporary_root) as trial:
                        tables = trial.execute("SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('public','gateway_ext')").fetchall()
                        if tables != [("public", "_prisma_migrations")]:
                            raise RecoveryVerificationError("Failed transactional restore retained partial schema writes.")
                        trial.execute('DROP TABLE public."_prisma_migrations"')
                    failed_restore_rolled_back = True
                if not failed_restore_rolled_back:
                    raise RecoveryVerificationError("Expected disposable restore conflict did not fail.")
                _run_binary(binaries["pg_restore"], [
                    *connection_args, f"--dbname={RESTORE_DATABASE}",
                    "--exit-on-error", "--single-transaction", archive_name,
                ], env)
                with _connect(port, RESTORE_DATABASE, temporary_root) as restored:
                    if _assert_fixture(restored, temporary_root, RESTORE_DATABASE) != server_version:
                        raise RecoveryVerificationError("Restored server version differs from the source.")
                    restored.autocommit = False
                    restored.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    restored_manifest = _table_manifest(restored)
                    restored_migrations = _migration_counts(restored)
                _same_manifest(source_manifest, restored_manifest)
                if source_migrations != restored_migrations:
                    raise RecoveryVerificationError("Restored migration counts differ from the source snapshot.")
        return {
            "status": "passed",
            "scope": "same-version disposable native PostgreSQL database restore",
            "postgresql_version": server_version,
            "tool_versions": versions,
            "backup_format": "custom",
            "backup_bytes": archive_bytes,
            "backup_deleted": not Path(archive_name).exists(),
            "source_snapshot_consistent": True,
            "restore_transactional": True,
            "actual_failed_restore_rolled_back_all_partial_ddl": failed_restore_rolled_back,
            "schema_names": list(SCHEMAS),
            "table_count": len(source_manifest),
            "total_rows": sum(table["rows"] for table in source_manifest.values()),
            "migration_counts": source_migrations,
            "tables_identical": True,
            "observations_retained": True,
            "observation_rows": source_manifest[OBSERVATIONS]["rows"],
            "native_active_key_rows": source_manifest[ACTIVE_KEYS]["rows"],
            "native_deleted_key_rows": source_manifest[DELETED_KEYS]["rows"],
            "native_key_tables_identical": True,
            "table_manifest": source_manifest,
            "manifest_sha256": hashlib.sha256(json.dumps(source_manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "excludes": ["Docker/Compose deployment", "configuration rollback", "cross-version migration", "restored proxy authentication", "real provider credentials"],
        }
    except psycopg.Error:
        raise RecoveryVerificationError("Recovery verification database operation failed; SQL details suppressed.") from None


class RecoveryGuardTests(unittest.TestCase):
    """Offline guards, including non-destructive failure paths."""

    def test_accepts_explicit_disposable_loopback_uri(self):
        self.assertEqual(_source_port("postgresql://gateway_test@127.0.0.1:6543/gateway_test"), 6543)

    def test_rejects_other_targets_and_overrides(self):
        for dsn in (
            "postgresql://gateway_test@production:6543/gateway_test",
            "postgresql://gateway_test@localhost:6543/gateway_test",
            "postgresql://gateway_test@127.0.0.1:6543/gateway",
            "postgresql://admin@127.0.0.1:6543/gateway_test",
            "postgresql://gateway_test:secret@127.0.0.1:6543/gateway_test",
            "postgresql://gateway_test@127.0.0.1:6543/gateway_test?host=production",
            "postgresql://gateway_test@127.0.0.1:6543/gateway_test#fragment",
            "postgresql://gateway_test@127.0.0.1/gateway_test",
            "postgresql://gateway_test@127.0.0.1:0/gateway_test",
            "postgresql://gateway_test@127.0.0.1:99999/gateway_test",
            "host=127.0.0.1 dbname=gateway_test",
            "",
        ):
            with self.subTest(dsn=dsn), self.assertRaises(RecoveryVerificationError):
                _source_port(dsn)

    def test_changed_table_content_is_rejected_even_when_count_matches(self):
        with self.assertRaises(RecoveryVerificationError):
            _same_manifest({"table": {"rows": 1, "sha256": "a"}}, {"table": {"rows": 1, "sha256": "b"}})

    def test_missing_table_is_rejected(self):
        with self.assertRaises(RecoveryVerificationError):
            _same_manifest({"table": {"rows": 0, "sha256": "a"}}, {})

    def test_equal_manifests_pass(self):
        manifest = {"table": {"rows": 1, "sha256": "a"}}
        self.assertIsNone(_same_manifest(manifest, dict(manifest)))

    def test_required_nonempty_observations(self):
        manifest = {name: {"rows": 0} for name in (MIGRATIONS, OBSERVATIONS, ACTIVE_KEYS, DELETED_KEYS)}
        with self.assertRaises(RecoveryVerificationError):
            _validate_source(manifest, {"finished": 189, "unfinished": 0})

    def test_command_errors_do_not_include_stderr_or_args(self):
        from unittest.mock import patch
        completed = subprocess.CompletedProcess(["ignored"], 1, "secret-output", "secret-error")
        with patch("subprocess.run", return_value=completed), self.assertRaises(RecoveryVerificationError) as result:
            _run_binary(Path("pg_restore"), ["secret-argument"], {})
        self.assertNotIn("secret", str(result.exception))

    def test_server_data_directory_must_match_runner_root(self):
        from unittest.mock import MagicMock
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = (
            SOURCE_DATABASE, FIXTURE_USER, "127.0.0.1", "/other/data", "17.11", "UTF8",
        )
        with self.assertRaises(RecoveryVerificationError):
            _assert_fixture(connection, Path("/expected"), SOURCE_DATABASE)
        self.assertIn("host(inet_server_addr())", connection.execute.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
