#!/usr/bin/env python3
"""Transactional secondary AT-port rule, called only by managed activation/removal."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

RULE_NAME = "78-mdd-mms-at-port.rules"
RULE = (b'# MDD Sim Gateway: reserve only the verified secondary Quectel AT port for MMS.\n'
        b'ACTION!="remove", SUBSYSTEM=="tty", ATTRS{idVendor}=="2c7c", '
        b'ENV{ID_MM_PORT_TYPE_AT_SECONDARY}=="1", ENV{ID_MM_PORT_IGNORE}="1"\n')


def read_state(path, limit=65536):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, 0o644
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > limit
                or info.st_uid != os.geteuid() or info.st_mode & 0o022 or info.st_nlink != 1):
            raise ValueError('unsafe secondary AT-port rule')
        return stream.read(), stat.S_IMODE(info.st_mode)


def read_rule(path, limit=65536):
    return read_state(path, limit)[0]

def atomic(path, data, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix='.mdd-rule-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def snapshot(path, backup):
    if os.path.lexists(backup):
        raise ValueError('rule rollback material already exists')
    content, mode = read_state(path)
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {'version': 1, 'present': content is not None, 'mode': mode,
               'content': base64.b64encode(content or b'').decode(),
               'sha256': hashlib.sha256(content or b'').hexdigest()}
    atomic(backup, json.dumps(payload).encode())


def reapply(runner=subprocess.run):
    runner(['udevadm', 'control', '--reload-rules'], check=True, timeout=15)
    runner(['udevadm', 'trigger', '--action=change', '--subsystem-match=tty'], check=True, timeout=15)
    runner(['udevadm', 'settle', '--timeout=10'], check=True, timeout=15)
    active = runner(['systemctl', 'is-active', '--quiet', 'ModemManager.service'],
                    check=False, timeout=10)
    if active.returncode == 0:
        runner(['systemctl', 'restart', 'ModemManager.service'], check=True, timeout=60)


def replace(path, content, runner=subprocess.run, mode=0o644):
    before, before_mode = read_state(path)
    if before == content and (content is None or before_mode == mode):
        return
    def write(data, file_mode):
        if data is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, data, file_mode)
    write(content, mode)
    try:
        reapply(runner)
    except BaseException:
        write(before, before_mode)
        reapply(runner)
        raise


def restore(path, backup, runner=subprocess.run):
    value = json.loads(read_rule(backup, 131072))
    content = base64.b64decode(value['content'], validate=True)
    if value.get('version') != 1 or hashlib.sha256(content).hexdigest() != value['sha256']:
        raise ValueError('secondary AT-port rollback digest mismatch')
    mode = value.get('mode', 0o644)
    if type(mode) is not int or mode & ~0o777 or mode & 0o022 or type(value.get('present')) is not bool:
        raise ValueError('unsafe secondary AT-port rollback metadata')
    if len(content) > 65536 or (not value['present'] and content):
        raise ValueError('unsafe secondary AT-port rollback content')
    # Refuse to overwrite a concurrent administrator edit after this transaction.
    current = read_rule(path)
    expected = content if value['present'] else None
    if current not in (RULE, expected):
        raise ValueError('secondary AT-port rule changed outside the transaction')
    replace(path, expected, runner, mode=mode)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('snapshot', 'apply', 'restore', 'remove'))
    parser.add_argument('--rules-dir', type=Path, default=Path('/etc/udev/rules.d'))
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    path = args.rules_dir / RULE_NAME
    if args.action == 'snapshot':
        snapshot(path, args.backup)
    elif args.action == 'restore':
        restore(path, args.backup)
    elif args.action == 'apply':
        replace(path, RULE)
    else:
        if read_rule(path) not in (None, RULE):
            raise ValueError('secondary AT-port rule no longer belongs to MDD')
        replace(path, None)

if __name__ == '__main__':
    main()
