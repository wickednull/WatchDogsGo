"""Minimal QR Code encoder (Model 2, byte mode), stdlib only.

Enough to render the phone-GPS pairing URL on the Pager LCD. The target
payload runs on a MIPS OpenWrt box where `qrencode` is *not* guaranteed to
be installed, so this ships its own encoder:

  * versions 1-6 (up to 106 bytes at EC level M — a pairing URL is ~24 bytes)
  * byte mode only
  * error correction level M, auto mask selection (ISO/IEC 18004 penalties)

The layout follows the reference `qrcode-generator` implementation. It was
validated module-for-module (mask choice included) against `segno` during
development; the resulting matrices are pinned in `tests/test_qr.py`.
"""

from __future__ import annotations

from typing import Sequence

# (total_codewords, data_codewords, ec_codewords_per_block, num_blocks,
#  remainder_bits) for error correction level M.
_EC_TABLE = {
    1: (26, 16, 10, 1, 0),
    2: (44, 28, 16, 1, 7),
    3: (70, 44, 26, 1, 7),
    4: (100, 64, 18, 2, 7),
    5: (134, 86, 24, 2, 7),
    6: (172, 108, 16, 4, 7),
}

# Alignment pattern centre coordinates per version.
_ALIGNMENT = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
}

_BYTE_MODE = 0b0100
# EC level M encodes to the two format bits "00".
_FORMAT_EC_M = 0b00
_FORMAT_POLY = 0x537
_FORMAT_MASK = 0x5412

# Byte-mode capacity for a version: data codewords minus 2 bytes of header,
# minus another byte of headroom for the terminator + boundary padding.
def _capacity(version: int) -> int:
    return _EC_TABLE[version][1] - 3


def best_version(length: int) -> int:
    """Smallest version (1-6) whose byte-mode capacity fits *length*."""
    for v in range(1, 7):
        if _capacity(v) >= length:
            return v
    raise ValueError(f"QR payload too long for this encoder: {length} bytes")


# ── GF(256) / Reed-Solomon ────────────────────────────────────────────────

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _init_gf() -> None:
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


_init_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    """Generator polynomial with roots at alpha^0..alpha^(degree-1).
    Returned as coefficients, index 0 = constant term (lowest degree)."""
    result = [1]
    for i in range(degree):
        # multiply result by (x + alpha^i); in GF(2^n) addition == subtraction
        gen = [1, _GF_EXP[i]]
        product = [0] * (len(result) + 1)
        for j, a in enumerate(result):
            for k, b in enumerate(gen):
                product[j + k] ^= _gf_mul(a, b)
        result = product
    return result


def _rs_ecc(data: Sequence[int], degree: int) -> list[int]:
    """Reed-Solomon error-correction codewords for *data*."""
    gen = _rs_generator(degree)
    buf = list(data) + [0] * degree
    for i in range(len(data)):
        factor = buf[i]
        if factor:
            for j, g in enumerate(gen):
                buf[i + j] ^= _gf_mul(g, factor)
    return buf[-degree:]


# ── placement helpers ─────────────────────────────────────────────────────

def _format_bits(mask: int) -> int:
    """The 15-bit format information word (level M, chosen mask, masked)."""
    data = (_FORMAT_EC_M << 3) | mask
    d = data << 10
    while d.bit_length() >= _FORMAT_POLY.bit_length():
        d ^= _FORMAT_POLY << (d.bit_length() - _FORMAT_POLY.bit_length())
    return ((data << 10) | d) ^ _FORMAT_MASK


def _format_cells(size: int) -> list[tuple[int, int, int]]:
    """(row, col, format-bit index) for the 15 format cells of a QR symbol.

    Layout per ISO/IEC 18004:2015 §7.9 (identical to the reference writer):
    copy 1 runs along column 8 (bits 0..7) and row 8 (bits 14..8); copy 2 is
    the same information mirrored to the lower-left column and upper-right row.
    """
    cells: list[tuple[int, int, int]] = []
    voffset = 0
    hoffset = 0
    for i in range(8):
        if i == 6:  # timing pattern occupies (8, 6) / (6, 8)
            voffset += 1
            hoffset = 1
        # copy 1 (top-left): vertical leg in column 8, horizontal leg in row 8
        cells.append((i + voffset, 8, i))
        cells.append((8, i + hoffset, 14 - i))
        # copy 2: horizontal leg in row 8 (right edge), vertical leg in
        # column 8 (bottom edge)
        cells.append((8, size - 1 - i, i))
        cells.append((size - 1 - i, 8, 14 - i))
    return cells


