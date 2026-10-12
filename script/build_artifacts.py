"""Build deploy artifacts. The genesis implementation is frozen: it is written once and never
overwritten, because the proxy address depends on it (see README)."""

import json
import subprocess
import sys

from common import ARTIFACTS, ROOT, find_forbidden_opcodes, sha256_of

VYPER = [sys.executable, '-m', 'vyper.cli.vyper_compile']


def write(name: str, data: bytes, *, frozen: bool = False) -> None:
    path = ARTIFACTS / name
    if frozen and path.exists():
        print(f'{name}: frozen, kept')
        return
    path.write_text(data.hex() + '\n')
    print(f'{name}: written ({len(data)} bytes)')


def main() -> None:
    ARTIFACTS.mkdir(exist_ok=True)
    vy = bytes.fromhex(
        subprocess.check_output(
            [*VYPER, '-f', 'bytecode', ROOT / 'src' / 'PoolProbe.vy'], text=True
        ).strip()[2:]
    )
    write('PoolProbe.genesis.initcode.hex', vy, frozen=True)
    write('PoolProbe.current.initcode.hex', vy)

    subprocess.check_call(['forge', 'build', '--root', ROOT / 'proxy'])
    out = ROOT / 'proxy' / 'out'
    tup = json.loads(
        (out / 'TransparentUpgradeableProxy.sol' / 'TransparentUpgradeableProxy.json').read_text()
    )
    write(
        'TransparentUpgradeableProxy.creation.hex',
        bytes.fromhex(tup['bytecode']['object'][2:]),
        frozen=True,
    )

    for name in (
        'PoolProbe.genesis.initcode.hex',
        'PoolProbe.current.initcode.hex',
        'TransparentUpgradeableProxy.creation.hex',
    ):
        bad = find_forbidden_opcodes(bytes.fromhex((ARTIFACTS / name).read_text().strip()))
        if bad:
            sys.exit(f'{name} contains PUSH0/MCOPY at {bad[:5]}')
    manifest = {
        n: sha256_of(ARTIFACTS / n) for n in sorted(p.name for p in ARTIFACTS.glob('*.hex'))
    }
    (ARTIFACTS / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('manifest.json written; paris-safe (no PUSH0/MCOPY)')


if __name__ == '__main__':
    main()
