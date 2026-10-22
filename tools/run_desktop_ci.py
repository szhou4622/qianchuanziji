"""Partition every test module across Windows CI jobs using the original isolation runner."""
from __future__ import annotations
import argparse
from pathlib import Path
import subprocess
import sys

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shard', type=int, required=True)
    parser.add_argument('--shards', type=int, required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    if not 0 <= args.shard < args.shards:
        parser.error('Invalid shard index/count')
    desktop = Path(__file__).resolve().parents[1] / 'qcsckp-desktop'
    files = sorted((desktop / 'tests').glob('test*.py'))
    modules = ['tests.' + path.stem for index, path in enumerate(files)
               if index % args.shards == args.shard]
    if not modules:
        raise SystemExit('Empty test shard is forbidden')
    print(f'Shard {args.shard + 1}/{args.shards}: {len(modules)} of {len(files)} modules', flush=True)
    print('\n'.join(modules), flush=True)
    return subprocess.run([sys.executable, '-B', 'scripts/run_isolated_tests.py',
                           *modules, '--report', args.report], cwd=desktop).returncode

if __name__ == '__main__':
    raise SystemExit(main())