_MASK_FUNCS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: (((r + c) % 2) + ((r * c) % 3)) % 2 == 0,
)


def _interleave(data_blocks: Sequence[Sequence[int]],
                ec_blocks: Sequence[Sequence[int]]) -> list[int]:
    out: list[int] = []
    n_data = max(len(b) for b in data_blocks)
    for i in range(n_data):
        for b in data_blocks:
            if i < len(b):
                out.append(b[i])
    n_ec = max(len(b) for b in ec_blocks)
    for i in range(n_ec):
        for b in ec_blocks:
            if i < len(b):
                out.append(b[i])
    return out


# ── the code ──────────────────────────────────────────────────────────────

class QrCode:
    """A QR code symbol.

    `modules` is a list of rows; each row is a list of booleans (`True` =
    dark module). Row 0 is the top of the symbol. No quiet zone is included —
    callers add their own margin when rendering.
    """

    def __init__(self, data: bytes | str, mask: int | None = None) -> None:
        if isinstance(data, str):
            data = data.encode("ascii", errors="replace")
        self.data = data
        self.version = best_version(len(data))
        self.size = 17 + 4 * self.version
        self.mask = self._pick_mask(mask)
        self.modules, self.function = self._build(self.mask)

    # ── encoding ──────────────────────────────────────────────────────────

    def _codewords(self) -> list[int]:
        total, data_cw, ec_cw, blocks, _rem = _EC_TABLE[self.version]
        bits: list[int] = []
        bits += list(map(int, f"{_BYTE_MODE:04b}" + f"{len(self.data):08b}"))  # header
        for byte in self.data:
            bits += list(map(int, f"{byte:08b}"))
        # terminator (ISO/IEC 18004 §7.4.9): up to 4 zero bits
        bits += [0] * min(4, data_cw * 8 - len(bits))
        # pad with zeros to the next codeword boundary, exactly as the
        # reference writer does (an already-aligned stream gets a full extra
        # zero byte, which decoders treat as trailing padding)
        bits += [0] * (8 - len(bits) % 8)
        base = len(bits)
        while len(bits) < data_cw * 8:
            pad = 0xEC if (len(bits) - base) // 8 % 2 == 0 else 0x11
            bits += list(map(int, f"{pad:08b}"))
        words = []
        for i in range(0, len(bits), 8):
            word = 0
            for b in bits[i:i + 8]:
                word = (word << 1) | b
            assert word < 256
            words.append(word)
        return words

    # ── matrix ────────────────────────────────────────────────────────────

    def _build(self, mask: int):
        size = self.size
        total, _dc, ec_cw, blocks, remainder = _EC_TABLE[self.version]
        data_cw = total - blocks * ec_cw

        modules = [[False] * size for _ in range(size)]
        is_func = [[False] * size for _ in range(size)]

        def set_module(r: int, c: int, dark: bool, fns: bool = False) -> None:
            modules[r][c] = dark
            if fns:
                is_func[r][c] = True

        def draw_finder(r0: int, c0: int) -> None:
            for dr in range(-1, 8):
                for dc in range(-1, 8):
                    if not (0 <= r0 + dr < size and 0 <= c0 + dc < size):
                        continue
                    if dr in (-1, 7) or dc in (-1, 7):   # light separator
                        dark = False
                    else:
                        dark = max(abs(dr - 3), abs(dc - 3)) != 2
                    set_module(r0 + dr, c0 + dc, dark, True)

        draw_finder(0, 0)
        draw_finder(0, size - 7)
        draw_finder(size - 7, 0)

        for i in range(8, size - 8):
            set_module(6, i, i % 2 == 0, True)
            set_module(i, 6, i % 2 == 0, True)

        for ar in _ALIGNMENT[self.version]:
            for ac in _ALIGNMENT[self.version]:
                if (ar < 9 and ac < 9) or (ar < 9 and ac >= size - 8) \
                        or (ar >= size - 8 and ac < 9):
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        dark = max(abs(dr), abs(dc)) != 1
                        set_module(ar + dr, ac + dc, dark, True)

        # dark module (always present, near the bottom-left finder)
        set_module(4 * self.version + 9, 8, True, True)

        # reserve the format-information strip before placing data so the
        # zig-zag skips it (the actual bits are drawn afterwards)
        for fr, fc, _fbit in _format_cells(size):
            is_func[fr][fc] = True

        # ── data codewords + ECC ──
        words = self._codewords()
        data_blocks = []
        for bi in range(blocks):
            start = bi * (data_cw // blocks)
            data_blocks.append(words[start:start + (data_cw // blocks)])
        ec_blocks = [_rs_ecc(b, ec_cw) for b in data_blocks]
        stream = _interleave(data_blocks, ec_blocks)

        # ── zig-zag placement ──
        bit_index = 7
        byte_index = 0
        r = size - 1
        inc = -1
        for c in range(size - 1, 0, -2):
            if c <= 6:
                c -= 1
            while True:
                for cc in (c, c - 1):
                    if not is_func[r][cc]:
                        dark = False
                        if byte_index < len(stream):
                            dark = (stream[byte_index] >> bit_index) & 1
                        if _MASK_FUNCS[mask](r, cc):
                            dark = not dark
                        modules[r][cc] = dark
                        bit_index -= 1
                        if bit_index < 0:
                            byte_index += 1
                            bit_index = 7
                r += inc
                if r < 0 or r >= size:
                    r -= inc
                    inc = -inc
                    break
        # remainder bits stay light (modules already False)

        # ── format info (ISO/IEC 18004:2015 §7.9, layout as in segno) ──
        bits = _format_bits(mask)
        for i, (row, col, bit) in enumerate(_format_cells(size)):
            set_module(row, col, (bits >> bit) & 1, True)

        # the dark module sits inside the format strip and is always dark,
        # so it is drawn last to override the format copy-2 bit there
        set_module(4 * self.version + 9, 8, True, True)

        return modules, is_func

    # ── mask selection ────────────────────────────────────────────────────

    def _pick_mask(self, forced: int | None) -> int:
        if forced is not None:
            self._build(forced)  # throws on invalid mask via lookup later
            return forced & 7
        best = (10 ** 9, -1)
        for m in range(8):
            modules, _ = self._build(m)
            score = self._penalty(modules)
            if score < best[0]:
                best = (score, m)
        return best[1]

    @staticmethod
    def _penalty(modules: Sequence[Sequence[bool]]) -> int:
        size = len(modules)
        total = size * size
        dark_count = sum(row.count(True) for row in modules)
        score = 0

        def runs(seq: Sequence[bool]) -> int:
            cost = 0
            run = 0
            prev = None
            for v in seq:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        cost += 3 + (run - 5)
                    prev, run = v, 1
            if run >= 5:
                cost += 3 + (run - 5)
            return cost

        for r in range(size):
            row = modules[r]
            score += runs(row)
            col = [modules[i][r] for i in range(size)]
            score += runs(col)

        # 2x2 blocks of the same colour
        for r in range(size - 1):
            for c in range(size - 1):
                if modules[r][c] == modules[r][c + 1] \
                        == modules[r + 1][c] == modules[r + 1][c + 1]:
                    score += 3

        # finder-like dark:light patterns 1011101 with light border on both ends
        def patterns(seq: Sequence[bool], size_: int) -> int:
            cost = 0
            for i in range(size_ - 6):
                if (seq[i] and not seq[i + 1] and seq[i + 2] and seq[i + 3]
                        and seq[i + 4] and seq[i + 5] and not seq[i + 6]):
                    before = seq[i - 4:i] if i >= 4 else []
                    after = seq[i + 7:i + 11]
                    if (len(before) == 4 and not any(before)) \
                            or (len(after) == 4 and not any(after)):
                        cost += 40
            return cost

        for r in range(size):
            score += patterns(modules[r], size)
            col = [modules[i][r] for i in range(size)]
            score += patterns(col, size)

        # proportion of dark modules
        ratio = dark_count * 100 / total
        score += abs(int((ratio + 4) // 5) - 10) * 10
        return score

    # ── rendering ─────────────────────────────────────────────────────────

    def draw(self, p, x: int, y: int, px: int,
             dark_color: int, light_color: int | None = None) -> None:
        """Draw the code onto a Pager screen using fill_rect.

        `px` is the pixel size of one module. A quiet zone is *not* drawn —
        the caller supplies a margin via x/y and a background brighter than
        the surrounding chrome if desired.
        """
        for r, row in enumerate(self.modules):
            for c, dark in enumerate(row):
                if dark:
                    p.fill_rect(x + c * px, y + r * px, px, px, dark_color)
                elif light_color is not None:
                    p.fill_rect(x + c * px, y + r * px, px, px, light_color)


def fit_scale(qr: QrCode, avail_w: int, avail_h: int, margin: int = 4) -> tuple[int, int, int]:
    """Largest module pixel size that fits, returning (px, x, y) centred."""
    inner = qr.size + margin * 2
    px = max(2, min(avail_w // inner, avail_h // inner, 8))
    qw = (qr.size + margin * 2) * px
    qh = qw
    x = max(0, (avail_w - qw) // 2)
    y = max(0, (avail_h - qh) // 2)
    return px, x, y