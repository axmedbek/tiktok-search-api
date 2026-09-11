import base64
import hashlib
import random
import secrets
import struct
import zlib
from typing import Optional

from gmssl import sm3

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from cipher.RC4 import RC4
from cipher.SIMON import SIMON
from exception import UnsupportedDynVersion
from native import reverse_bytes, reverse_bits_native, byteswap, bit_swap, byteswap_32
from protobuf.protobuf import ProtoBuf


# TikTok sign_key (base64 decoded)
SIGN_KEY = base64.b64decode("wC8lD4bMTxmNVwY5jSkqi3QWmrphr/58ugLko7UZgWM=")


def do_cipher(data: bytes, key: str) -> int:
    cipher = AES.new(key[:16].encode(), AES.MODE_OFB, iv=key[16:].encode())
    data_padded = pad(data, 16)
    encrypted = cipher.encrypt(data_padded)
    return int.from_bytes(encrypted[-4:], "little")


def simon_encode(pt: list, key: list):
    return SIMON().encode(pt=pt, k=key)


def get_request_hash(
        data: bytes
):
    return bytes.fromhex(sm3.sm3_hash(msg=data))[0:6]


def dyn_encode(
        dyn_version: int,
        params: bytes,
        payload: bytes,
        rand: int
):
    if dyn_version > 8:
        raise UnsupportedDynVersion(f"This version - {dyn_version} is not supported")

    if dyn_version == 1:
        unk_hash = hashlib.md5(bytes([0, 0, 0, 1])).digest()
        ss_stub_hash = hashlib.md5(payload).digest()
        params_hash = hashlib.md5(params).digest()

        unk = int.from_bytes(unk_hash[:4], 'little') ^ rand
        ss_stub = int.from_bytes(ss_stub_hash[:4], 'little') ^ rand
        params = int.from_bytes(params_hash[:4], 'little') ^ rand

        res = (
                unk.to_bytes(4, 'big').hex() +
                ss_stub.to_bytes(4, 'big').hex() +
                params.to_bytes(4, 'big').hex()
        )
        return res

    elif dyn_version == 2:
        unk_hash = hashlib.md5(bytes([0, 0, 0, 1])).digest()
        ss_stub_hash = hashlib.md5(payload).digest()
        params_hash = hashlib.md5(params).digest()

        unk = int.from_bytes(reverse_bytes(unk_hash[:4]), 'little') ^ rand
        ss_stub = int.from_bytes(reverse_bytes(ss_stub_hash[:4]), 'little') ^ rand
        params = int.from_bytes(reverse_bytes(params_hash[:4]), 'little') ^ rand

        res = (
                unk.to_bytes(4, 'big').hex() +
                ss_stub.to_bytes(4, 'big').hex() +
                params.to_bytes(4, 'big').hex()
        )
        return res

    elif dyn_version == 3:
        unk_hash = hashlib.md5(bytes([0, 0, 0, 1])).digest()
        ss_stub_hash = hashlib.md5(payload).digest()
        params_hash = hashlib.md5(params).digest()

        unk = int.from_bytes(unk_hash[:4], 'big').to_bytes(4, 'little')
        unk = int.from_bytes(unk, 'big') ^ 0x5A5A5A5A ^ rand

        ss_stub = int.from_bytes(ss_stub_hash[:4], 'big').to_bytes(4, 'little')
        ss_stub = int.from_bytes(ss_stub, 'big') ^ 0x5A5A5A5A ^ rand

        params = int.from_bytes(params_hash[:4], 'big').to_bytes(4, 'little')
        params = int.from_bytes(params, 'big') ^ 0x5A5A5A5A ^ rand

        res = (
                (unk & 0xFFFFFFFF).to_bytes(4, 'big') +  # Ensure unk fits in 4 bytes
                (ss_stub & 0xFFFFFFFF).to_bytes(4, 'big') +  # Ensure ss_stub fits in 4 bytes
                (params & 0xFFFFFFFF).to_bytes(4, 'big')  # Ensure params fits in 4 bytes
        )

        return res.hex()

    elif dyn_version == 4:
        unk_hash = hashlib.md5(bytes([0, 0, 0, 1])).digest()
        ss_stub_hash = hashlib.md5(payload).digest()
        params_hash = hashlib.md5(params).digest()

        unk = reverse_bits_native(int.from_bytes(unk_hash[:4], 'big')) ^ rand
        ss_stub = reverse_bits_native(int.from_bytes(ss_stub_hash[:4], 'big')) ^ rand
        params = reverse_bits_native(int.from_bytes(params_hash[:4], 'big')) ^ rand

        res = (
                unk.to_bytes(4, 'big').hex() +
                ss_stub.to_bytes(4, 'big').hex() +
                params.to_bytes(4, 'big').hex()
        )
        return res

    elif dyn_version == 5:
        unk_hash = bytes.fromhex(sm3.sm3_hash(bytearray([0, 0, 0, 1])))
        ss_stub_hash = bytes.fromhex(sm3.sm3_hash(bytearray(payload)))
        params_hash = bytes.fromhex(sm3.sm3_hash(bytearray(params)))

        unk = int.from_bytes(unk_hash[28:], 'little') ^ rand
        ss_stub = int.from_bytes(ss_stub_hash[28:], 'little') ^ rand
        params = int.from_bytes(params_hash[28:], 'little') ^ rand

        res = (
                unk.to_bytes(4, 'big').hex() +
                ss_stub.to_bytes(4, 'big').hex() +
                params.to_bytes(4, 'big').hex()
        )
        return res

    elif dyn_version == 6:
        key = hashlib.md5(rand.to_bytes(4, 'big')).hexdigest()

        params = do_cipher(params, key=key) ^ rand
        ss_stub = do_cipher(payload, key=key) ^ rand
        unk = do_cipher(bytes([0, 0, 0, 1]), key=key) ^ rand

        res = (
                unk.to_bytes(4, 'big').hex() +
                ss_stub.to_bytes(4, 'big').hex() +
                params.to_bytes(4, 'big').hex()
        )
        return res

    elif dyn_version == 7:
        key = hashlib.md5(rand.to_bytes(4, byteorder="big")).digest().hex()

        rc4 = RC4(key.encode())
        rc4.init()

        unk = bytearray(rc4.encrypt(bytes.fromhex("00000001")))
        for i in range(0, len(unk)):
            unk[i] = byteswap(bit_swap(unk[i]))

        rand = byteswap_32(rand)
        unk = int.from_bytes(unk, byteorder="big") ^ rand

        x_ss_stub = (
                int.from_bytes(
                    hashlib.md5(payload).digest()[-4:], byteorder="big"
                )
                ^ rand
        )
        params = (
                int.from_bytes(
                    hashlib.sha256(params).digest()[0:4], byteorder="big"
                )
                ^ 0x5A5A5A5A
                ^ rand
        )

        res = (
                unk.to_bytes(4, 'little').hex() +
                x_ss_stub.to_bytes(4, 'little').hex() +
                params.to_bytes(4, 'little').hex()
        )
        return res

    elif dyn_version == 8:
        unk = bytearray(hashlib.sha256(bytes.fromhex("00000001")).digest()[0:4])
        for i in range(0, len(unk)):
            unk[i] = byteswap(unk[i])

        x_ss_stub = bytearray(
            (zlib.crc32(payload) & 0xFFFFFFFF).to_bytes(
                4, byteorder="big"
            )
        )
        for i in range(0, len(x_ss_stub)):
            x_ss_stub[i] = byteswap(bit_swap(x_ss_stub[i]))

        params = hashlib.sha1(params).digest()[0:4]

        part_1 = int.from_bytes(unk, byteorder="little") ^ rand
        part_2 = int.from_bytes(x_ss_stub, byteorder="little") ^ rand
        part_3 = int.from_bytes(params, byteorder="little") ^ 0x5A5A5A5A ^ rand

        return (
                part_1.to_bytes(4, byteorder="big").hex()
                + part_2.to_bytes(4, byteorder="big").hex()
                + part_3.to_bytes(4, byteorder="big").hex()
        )


