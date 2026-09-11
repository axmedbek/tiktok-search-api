"""Recover the captured argus `(f24 dyn_seed, f3 rand)` pair out of an `x-argus`.

The user-scoped endpoints on `api32-core-alisg.tiktokv.com` are refused unless
the argus payload carries a real `f24` seed beside the `f3` rand it was minted
with (measured 2026-09-11: `f24` alone is refused, and a freely chosen `f3`
beside a good `f24` is refused too). The seed is minted by `POST /ms/get_seed`,
whose response is encrypted, so the only way to read the pair back out is to
decrypt an `x-argus` the app itself sent — which is what this module does.

It is the other half of the capture path: `capture_identity_addon.py` consumes
`{"dyn_seed": ..., "dyn_rand": ...}` from a file (`--set dyn_pair=`), and this
is what writes that file. It is deliberately NOT part of the addon: decrypting
needs `pycryptodome` + `gmssl`, and the addon is stdlib-only at import time so
mitmdump can load it in any environment (same split, and the same reason, as
`capture_record.py` vs `capture_diff.py`).

Usage:

    # from a mitmproxy flow file (the app must have VISITED A PROFILE, so the
    # capture actually contains a posts request — see --path below)
    ../.venv/bin/python -m tiktoksearch.argus_pair --flow cap.mitm -o dyn_pair.json

    # or from one raw x-argus header value
    ../.venv/bin/python -m tiktoksearch.argus_pair --argus '<base64>' -o dyn_pair.json

    # prove the decryptor still inverts our own signer, on invented values
    ../.venv/bin/python -m tiktoksearch.argus_pair --self-test

`--flow` shells out to a SEPARATE interpreter (`--mitmproxy-python`, default
`/usr/bin/python3`) to read the flow file. mitmproxy is not installed in the
project venv and putting its dist-packages directory on a venv script's
`sys.path` breaks `cffi`/`pycryptodome` — measured — so the two halves must not
share a process. The subprocess only ever prints `host<TAB>path<TAB>x-argus`;
all crypto happens here.

**The recovered pair is a SECRET of the same class as the cookie.** Nothing in
this module logs, prints or returns it: diagnostics carry its LENGTH and a
SHA-256 prefix and nothing else, and the value reaches disk only through the
output file, which `.gitignore` covers as `dyn_pair*.json`.

The decrypt chain, inverted from `helpers.argus.encode_argus_fn`:

  1. base64-decode; the first 2 bytes are the first half of the 4-byte seed
     material that keys SIMON
  2. AES-CBC decrypt the rest, key `md5(SIGN_KEY[:16])`, iv `md5(SIGN_KEY[16:])`
  3. the plaintext is `[9-byte header][region][2 more seed bytes][pad]`; the pad
     records its own length in its final byte on BOTH framings (PKCS7 on the
     legacy path, length-only random filler on the corrected one), so
     `region_end = len(buf) - buf[-1] - 2`
  4. invert `encrypt_enc_pb` over the region — it xors then REVERSES, so the
     inverse reverses then un-xors; it is not an involution
  5. drop the leading 8 bytes (the `mix()` xor-key, which the encoder prepends
     and which carries no payload); the rest is SIMON-128/256 ciphertext
  6. SIMON-decrypt in 16-byte `<QQ` blocks under `SM3(SIGN_KEY ‖ seed4 ‖
     SIGN_KEY)[:32]`
  7. strip the inner PKCS7 tail — what is left is the argus protobuf
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

from Crypto.Cipher import AES
from gmssl import sm3

# Importing the package FIRST installs the sys.path shim the vendored modules
# need (see `tiktok_signer/__init__`), which is what makes the flat imports
# below resolve. They must stay flat: `helpers.argus` and `cipher.SIMON` import
# each other flatly, so reaching the same files by their package path would
# yield second module objects with different class identities — the exact trap
# that `__init__` documents for the exception re-exports.
from .tiktok_signer import ProtoError
from cipher.SIMON import SIMON  # noqa: E402  (resolved via the shim above)
from helpers.argus import SIGN_KEY  # noqa: E402
from native import rotate_left  # noqa: E402
from protobuf.protobuf import ProtoBuf  # noqa: E402

logger = logging.getLogger('tiktoksearch.argus_pair')

# SIMON-128/256: 72 rounds, four 64-bit key words. Both numbers are fixed by
# the cipher and by `SIMON.key_expansion`, which sizes its schedule to 72.
SIMON_ROUNDS = 72
SIMON_KEY_WORDS = 4
# The encoder's framing, all of it fixed by `encode_argus_fn`.
# The SM3 salt that keys SIMON: 4 bytes, split by the encoder — the first two
# ride in front of the AES ciphertext, the last two inside it, behind the
# payload region.
SEED_HEAD_BYTES = 2
SEED_TAIL_BYTES = 2
HEADER_BYTES = 9          # the plaintext header `encode_argus_fn` writes
XOR_KEY_BYTES = 8         # the `mix()` key prepended to the SIMON ciphertext
AES_BLOCK = AES.block_size
# Protobuf field numbers inside the argus payload.
FIELD_DYN_SEED = 24       # f24 — the `/ms/get_seed` seed, base64, 132 chars
FIELD_RAND = 3            # f3 — stored shifted left by one, like every scalar
# What a captured `f24` looks like, used only to warn on a surprising shape.
EXPECTED_SEED_LEN = 132
# The endpoint the pair is harvested FOR. Made the default `--path` because a
# pair taken from a request the app sent to another endpoint has been measured
# to be refused on the posts endpoint (2026-09-11, with the honest caveat that
# the seed itself also differed between the two runs, so the binding is not
# cleanly isolated). Defaulting to the destination endpoint costs nothing when
# the hypothesis is wrong and saves a wasted capture when it is right.
DEFAULT_FLOW_PATH = '/aweme/v1/aweme/post/'
# Read the flow file with a SEPARATE interpreter — see the module docstring.
DEFAULT_MITMPROXY_PYTHON = '/usr/bin/python3'
_FLOW_DUMP_SOURCE = '''
import sys
from mitmproxy.io import FlowReader
with open(sys.argv[1], "rb") as fh:
    for flow in FlowReader(fh).stream():
        req = getattr(flow, "request", None)
        if req is None:
            continue
        argus = req.headers.get("x-argus")
        if argus:
            sys.stdout.write("%s\\t%s\\t%s\\n" % (req.pretty_host, req.path.split("?")[0], argus))
'''


class ArgusDecodeError(ValueError):
    """An `x-argus` this module could not turn back into a protobuf.

    A `ValueError`, because that is what every other bad-external-input failure
    in the signing stack raises and what `signing._SIGNING_FAILURES` already
    covers. The message names the failure, never any decrypted byte.
    """


@dataclass(frozen=True, slots=True)
class DynPair:
    """One captured `(f24, f3)` pair, in the shape the capture addon consumes.

    SECRET — the same class as the cookie. `repr` is suppressed on `dyn_seed`
    so the value cannot reach a log line, a traceback or a debugger dump by
    accident; `fingerprint()` is what diagnostics are allowed to say.
    """
    # `repr=False`: the seed is half a live credential, and a dataclass repr is
    # the easiest way for one to reach a log line, a traceback frame or a
    # debugger dump. `fingerprint()` is what diagnostics may say instead.
    dyn_seed: str = field(repr=False)
    dyn_rand: int

    def as_dict(self) -> dict[str, object]:
        """The `{"dyn_seed": ..., "dyn_rand": ...}` object
        `capture_identity_addon.py --set dyn_pair=<path>` reads."""
        return {'dyn_seed': self.dyn_seed, 'dyn_rand': self.dyn_rand}

    def fingerprint(self) -> str:
        """A non-reversible description, for logs and stdout.

        Length plus a SHA-256 prefix: enough to tell two pairs apart and to
        tell a re-capture from a no-op, and it discloses neither half. The
        `dyn_rand` is hashed rather than printed because it is half of the
        credential, not a harmless counter.
        """
        seed_h = hashlib.sha256(self.dyn_seed.encode()).hexdigest()[:12]
        rand_h = hashlib.sha256(str(self.dyn_rand).encode()).hexdigest()[:12]
        return f'seed(len={len(self.dyn_seed)}, sha256={seed_h}) rand(sha256={rand_h})'


def _simon_key_schedule(seed4: bytes) -> list[int]:
    """The 72-word SIMON schedule `encode_argus_fn` signs with, for `seed4`.

    Built through the vendored `SIMON.key_expansion` rather than a copy of it,
    so the encoder and this decoder can never disagree about the schedule.
    """
    digest = bytes.fromhex(sm3.sm3_hash(bytearray(SIGN_KEY + seed4 + SIGN_KEY)))[:32]
    words: list[int] = []
    for i in range(2):
        words += list(struct.unpack('<QQ', digest[i * 16:i * 16 + 16]))
    schedule = [0] * SIMON_ROUNDS
    schedule[:SIMON_KEY_WORDS] = words[:SIMON_KEY_WORDS]
    return SIMON.key_expansion(key=schedule)


def _simon_decode_block(ct: tuple[int, int], schedule: list[int]) -> tuple[int, int]:
    """One SIMON-128/256 block, decrypted.

    `cipher.SIMON.decode` is `NotImplemented` in the vendored tree, and the
    inverse lives here rather than in that file so the vendored code stays
    exactly as vendored. The forward round in `SIMON.encode` is

        a1 = b0                                  ; b1 = a0 ^ f(b0) ^ key[i]
        f(x) = (ROL(x,1) & ROL(x,8)) ^ ROL(x,2)

    so running the rounds backwards gives `b0 = a1` and
    `a0 = b1 ^ f(a1) ^ key[i]`.
    """
    a, b = ct
    for i in range(SIMON_ROUNDS - 1, -1, -1):
        b_prev = a
        f = (rotate_left(b_prev, 1) & rotate_left(b_prev, 8)) ^ rotate_left(b_prev, 2)
        a = b ^ f ^ schedule[i]
        b = b_prev
    return a, b


def _strip_pkcs7(buf: bytes) -> bytes:
    """The inner PKCS7 tail removed, with the length byte validated.

    Checked rather than trusted: a wrong SIMON key produces plausible-looking
    bytes whose final one is arbitrary, and slicing on it would hand back a
    silently truncated payload that `ProtoBuf` might even parse. The pad's
    every byte must equal its length, which a wrong key passes with
    probability ~2**-8N.
    """
    if not buf:
        raise ArgusDecodeError('decrypted payload is empty')
    size = buf[-1]
    if not 1 <= size <= AES_BLOCK or size > len(buf):
        raise ArgusDecodeError(f'inner pad length {size} is not a valid PKCS7 pad')
    if buf[-size:] != bytes([size]) * size:
        raise ArgusDecodeError('inner PKCS7 pad is malformed — wrong sign key or a re-framed argus')
    return buf[:-size]


def decrypt_argus(x_argus: str, *, sign_key: bytes = SIGN_KEY) -> bytes:
    """The argus protobuf inside `x_argus`, as raw bytes.

    Inverts `helpers.argus.encode_argus_fn` step for step; see the module
    docstring for the chain. Raises `ArgusDecodeError` for anything that is not
    an argus this sign key can open — never a bare `binascii`/`struct` error,
    and never with a decrypted byte in the message.
    """
    try:
        raw = base64.b64decode(x_argus, validate=True)
    except (ValueError, TypeError) as exc:
        raise ArgusDecodeError(f'x-argus is not valid base64: {type(exc).__name__}') from exc
    body = raw[SEED_HEAD_BYTES:]
    if len(body) < AES_BLOCK or len(body) % AES_BLOCK:
        raise ArgusDecodeError(f'x-argus body is {len(body)} bytes, not a whole number of AES blocks')
    cipher = AES.new(hashlib.md5(sign_key[:16]).digest(), AES.MODE_CBC,
                     hashlib.md5(sign_key[16:]).digest())
    buf = cipher.decrypt(body)
    # The tail: `[... region][2 seed bytes][pad]`, the pad's length in its own
    # final byte. See the module docstring, step 3.
    region_end = len(buf) - buf[-1] - SEED_TAIL_BYTES
    if region_end <= HEADER_BYTES + XOR_KEY_BYTES:
        raise ArgusDecodeError('x-argus plaintext has no payload region — wrong sign key?')
    seed4 = raw[:SEED_HEAD_BYTES] + buf[region_end:region_end + SEED_TAIL_BYTES]
    region = buf[HEADER_BYTES:region_end]
    # Invert `encrypt_enc_pb`: it xors bytes 8.. with the first four and THEN
    # reverses, so undoing it reverses first. Reapplying the forward function
    # is NOT the inverse, however symmetric it looks.
    unwound = bytearray(region[::-1])
    for i in range(XOR_KEY_BYTES, len(unwound)):
        unwound[i] ^= unwound[i % 4]
    ciphertext = bytes(unwound[XOR_KEY_BYTES:])
    if len(ciphertext) % AES_BLOCK:
        raise ArgusDecodeError(f'SIMON region is {len(ciphertext)} bytes, not a whole number of blocks')
    schedule = _simon_key_schedule(seed4)
    out = bytearray()
    for i in range(len(ciphertext) // AES_BLOCK):
        block = struct.unpack('<QQ', ciphertext[i * AES_BLOCK:(i + 1) * AES_BLOCK])
        lo, hi = _simon_decode_block(block, schedule)
        out += lo.to_bytes(8, 'little') + hi.to_bytes(8, 'little')
    return _strip_pkcs7(bytes(out))


def dyn_pair_from_argus(x_argus: str, *, sign_key: bytes = SIGN_KEY) -> DynPair | None:
    """The `(f24, f3)` pair carried by `x_argus`, or None when it carries no seed.

    None rather than an exception for a seedless argus: most of the app's own
    requests are signed before `/ms/get_seed` has ever answered, and "this flow
    has no pair, try another" is an ordinary outcome of scanning a capture, not
    an error. A payload this sign key cannot open still raises.

    `f3` is stored shifted left by one, like every scalar in the argus payload,
    and is shifted back here so the value matches the `rand` the signer feeds to
    `dyn_encode`.
    """
    payload = decrypt_argus(x_argus, sign_key=sign_key)
    try:
        proto = ProtoBuf(payload)
        seed = proto.getBytes(FIELD_DYN_SEED)
        shifted = proto.getInt(FIELD_RAND)
    except (ProtoError, AssertionError, IndexError) as exc:
        raise ArgusDecodeError(f'decrypted payload is not an argus protobuf: {type(exc).__name__}') from exc
    if not seed:
        return None
    try:
        dyn_seed = seed.decode('ascii')
    except UnicodeDecodeError as exc:
        raise ArgusDecodeError('f24 is not an ASCII seed') from exc
    if len(dyn_seed) != EXPECTED_SEED_LEN:
        # Not fatal: the length is an observation from every capture so far,
        # not a documented invariant, and refusing a valid pair over it would
        # cost a whole capture session. Logged so a changed seed format is
        # noticed rather than silently carried.
        logger.warning('captured f24 is %d characters, not the usual %d — seed format may have changed',
                       len(dyn_seed), EXPECTED_SEED_LEN)
    return DynPair(dyn_seed=dyn_seed, dyn_rand=shifted >> 1)


@dataclass(frozen=True, slots=True)
class FlowArgus:
    """One signed request read out of a mitmproxy flow file. No secret: the
    `x_argus` is encrypted, and the pair only exists once it is decrypted."""
    host: str
    path: str
    x_argus: str


def read_flow_argus(flow_path: str | os.PathLike[str], *,
                    interpreter: str = DEFAULT_MITMPROXY_PYTHON) -> list[FlowArgus]:
    """Every `x-argus`-carrying request in `flow_path`, in capture order.

    Runs `interpreter` as a subprocess because mitmproxy is not installed in
    the project venv and cannot be put on its `sys.path` — see the module
    docstring. The script is written to a temp file rather than passed with
    `-c` so a traceback names real line numbers, and the argument list is
    fixed — no shell, and the flow path is an argv element, never interpolated
    into source.
    """
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, 'dump_flow_argus.py')
        with open(script, 'w', encoding='utf-8') as fh:
            fh.write(_FLOW_DUMP_SOURCE)
        try:
            proc = subprocess.run([interpreter, script, os.fspath(flow_path)],
                                  capture_output=True, text=True, check=False)
        except OSError as exc:
            raise ArgusDecodeError(
                f'could not run {interpreter} to read the flow file: {type(exc).__name__}') from exc
    if proc.returncode != 0:
        # stderr is the subprocess's own traceback over a flow FILE — it names
        # no header value, because the dump script only ever reads x-argus and
        # prints it on stdout.
        raise ArgusDecodeError(
            f'{interpreter} failed to read {os.fspath(flow_path)} (exit {proc.returncode}); '
            f'is mitmproxy importable there? {proc.stderr.strip().splitlines()[-1:] or ""}')
    flows: list[FlowArgus] = []
    for line in proc.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) == 3:
            flows.append(FlowArgus(host=parts[0], path=parts[1], x_argus=parts[2]))
    return flows


def pair_from_flows(flows: list[FlowArgus], *, path: str) -> tuple[DynPair, FlowArgus]:
    """The pair carried by the LAST flow in `flows` whose request path is `path`.

    The last, not the first: a capture session re-mints its seed (a second
    `/ms/get_seed` mid-session was observed in a 30-flow capture), and the
    freshest pair is the one with the most life left in it.

    BOTH halves come from ONE flow, and that is the whole reason this takes a
    flow rather than a seed. `f24` and `f3` were measured to be accepted as a
    pair and refused when either half was changed alone (2026-09-11, Arms G and
    I), so mixing a seed from one request with a rand from another is exactly
    the combination known not to work.
    """
    matching = [f for f in flows if f.path == path]
    if not matching:
        seen = sorted({f.path for f in flows})
        raise ArgusDecodeError(
            f'no signed request to {path} in this capture. '
            f'Paths present: {seen}. Re-capture with the app actually VISITING a profile, '
            f'or pass --path to harvest from one of these instead.')
    for flow in reversed(matching):
        pair = dyn_pair_from_argus(flow.x_argus)
        if pair is not None:
            return pair, flow
    raise ArgusDecodeError(
        f'{len(matching)} request(s) to {path} carry no f24 seed — the app had not '
        f'called /ms/get_seed yet when they were signed')


def write_pair(pair: DynPair, out_path: str | os.PathLike[str]) -> None:
    """Write `pair` to `out_path` by atomic replace, readable by its owner only.

    `0o600` and the atomic rename are both deliberate: the file holds half a
    live credential, and `capture_identity_addon` watches it, so a reader must
    never see a half-written one. Same write discipline as the addon's own
    `identities.json`.
    """
    out_path = os.fspath(out_path)
    tmp = out_path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        json.dump(pair.as_dict(), fh, indent=2)
    os.replace(tmp, out_path)


def self_test() -> bool:
    """Sign an INVENTED payload with our own signer and check the decrypt
    reproduces it byte-exact, on both framings.

    A decryptor that has not round-tripped is not evidence, and this is the
    round trip — kept in the shipped tool rather than in a scratch file so the
    capability cannot be lost again. It reaches nothing external: no TikTok, no
    RapidAPI, no identity file, and every value below is made up.
    """
    from helpers.argus import encode_argus_fn, generate_protobuf  # noqa: PLC0415

    params = b'aid=1233&device_id=0000000000000000000&ts=1700000000'
    ts = 1700000000
    invented_seed, invented_rand = 'A' * EXPECTED_SEED_LEN, 1234567
    ok = True
    for label, corrected, kwargs in (
        ('corrected (captured pair)', True, {'dyn_seed': invented_seed, 'rand': invented_rand}),
        ('legacy (no pair)', False, {'dyn_seed': None, 'rand': None}),
    ):
        built = generate_protobuf(
            params=params, payload=b'', app_launch_time=ts - 900, device_type='SM-A207F',
            ts=ts, app_version='46.9.1', app_id=1233, license_id=2142840551,
            sdk_version='v05.03.01-ov-android', sdk_version_code=84082976,
            device_id=None, device_token=None, dyn_version=3, **kwargs)
        recovered = decrypt_argus(encode_argus_fn(built, corrected=corrected))
        match = recovered == built
        ok = ok and match
        print(f'  {label:28} {len(built):4d} bytes  round-trip: {"OK" if match else "FAILED"}')
        if match and corrected:
            pair = dyn_pair_from_argus(encode_argus_fn(built, corrected=True))
            fields_ok = pair is not None and pair.dyn_seed == invented_seed and pair.dyn_rand == invented_rand
            ok = ok and fields_ok
            print(f'  {"f24/f3 recovered":28} {"OK" if fields_ok else "FAILED"}')
    return ok


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m tiktoksearch.argus_pair',
        description='Recover the captured argus (f24 dyn_seed, f3 rand) pair from an x-argus.')
    src = parser.add_mutually_exclusive_group()
    src.add_argument('--flow', help='mitmproxy flow file to harvest the pair from')
    src.add_argument('--argus', help='one raw x-argus header value')
    src.add_argument('--self-test', action='store_true',
                     help='prove the decryptor inverts our own signer (invented values only)')
    parser.add_argument('--path', default=DEFAULT_FLOW_PATH,
                        help=f'with --flow, the request path to harvest from (default: {DEFAULT_FLOW_PATH})')
    parser.add_argument('--mitmproxy-python', default=DEFAULT_MITMPROXY_PYTHON,
                        help=f'interpreter that can import mitmproxy (default: {DEFAULT_MITMPROXY_PYTHON})')
    parser.add_argument('-o', '--out', help='write the pair here as JSON, for capture_identity_addon --set dyn_pair=')
    parser.add_argument('--list', action='store_true',
                        help='with --flow, list the signed paths in the capture and exit')
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Prints only lengths, hashes, hosts and paths — never a
    seed or a rand; the value reaches disk through `--out` and nowhere else."""
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    args = _build_parser().parse_args(argv)

    if args.self_test:
        print('argus decrypt self-test (invented values, nothing external):')
        return 0 if self_test() else 1

    if args.argus:
        pair = dyn_pair_from_argus(args.argus)
        if pair is None:
            print('this x-argus carries no f24 seed')
            return 1
        source = 'the supplied x-argus'
    elif args.flow:
        flows = read_flow_argus(args.flow, interpreter=args.mitmproxy_python)
        print(f'{len(flows)} signed request(s) in {args.flow}')
        if args.list:
            for path in sorted({f.path for f in flows}):
                print(f'  {sum(1 for f in flows if f.path == path):3d}  {path}')
            return 0
        pair, flow = pair_from_flows(flows, path=args.path)
        source = f'{flow.host}{flow.path}'
    else:
        _build_parser().print_help()
        return 2

    print(f'recovered pair from {source}')
    print(f'  {pair.fingerprint()}')
    if args.out:
        write_pair(pair, args.out)
        print(f'  wrote {args.out} (mode 0600) — git-ignored as dyn_pair*.json')
    else:
        print('  not written: pass -o/--out to save it')
    return 0


if __name__ == '__main__':
    sys.exit(main())
