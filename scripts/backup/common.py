"""Private backup configuration, Docker operations and integrity contracts."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
HELPER_IMAGE = 'pgvector/pgvector:0.8.0-pg17@sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87'


def read_env(path):
    """Read assignments without executing shell expressions."""
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', key):
            raise ValueError('Invalid configuration assignment')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def write_json(path, data):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def checksums(path):
    result = {}
    for file in sorted(Path(path).rglob('*')):
        if file.is_symlink():
            raise ValueError('Bundle symlinks are forbidden')
        if file.is_file() and file != Path(path) / 'manifest.json':
            with file.open('rb') as stream:
                result[str(file.relative_to(path))] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


def validate_bundle(path):
    path = Path(path)
    manifest = json.loads((path / 'manifest.json').read_text())
    if manifest.get('schema_version') != 1 or manifest.get('state') != 'complete':
        raise ValueError('Unsupported or incomplete backup manifest')
    if not manifest.get('checksums') or checksums(path) != manifest['checksums']:
        raise ValueError('Backup checksum mismatch')
    for name in manifest.get('volumes', []):
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
            raise ValueError('Unsafe volume name')
        with tarfile.open(path / 'volumes' / (name + '.tar')) as archive:
            for member in archive:
                target = Path(member.name)
                if target.is_absolute() or '..' in target.parts or member.isdev() or member.isfifo():
                    raise ValueError('Unsafe volume archive member')
                if member.issym() or member.islnk():
                    link = Path(member.linkname)
                    if link.is_absolute() or '..' in link.parts:
                        raise ValueError('Unsafe archive link')
    for dump in manifest.get('dumps', []):
        file = Path(dump['file'])
        if file.is_absolute() or '..' in file.parts or not (path / file).is_file():
            raise ValueError('Invalid database dump')
    return manifest


class Config:
    def __init__(self, root=None, env=None):
        os.umask(0o077)
        self.root = Path(root or ROOT).resolve()
        self.env = dict(os.environ if env is None else env)
        config = self.env.get('BACKUP_CONFIG')
        if config:
            self.env.update(read_env(config))
        self.state = Path(self.env.get('BACKUP_STATE_DIR', str(self.root / '.backup-state'))).resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        self.compose = ['docker', 'compose', '--project-directory', str(self.root)]
        for name in self.env.get('BACKUP_COMPOSE_FILES', 'compose.yaml').split(':'):
            self.compose.extend(['-f', str(self.root / name)])
        self._inventory = None

    def run(self, args, input=None):
        try:
            result = subprocess.run(args, input=input, stdin=subprocess.DEVNULL if input is None else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=self.root, env=self.env, check=False,
                                    timeout=int(self.env.get('BACKUP_COMMAND_TIMEOUT', '1800')))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError('Backup command unavailable or timed out') from error
        if result.returncode:
            # Database tools can include connection credentials in stderr.
            raise RuntimeError('Backup command failed: ' + Path(args[0]).name)
        return result.stdout

    def run_to_file(self, args, destination):
        """Stream large artifacts to disk instead of holding them in RAM."""
        try:
            with Path(destination).open('wb') as stream:
                result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.PIPE,
                                        cwd=self.root, env=self.env, check=False,
                                        timeout=int(self.env.get('BACKUP_COMMAND_TIMEOUT', '1800')))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError('Backup command unavailable or timed out') from error
        if result.returncode:
            raise RuntimeError('Backup command failed: ' + Path(args[0]).name)

    def run_from_file(self, args, source):
        try:
            with Path(source).open('rb') as stream:
                result = subprocess.run(args, stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        cwd=self.root, env=self.env, check=False,
                                        timeout=int(self.env.get('BACKUP_COMMAND_TIMEOUT', '1800')))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError('Restore command unavailable or timed out') from error
        if result.returncode:
            raise RuntimeError('Restore command failed: ' + Path(args[0]).name)

    def docker(self, service, args, input=None):
        return self.run(self.compose + ['exec', '-T', service] + list(args), input=input)

    def restic(self, args):
        return self.run(['restic'] + list(args))

    def inventory(self):
        if self._inventory is None:
            self._inventory = json.loads(self.run(self.compose + ['config', '--format', 'json']))
        return self._inventory

    def running(self):
        return self.run(self.compose + ['ps', '--status', 'running', '--services']).decode().split()

    def volume_name(self, logical):
        volume = self.inventory()['volumes'][logical]
        if volume.get('external'):
            raise ValueError('External volumes require a dedicated recovery plan')
        return volume['name']

    def archive_volume(self, logical, destination):
        name = self.volume_name(logical)
        image = self.env.get('BACKUP_HELPER_IMAGE', HELPER_IMAGE)
        self.run_to_file(['docker', 'run', '--rm', '--network', 'none', '--read-only',
                         '-v', name + ':/volume:ro', '--entrypoint', 'tar', image,
                         '-C', '/volume', '-cpf', '-', '.'], destination)

    def restore_volume(self, logical, source):
        name = self.volume_name(logical)
        image = self.env.get('BACKUP_HELPER_IMAGE', HELPER_IMAGE)
        self.run_from_file(['docker', 'run', '--rm', '-i', '--network', 'none', '--read-only',
                  '-v', name + ':/volume', '--entrypoint', 'tar', image,
                  '-C', '/volume', '--numeric-owner', '-xpf', '-'], source)

    @contextlib.contextmanager
    def lock(self):
        with (self.state / 'operation.lock').open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError('Another backup or restore is running') from error
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def database_evidence(c):
    """Fingerprint restored public tables/collections, independent of LLM output."""
    result = {}
    for service, specification in c.inventory()['services'].items():
        if service in ('postgres', 'activepieces-postgres'):
            environment = specification.get('environment', {})
            database = environment.get('POSTGRES_DB', 'postgres')
            user = environment.get('POSTGRES_USER', 'postgres')
            base = ['psql', '-X', '-A', '-t', '-U', user, '-d', database, '-c']
            tables = c.docker(service, base + ["SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"]).decode().splitlines()
            entries = {}
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                sql = f"SELECT row_to_json(t)::text FROM public.{quoted} t"
                rows = sorted(c.docker(service, base + [sql]).splitlines())
                entries[table] = {'count': len(rows), 'sha256': hashlib.sha256(b'\n'.join(rows)).hexdigest()}
            result[service] = entries
        elif service == 'mongodb':
            script = """const out={}; db.adminCommand({listDatabases:1}).databases.forEach(d=>{if(!['admin','local','config'].includes(d.name)){const x=db.getSiblingDB(d.name);out[d.name]={};x.getCollectionNames().sort().forEach(n=>{const rows=x.getCollection(n).find().toArray().map(v=>EJSON.stringify(v)).sort();out[d.name][n]=rows;});}});print(EJSON.stringify(out));"""
            command = ['sh', '-c', 'exec mongosh --quiet --username "$MONGO_INITDB_ROOT_USERNAME" --password "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --eval "$1"', 'sh', script]
            databases = json.loads(c.docker(service, command))
            result[service] = {db: {collection: {'count': len(rows), 'sha256': hashlib.sha256('\n'.join(rows).encode()).hexdigest()} for collection, rows in collections.items()} for db, collections in databases.items()}
    return result
