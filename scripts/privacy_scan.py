"""Scan the files that would be published for material that must never be there.

    python scripts/privacy_scan.py                      # tracked + untracked files (git), or the tree
    python scripts/privacy_scan.py --terms-file ~/private-terms.txt
    python scripts/privacy_scan.py --ref HEAD           # the committed tree instead of the worktree

Checks every text file for credential-shaped strings, e-mail addresses, personal
home directories, UUIDs, signed URLs, private IP addresses, telephone numbers and
real-company suffixes, and refuses binary, audio, model and document formats. An
optional terms file (one term per line, never committed) adds project-specific
words such as names or former employers. Only paths, rule names and line numbers
are printed; matched text is never echoed. Exit status is non-zero on findings.
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_SUFFIXES = {'.py', '.md', '.json', '.txt', '.html', '.ps1', '.sh', '.toml', '.yml', '.yaml', '.cfg', ''}
FORBIDDEN_DIRS = {'artifacts', 'models', 'cache', '.cache', '.venv', '.venv-wsl', '.venv-sherpa', 'private',
                  'personal', 'recordings', 'transcripts', 'logs', '__pycache__'}
FORBIDDEN_PREFIXES = ('fixtures/audio/', 'fixtures/manifests/')
MAX_BYTES = 200_000
# RFC 1918 / link-local / CGNAT ranges, given as (integer address, prefix) so that no
# literal private address appears in this file.
PRIVATE_NETWORKS = [ipaddress.ip_network((address, prefix)) for address, prefix in
                    ((0x0A000000, 8), (0xAC100000, 12), (0xC0A80000, 16), (0xA9FE0000, 16), (0x64400000, 10))]


def _chars(*codepoints):
    return ''.join(chr(value) for value in codepoints)


# Japanese company suffixes (kabushiki-gaisha, yugen-gaisha, godo-gaisha) built from code
# points, so that this file never contains them literally and does not flag itself.
COMPANY_SUFFIXES = '|'.join((_chars(0x682a, 0x5f0f, 0x4f1a, 0x793e), _chars(0x6709, 0x9650, 0x4f1a, 0x793e),
                             _chars(0x5408, 0x540c, 0x4f1a, 0x793e)))
CHECKS = {
    'secret': re.compile(r'(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|'
                         r'github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|'
                         r'eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)'),
    'email': re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}'),
    'personal_path': re.compile(r'(?:[A-Za-z]:[\\/]Users[\\/]|/home/[A-Za-z]|/Users/[A-Za-z]|\x25USERPROFILE\x25)'),
    'uuid': re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.I),
    'signed_url': re.compile(r'[?&](?:X-Amz-Signature|X-Goog-Signature|token|sig|access_token)=[^\s\x27\x22]+'),
    'telephone': re.compile(r'(?<!\d)0\d{1,4}-\d{1,4}-\d{3,4}(?!\d)'),
    'real_company': re.compile(COMPANY_SUFFIXES),
}
# Addresses that appear in these files are documentation of a policy, not a contact.
EMAIL_ALLOWLIST = {'tester@example.com'}


def listed_files(ref):
    if ref:
        output = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', '-z', ref], cwd=ROOT)
        return sorted(item for item in output.decode().split('\0') if item)
    try:
        output = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                                         cwd=ROOT, stderr=subprocess.DEVNULL)
        return sorted(item for item in output.decode().split('\0') if item)
    except (OSError, subprocess.CalledProcessError):
        files = []
        for path in ROOT.rglob('*'):
            if path.is_file() and not any(part in FORBIDDEN_DIRS or part.startswith('.git') for part in path.parts):
                files.append(path.relative_to(ROOT).as_posix())
        return sorted(files)


def read_blob(ref, relative):
    if ref:
        return subprocess.check_output(['git', 'show', f'{ref}:{relative}'], cwd=ROOT)
    return (ROOT / relative).read_bytes()


def scan(ref=None, terms=()):
    findings = []
    for relative in listed_files(ref):
        path = Path(relative)
        parts = path.parts
        if any(part in FORBIDDEN_DIRS for part in parts) or relative.startswith(FORBIDDEN_PREFIXES):
            findings.append((relative, 'forbidden_path', []))
            continue
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            findings.append((relative, 'forbidden_extension', []))
            continue
        data = read_blob(ref, relative)
        if b'\0' in data:
            findings.append((relative, 'binary', []))
            continue
        if len(data) > MAX_BYTES:
            findings.append((relative, 'too_large', []))
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            findings.append((relative, 'non_utf8', []))
            continue
        lines = text.splitlines()
        for name, pattern in CHECKS.items():
            hits = []
            for number, line in enumerate(lines, 1):
                for match in pattern.finditer(line):
                    if name == 'email' and match.group(0) in EMAIL_ALLOWLIST:
                        continue
                    hits.append(number)
                    break
            if hits:
                findings.append((relative, name, hits))
        for number, line in enumerate(lines, 1):
            if relative.startswith('requirements') and re.fullmatch(r'[A-Za-z0-9_.-]+==\S+', line.strip()):
                continue
            for value in re.findall(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', line):
                try:
                    address = ipaddress.ip_address(value)
                except ValueError:
                    continue
                if any(address in network for network in PRIVATE_NETWORKS):
                    findings.append((relative, 'private_ip', [number]))
        if terms:
            folded = text.casefold()
            hit_lines = [number for number, line in enumerate(lines, 1)
                         if any(term in line.casefold() for term in terms)]
            if any(term in folded for term in terms):
                findings.append((relative, 'private_term', hit_lines))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description='Privacy scan of publishable files')
    parser.add_argument('--ref', default=None, help='scan a committed tree (e.g. HEAD) instead of the worktree')
    parser.add_argument('--terms-file', type=Path, default=None,
                        help='extra case-insensitive terms, one per line; keep this file outside the repository')
    args = parser.parse_args(argv)
    terms = ()
    if args.terms_file:
        terms = tuple(line.strip().casefold() for line in args.terms_file.read_text(encoding='utf-8').splitlines()
                      if line.strip() and not line.startswith('#'))
    findings = scan(args.ref, terms)
    files = listed_files(args.ref)
    print(f'scanned {len(files)} file(s); extra terms: {len(terms)}')
    for relative, rule, lines in findings:
        where = f' lines {lines[:10]}' if lines else ''
        print(f'  - {relative}: {rule}{where}')
    if findings:
        print(f'FAIL: {len(findings)} finding(s)')
        return 1
    print('OK: no findings')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
