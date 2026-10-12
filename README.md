# PoolProbe

Stateless batch reader of DEX pool state (see `onchain_dex_oracle_spec.md`). `src/PoolProbe.vy`,
Vyper 0.4.3, `evm-version paris`. uv project (Python 3.12): `uv sync`, `uv run pytest`, `uv run ruff check .`.

## Layout
- `src/PoolProbe.vy` – the contract. `tests/` – titanoboa (mocks), anvil (deploy/upgrade), optional mainnet fork.
- `proxy/` – pinned OpenZeppelin 5.7.0 (tarball sha1 `3b13b3e6…`), solc 0.8.28, paris, no metadata hash.
- `artifacts/` – committed initcode + `manifest.json` (sha256). The deploy script reads only these.
- `script/build_artifacts.py`, `script/deploy.py`.

## Deterministic addresses
Everything deploys through the Arachnid CREATE2 deployer (`0x4e59…956C`), so addresses depend only on
initcode, salt and constructor args:

- implementation = CREATE2(salt `rotki.PoolProbe.implementation.v1`, **genesis** initcode)
- proxy = CREATE2(salt `rotki.PoolProbe.proxy.v1`, proxy initcode ++ abi.encode(genesis impl, **owner**, `version()`))
- ProxyAdmin = CREATE(proxy, nonce 1)

Rules that keep the address identical on every chain:
1. **`PoolProbe.genesis.initcode.hex` and `TransparentUpgradeableProxy.creation.hex` are frozen.** `build_artifacts.py` never overwrites it. A
   new contract version goes to `PoolProbe.current.initcode.hex` and is applied with `--upgrade-to-current`.
2. **`--owner` is part of the proxy address.** Use the same address on every chain, then
   `--transfer-admin <multisig>`.
3. Init data is `version()` (not empty): OZ 5.7.0 (pinned) reverts on empty data.

The genesis artifact was frozen before any fork test or review: it may be regenerated (delete it and
rebuild) **until the first live deployment, and is permanent after that**. `test_golden_addresses`
pins the resulting addresses; update it deliberately when regenerating.

## Usage
```
uv run python script/quote.py --rpc $RPC [--block latest|0x…]  # price WBTC/UNI/WETH, no deploy:
                                                                # eth_call + code state-override
uv run python script/build_artifacts.py                       # only when changing the contract
uv run python script/deploy.py --rpc $RPC --owner $OWNER      # dry run: addresses + on-chain status
PRIVATE_KEY=… uv run python script/deploy.py --rpc $RPC --owner $OWNER --broadcast
PRIVATE_KEY=… … --broadcast --upgrade-to-current                 # genesis -> current
PRIVATE_KEY=… … --broadcast --transfer-admin $MULTISIG           # one-step, irreversible; target must have code
```
Re-running is safe: targets that already have code are skipped. Transfer ownership **last**.
Admin calls never go through the proxy's implementation: use a normal address for `eth_call`s.

## Not done / manual
- Explorer verification (Vyper source + OZ standard JSON) per chain.
- Mainnet-fork test (`MAINNET_RPC_URL=… pytest tests/test_fork_mainnet.py`) has never been run.
- Never broadcast to a live chain from CI; tests only use a local anvil.
