# __init__.py Common utility functions for micropython-iot

# Released under the MIT licence.
# Copyright (C) Peter Hinch 2019-2020

# Now uses and requires uasyncio V3. This is incorporated in daily builds
# and release builds later than V1.12
# Under CPython requires CPython 3.8 or later.

# Create message ID's. Initially 0 then 1 2 ... 254 255 1 2
def gmid():
    mid = 0
    while True:
        yield mid
        mid = (mid + 1) & 0xff
        mid = mid if mid else 1

# Return True if a message ID has not already been received
def isnew(mid, lst=bytearray(32)):
    if mid == -1:
        for idx in range(32):
            lst[idx] = 0
        return
    idx = mid >> 3
    bit = 1 << (mid & 7)
    res = not(lst[idx] & bit)
    lst[idx] |= bit
    lst[(idx + 16 & 0x1f)] = 0
    return res

# Minimal implementation of set for integers in range 0-255
class SetByte:
    def __init__(self):
        self._ba = bytearray(32)

    def __bool__(self):
        return any(self._ba)

    def __contains__(self, i):
        return (self._ba[i >> 3] & 1 << (i & 7)) > 0

    def discard(self, i):
        self._ba[i >> 3] &= ~(1 << (i &7))

    def add(self, i):
        self._ba[i >> 3] |= 1 << (i & 7)
