from ctypes import c_ulonglong

from native import rotate_left, rotate_right, get_bit


class SIMON(object):

    def encode(self, pt: list, k: list, c: int = 0):
        tmp = 0
        f = 0

        key = [0] * 72
        key[0] = k[0]
        key[1] = k[1]
        key[2] = k[2]
        key[3] = k[3]

        key = self.key_expansion(key=key)

        x_i = pt[0]
        x_i1 = pt[1]

        for i in range(72):
            tmp = x_i1
            f = rotate_left(x_i1, 1) if c == 1 else rotate_left(x_i1, 1) & rotate_left(x_i1, 8)
            x_i1 = x_i ^ f ^ rotate_left(x_i1, 2) ^ key[i]
            x_i = tmp

        ct = [x_i, x_i1]
        return ct

    def decode(self):
        raise NotImplemented()

    @staticmethod
    def key_expansion(key: list) -> list:
        tmp = 0
        for i in range(4, 72):
            tmp = rotate_right(key[i - 1], 3)
            tmp = tmp ^ key[i - 3]
            tmp = tmp ^ rotate_right(tmp, 1)
            key[i] = c_ulonglong(~key[i - 4]).value ^ tmp ^ get_bit(0x3DC94C3A046D678B, (i - 4) % 62) ^ 3

        return key