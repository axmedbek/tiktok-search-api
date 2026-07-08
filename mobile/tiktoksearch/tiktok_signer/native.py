

def reverse_bits(x: int) -> int:
    x = ((x & 0x55555555) << 1) | ((x & 0xAAAAAAAA) >> 1)
    x = ((x & 0x33333333) << 2) | ((x & 0xCCCCCCCC) >> 2)
    x = ((x & 0x0F0F0F0F) << 4) | ((x & 0xF0F0F0F0) >> 4)
    return x


def ror(x: int, v: int) -> int:
    a = (x << (64 - v)) | (x >> v)
    return a & 0xffffffffffffffff


def validate(x: int) -> int:
    return x & 0xffffffffffffffff


def validate_32(x: int) -> int:
    return x & 0xffffffff


def get_bit(val: int, pos: int) -> int:
    return 1 if val & (1 << pos) else 0


def rotate_left(v: int, n: int) -> int:
    r = (v << n) | (v >> (64 - n))
    return r & 0xffffffffffffffff


def rotate_right(v: int, n: int) -> int:
    r = (v << (64 - n)) | (v >> n)
    return r & 0xffffffffffffffff


def reverse_bits_native(n: int, bit_length: int = 32) -> int:
    return int(bin(n)[2:].zfill(bit_length)[::-1], 2)


def bit_swap(value: int):
    odd_bits = value & 0x55
    even_bits = value & 0xAA

    swapped = (odd_bits << 1) | (even_bits >> 1)

    odd_bits = swapped & 0x33
    even_bits = swapped & 0xCC

    result = (odd_bits << 2) | (even_bits >> 2)

    return result


def byteswap_32(val: int):
    return (
            ((val & 0xFF000000) >> 24)
            | ((val & 0x00FF0000) >> 8)
            | ((val & 0x0000FF00) << 8)
            | ((val & 0x000000FF) << 24)
    )


def byteswap(val: int):
    return ((val & 0xF) << 4) | ((val & 0xF0) >> 4)


def reverse_bytes(hash_bytes: bytes) -> bytes:
    return bytes([((b & 0xF) << 4) | ((b >> 4) & 0xF) for b in hash_bytes])