"""Shared helpers: artifact loading and deterministic (CREATE2) address derivation."""

import hashlib
from pathlib import Path

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / 'artifacts'
ARACHNID_DEPLOYER = '0x4e59b44847b379578588920cA78FbF26c0B4956C'
IMPL_SALT = keccak(text='rotki.PoolProbe.implementation.v1')
PROXY_SALT = keccak(text='rotki.PoolProbe.proxy.v1')

PUSH0, MCOPY = 0x5F, 0x5E


def read_artifact(name: str) -> bytes:
    return bytes.fromhex((ARTIFACTS / name).read_text().strip().removeprefix('0x'))


def create2_address(deployer: str, salt: bytes, initcode: bytes) -> str:
    raw = keccak(b'\xff' + bytes.fromhex(deployer[2:]) + salt + keccak(initcode))
    return to_checksum_address(raw[12:])


def create_address(sender: str, nonce: int) -> str:
    assert 0 < nonce < 0x80
    raw = keccak(b'\xd6\x94' + bytes.fromhex(sender[2:]) + bytes([nonce]))
    return to_checksum_address(raw[12:])


# OZ >= 5.7 reverts on empty init data (ERC1967ProxyUninitialized). PoolProbe has no storage or
# initializer, so the "init call" is a harmless delegatecall to the view `version()`.
INIT_DATA = keccak(text='version()')[:4]


def proxy_initcode(implementation: str, owner: str) -> bytes:
    args = encode(['address', 'address', 'bytes'], [implementation, owner, INIT_DATA])
    return read_artifact('TransparentUpgradeableProxy.creation.hex') + args


def plan(owner: str, deployer: str = ARACHNID_DEPLOYER) -> dict:
    """All addresses that deploying with `owner` yields; identical on every chain."""
    impl_code = read_artifact('PoolProbe.genesis.initcode.hex')
    impl = create2_address(deployer, IMPL_SALT, impl_code)
    p_code = proxy_initcode(impl, owner)
    proxy = create2_address(deployer, PROXY_SALT, p_code)
    return {
        'implementation': impl,
        'impl_initcode': impl_code,
        'proxy': proxy,
        'proxy_initcode': p_code,
        'proxy_admin': create_address(proxy, 1),  # OZ v5 proxy CREATEs its ProxyAdmin first
    }


def find_forbidden_opcodes(code: bytes) -> list[int]:
    """Offsets of PUSH0/MCOPY, walking the code so PUSH immediates are skipped."""
    found, i = [], 0
    while i < len(code):
        op = code[i]
        if op in (PUSH0, MCOPY):
            found.append(i)
        i += 1 + (op - 0x5F if 0x60 <= op <= 0x7F else 0)
    return found


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
