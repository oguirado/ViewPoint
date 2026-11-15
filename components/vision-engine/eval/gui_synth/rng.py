"""Portable integer-only SplitMix64, including unbiased inclusive ranges."""
MASK = (1 << 64) - 1


class SplitMix64:
    def __init__(self, seed):
        if type(seed) is not int:
            raise TypeError('integer seed required')
        self.state = seed & MASK

    def u64(self):
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        return (z ^ (z >> 31)) & MASK

    def randint(self, lo, hi):
        if type(lo) is not int or type(hi) is not int or hi < lo:
            raise ValueError('integer inclusive range required')
        span = hi - lo + 1
        if span > 1 << 64:
            raise ValueError('range exceeds 64 bits')
        limit = (1 << 64) - ((1 << 64) % span)
        while True:
            z = self.u64()
            if z < limit:
                return lo + z % span
