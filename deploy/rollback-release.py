#!/usr/bin/env python3
"""Verify or restore a deployment's source backups while the detector is stopped.

Default is a dry run. --apply restores changed original files; newly added files
are moved into the backup directory, not deleted. Reinstall the restored unit
separately only if required. Captures and service enablement are not changed here.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('manifest',type=Path)
    ap.add_argument('--apply',action='store_true')
    args=ap.parse_args()
    data=json.loads(args.manifest.read_text())
    root=Path(data['root']).resolve()
    backup=Path(data['backup']).resolve()
    if not backup.is_relative_to(root/'backups'):
        raise ValueError('backup is outside project backups')
    sys.path.insert(0,str(root/'p0'))
    from instance import acquire
    owner=acquire(root/'runtime/watch.lock')
    # Refuse a running supervisor even if it is between child processes.
    for p in Path('/proc').glob('[0-9]*/cmdline'):
        try: parts=p.read_bytes().split(b'\0')[1:]
        except OSError: continue
        if any(x.rsplit(b'/',1)[-1] in (b'watch.py',b'supervise.py') for x in parts):
            raise RuntimeError('stop the detector/supervisor before rollback')
    def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
    for row in data['changes']:
        target=(root/row['path']).resolve()
        if not target.is_relative_to(root) or target.is_relative_to(root/'captures'):
            raise ValueError('invalid rollback target')
        if digest(target)!=row['sha256']:
            raise RuntimeError('source changed since deployment: '+row['path'])
        if row['before'] and digest(backup/'files'/row['path'])!=row['before']:
            raise RuntimeError('backup integrity failed: '+row['path'])
    print(f"Verified {len(data['changes'])} source paths; apply={args.apply}")
    if args.apply:
        for row in data['changes']:
            target=root/row['path']
            if row['before']:
                tmp=target.with_name(target.name+'.rollback-tmp')
                shutil.copy2(backup/'files'/row['path'],tmp)
                os.replace(tmp,target)
            else:
                dest=backup/'added-files'/row['path']
                dest.parent.mkdir(parents=True,exist_ok=True)
                target.replace(dest)
        print('Source restored. Service remains stopped; verify its unit before later use.')
    owner.close()


if __name__=='__main__':main()
