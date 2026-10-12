from pathlib import Path

import boa
import pytest

ROOT = Path(__file__).parent
SRC = ROOT.parent / 'src' / 'PoolProbe.vy'
Q96 = 2**96

KIND_V2, KIND_V3 = 0, 1


def sel(signature: str) -> bytes:
    from eth_utils import keccak

    return keccak(text=signature)[:4]


def deploy_mock(name: str, *args):
    return boa.load(str(ROOT / 'mocks' / f'{name}.vy'), *args)


@pytest.fixture(scope='session')
def probe_contract():
    return boa.load(str(SRC))


@pytest.fixture
def probe(probe_contract):
    return probe_contract


def source(kind, factory, pool_sig, has_param=False, state_sig='', params=()):
    state = sel(state_sig) if state_sig else b'\x00' * 4
    return (kind, str(factory.address), sel(pool_sig), has_param, state, list(params))


_raw_counter = [0xDEAD0000]

# Raw EVM bytecode for hostile contracts (fallback-only behaviour)
HOSTILE_CODE = {
    'revert': '60006000fd',
    'burn_gas': '5b600056',
    'one_zero_byte': '600060005360016000f3',  # returns a single 0x00 byte
    'short_return': '60126000526004601cf3',  # returns 0x00000012 (4 bytes)
    'big_return': '6012600052610c8060 00f3'.replace(' ', ''),  # 3200 bytes, first word = 18
}


def deploy_hostile(kind: str) -> str:
    _raw_counter[0] += 1
    address = '0x' + _raw_counter[0].to_bytes(20, 'big').hex()
    boa.env.evm.vm.state.set_code(bytes.fromhex(address[2:]), bytes.fromhex(HOSTILE_CODE[kind]))
    return address