def encrypt_enc_pb(data: bytes, length: int) -> bytes:
    data = list(data)
    xor_array = data[:8]
    for i in range(8, length):
        data[i] ^= xor_array[i % 4]

    data = data[::-1]
    data = bytes(data)

    return data


def has_captured_pair(dyn_seed: Optional[str], rand: Optional[int]) -> bool:
    """True when BOTH halves of the captured `(f24 dyn_seed, f3 rand)` pair are
    present, which is the only condition under which the corrected v46.9 argus
    payload is emitted.

    Measured 2026-09-11 on api32-core-alisg.tiktokv.com `/aweme/v1/aweme/post/`:
    `f24` on its own is refused, and so is a freely chosen `f3` beside a good
    `f24`. There is therefore no such thing as half a pair, and a caller holding
    only one half gets the legacy payload rather than a signature that is
    wrong in a way the gateway answers with an empty body.

    A half pair cannot reach here from this project: `ClientConfig` refuses to
    be constructed with one, and `identity_manager.Identity` refuses to hold
    one, so both halves arrive together or neither does.
    """
    return bool(dyn_seed) and rand is not None


def _corrected_protobuf(
        params: bytes,
        payload: bytes,
        device_type: str,
        ts: int,
        app_version: str,
        app_id: int,
        license_id: int,
        sdk_version: str,
        sdk_version_code: int,
        dyn_seed: str,
        dyn_version: int,
        rand_value: int,
) -> bytes:
    """The argus payload the real 46.9.1 app emits, as measured against
    api32-core-alisg.tiktokv.com on 2026-09-11.

    EVERY constant here differs from the shipped legacy payload below and each
    one was read off a decoded app argus. Do NOT "correct" one back to the
    legacy value: the two payloads are accepted by DIFFERENT gateways, and the
    legacy values are kept verbatim in `generate_protobuf` for the search
    gateway that still accepts them.

    Note `app_launch_time`, `device_id` and `device_token` are absent from the
    parameter list on purpose — see `generate_protobuf`.
    """
    proto = {
        1: 0x20200929 << 1,       # f1
        2: 2,                     # f2
        3: rand_value << 1,       # f3 — the CAPTURED rand, also fed to dyn_encode
        4: str(app_id),           # f4
        6: str(license_id),       # f6
        7: app_version,           # f7
        8: sdk_version,           # f8
        9: sdk_version_code << 1,  # f9
        10: bytes(8),             # f10
        12: ts << 1,              # f12
        13: get_request_hash(bytearray(payload)),  # f13
        14: get_request_hash(bytearray(params)),   # f14
        15: {
            1: 9 << 1,   # f15.1 — a FIXED 9; the legacy payload randomises 20..250
            6: 3 << 1,   # f15.6 — absent from the legacy payload
            7: ts << 1,  # f15.7 — the REQUEST ts, not app_launch_time
        },                        # f15
        17: ts << 1,              # f17
        20: "none",               # f20
        21: 312 << 1,             # f21
        23: {
            1: device_type,
            2: 9 << 1,           # f23.2 — legacy sends 5
            3: 'googleplay',
            4: 76593152 << 1,    # f23.4 — legacy sends 209748992
            5: 13631488 << 1,    # f23.5 — absent from the legacy payload
        },                        # f23
        24: dyn_seed,             # f24 — the captured seed; refused when absent
        25: 3 << 1,               # f25 — legacy sends 1, and 5 in its dyn branch
        26: {
            1: dyn_version << 1,
            # Shares ONE rand with f3 above, which is the whole point of taking
            # `rand` as a parameter: the two are consistent by construction.
            2: bytes.fromhex(dyn_encode(dyn_version=dyn_version, params=params,
                                        payload=payload, rand=rand_value)),
        },                        # f26
        28: 5 << 1,               # f28 — legacy sends 1008
        29: 2 << 1,               # f29 dyn_inverse_ver — legacy emits 516112 UNSHIFTED
        30: 6 << 1,               # f30 dyn_task_req_status — legacy emits 6 UNSHIFTED
        31: 477937796 << 1,       # f31 — legacy sends 620944317
        33: 2 << 1,               # f33 — absent from the legacy payload
    }
    proto = {key: proto[key] for key in sorted(proto.keys(), reverse=False)}
    return ProtoBuf(proto).toBuf()


