import binascii

from native import ror, validate, validate_32


def ladon_calculation(x9, x8, x22):
    r_shifted = ror(x8, 8)
    r_res = ((r_shifted + x9) ^ x22) & 0xFFFFFFFFFFFFFFFF
    l_value_shifted = ((x9 >> 32 + 29) | (x9 << 3)) & 0xFFFFFFFFFFFFFFFF
    l_res = l_value_shifted ^ r_res

    return r_res, l_res


def get_ladon_keys(app_id: bytes) -> list:
    key = binascii.hexlify(app_id[0:4])
    md5_1 = int.from_bytes(key, byteorder="little")
    key = binascii.hexlify(app_id[4:8])
    md5_2 = int.from_bytes(key, byteorder="little")
    key = binascii.hexlify(app_id[8:12])
    md5_3 = int.from_bytes(key, byteorder="little")
    key = binascii.hexlify(app_id[12:16])
    md5_4 = int.from_bytes(key, byteorder="little")

    R0_list = [md5_2, md5_3, md5_4]
    L0_list = [md5_1]

    for i in range(0, 33):
        l_vaue, r_value = ladon_calculation(L0_list[i], R0_list[i], i)
        L0_list.append(validate(r_value))
        R0_list.append(validate(l_vaue))

    return L0_list


def encode_ladon(
        keys: list,
        da7c: int,
        da84: int,
        da88: int,
        da8c: int
):

    for key in keys:
        da70 = validate_32(da84 >> 8)
        da94 = validate_32(da88 << 0x18)
        da70 = validate_32(da70 | da94)
        da94 = validate_32(da8c + da70)

        if da70 < da94:
            da70 = 0
        else:
            da70 = 1

        da84 = validate_32(da84 << 0x18)
        da88 = validate_32(da88 >> 8)
        da84 = validate_32(da88 | da84)
        da84 = validate_32(da84 + da7c)
        da70 = validate_32(da70 + da84)

        da84 = validate_32(key & 0xFFFFFFFF)
        da84 = validate_32(da94 ^ da84)
        da88 = validate_32(key >> 32)
        da90 = validate_32(da8c << 3)
        da94 = validate_32(da7c >> 0x1d)
        da90 = validate_32(da90 | da94)
        da90 = validate_32(da90 ^ da84)
        da88 = validate_32(da88 ^ da70)
        da70 = validate_32(da8c >> 0x1d)
        da7c = validate_32(da7c << 3)
        da70 = validate_32(da70 | da7c)
        da7c = validate_32(da70 ^ da88)
        da8c = validate_32(da90 | 0x0)

    first = da8c.to_bytes(4, byteorder="little") + da7c.to_bytes(4, byteorder="little")
    second = da84.to_bytes(4, byteorder="little") + da88.to_bytes(4, byteorder="little")

    return first, second