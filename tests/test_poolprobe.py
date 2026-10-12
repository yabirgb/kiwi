import pytest
from conftest import KIND_V2, KIND_V3, Q96, SRC, deploy_hostile, deploy_mock, sel, source
from eth_utils import keccak

V2_SIG = 'getPair(address,address)'
V3_SIG = 'getPool(address,address,uint24)'
SLOT0 = 'slot0()'


def meta(t, h, s, p=0):
    return t | (h << 8) | (s << 16) | (p << 24)


def test_selectors_match_signatures():
    src = SRC.read_text()
    for name, signature in {
        'SEL_DECIMALS': 'decimals()',
        'SEL_GET_RESERVES': 'getReserves()',
        'SEL_LIQUIDITY': 'liquidity()',
        'SEL_BALANCE_OF': 'balanceOf(address)',
    }.items():
        assert f'{name}: constant(bytes4) = 0x{keccak(text=signature)[:4].hex()}' in src


def test_version(probe):
    assert probe.version() == 100


def setup_world():
    token = deploy_mock('MockToken', 6)
    hub = deploy_mock('MockToken', 18)
    return token, hub


def test_v2_hit_and_decimals(probe):
    token, hub = setup_world()
    factory = deploy_mock('MockFactory')
    pool = deploy_mock('MockV2Pool', 5_000 * 10**6, 2 * 10**18)
    factory.set_pool(token.address, hub.address, 0, pool.address)

    res = probe.probe([token.address], [hub.address], [source(KIND_V2, factory, V2_SIG)])
    assert res.version == 100
    assert list(res.decimals) == [6, 18]
    assert len(res.hits) == 1
    hit = res.hits[0]
    assert hit.meta == meta(0, 0, 0)
    assert hit.pool == pool.address
    assert (hit.w0, hit.w1, hit.hub_balance) == (5_000 * 10**6, 2 * 10**18, 0)
    assert not res.truncated


def test_v3_hit_across_fee_tiers(probe):
    token, hub = setup_world()
    factory = deploy_mock('MockFactory')
    pool = deploy_mock('MockV3Pool', Q96 * 2, 10**15)
    factory.set_pool(token.address, hub.address, 3000, pool.address)
    hub.set_balance(pool.address, 123)

    src = source(KIND_V3, factory, V3_SIG, True, SLOT0, [100, 500, 3000, 10000])
    res = probe.probe([token.address], [hub.address], [src])
    assert len(res.hits) == 1
    hit = res.hits[0]
    assert hit.meta == meta(0, 0, 0, 3000)
    assert (hit.w0, hit.w1, hit.hub_balance) == (Q96 * 2, 10**15, 123)


def test_missing_pair_zero_reserve_zero_liquidity_and_self_hub(probe):
    token, hub = setup_world()
    f2, f3 = deploy_mock('MockFactory'), deploy_mock('MockFactory')
    empty_v2 = deploy_mock('MockV2Pool', 0, 10)
    empty_v3 = deploy_mock('MockV3Pool', Q96, 0)
    f2.set_pool(token.address, hub.address, 0, empty_v2.address)
    f3.set_pool(token.address, hub.address, 500, empty_v3.address)
    sources = [
        source(KIND_V2, f2, V2_SIG),
        source(KIND_V3, f3, V3_SIG, True, SLOT0, [500]),
        source(KIND_V2, deploy_mock('MockFactory'), V2_SIG),  # no pair at all
    ]
    # token list also contains the hub itself, which must be skipped even if a (hub, hub) pool exists
    f2.set_pool(hub.address, hub.address, 0, deploy_mock('MockV2Pool', 10, 10).address)
    res = probe.probe([token.address, hub.address], [hub.address], sources)
    assert len(res.hits) == 0


def test_hostile_tokens_do_not_break_batch(probe):
    token, hub = setup_world()
    factory = deploy_mock('MockFactory')
    pool = deploy_mock('MockV2Pool', 10**9, 10**18)
    factory.set_pool(token.address, hub.address, 0, pool.address)
    hostile = [deploy_hostile(k) for k in ('revert', 'burn_gas', 'short_return', 'big_return')]

    tokens = hostile + [token.address]
    res = probe.probe(tokens, [hub.address], [source(KIND_V2, factory, V2_SIG)])
    dec = list(res.decimals)
    assert dec[0] == 255  # reverting
    assert dec[1] == 255  # gas burner
    assert dec[2] == 255  # short return
    assert dec[3] == 18  # 3200-byte return truncated to its first word
    assert dec[4] == 6 and dec[5] == 18
    assert [h.meta for h in res.hits] == [meta(4, 0, 0)]


@pytest.mark.parametrize('word', [2**200, 2**160])
def test_factory_returning_non_address_word(probe, word):
    token, hub = setup_world()
    factory = deploy_mock('MockFactory')
    factory.set_raw(word)
    res = probe.probe([token.address], [hub.address], [source(KIND_V2, factory, V2_SIG)])
    assert len(res.hits) == 0


def test_gas_burning_balance_of_still_returns_hit(probe):
    token, _ = setup_world()
    hub_addr = deploy_hostile('burn_gas')  # balanceOf burns gas; decimals unreadable too
    factory = deploy_mock('MockFactory')
    pool = deploy_mock('MockV3Pool', Q96, 10**12)
    factory.set_pool(token.address, hub_addr, 500, pool.address)
    src = source(KIND_V3, factory, V3_SIG, True, SLOT0, [500])
    res = probe.probe([token.address], [hub_addr], [src])
    assert len(res.hits) == 1 and res.hits[0].hub_balance == 0


