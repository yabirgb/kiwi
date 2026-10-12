# pragma version 0.4.3
sqrt_p: uint160
liq: uint128

@deploy
def __init__(s: uint160, l: uint128):
    self.sqrt_p = s
    self.liq = l

@external
@view
def slot0() -> (uint160, int24, uint16, uint16, uint16, uint8, bool):
    return self.sqrt_p, 0, 0, 0, 0, 0, True

@external
@view
def liquidity() -> uint128:
    return self.liq
