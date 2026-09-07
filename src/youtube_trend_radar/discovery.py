"""Bounded follow-up of established repositories; search activity is not momentum."""
from __future__ import annotations

from datetime import datetime, timedelta

from youtube_trend_radar.models import isoformat


DISCOVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_repositories (
    repo_full_name TEXT PRIMARY KEY, admitted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL, last_attempt_at TEXT
);
"""


class RepositoryDiscovery:
    def __init__(self, database):
        self.database = database

    def prepare(self, config, now: datetime) -> list[str]:
        """Expire fixed observation windows; failures also consume a fair follow-up slot."""
        with self.database.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            cx.execute('DELETE FROM discovery_repositories WHERE expires_at<=?', (isoformat(now),))
            for repo in config.github.get('watched_repositories', []):
                cx.execute('DELETE FROM discovery_repositories WHERE repo_full_name=?', (str(repo).lower(),))
            # Lowering the cap takes effect immediately, preserving older admissions.
            cx.execute('''DELETE FROM discovery_repositories WHERE repo_full_name NOT IN
                          (SELECT repo_full_name FROM discovery_repositories ORDER BY admitted_at,repo_full_name LIMIT ?)''',
                       (config.github.get('established_tracking_limit', 20),))
            rows = cx.execute('''SELECT repo_full_name FROM discovery_repositories
                                 ORDER BY COALESCE(last_attempt_at,admitted_at),repo_full_name LIMIT ?''',
                              (config.github.get('established_followup_per_scan', 10),)).fetchall()
            for row in rows:
                cx.execute('UPDATE discovery_repositories SET last_attempt_at=? WHERE repo_full_name=?', (isoformat(now), row[0]))
        return [row[0] for row in rows]

    def admit(self, repo: str, config, now: datetime) -> bool:
        with self.database.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            if cx.execute('SELECT 1 FROM discovery_repositories WHERE repo_full_name=?', (repo,)).fetchone():
                return True
            if cx.execute('SELECT COUNT(*) FROM discovery_repositories').fetchone()[0] >= config.github.get('established_tracking_limit', 20):
                return False
            # A re-admission starts a new measurement window, not growth across an unobserved gap.
            cx.execute('DELETE FROM growth_checkpoints WHERE source_key=?',
                       (f'github_explore:github_repository_snapshot:{repo}',))
            cx.execute('INSERT INTO discovery_repositories VALUES (?,?,?,?)',
                       (repo, isoformat(now), isoformat(now + timedelta(days=config.github.get('established_tracking_days', 14))), isoformat(now)))
        return True

    def remove(self, repo: str) -> None:
        with self.database.connect() as cx:
            cx.execute('DELETE FROM discovery_repositories WHERE repo_full_name=?', (repo,))

    def count(self) -> int:
        with self.database.connect() as cx:
            return cx.execute('SELECT COUNT(*) FROM discovery_repositories').fetchone()[0]
