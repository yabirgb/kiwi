# pragma version 0.4.3
pools: HashMap[bytes32, address]
raw_word: public(uint256)
use_raw: public(bool)

@external
def set_pool(a: address, b: address, p: uint256, pool: address):
    self.pools[keccak256(abi_encode(a, b, p))] = pool

@external
def set_raw(w: uint256):
    self.raw_word = w
    self.use_raw = True

@external
@view
def getPair(a: address, b: address) -> uint256:
    if self.use_raw:
        return self.raw_word
    return convert(self.pools[keccak256(abi_encode(a, b, empty(uint256)))], uint256)

@external
@view
def getPool(a: address, b: address, p: uint24) -> uint256:
    if self.use_raw:
        return self.raw_word
    return convert(self.pools[keccak256(abi_encode(a, b, convert(p, uint256)))], uint256)
