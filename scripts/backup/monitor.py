"""Fail independently when scheduled backups or recovery drills are overdue."""
import json
import sys
from datetime import datetime, timezone
from common import Config


def check(c, now=None):
    now = now or datetime.now(timezone.utc)
    for filename, hours in [('last-backup.json', int(c.env.get('BACKUP_MAX_AGE_HOURS', '26'))),
                            ('last-drill.json', int(c.env.get('BACKUP_DRILL_MAX_AGE_HOURS', '192')))]:
        path = c.state / filename
        if not path.exists():
            raise RuntimeError('Missing recovery status: ' + filename)
        status = json.loads(path.read_text())
        if status.get('state', status.get('status')) != 'complete':
            raise RuntimeError('Recovery operation failed or unverified: ' + filename)
        stamp = status.get('finished_at') or status.get('completed_at')
        if not stamp:
            raise RuntimeError('Recovery status has no completion time')
        completed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if completed.tzinfo is None:
            raise RuntimeError('Recovery timestamp must include timezone')
        age = (now - completed).total_seconds()
        if age < -300 or age > hours * 3600:
            raise RuntimeError('Recovery operation overdue: ' + filename)


if __name__ == '__main__':
    try:
        check(Config())
        print('Backup and restore drill are current')
    except (RuntimeError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
