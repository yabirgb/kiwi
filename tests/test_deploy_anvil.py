"""End-to-end deployment tests on a local anvil (skipped if anvil is missing)."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'script'))
import common

pytestmark = pytest.mark.skipif(not shutil.which('anvil'), reason='anvil not installed')

PORT = 8599
RPC = f'http://127.0.0.1:{PORT}'
# Well-known anvil dev account #0; local test chain only.
DEV_KEY = '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80'
OWNER = '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266'
PYTHON = sys.executable
VYPER = [PYTHON, '-m', 'vyper.cli.vyper_compile']


@pytest.fixture(scope='module')
def chain():
    proc = subprocess.Popen(['anvil', '--silent', '--port', str(PORT)])
    w3 = Web3(Web3.HTTPProvider(RPC))
    for _ in range(50):
        if w3.is_connected():
            break
        time.sleep(0.1)
    yield w3
    proc.terminate()


def run_deploy(*extra):
    return subprocess.run(
        [PYTHON, str(ROOT / 'script' / 'deploy.py'), '--rpc', RPC, '--owner', OWNER, *extra],
        env={**os.environ, 'PRIVATE_KEY': DEV_KEY},
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def abi():
    return json.loads(
        subprocess.check_output([*VYPER, '-f', 'abi', str(ROOT / 'src' / 'PoolProbe.vy')])
    )


def test_artifacts_match_manifest():
    manifest = json.loads((common.ARTIFACTS / 'manifest.json').read_text())
    for name, digest in manifest.items():
        assert common.sha256_of(common.ARTIFACTS / name) == digest
    for name in manifest:
        assert (
            common.find_forbidden_opcodes(bytes.fromhex((common.ARTIFACTS / name).read_text()))
            == []
        )


def test_find_forbidden_opcodes_skips_push_data():
    assert common.find_forbidden_opcodes(bytes([0x60, 0x5F, 0x5F])) == [2]  # PUSH1 0x5f, then PUSH0
    assert common.find_forbidden_opcodes(bytes([0x60, 0x5F])) == []


def test_golden_addresses():
    # Any change to the frozen initcode, salts, init data or owner moves these. Update deliberately.
    p = common.plan(OWNER)
    assert p['implementation'] == '0x23667a1091C914441C6918857847e27735fa5118'
    assert p['proxy'] == '0xE0a183F093D61DE2214ABdD8bBd739b0CF1f86d9'
    assert p['proxy_admin'] == '0x59ecB19a0C8351bF7787bA8331ea7B6C61be1ca3'


def test_dry_run_sends_nothing(chain):
    out = run_deploy()
    assert 'dry run' in out
    assert chain.eth.get_code(common.plan(OWNER)['proxy']) == b''


def test_deploy_is_deterministic_idempotent_and_upgradeable(chain, tmp_path):
    p = common.plan(OWNER)
    out = run_deploy('--broadcast')
    assert 'version() = 100' in out
    for key in ('implementation', 'proxy', 'proxy_admin'):
        assert chain.eth.get_code(p[key]), key
    # ERC-1967 slots match the offline plan
    impl_slot = chain.eth.get_storage_at(
        p['proxy'], '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc'
    )
    assert Web3.to_checksum_address(impl_slot[12:]) == p['implementation']

    # second run is a no-op: nothing redeployed, no new transactions
    nonce = chain.eth.get_transaction_count(OWNER)
    assert 'already deployed' in run_deploy('--broadcast')
    assert chain.eth.get_transaction_count(OWNER) == nonce

    # proxy responds like the implementation
    contract = chain.eth.contract(address=p['proxy'], abi=abi())
    direct = chain.eth.contract(address=p['implementation'], abi=abi())
    assert contract.functions.version().call() == 100
    args = ([], [], [])
    assert contract.functions.probe(*args).call()[0] == direct.functions.probe(*args).call()[0]

    # upgrade to a v1.1 that bumps version and adds a kind; v1 calldata must still decode
    v11 = (
        (ROOT / 'src' / 'PoolProbe.vy')
        .read_text()
        .replace('VERSION: constant(uint16) = 100', 'VERSION: constant(uint16) = 101')
    )
    head, _, tail = v11.rpartition('    return False, 0, 0, 0\n')
    v11 = (
        head
        + '    elif kind == 2:\n        return True, 1, 2, 3\n'
        + '    return False, 0, 0, 0\n'
        + tail
    )
    src = tmp_path / 'PoolProbeV11.vy'
    src.write_text(v11)
    initcode = bytes.fromhex(
        subprocess.check_output([*VYPER, '-f', 'bytecode', str(src)], text=True).strip()[2:]
    )
    new_impl = common.create2_address(common.ARACHNID_DEPLOYER, common.IMPL_SALT, initcode)
    tx = chain.eth.send_transaction(
        {
            'from': OWNER,
            'to': common.ARACHNID_DEPLOYER,
            'data': '0x' + (common.IMPL_SALT + initcode).hex(),
        }
    )
    chain.eth.wait_for_transaction_receipt(tx)
    admin = chain.eth.contract(
        address=p['proxy_admin'],
        abi=[
            {
                'name': 'upgradeAndCall',
                'type': 'function',
                'stateMutability': 'payable',
                'inputs': [{'type': 'address'}, {'type': 'address'}, {'type': 'bytes'}],
                'outputs': [],
            }
        ],
    )
    chain.eth.wait_for_transaction_receipt(
        admin.functions.upgradeAndCall(p['proxy'], new_impl, b'').transact({'from': OWNER})
    )

    assert contract.functions.version().call() == 101
    assert contract.functions.probe(*args).call()[0] == 101  # same ABI, same address
    read = contract.functions.read_pools([(2, OWNER, OWNER, b'\0' * 4, 7)]).call()
    assert read[4][0][0] == 7  # the new kind=2 is served through the unchanged read_pools ABI


def test_transfer_admin_refuses_eoa(chain):
    with pytest.raises(subprocess.CalledProcessError) as exc:
        run_deploy('--broadcast', '--transfer-admin', '0x70997970C51812dc3A010C7d01b50e0d17dc79C8')
    assert 'no code' in exc.value.stderr + exc.value.stdout


def test_rerun_flags_are_idempotent(chain):
    run_deploy('--broadcast', '--upgrade-to-current')
    nonce = chain.eth.get_transaction_count(OWNER)
    out = run_deploy('--broadcast', '--upgrade-to-current')
    assert chain.eth.get_transaction_count(OWNER) == nonce
    assert 'skipping' in out
