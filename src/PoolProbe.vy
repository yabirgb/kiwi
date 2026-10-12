# pragma version 0.4.3
# pragma evm-version paris
"""
@title PoolProbe
@notice Stateless, ownerless batch reader of raw DEX pool state. One eth_call
        returns reserves / sqrt prices / liquidity for many tokens against a set
        of hub assets across a set of V2-style and V3-style sources. All pricing
        math and selection happens off-chain. No storage, no value transfer, no
        delegatecall, no selfdestruct.
@dev Every external call is a bounded staticcall that cannot revert the batch.
     Within a major version the ABI of existing functions/structs never changes.
"""

MAX_TOKENS: constant(uint256) = 48
MAX_HUBS: constant(uint256) = 6
MAX_SOURCES: constant(uint256) = 12
MAX_PARAMS: constant(uint256) = 6
MAX_HITS: constant(uint256) = 384
MAX_POOL_REFS: constant(uint256) = 256
GAS_CALL: constant(uint256) = 50_000
SENTINEL_DECIMALS: constant(uint8) = 255

KIND_V2: constant(uint8) = 0
KIND_V3: constant(uint8) = 1

VERSION: constant(uint16) = 100

# keccak256(signature)[:4], checked against the signatures in tests
SEL_DECIMALS: constant(bytes4) = 0x313ce567         # decimals()
SEL_GET_RESERVES: constant(bytes4) = 0x0902f1ac     # getReserves()
SEL_LIQUIDITY: constant(bytes4) = 0x1a686502        # liquidity()
SEL_BALANCE_OF: constant(bytes4) = 0x70a08231       # balanceOf(address)

struct Source:
    kind: uint8
    factory: address
    pool_selector: bytes4
    has_param: bool
    state_selector: bytes4
    params: DynArray[uint24, MAX_PARAMS]

struct Hit:
    meta: uint256
    pool: address
    w0: uint256
    w1: uint256
    hub_balance: uint256

struct PoolRef:
    kind: uint8
    pool: address
    hub: address
    state_selector: bytes4
    tag: uint256

struct ProbeResult:
    version: uint16
    block_number: uint256
    truncated: bool
    decimals: DynArray[uint8, MAX_TOKENS + MAX_HUBS]
    hits: DynArray[Hit, MAX_HITS]


@external
@view
def version() -> uint16:
    """
    @notice Implementation version as major * 100 + minor (100 = 1.0).
    """
    return VERSION


@external
@view
def probe(
    tokens: DynArray[address, MAX_TOKENS],
    hubs: DynArray[address, MAX_HUBS],
    sources: DynArray[Source, MAX_SOURCES],
) -> ProbeResult:
    """
    @notice Discover pools for every (token, hub, source, param) combination and return their raw state.
    @param tokens Tokens to price, at most MAX_TOKENS.
    @param hubs Quote assets to pair each token with, at most MAX_HUBS.
    @param sources Factories to query, at most MAX_SOURCES; see Source.
    @return ProbeResult with decimals (tokens then hubs, 255 = unreadable) and one Hit per live pool.
            `truncated` is set if MAX_HITS was reached.
    """
    result: ProbeResult = empty(ProbeResult)
    result.version = VERSION
    result.block_number = block.number

    for token: address in tokens:
        result.decimals.append(self._decimals(token))
    for hub: address in hubs:
        result.decimals.append(self._decimals(hub))

    for t: uint256 in range(MAX_TOKENS):
        if t >= len(tokens):
            break
        token: address = tokens[t]
        for h: uint256 in range(MAX_HUBS):
            if h >= len(hubs):
                break
            hub: address = hubs[h]
            if token == hub:
                continue
            for s: uint256 in range(MAX_SOURCES):
                if s >= len(sources):
                    break
                has_param: bool = sources[s].has_param
                factory: address = sources[s].factory
                pool_selector: bytes4 = sources[s].pool_selector
                n: uint256 = 1
                if has_param:
                    n = len(sources[s].params)
                for i: uint256 in range(MAX_PARAMS):
                    if i >= n:
                        break
                    p: uint24 = 0
                    data: Bytes[100] = b""
                    if has_param:
                        p = sources[s].params[i]
                        data = concat(
                            pool_selector,
                            convert(token, bytes32),
                            convert(hub, bytes32),
                            convert(convert(p, uint256), bytes32),
                        )
                    else:
                        data = concat(pool_selector, convert(token, bytes32), convert(hub, bytes32))
                    success: bool = False
                    ret: Bytes[32] = b""
                    success, ret = raw_call(
                        factory,
                        data,
                        max_outsize=32,
                        gas=GAS_CALL,
                        is_static_call=True,
                        revert_on_failure=False,
                    )
                    if not success or len(ret) != 32:
                        continue
                    w: uint256 = convert(convert(ret, bytes32), uint256)
                    if w == 0 or w >= 2 ** 160:
                        continue
                    pool: address = convert(w, address)

                    ok: bool = False
                    w0: uint256 = 0
                    w1: uint256 = 0
                    bal: uint256 = 0
                    ok, w0, w1, bal = self._read_state(
                        sources[s].kind, pool, hub, sources[s].state_selector
                    )
                    if not ok:
                        continue

                    result.hits.append(
                        Hit(
                            meta=t | (h << 8) | (s << 16) | (convert(p, uint256) << 24),
                            pool=pool,
                            w0=w0,
                            w1=w1,
                            hub_balance=bal,
                        )
                    )
                    if len(result.hits) == MAX_HITS:
                        result.truncated = True
                        return result
    return result


