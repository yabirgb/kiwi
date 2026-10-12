"""Deterministic PoolProbe deployment (implementation + TransparentUpgradeableProxy) through the
Arachnid CREATE2 deployer. Dry-run by default: nothing is sent without --broadcast.

  deploy.py --owner 0xOWNER --rpc URL                      # print plan + on-chain status
  PRIVATE_KEY=... deploy.py --owner 0xOWNER --rpc URL --broadcast
  PRIVATE_KEY=... deploy.py ... --broadcast --upgrade-to-current
  PRIVATE_KEY=... deploy.py ... --broadcast --transfer-admin 0xMULTISIG
"""

import argparse
import os
import sys

from common import ARACHNID_DEPLOYER, IMPL_SALT, PROXY_SALT, plan, read_artifact
from eth_utils import is_checksum_address, to_checksum_address
from web3 import Web3

IMPLEMENTATION_SLOT = '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc'
ADMIN_SLOT = '0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103'
PROXY_ADMIN_ABI = [
    {
        'name': 'upgradeAndCall',
        'type': 'function',
        'stateMutability': 'payable',
        'inputs': [
            {'name': 'proxy', 'type': 'address'},
            {'name': 'implementation', 'type': 'address'},
            {'name': 'data', 'type': 'bytes'},
        ],
        'outputs': [],
    },
    {
        'name': 'transferOwnership',
        'type': 'function',
        'stateMutability': 'nonpayable',
        'inputs': [{'name': 'newOwner', 'type': 'address'}],
        'outputs': [],
    },
    {
        'name': 'owner',
        'type': 'function',
        'stateMutability': 'view',
        'inputs': [],
        'outputs': [{'type': 'address'}],
    },
]
VERSION_SELECTOR = Web3.keccak(text='version()')[:4]


def slot_address(w3: Web3, contract: str, slot: str) -> str:
    return to_checksum_address(w3.eth.get_storage_at(contract, slot)[12:])


def create2_deploy(w3: Web3, acct, salt: bytes, initcode: bytes, expected: str) -> None:
    if w3.eth.get_code(expected):
        print(f'  {expected}: already deployed, skipping')
        return
    tx = {
        'to': ARACHNID_DEPLOYER,
        'data': '0x' + (salt + initcode).hex(),
        'from': acct.address,
        'nonce': w3.eth.get_transaction_count(acct.address),
        'chainId': w3.eth.chain_id,
    }
    tx['gas'] = int(w3.eth.estimate_gas(tx) * 1.2)
    tx.update(_fees(w3))
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    )
    if receipt.status != 1 or not w3.eth.get_code(expected):
        sys.exit(f'  deploy of {expected} failed (tx {receipt.transactionHash.hex()})')
    print(f'  {expected}: deployed in {receipt.transactionHash.hex()}')


def _fees(w3: Web3) -> dict:
    latest = w3.eth.get_block('latest')
    if 'baseFeePerGas' in latest:
        tip = w3.eth.max_priority_fee
        return {'maxFeePerGas': latest['baseFeePerGas'] * 2 + tip, 'maxPriorityFeePerGas': tip}
    return {'gasPrice': w3.eth.gas_price}


def send(w3: Web3, acct, fn) -> None:
    tx = fn.build_transaction(
        {
            'from': acct.address,
            'nonce': w3.eth.get_transaction_count(acct.address),
            'chainId': w3.eth.chain_id,
            **_fees(w3),
        }
    )
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    )
    if receipt.status != 1:
        sys.exit(f'tx failed: {receipt.transactionHash.hex()}')


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('--rpc', required=True)
    ap.add_argument(
        '--owner',
        required=True,
        help='ProxyAdmin owner; part of the proxy address, so use the same address on every chain',
    )
    ap.add_argument('--broadcast', action='store_true')
    ap.add_argument('--upgrade-to-current', action='store_true')
    ap.add_argument('--transfer-admin', metavar='MULTISIG')
    args = ap.parse_args()
    if not is_checksum_address(args.owner):
        sys.exit('--owner must be a checksummed address')

    w3 = Web3(Web3.HTTPProvider(args.rpc))
    p = plan(args.owner)
    print(
        f'chain id {w3.eth.chain_id}\n  implementation (genesis): {p["implementation"]}\n'
        f'  proxy:                    {p["proxy"]}\n  proxy admin:              {p["proxy_admin"]}'
    )
    if not w3.eth.get_code(ARACHNID_DEPLOYER):
        sys.exit('Arachnid CREATE2 deployer has no code on this chain; aborting')

    status = {k: bool(w3.eth.get_code(p[k])) for k in ('implementation', 'proxy', 'proxy_admin')}
    print('  on-chain:', {k: 'deployed' if v else 'missing' for k, v in status.items()})
    if not args.broadcast:
        print('dry run: pass --broadcast (and PRIVATE_KEY) to send transactions')
        return
    if not os.environ.get('PRIVATE_KEY'):
        sys.exit('PRIVATE_KEY env var required with --broadcast')
    acct = w3.eth.account.from_key(os.environ['PRIVATE_KEY'])

    create2_deploy(w3, acct, IMPL_SALT, p['impl_initcode'], p['implementation'])
    create2_deploy(w3, acct, PROXY_SALT, p['proxy_initcode'], p['proxy'])

    # verify wiring against the offline plan
    admin_addr = slot_address(w3, p['proxy'], ADMIN_SLOT)
    if admin_addr != p['proxy_admin']:
        sys.exit(f'proxy admin {admin_addr} does not match the offline plan {p["proxy_admin"]}')
    pa = w3.eth.contract(address=admin_addr, abi=PROXY_ADMIN_ABI)
    if args.upgrade_to_current:
        current = read_artifact('PoolProbe.current.initcode.hex')
        cur_impl = _create2_current(current)
        create2_deploy(w3, acct, IMPL_SALT, current, cur_impl)
        if slot_address(w3, p['proxy'], IMPLEMENTATION_SLOT) == cur_impl:
            print('  proxy already points at the current implementation, skipping upgrade')
        elif pa.functions.owner().call() != acct.address:
            call = pa.functions.upgradeAndCall(p['proxy'], cur_impl, b'')
            print(
                f'  signer is not the ProxyAdmin owner; submit this from the owner (multisig):\n'
                f'    to {admin_addr}\n    data {call._encode_transaction_data()}'
            )
        else:
            send(w3, acct, pa.functions.upgradeAndCall(p['proxy'], cur_impl, b''))
    if args.transfer_admin:
        target = to_checksum_address(args.transfer_admin)
        if not w3.eth.get_code(target):
            sys.exit(
                '--transfer-admin target has no code on this chain; refusing (one-step, irreversible)'
            )
        owner = pa.functions.owner().call()
        if owner == target:
            print('  ProxyAdmin already owned by target, skipping transfer')
        elif owner != acct.address:
            sys.exit(f'signer is not the ProxyAdmin owner ({owner}); cannot transfer')
        else:
            send(w3, acct, pa.functions.transferOwnership(target))

    impl_now = slot_address(w3, p['proxy'], IMPLEMENTATION_SLOT)
    version = int.from_bytes(w3.eth.call({'to': p['proxy'], 'data': VERSION_SELECTOR}), 'big')
    print(f'proxy -> implementation {impl_now}, version() = {version}')


def _create2_current(initcode: bytes) -> str:
    from common import create2_address

    # genesis and current share IMPL_SALT only if their initcode differs; if identical, same address
    return create2_address(ARACHNID_DEPLOYER, IMPL_SALT, initcode)


if __name__ == '__main__':
    main()
