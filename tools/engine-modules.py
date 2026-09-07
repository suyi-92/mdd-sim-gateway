#!/usr/bin/env python3
"""Validate the exact Asterisk module set used by a VMware build manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


def module_names(text: str, *, comments: bool = False) -> list[str]:
    names = []
    for line in text.splitlines():
        name = (line.split('#', 1)[0] if comments else line).strip()
        if not name:
            continue
        if not re.fullmatch(r'[a-z0-9_]+\.so', name):
            raise ValueError('invalid Asterisk module filename')
        names.append(name)
    if len(names) != len(set(names)):
        raise ValueError('duplicate Asterisk module filename')
    if len(names) <= 20:
        raise ValueError('Asterisk module list is unexpectedly short')
    return sorted(names)


def module_contract(expected_text: str, actual_text: str | None = None) -> dict:
    expected = module_names(expected_text, comments=True)
    if actual_text is not None:
        actual = module_names(actual_text)
        if actual != expected:
            missing = ', '.join(sorted(set(expected) - set(actual))) or 'none'
            extra = ', '.join(sorted(set(actual) - set(expected))) or 'none'
            raise ValueError(f'Asterisk module set mismatch; missing: {missing}; extra: {extra}')
    digest = hashlib.sha256(('\n'.join(expected) + '\n').encode('ascii')).hexdigest()
    return {'count': len(expected), 'sha256': digest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('keep_list', type=Path)
    parser.add_argument('--actual-stdin', action='store_true')
    args = parser.parse_args()
    try:
        result = module_contract(args.keep_list.read_text(encoding='utf-8'),
                                 sys.stdin.read() if args.actual_stdin else None)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
