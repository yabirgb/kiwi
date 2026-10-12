"""Price WBTC / UNI / WETH with a single eth_call, no deployment.

Compiles `src/PoolProbe.vy` in-process, then runs `probe()` via `eth_call` with a
geth-style state override that sets the runtime bytecode at a scratch address.
Infura (and any geth-like node) supports the third `eth_call` argument, so nothing
is ever deployed. Pricing math happens off-chain, as designed.

Usage:
    ETH_RPC_URL=https://mainnet.infura.io/v3/<key> uv run python script/quote.py
    ETH_RPC_URL=… uv run python script/quote.py --block 21000000
"""

import argparse
import os
from decimal import Decimal, localcontext
from pathlib import Path

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'src' / 'PoolProbe.vy'

# Scratch address that gets the runtime code via the eth_call state override. Never deployed to.
SCRATCH = to_checksum_address('0x0000000000000000000000000000000000000099')

# Uniswap mainnet deployments (same as tests/test_fork_mainnet.py).
UNI_V2_FACTORY = to_checksum_address('0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f')
UNI_V3_FACTORY = to_checksum_address('0x1F98431c8aD98523631AE4a59f267346ea31F984')

WETH = to_checksum_address('0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2')
WBTC = to_checksum_address('0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599')
UNI = to_checksum_address('0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984')
USDC = to_checksum_address('0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48')

TOKENS = [WBTC, UNI, WETH]
HUBS = [WETH, USDC]
NAMES = {WETH: 'WETH', WBTC: 'WBTC', UNI: 'UNI', USDC: 'USDC'}

KIND_V2, KIND_V3 = 0, 1

SEL_GET_PAIR = keccak(b'getPair(address,address)')[:4]
SEL_GET_POOL = keccak(b'getPool(address,address,uint24)')[:4]
SEL_SLOT0 = keccak(b'slot0()')[:4]
V3_FEE_TIERS = [500, 3000, 10000]


def runtime_bytecode() -> str:
    """Compile the contract in-process; no artifact, no deployment."""
    from vyper import compile_code

    out = compile_code(SRC.read_text(), output_formats=['bytecode_runtime'])
    return '0x' + out['bytecode_runtime'].removeprefix('0x')


def build_calldata() -> str:
    """abi-encoded probe(tokens, hubs, sources) matching the Source struct layout."""
    sources = [
        (KIND_V2, UNI_V2_FACTORY, SEL_GET_PAIR, False, b'\x00\x00\x00\x00', []),
    ]
    sources.append((KIND_V3, UNI_V3_FACTORY, SEL_GET_POOL, True, SEL_SLOT0, V3_FEE_TIERS))
    data = keccak(b'probe(address[],address[],(uint8,address,bytes4,bool,bytes4,uint24[])[])')[:4]
    data += abi_encode(
        ['address[]', 'address[]', '(uint8,address,bytes4,bool,bytes4,uint24[])[]'],
        [TOKENS, HUBS, sources],
    )
    return '0x' + data.hex()


def call_probe(rpc: str, block: str) -> tuple:
    w3 = Web3(Web3.HTTPProvider(rpc))
    chain_id = w3.eth.chain_id
    result = w3.eth.call(
        {'to': SCRATCH, 'data': build_calldata()},
        block_identifier=block,
        state_override={SCRATCH: {'code': runtime_bytecode()}},
    )
    # Vyper returns the struct as a single dynamic value: one offset word, then the tuple data.
    decoded = abi_decode(
        ['(uint16,uint256,bool,uint8[],(uint256,address,uint256,uint256,uint256)[])'], result
    )[0]
    return chain_id, decoded


def v2_price(token: str, hub: str, w0: int, w1: int, dt: int, dh: int) -> Decimal:
    """Hub per token from getReserves; reserve0 belongs to token0 = min(token, hub)."""
    r_token, r_hub = (w0, w1) if token < hub else (w1, w0)
    return (Decimal(r_hub) / 10**dh) / (Decimal(r_token) / 10**dt)


def v3_price(token: str, hub: str, sqrt_p: int, dt: int, dh: int) -> Decimal:
    """Hub per token from sqrtPriceX96; raw price = token1/token0 = (sqrtP / 2^96)^2."""
    raw = Decimal(sqrt_p**2) / Decimal(2**192)  # token1 per token0, raw units
    if token < hub:  # token is token0, hub is token1
        return raw * Decimal(10) ** (dt - dh)
    return (Decimal(1) / raw) * Decimal(10) ** (dt - dh)


def report(chain_id: int, block: str, decoded: tuple) -> None:
    with localcontext() as ctx:
        ctx.prec = 40
        version, block_number, truncated, decimals, hits = decoded
        print(f'chain {chain_id}  block {block_number} (requested {block})  version {version}')
    if truncated:
        print('WARNING: result truncated at MAX_HITS')

    by_pair: dict[tuple[str, str], list] = {}
    for meta, pool, w0, w1, _bal in hits:
        t, h, s = meta & 0xFF, (meta >> 8) & 0xFF, (meta >> 16) & 0xFF
        by_pair.setdefault((TOKENS[t], HUBS[h]), []).append((s, pool, w0, w1))

    for token in TOKENS:
        for hub in HUBS:
            if token == hub:
                continue
            dt = decimals[TOKENS.index(token)]
            dh = decimals[len(TOKENS) + HUBS.index(hub)]
            label = f'{NAMES[token]}/{NAMES[hub]}'
            if dt == 255 or dh == 255 or (token, hub) not in by_pair:
                print(f'{label:10}  no pool found')
                continue
            hits_sorted = sorted(by_pair[(token, hub)], key=lambda x: x[3], reverse=True)
            for s, pool, w0, w1 in hits_sorted:
                kind = 'v2' if s == 0 else 'v3'
                price = (
                    v2_price(token, hub, w0, w1, dt, dh)
                    if s == 0
                    else v3_price(token, hub, w0, dt, dh)
                )
                star = ' *' if (s, pool, w0, w1) == hits_sorted[0] else ''
                print(
                    f'{label:10}  {kind}  {price:.8f} {NAMES[hub]}'
                    f'  liq {w1 if s else w0 + w1}  {pool}{star}'
                )
            print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--rpc', default=os.environ.get('ETH_RPC_URL'), help='defaults to $ETH_RPC_URL')
    ap.add_argument('--block', default='latest', help='block tag or number (default: latest)')
    args = ap.parse_args()
    if not args.rpc:
        ap.error('no RPC URL: pass --rpc or set $ETH_RPC_URL')

    chain_id, decoded = call_probe(args.rpc, args.block)
    report(chain_id, args.block, decoded)


if __name__ == '__main__':
    main()
