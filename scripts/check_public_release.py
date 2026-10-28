#!/usr/bin/env python3
"""Check publishable source paths and common credentials; never print values."""
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
try:
    top = subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], cwd=root,
                                   stderr=subprocess.DEVNULL).decode().strip()
    if Path(top).resolve() != root:
        raise subprocess.CalledProcessError(1, 'git')
    names = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                                    cwd=root, stderr=subprocess.DEVNULL).decode().split('\0')
except subprocess.CalledProcessError:
    names = [str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()
             and not any(part in {'.git', '.venv', 'data', '__pycache__', 'node_modules'} for part in p.relative_to(root).parts)]
patterns = [
    rb'\b(?:sk|rk|rkcs)_(?:live|test)_[A-Za-z0-9]{20,}\b',
    rb'\bwhsec_[A-Za-z0-9]{20,}\b',
    rb'\bgh[pousr]_[A-Za-z0-9]{20,}\b',
    rb'\bgithub_pat_[A-Za-z0-9_]{20,}\b',
    rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
]
problems = []
checked = 0
for name in sorted(set(filter(None, names))):
    path = root / name
    if not path.is_file():
        continue
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        problems.append((name, 'symlink or external path')); continue
    parts = Path(name).parts
    if any(part in {'data', 'backups', 'exports', 'node_modules', '.venv', '__pycache__'} for part in parts) or Path(name).name.startswith('.env'):
        problems.append((name, 'runtime/configuration path')); continue
    data = path.read_bytes()
    if any(re.search(pattern, data) for pattern in patterns):
        problems.append((name, 'credential pattern'))
    checked += 1
for name, reason in problems:
    print(f'REVIEW: {name} ({reason}; values hidden)')
print(f'Checked {checked} source files; {len(problems)} findings. Live runtime files were not inspected.')
sys.exit(bool(problems))
