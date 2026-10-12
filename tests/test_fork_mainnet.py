"""Mainnet-fork tests. Skipped unless MAINNET_RPC_URL is set (needs an archive-capable RPC)."""

import os

import boa
import pytest
from conftest import KIND_V2, KIND_V3, SRC, source

RPC = os.environ.get('MAINNET_RPC_URL')
FORK_BLOCK = int(os.environ.get('MAINNET_FORK_BLOCK', '21000000'))  # pinned for reproducibility
pytestmark = pytest.mark.skipif(not RPC, reason='MAINNET_RPC_URL not set')

# Official Uniswap deployment addresses (a non-zero WETH/USDC pool below confirms them).
UNI_V2_FACTORY = '0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f'
UNI_V3_FACTORY = '0x1F98431c8aD98523631AE4a59f267346ea31F984'
WETH = '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2'
USDC = '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48'
DEAD_TOKEN = '0x000000000000000000000000000000000000dEaD'  # no pair anywhere


class Addr:  # source() expects an object with .address
    def __init__(self, address):
        self.address = address


V2_ABI = [
    {
        'name': 'getReserves',
        'type': 'function',
        'stateMutability': 'view',
        'inputs': [],
        'outputs': [{'type': 'uint112'}, {'type': 'uint112'}, {'type': 'uint32'}],
    }
]
V3_ABI = [
    {
        'name': 'liquidity',
        'type': 'function',
        'stateMutability': 'view',
        'inputs': [],
        'outputs': [{'type': 'uint128'}],
    },
    {
        'name': 'slot0',
        'type': 'function',
        'stateMutability': 'view',
        'inputs': [],
        'outputs': [
            {'type': 'uint160'},
            {'type': 'int24'},
            {'type': 'uint16'},
            {'type': 'uint16'},
            {'type': 'uint16'},
            {'type': 'uint8'},
            {'type': 'bool'},
        ],
    },
]
ERC20_ABI = [
    {
        'name': 'balanceOf',
        'type': 'function',
        'stateMutability': 'view',
        'inputs': [{'type': 'address'}],
        'outputs': [{'type': 'uint256'}],
    },
]


@pytest.fixture(scope='module')
def fork_probe():
    with boa.swap_env(boa.Env()):
        boa.fork(RPC, block_identifier=FORK_BLOCK)
        yield boa.load(str(SRC))


def test_matches_direct_reads(fork_probe):
    sources = [
        source(KIND_V2, Addr(UNI_V2_FACTORY), 'getPair(address,address)'),
        source(
            KIND_V3,
            Addr(UNI_V3_FACTORY),
            'getPool(address,address,uint24)',
            True,
            'slot0()',
            [100, 500, 3000, 10000],
        ),
    ]
    res = fork_probe.probe([USDC, DEAD_TOKEN], [WETH], sources)
    assert list(res.decimals) == [6, 255, 18]
    assert len(res.hits) >= 2
    assert all(h.meta & 0xFF == 0 for h in res.hits)  # DEAD_TOKEN produced no hits

    for hit in res.hits:
        source_idx = (hit.meta >> 16) & 0xFF
        if source_idx == 0:
            pool = boa.loads_abi(__import__('json').dumps(V2_ABI)).at(hit.pool)
            r0, r1, _ = pool.getReserves()
            assert (hit.w0, hit.w1) == (r0, r1)
        else:
            pool = boa.loads_abi(__import__('json').dumps(V3_ABI)).at(hit.pool)
            weth = boa.loads_abi(__import__('json').dumps(ERC20_ABI)).at(WETH)
            assert hit.w0 == pool.slot0()[0]
            assert hit.w1 == pool.liquidity()
            assert hit.hub_balance == weth.balanceOf(hit.pool)
