"""Check collaboration contracts and reviewed r31 source import, offline."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ('README.md', 'AGENTS.md', '.gitignore', '.gitattributes', '.env.example',
            '.github/pull_request_template.md', '.github/workflows/ci.yml',
            'docs/COLLABORATION.md', 'docs/SETUP_ACCEPTANCE.md', 'docs/source-r31.json',
            'qcsckp-desktop/pyproject.toml', 'qcsckp-desktop/uv.lock',
            'qcsckp-desktop/startup_bootstrap.py', 'qcsckp-api-services/config.example.php')

def check() -> None:
    errors = [f'Missing required file: {n}' for n in REQUIRED if not (ROOT / n).is_file()]
    files = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT).decode().split('\0')
    for name in sorted(set(files) - {''}):
        path = ROOT / name
        if not path.is_file():
            errors.append(f'Missing indexed file: {name}')
            continue
        if set(path.relative_to(ROOT).parts) & {'node_modules', '.venv', '__pycache__', 'logs', 'output', 'data'}:
            errors.append(f'Runtime/generated directory tracked: {name}')
        if path.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.dpapi', '.pfx', '.p12', '.key'}:
            errors.append(f'Private/runtime file tracked: {name}')
        if path.name in {'.env', 'config.php', 'secrets.json', 'credentials.json'} or (path.name.startswith('.env.') and path.name != '.env.example'):
            errors.append(f'Private configuration tracked: {name}')
        if path.stat().st_size > 5_000_000:
            errors.append(f'Source exceeds 5 MB: {name}')
        try:
            content = path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            continue
        if re.search(r'^(?:<{7} |>{7} )', content, re.MULTILINE):
            errors.append(f'Conflict marker: {name}')
        if re.search(r'(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,})', content):
            errors.append(f'Potential GitHub credential: {name}')
        if re.search(r'^-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----', content, re.MULTILINE):
            errors.append(f'Private key: {name}')
    manifest = json.loads((ROOT / 'docs/source-r31.json').read_text(encoding='utf-8'))
    changes = manifest.get('documented_changes', {})
    pristine = []
    for entry in manifest['files']:
        path = ROOT / entry['path']
        actual = hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest() if path.is_file() else None
        if actual != entry['sha256_lf'] and not changes.get(entry['path']):
            errors.append(f'Unrecorded change from r31: {entry["path"]}')
        if actual == entry['sha256_lf']:
            pristine.append(entry['path'])
    if len(manifest['files']) != 395:
        errors.append('Expected 395 reviewed source entries')
    if errors:
        raise SystemExit('\n'.join(errors))
    # Keep historical source formatting intact; check every new/modified file.
    has_parent = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD^'], cwd=ROOT,
                                capture_output=True).returncode == 0
    if has_parent:
        subprocess.run(['git', 'diff', '--check', 'HEAD^', 'HEAD', '--', '.',
                        *[':(exclude)' + path for path in pristine]], cwd=ROOT, check=True)
    print(f'Repository contracts and {len(manifest["files"])} source entries verified.')

if __name__ == '__main__':
    check()
