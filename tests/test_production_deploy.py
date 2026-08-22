from pathlib import Path
import os
import sqlite3
import subprocess


def test_gunicorn_is_single_worker_and_loopback_only():
    settings = Path("deploy/gunicorn.conf.py").read_text()
    assert "workers = 1" in settings
    assert 'bind = f"127.0.0.1:' in settings
    assert "timeout = int(" in settings
    assert "graceful_timeout = int(" in settings


def test_backup_is_consistent_and_restrictive(tmp_path):
    database = tmp_path / "source.sqlite3"
    backups = tmp_path / "backups"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE sample(value TEXT)")
        db.execute("INSERT INTO sample VALUES ('preserved')")
    env = {**os.environ, "OPTIONS_SCANNER_DB": str(database),
           "OPTIONS_SCANNER_BACKUP_DIR": str(backups),
           "OPTIONS_SCANNER_BACKUP_RETENTION_DAYS": "1"}
    result = subprocess.run(["deploy/backup-sqlite.sh"], env=env, text=True,
                            capture_output=True, check=True)
    output = Path(result.stdout.strip())
    assert output.parent == backups
    assert output.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT value FROM sample").fetchone() == ("preserved",)