def test_truncation(probe):
    token, hub = setup_world()
    factory = deploy_mock('MockFactory')
    pool = deploy_mock('MockV2Pool', 10, 10)
    factory.set_pool(token.address, hub.address, 0, pool.address)
    # 48 tokens x same source repeated: use 12 sources x 6 hubs-> need > 384 hits
    tokens = [token.address] * 48
    hubs = [hub.address] * 1
    # 48 tokens * 1 hub * 12 sources = 576 hits > 384
    sources = [source(KIND_V2, factory, V2_SIG)] * 12
    res = probe.probe(tokens, hubs, sources)
    assert res.truncated
    assert len(res.hits) == 384


def test_read_pools_matches_probe(probe):
    token, hub = setup_world()
    f2, f3 = deploy_mock('MockFactory'), deploy_mock('MockFactory')
    p2 = deploy_mock('MockV2Pool', 7 * 10**6, 3 * 10**18)
    p3 = deploy_mock('MockV3Pool', Q96 * 3, 10**14)
    f2.set_pool(token.address, hub.address, 0, p2.address)
    f3.set_pool(token.address, hub.address, 500, p3.address)
    hub.set_balance(p3.address, 99)
    sources = [source(KIND_V2, f2, V2_SIG), source(KIND_V3, f3, V3_SIG, True, SLOT0, [500])]
    full = probe.probe([token.address], [hub.address], sources)

    refs = [
        (KIND_V2, p2.address, hub.address, b'\x00' * 4, full.hits[0].meta),
        (KIND_V3, p3.address, hub.address, sel(SLOT0), full.hits[1].meta),
        (KIND_V2, deploy_mock('MockToken', 1).address, hub.address, b'\x00' * 4, 77),  # fails
    ]
    fast = probe.read_pools(refs)
    assert list(fast.decimals) == []
    assert [tuple(h) for h in fast.hits] == [tuple(h) for h in full.hits]


GAS_BUDGET = 15_000_000


def gas_of(probe, *args):
    res = probe.probe(*args)
    return probe._computation.get_gas_used(), res


def test_gas_empty_input_fixed_cost(probe):
    gas, _ = gas_of(probe, [], [], [])
    print('empty probe gas:', gas)
    assert gas < 200_000


def test_gas_full_hits_truncation(probe):
    token, hub = setup_world()
    factory = deploy_mock('MockFactory')
    pool = deploy_mock('MockV2Pool', 10, 10)
    factory.set_pool(token.address, hub.address, 0, pool.address)
    gas, res = gas_of(
        probe, [token.address] * 48, [hub.address], [source(KIND_V2, factory, V2_SIG)] * 12
    )
    print('384-hit gas:', gas, 'response bytes:', len(probe._computation.output))
    assert res.truncated and gas < GAS_BUDGET


def test_gas_realistic_worst_case_mostly_misses(probe):
    tokens = [deploy_mock('MockToken', 18).address for _ in range(48)]
    hubs = [deploy_mock('MockToken', 18).address for _ in range(6)]
    f2a, f2b, f3a, f3b = (deploy_mock('MockFactory') for _ in range(4))
    # one real V2 hit to make sure the response isn't empty
    pool = deploy_mock('MockV2Pool', 10**9, 10**18)
    f2a.set_pool(tokens[0], hubs[0], 0, pool.address)
    sources = [
        source(KIND_V2, f2a, V2_SIG),
        source(KIND_V2, f2b, V2_SIG),
        source(KIND_V3, f3a, V3_SIG, True, SLOT0, [100, 500, 3000, 10000]),
        source(KIND_V3, f3b, V3_SIG, True, SLOT0, [100, 500, 2500, 10000]),
    ]
    gas, res = gas_of(probe, tokens, hubs, sources)
    calls = 48 * 6 * 10
    print(
        f'worst-case gas: {gas} for {calls} factory calls; {gas / 48:.0f} gas/token; '
        f'response bytes: {len(probe._computation.output)}'
    )
    assert len(res.hits) == 1
    assert gas < GAS_BUDGET


def test_codeless_and_one_byte_returns_are_unreadable_decimals(probe):
    # Without the exact-length check these would decode as decimals 0 (empty/zero-padded word).
    _, hub = setup_world()
    res = probe.probe(
        ['0x000000000000000000000000000000000000bEEF', deploy_hostile('one_zero_byte')],
        [hub.address],
        [],
    )
    assert list(res.decimals) == [255, 255, 18]


def test_gas_burner_cannot_starve_the_batch(probe):
    # A real gas limit (the default in boa is huge, which would hide a missing per-call cap).
    token, hub = setup_world()
    factory = deploy_mock('MockFactory')
    pool = deploy_mock('MockV2Pool', 10**9, 10**18)
    factory.set_pool(token.address, hub.address, 0, pool.address)
    burner = deploy_hostile('burn_gas')
    res = probe.probe(
        [burner, burner, burner, token.address],
        [hub.address],
        [source(KIND_V2, factory, V2_SIG)],
        gas=15_000_000,
    )
    assert list(res.decimals)[:3] == [255, 255, 255]
    assert [h.meta for h in res.hits] == [meta(3, 0, 0)]