def generate_protobuf(
        params: bytes,
        payload: bytes,
        app_launch_time: int,
        device_type: str,
        ts: int,
        app_version: str,
        app_id: int,
        license_id: int,
        sdk_version: str,
        sdk_version_code: int,
        device_id: Optional[str],
        device_token: Optional[str],
        dyn_seed: Optional[str],
        dyn_version: Optional[int],
        rand: Optional[int] = None,

) -> bytes:
    """The argus protobuf for one request.

    `rand` is f3 AND the `rand` handed to `dyn_encode` — one value, so the two
    cannot drift apart. Pass the CAPTURED f3 alongside the captured f24
    `dyn_seed` and the corrected v46.9 payload is emitted; with either half
    missing this falls through to the payload this file has always produced,
    `rand` drawn at random exactly as before, so the search gateway keeps
    receiving byte-for-byte what it has been accepting.

    `app_launch_time`, `device_id` (f5) and `device_token` (f16) are used by
    the legacy payload only. The real app emits no f5 and no f16, and puts the
    request `ts` where this file put `app_launch_time` (f15.7) — so on the
    corrected path all three arguments are unused. They stay in the signature
    because the legacy path still needs them.
    """
    if has_captured_pair(dyn_seed, rand):
        return _corrected_protobuf(
            params=params, payload=payload, device_type=device_type, ts=ts,
            app_version=app_version, app_id=app_id, license_id=license_id,
            sdk_version=sdk_version, sdk_version_code=sdk_version_code,
            dyn_seed=dyn_seed, dyn_version=dyn_version, rand_value=rand)
    rand_value = random.randint(0, 0x7fffffff)
    proto = {
        1: 0x20200929 << 1, # f1
        2: 2,               # f2 
        3: rand_value << 1, # f3
        4: str(app_id),       # f4
        6: str(license_id),  # f6
        7: app_version,   # f7
        8: sdk_version, # f8
        9: sdk_version_code << 1, # f9
        10: bytes(8),      # f10
        12: ts << 1,     # f12
        13: get_request_hash(bytearray(payload)), # f13
        14: get_request_hash(bytearray(params)),  # f14
        15: { 
            1: random.randint(20, 250) << 1,
            7: app_launch_time << 1
        }, # f15
        17: ts << 1,    # f17
        20: "none",     # f20
        21: 312 << 1,   # f21
        23: {
            1: device_type,
            2: 5 << 1,
            3: 'googleplay',
            4: 209748992 << 1
        }, # f23
        25: 1 << 1,    # f25
        28: 1008 << 1  # f26
    }

    if device_id:
        proto[5] = device_id

    if device_token:
        proto[16] = device_token

    if dyn_seed:
        proto[24] = dyn_seed
        proto[25] = 5 << 1

        proto[26] = {
            1: dyn_version << 1,
            2: bytes.fromhex(dyn_encode(dyn_version=dyn_version, params=params, payload=payload, rand=rand_value))
        }
        
        proto[29] = 516112      # dyn_inverse_ver
        proto[30] = 6           # dyn_task_req_status
        proto[31] = 620944317 << 1

    proto = {key: proto[key] for key in sorted(proto.keys(), reverse=False)}
    return ProtoBuf(proto).toBuf()