@external
@view
def read_pools(refs: DynArray[PoolRef, MAX_POOL_REFS]) -> ProbeResult:
    """
    @notice Read state of already-known pools without any factory discovery.
    @param refs Pools to read; each `tag` is echoed back as `Hit.meta`. Refs that fail are omitted.
    @return ProbeResult with empty `decimals`.
    """
    result: ProbeResult = empty(ProbeResult)
    result.version = VERSION
    result.block_number = block.number

    for ref: PoolRef in refs:
        ok: bool = False
        w0: uint256 = 0
        w1: uint256 = 0
        bal: uint256 = 0
        ok, w0, w1, bal = self._read_state(ref.kind, ref.pool, ref.hub, ref.state_selector)
        if not ok:
            continue
        result.hits.append(
            Hit(meta=ref.tag, pool=ref.pool, w0=w0, w1=w1, hub_balance=bal)
        )
    return result


@internal
@view
def _word(target: address, data: Bytes[100]) -> (bool, uint256):
    """
    @dev Bounded staticcall returning one 32-byte word. (False, 0) on revert,
         out of gas or short returndata. Longer returndata is truncated.
    """
    success: bool = False
    ret: Bytes[32] = b""
    success, ret = raw_call(
        target,
        data,
        max_outsize=32,
        gas=GAS_CALL,
        is_static_call=True,
        revert_on_failure=False,
    )
    if not success or len(ret) != 32:
        return False, 0
    return True, convert(convert(ret, bytes32), uint256)


@internal
@view
def _decimals(token: address) -> uint8:
    ok: bool = False
    w: uint256 = 0
    ok, w = self._word(token, concat(SEL_DECIMALS, b""))
    if not ok or w > 255:
        return SENTINEL_DECIMALS
    return convert(w, uint8)


@internal
@view
def _read_state(
    kind: uint8, pool: address, hub: address, state_selector: bytes4
) -> (bool, uint256, uint256, uint256):
    """
    @return (ok, w0, w1, hub_balance); see Hit for the meaning per kind.
    """
    if kind == KIND_V2:
        success: bool = False
        ret: Bytes[64] = b""
        success, ret = raw_call(
            pool,
            concat(SEL_GET_RESERVES, b""),
            max_outsize=64,
            gas=GAS_CALL,
            is_static_call=True,
            revert_on_failure=False,
        )
        if not success or len(ret) != 64:
            return False, 0, 0, 0
        r0: uint256 = extract32(ret, 0, output_type=uint256)
        r1: uint256 = extract32(ret, 32, output_type=uint256)
        if r0 == 0 or r1 == 0:
            return False, 0, 0, 0
        return True, r0, r1, 0
    elif kind == KIND_V3:
        ok: bool = False
        sqrt_price: uint256 = 0
        liq: uint256 = 0
        bal: uint256 = 0
        ok, sqrt_price = self._word(pool, concat(state_selector, b""))
        if not ok or sqrt_price == 0:
            return False, 0, 0, 0
        ok, liq = self._word(pool, concat(SEL_LIQUIDITY, b""))
        if not ok or liq == 0:
            return False, 0, 0, 0
        ok, bal = self._word(hub, concat(SEL_BALANCE_OF, convert(pool, bytes32)))
        if not ok:
            bal = 0
        return True, sqrt_price, liq, bal
    return False, 0, 0, 0
