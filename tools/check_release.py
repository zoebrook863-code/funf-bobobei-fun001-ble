"""Check the Git index, or a clean source directory, before publication.

This heuristic prints paths and categories only. Review the staged diff as well.
"""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git', '.venv', 'venv', '__pycache__'}
PRIVATE_DIRS = {'private', 'reference', '.codex', '.claude', '.agents'}
PRIVATE_SUFFIXES = {'.apk', '.hbc', '.log', '.pcap', '.pcapng', '.btsnoop',
                    '.pem', '.key', '.p12', '.zip', '.7z', '.db', '.sqlite'}
SYNTHETIC_ADDRESSES = {'00:00:00:00:00:01', '00:00:00:00:00:02',
                       '11:22:33:44:55:66', 'AA:BB:CC:DD:EE:FF'}
PATTERNS = {
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'access token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{32,})\b'),
    'personal home path': re.compile(r'(?:[A-Za-z]:[/\\]Users[/\\][^/\\\s]+|/'
                                     r'home/[^/\s]+)'),
}
ADDRESS = re.compile(r'\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b')


def publication_files():
    if (ROOT / '.git').exists():
        result = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'],
                                check=True, capture_output=True)
        for name in result.stdout.decode('utf-8').split('\0'):
            if name:
                content = subprocess.run(['git', '-C', str(ROOT), 'show', ':' + name],
                                         check=True, capture_output=True).stdout
                yield Path(name), content
        return
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if path.is_file() and not set(relative.parts).intersection(EXCLUDED):
            yield relative, path.read_bytes()


def issues(path, data):
    if (set(path.parts).intersection(PRIVATE_DIRS) or path.suffix.lower() in PRIVATE_SUFFIXES
            or path.name.startswith('.env') or 'bugreport' in path.name.lower()):
        yield 'private or runtime artifact'
    try:
        content = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        yield 'binary requires manual review'
        return
    for category, pattern in PATTERNS.items():
        if pattern.search(content):
            yield category
    if any(address.upper() not in SYNTHETIC_ADDRESSES for address in ADDRESS.findall(content)):
        yield 'device address'


def main():
    count = 0
    findings = []
    for path, data in publication_files():
        count += 1
        findings.extend((path, category) for category in issues(path, data))
    for path, category in findings:
        print(f'{path.as_posix()} [{category}]')
    print(f'Checked {count} publication files. Findings {len(findings)}.')
    return int(bool(findings) or count == 0)


if __name__ == '__main__':
    raise SystemExit(main())