def mix(
        key: bytes
) -> int:
    A = 0
    T = 0
    for i in range(0, len(key), 2):
        B = key[i] ^ A
        C = (T >> 0x3) & 0xFFFFFFFF
        D = C ^ B
        E = D ^ T
        F = (E >> 0x5) & 0xFFFFFFFF
        G = (E << 0xB) & 0xFFFFFFFF
        H = key[i + 1] | G
        I = F ^ H
        J = I ^ E
        T = ~J & 0xFFFFFFFF

        return T


def _pad_outer(data: bytes, corrected: bool) -> bytes:
    """Padding for the OUTER AES-CBC block.

    The real 46.9.1 app writes only the pad LENGTH — in the final byte — and
    fills the rest with random bytes. The legacy path keeps PKCS7 (N bytes each
    equal to N), which is what this file shipped and what the search gateway
    accepts. The INNER protobuf pad is PKCS7 on BOTH paths and is not touched
    here; see `encode_argus_fn`'s first line.
    """
    if not corrected:
        return pad(data, AES.block_size)
    fill = AES.block_size - len(data) % AES.block_size
    return data + secrets.token_bytes(fill - 1) + bytes([fill])


def encode_argus_fn(
        protobuf: bytes,
        sign_key: bytes = SIGN_KEY,
        corrected: bool = False
) -> str:
    """Encrypt one argus protobuf.

    `corrected` selects the 46.9.1 app's own framing — header byte 7 and the
    outer pad — and is set by the same captured-pair test that selects the
    corrected protobuf, so the two halves of a signature can never be mixed.
    """

    # INNER pad: PKCS7 on both paths. The `corrected` framing applies to the
    # OUTER AES block only (`_pad_outer`).
    protobuf = pad(protobuf, AES.block_size)

    length = len(protobuf)
    random_bytes = bytes.fromhex(secrets.token_hex(4))

    sm3_buffer = sign_key + random_bytes + sign_key
    sm3_output = bytes.fromhex(sm3.sm3_hash(bytearray(sm3_buffer)))
    key = sm3_output[:32]

    key_list = []

    for i in range(0, 2):
        key_list = key_list + list(struct.unpack("<QQ", key[i * 16:i * 16 + 16]))

    pointer = bytearray(length)

    for i in range(int(length / 16)):
        pt = list(struct.unpack("<QQ", protobuf[i * 16:i * 16 + 16]))
        ct = simon_encode(pt, key_list)
        pointer[i * 16: i * 16 + 8] = ct[0].to_bytes(8, byteorder='little')
        pointer[i * 16 + 8: i * 16 + 16] = ct[1].to_bytes(8, byteorder='little')

    pointer = pointer[:length]

    mixed = mix(random_bytes[2:4])
    mixed = struct.pack(">I", mixed)
    mixed = mixed[::-1]

    xor_key = mixed + mixed
    pointer = xor_key + pointer

    b_buffer = encrypt_enc_pb(pointer, length + 8)

    headers = [
        0xec,
        random.randint(0x10, 0xFF),
        random.randint(0x10, 0xFF),
        random.randint(0x10, 0xFF),
        random.randint(0x10, 0xFF),
        0x01,
        random.randint(0x10, 0xFF),
        # Byte 7 — the real 46.9.1 app sends 0x00 here; this file shipped a
        # hardcoded 0x02, which is kept for the legacy path.
        0x00 if corrected else 0x02,
        0x18
    ]

    b_buffer = bytes(headers) + b_buffer + random_bytes[2:4]

    aes_key = hashlib.md5(sign_key[:16]).digest()
    aes_iv = hashlib.md5(sign_key[16:]).digest()

    cipher = AES.new(aes_key, AES.MODE_CBC, aes_iv)

    output = cipher.encrypt(_pad_outer(b_buffer, corrected))
    output = random_bytes[0:2] + output

    x_argus = base64.b64encode(output).decode()

    return x_argus



