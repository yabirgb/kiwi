# pragma version 0.4.3
dec: public(uint8)
balances: HashMap[address, uint256]

@deploy
def __init__(d: uint8):
    self.dec = d

@external
def set_balance(a: address, v: uint256):
    self.balances[a] = v

@external
@view
def balanceOf(a: address) -> uint256:
    return self.balances[a]

@external
@view
def decimals() -> uint8:
    return self.dec
