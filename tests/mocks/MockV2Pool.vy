# pragma version 0.4.3
r0: uint112
r1: uint112

@deploy
def __init__(a: uint112, b: uint112):
    self.r0 = a
    self.r1 = b

@external
@view
def getReserves() -> (uint112, uint112, uint32):
    return self.r0, self.r1, 0
