#!/usr/bin/env python3
"""JPEG 3000 : prototype de format d'image compressé (fichiers .j3k).

Comment ça marche, en 4 étapes :
  1. Couleurs : RGB -> YCbCr (luminosité + 2 couleurs), comme le JPEG.
  2. Transformée en ondelettes CDF 9/7 sur toute l'image (comme JPEG 2000)
     au lieu de blocs 8x8 : plus d'effet « pavés » quand on compresse fort.
  3. Quantification à zone morte, avec un pas adapté à chaque sous-bande et une
     zone morte qui dépend du voisinage (on garde la texture, on jette le bruit isolé).
  4. Codage arithmétique adaptatif avec contextes (voisins, coefficient parent et,
     pour les couleurs, la luminosité au même endroit), plus efficace que le
     codage de Huffman du JPEG.

Usage :
  python jpeg3000.py encode photo.png photo.j3k --quality 50
  python jpeg3000.py encode photo.png photo.j3k --size 50000     (taille visée en octets)
  python jpeg3000.py decode photo.j3k photo_decodee.png
"""
import argparse
import os
import struct
import time

import numpy as np

MAGIC = b"J3K2"
HEADER = struct.Struct("<4sHHBBff")  # magie, largeur, hauteur, canaux, niveaux, pas, facteur chroma

CHROMA = 1.6     # les couleurs sont quantifiées plus fort que la luminosité (l'œil y est moins sensible)
RHO = 0.25       # zone morte : arrondi vers zéro des petits coefficients
RHO_ISO = 0.10   # ... plus forte pour un coefficient isolé (cher à coder, peu visible)
RHO_BUSY = 0.40  # ... plus faible au milieu d'une texture (peu cher à coder, garde le détail)
BIAS = 0.05      # point de reconstruction à l'intérieur de chaque intervalle
BLOCK = 4        # taille des blocs testés « tout à zéro » d'un seul coup

# ---------------------------------------------------------------------------
# 1. Couleurs
# ---------------------------------------------------------------------------


def rgb_to_ycc(rgb):
    r, g, b = (rgb[..., i].astype(np.float64) for i in range(3))
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = -0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 0.5 * r - 0.418688 * g - 0.081312 * b
    return [y - 128.0, cb, cr]


def ycc_to_rgb(y, cb, cr):
    y = y + 128.0
    rgb = np.stack([y + 1.402 * cr, y - 0.344136 * cb - 0.714136 * cr, y + 1.772 * cb], axis=-1)
    return np.clip(np.round(rgb), 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# 2. Ondelette CDF 9/7 (schéma « lifting », extension symétrique aux bords)
# ---------------------------------------------------------------------------

ALPHA = -1.586134342059924
BETA = -0.052980118572961
GAMMA = 0.882911075530934
DELTA = 0.443506852043971
K = 1.230174104914001


def _next(a):  # a[n+1], miroir au bord droit
    return np.concatenate([a[..., 1:], a[..., -1:]], axis=-1)


def _prev(a):  # a[n-1], miroir au bord gauche
    return np.concatenate([a[..., :1], a[..., :-1]], axis=-1)


def _fwd(x):
    """Une étape d'analyse le long du dernier axe (longueur paire) -> (basses, hautes fréquences)."""
    s = x[..., 0::2].copy()
    d = x[..., 1::2].copy()
    d += ALPHA * (s + _next(s))
    s += BETA * (_prev(d) + d)
    d += GAMMA * (s + _next(s))
    s += DELTA * (_prev(d) + d)
    return s / K, d * K


def _inv(s, d):
    s = s * K
    d = d / K
    s -= DELTA * (_prev(d) + d)
    d -= GAMMA * (s + _next(s))
    s -= BETA * (_prev(d) + d)
    d -= ALPHA * (s + _next(s))
    x = np.empty(s.shape[:-1] + (2 * s.shape[-1],))
    x[..., 0::2] = s
    x[..., 1::2] = d
    return x


def dwt2(img, levels):
    a = img.astype(np.float64)
    h, w = a.shape
    for _ in range(levels):
        lo, hi = _fwd(a[:h, :w])                          # le long des lignes
        lo, hi = _fwd(np.concatenate([lo, hi], axis=1).T)  # le long des colonnes
        a[:h, :w] = np.concatenate([lo, hi], axis=1).T
        h //= 2
        w //= 2
    return a


def idwt2(a, levels):
    a = a.copy()
    H, W = a.shape
    for lev in range(levels, 0, -1):
        h, w = H >> (lev - 1), W >> (lev - 1)
        t = a[:h, :w].T
        sub = _inv(t[:, :h // 2], t[:, h // 2:]).T
        a[:h, :w] = _inv(sub[:, :w // 2], sub[:, w // 2:])
    return a


def _synthesis_norms(max_levels=7, n=4096):
    """Énergie de chaque fonction de base : sert à donner à chaque sous-bande le bon pas de quantification."""
    low, high = {}, {}
    for lev in range(1, max_levels + 1):
        for store, is_high in ((low, False), (high, True)):
            m = n >> lev
            s, d = np.zeros(m), np.zeros(m)
            (d if is_high else s)[m // 2] = 1.0
            x = _inv(s, d)
            for _ in range(lev - 1):
                x = _inv(x, np.zeros_like(x))
            store[lev] = float(np.sqrt(np.sum(x * x)))
    return low, high


_LOW, _HIGH = _synthesis_norms()

LL, HL, LH, HH = range(4)


def band_specs(H, W, levels):
    """Sous-bandes dans l'ordre de codage : (niveau, orientation, lignes, colonnes, norme)."""
    h, w = H >> levels, W >> levels
    specs = [(levels, LL, slice(0, h), slice(0, w), _LOW[levels] ** 2)]
    for lev in range(levels, 0, -1):
        h, w = H >> lev, W >> lev
        specs.append((lev, HL, slice(0, h), slice(w, 2 * w), _HIGH[lev] * _LOW[lev]))
        specs.append((lev, LH, slice(h, 2 * h), slice(0, w), _LOW[lev] * _HIGH[lev]))
        specs.append((lev, HH, slice(h, 2 * h), slice(w, 2 * w), _HIGH[lev] ** 2))
    return specs


# ---------------------------------------------------------------------------
# 3. Quantification
# ---------------------------------------------------------------------------


def _quantize_detail(c, step, parent):
    """Zone morte adaptée au voisinage (8 voisins + parent) : décision côté encodeur seulement."""
    a = np.abs(c) / step
    q = np.floor(a + RHO)
    h, w = q.shape
    p = np.pad((q > 0).astype(np.int8), 1)
    n = sum(p[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
            for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx)
    if parent is not None:
        n = n + np.repeat(np.repeat(parent != 0, 2, axis=0), 2, axis=1)
    q = np.where(n == 0, np.floor(a + RHO_ISO), np.where(n >= 3, np.floor(a + RHO_BUSY), q))
    return (np.sign(c) * q).astype(np.int64)


def _dequantize(q, step, is_ll):
    q = np.asarray(q, dtype=np.float64)
    if is_ll:
        return q * step
    return np.sign(q) * (np.abs(q) + BIAS) * step


# ---------------------------------------------------------------------------
# 4. Codeur arithmétique binaire adaptatif (codeur d'intervalle façon LZMA)
#    Chaque contexte a deux probabilités sur 16 bits, l'une rapide, l'autre lente,
#    et on utilise leur moyenne : apprentissage rapide au début, précision ensuite.
# ---------------------------------------------------------------------------

TOP = 1 << 24


def _probs(n):
    """n contextes = 2n cases (rapide, lente), toutes à 50 %."""
    return [32768] * (2 * n)


class Encoder:
    def __init__(self):
        self.low = 0
        self.range = 0xFFFFFFFF
        self.cache = 0
        self.cache_size = 1
        self.out = bytearray()

    def _shift_low(self):
        low = self.low
        if low < 0xFF000000 or low >= 0x100000000:
            carry = low >> 32
            temp = self.cache
            while True:
                self.out.append((temp + carry) & 0xFF)
                temp = 0xFF
                self.cache_size -= 1
                if not self.cache_size:
                    break
            self.cache = (low >> 24) & 0xFF
        self.cache_size += 1
        self.low = (low & 0x00FFFFFF) << 8

    def bit(self, probs, i, b):
        i += i
        pf, ps = probs[i], probs[i + 1]
        bound = (self.range >> 12) * ((pf + ps) >> 5)
        if b:
            self.low += bound
            self.range -= bound
            probs[i] = pf - (pf >> 4)
            probs[i + 1] = ps - (ps >> 7)
        else:
            self.range = bound
            probs[i] = pf + ((65536 - pf) >> 4)
            probs[i + 1] = ps + ((65536 - ps) >> 7)
        while self.range < TOP:
            self.range <<= 8
            self._shift_low()
        return b

    def bypass(self, b):
        self.range >>= 1
        if b:
            self.low += self.range
        while self.range < TOP:
            self.range <<= 8
            self._shift_low()
        return b

    def ue(self, probs, base, v):
        """Entier >= 0 en Exp-Golomb : préfixe adaptatif, suffixe brut."""
        v += 1
        n = v.bit_length() - 1
        for i in range(n):
            self.bit(probs, base + (i if i < 19 else 19), 1)
        self.bit(probs, base + (n if n < 19 else 19), 0)
        for i in range(n - 1, -1, -1):
            self.bypass((v >> i) & 1)
        return v - 1

    def finish(self):
        for _ in range(5):
            self._shift_low()
        # Le 1er octet vaut toujours 0 et les zéros de fin sont implicites : on les retire.
        return bytes(self.out[1:]).rstrip(b"\x00")


class Decoder:
    def __init__(self, data, pos):
        self.data = data
        self.pos = pos + 4
        self.range = 0xFFFFFFFF
        self.code = int.from_bytes(data[pos:pos + 4].ljust(4, b"\x00"), "big")

    def _byte(self):
        p = self.pos
        self.pos = p + 1
        return self.data[p] if p < len(self.data) else 0

    def bit(self, probs, i, _b=0):
        i += i
        pf, ps = probs[i], probs[i + 1]
        bound = (self.range >> 12) * ((pf + ps) >> 5)
        if self.code < bound:
            self.range = bound
            probs[i] = pf + ((65536 - pf) >> 4)
            probs[i + 1] = ps + ((65536 - ps) >> 7)
            b = 0
        else:
            self.code -= bound
            self.range -= bound
            probs[i] = pf - (pf >> 4)
            probs[i + 1] = ps - (ps >> 7)
            b = 1
        while self.range < TOP:
            self.range <<= 8
            self.code = (self.code << 8) | self._byte()
        return b

    def bypass(self, _b=0):
        self.range >>= 1
        if self.code >= self.range:
            self.code -= self.range
            b = 1
        else:
            b = 0
        while self.range < TOP:
            self.range <<= 8
            self.code = (self.code << 8) | self._byte()
        return b

    def ue(self, probs, base, _v=0):
        n = 0
        while self.bit(probs, base + (n if n < 19 else 19)):
            n += 1
        v = 1
        for _ in range(n):
            v = (v << 1) | self.bypass()
        return v - 1


# ---------------------------------------------------------------------------
# Modèle de contexte : le même code sert à l'encodage et au décodage
# (Q = valeurs à coder, ou None pour décoder ; R se remplit au fur et à mesure).
# ---------------------------------------------------------------------------

# Regroupement de l'« activité » des voisins déjà codés en quelques classes.
SBIN = [0, 1, 2, 3, 3, 4, 4, 5, 5, 5, 6, 6, 6, 6, 7, 7]
MBIN = [0, 0, 1, 1, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 4]


class _BandModel:
    def __init__(self):
        self.blk = _probs(18)
        self.sig = _probs(48)
        self.gt1 = _probs(10)
        self.gt2 = _probs(10)
        self.eg = _probs(200)


class _Models:
    def __init__(self):
        self.dc = _probs(60)
        self.bands = [_BandModel() for _ in range(8)]


def _code_ll(rc, probs, Q, R, h, w):
    """Bande basse fréquence : prédiction à partir des voisins (comme JPEG-LS), puis le résidu."""
    enc = Q is not None
    ue = rc.ue
    for y in range(h):
        row = R[y]
        up = R[y - 1] if y else None
        for x in range(w):
            if y and x:
                a, b, c = row[x - 1], up[x], up[x - 1]
                lo, hi = (a, b) if a < b else (b, a)
                pred = lo if c >= hi else hi if c <= lo else a + b - c
                act = abs(a - c) + abs(b - c)
                k = 0 if act < 4 else 1 if act < 32 else 2
            elif x:
                pred, k = row[x - 1], 2
            elif y:
                pred, k = up[x], 2
            else:
                pred, k = 0, 2
            r = Q[y][x] - pred if enc else 0
            u = ue(probs, 20 * k, 2 * r if r >= 0 else -2 * r - 1)
            row[x] = pred + ((u >> 1) if not u & 1 else -((u + 1) >> 1))


def _code_band(rc, model, Q, R, P, X, h, w):
    """Bande haute fréquence. P = bande « parente » (même orientation, échelle plus grossière),
    X = même bande de la luminosité, déjà décodée (pour les couleurs ; None pour la luminosité)."""
    enc = Q is not None
    bit, bypass, ue = rc.bit, rc.bypass, rc.ue
    blk, sig, gt1, gt2, eg = model.blk, model.sig, model.gt1, model.gt2, model.eg
    nbh, nbw = -(-h // BLOCK), -(-w // BLOCK)
    flags = [[0] * nbw for _ in range(nbh)]
    for by in range(nbh):
        y0, y1 = by * BLOCK, min(by * BLOCK + BLOCK, h)
        for bx in range(nbw):
            x0, x1 = bx * BLOCK, min(bx * BLOCK + BLOCK, w)
            # a) Le bloc contient-il au moins un coefficient non nul ?
            nz = enc and any(any(Q[y][x0:x1]) for y in range(y0, y1))
            c = (flags[by - 1][bx] if by else 0) + (flags[by][bx - 1] if bx else 0)
            if P is None:
                pc = 2
            else:
                px0, px1 = x0 >> 1, (x1 + 1) >> 1
                pc = 1 if any(any(P[y][px0:px1]) for y in range(y0 >> 1, (y1 + 1) >> 1)) else 0
            lb = 1 if X is not None and any(any(X[y][x0:x1]) for y in range(y0, y1)) else 0
            nz = bit(blk, 2 * (3 * c + pc) + lb, nz)
            flags[by][bx] = nz
            if not nz:
                continue
            # b) Les coefficients du bloc, un par un.
            for y in range(y0, y1):
                row = R[y]
                up = R[y - 1] if y else None
                prow = P[y >> 1] if P is not None else None
                xrow = X[y] if X is not None else None
                qrow = Q[y] if enc else None
                for x in range(x0, x1):
                    sn = 2 * abs(row[x - 1]) if x else 0
                    if up is not None:
                        sn += 2 * abs(up[x])
                        if x:
                            sn += abs(up[x - 1])
                        if x + 1 < w:
                            sn += abs(up[x + 1])
                    p = abs(prow[x >> 1]) if prow is not None else 0
                    if p > 2:
                        p = 2
                    lc = 1 if xrow is not None and xrow[x] else 0
                    v = qrow[x] if enc else 0
                    m = abs(v)
                    if bit(sig, 2 * (3 * SBIN[sn if sn < 15 else 15] + p) + lc, m):
                        mc = sn + 2 * p
                        mc = 2 * MBIN[mc if mc < 15 else 15] + lc
                        if bit(gt1, mc, m > 1):
                            if bit(gt2, mc, m > 2):
                                m = 3 + ue(eg, 20 * mc, m - 3)
                            else:
                                m = 2
                        else:
                            m = 1
                        row[x] = -m if bypass(v < 0) else m


def _code_planes(rc, nplanes, H, W, levels, qbands=None):
    specs = band_specs(H, W, levels)
    luma, chroma = _Models(), _Models()
    out = []
    for ci in range(nplanes):
        models = chroma if ci else luma
        parents = {}
        bands = []
        for bi, (lev, o, ys, xs, _) in enumerate(specs):
            h, w = ys.stop - ys.start, xs.stop - xs.start
            Q = qbands[ci][bi].tolist() if qbands is not None else None
            R = [[0] * w for _ in range(h)]
            if o == LL:
                _code_ll(rc, models.dc, Q, R, h, w)
            else:
                model = models.bands[2 * (min(lev, 4) - 1) + (o == HH)]
                X = out[0][bi] if ci else None
                _code_band(rc, model, Q, R, parents.get(o), X, h, w)
                parents[o] = R
            bands.append(R)
        out.append(bands)
    return out


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _levels_for(h, w):
    m, levels = min(h, w), 1
    while levels < 6 and (m >> (levels + 1)) >= 8:
        levels += 1
    return levels


def _padded(n, levels):
    unit = 1 << levels
    return -(-n // unit) * unit


def prepare(img):
    """Transforme l'image une seule fois (pratique pour essayer plusieurs niveaux de compression)."""
    img = np.asarray(img)
    if img.dtype != np.uint8 or img.ndim not in (2, 3) or (img.ndim == 3 and img.shape[2] != 3):
        raise ValueError("image attendue : tableau uint8 HxW (gris) ou HxWx3 (RGB)")
    h, w = img.shape[:2]
    if h > 65535 or w > 65535:
        raise ValueError("image trop grande (65535 pixels de côté maximum)")
    levels = _levels_for(h, w)
    H, W = _padded(h, levels), _padded(w, levels)
    planes = [img.astype(np.float64) - 128.0] if img.ndim == 2 else rgb_to_ycc(img)
    coeffs = [dwt2(np.pad(p, ((0, H - h), (0, W - w)), mode="symmetric"), levels) for p in planes]
    return {"h": h, "w": w, "H": H, "W": W, "levels": levels, "coeffs": coeffs}


def _quantize_all(prep, step, chroma):
    specs = band_specs(prep["H"], prep["W"], prep["levels"])
    out = []
    for ci, a in enumerate(prep["coeffs"]):
        st = step * (chroma if ci else 1.0)
        bands, parents = [], {}
        for _, o, ys, xs, norm in specs:
            if o == LL:
                q = np.round(a[ys, xs] / (st / norm)).astype(np.int64)
            else:
                q = _quantize_detail(a[ys, xs], st / norm, parents.get(o))
                parents[o] = q
            bands.append(q)
        out.append(bands)
    return out


def _reconstruct(qbands, h, w, H, W, levels, step, chroma):
    specs = band_specs(H, W, levels)
    planes = []
    for ci, bands in enumerate(qbands):
        st = step * (chroma if ci else 1.0)
        a = np.zeros((H, W))
        for (_, o, ys, xs, norm), q in zip(specs, bands):
            a[ys, xs] = _dequantize(q, st / norm, o == LL)
        planes.append(idwt2(a, levels)[:h, :w])
    if len(planes) == 1:
        return np.clip(np.round(planes[0] + 128.0), 0, 255).astype(np.uint8)
    return ycc_to_rgb(*planes)


def quality_to_step(quality):
    """Qualité 1..100 (100 = presque sans perte) -> pas de quantification."""
    return 0.5 * 2.0 ** ((100.0 - quality) / 10.0)


def _f32(x):
    return float(np.float32(x))


def simulate(img_or_prep, step):
    """Image telle qu'elle sera après décodage, sans fabriquer le fichier (rapide)."""
    prep = img_or_prep if isinstance(img_or_prep, dict) else prepare(img_or_prep)
    step, chroma = _f32(step), _f32(CHROMA)
    q = _quantize_all(prep, step, chroma)
    return _reconstruct(q, prep["h"], prep["w"], prep["H"], prep["W"], prep["levels"], step, chroma)


def encode(img_or_prep, step):
    """Image (uint8, gris ou RGB) -> octets .j3k."""
    prep = img_or_prep if isinstance(img_or_prep, dict) else prepare(img_or_prep)
    step, chroma = _f32(step), _f32(CHROMA)
    q = _quantize_all(prep, step, chroma)
    rc = Encoder()
    _code_planes(rc, len(q), prep["H"], prep["W"], prep["levels"], q)
    header = HEADER.pack(MAGIC, prep["w"], prep["h"], len(q), prep["levels"], step, chroma)
    return header + rc.finish()


def encode_to_size(img_or_prep, max_bytes, iterations=12):
    """Meilleure qualité dont le fichier ne dépasse pas max_bytes. Renvoie (octets, pas)."""
    prep = img_or_prep if isinstance(img_or_prep, dict) else prepare(img_or_prep)
    lo, hi = -2.0, 11.0  # log2 du pas : de 0,25 (quasi sans perte) à 2048 (bouillie)
    best = encode(prep, 2.0 ** hi), 2.0 ** hi
    for _ in range(iterations):
        mid = (lo + hi) / 2
        data = encode(prep, 2.0 ** mid)
        if len(data) <= max_bytes:
            hi, best = mid, (data, 2.0 ** mid)
        else:
            lo = mid
    return best


def decode(data):
    """Octets .j3k -> image uint8 (HxW ou HxWx3, RGB)."""
    if len(data) < HEADER.size or data[:4] != MAGIC:
        raise ValueError("ce n'est pas un fichier JPEG 3000 (.j3k)")
    _, w, h, nch, levels, step, chroma = HEADER.unpack_from(data)
    H, W = _padded(h, levels), _padded(w, levels)
    bands = _code_planes(Decoder(data, HEADER.size), nch, H, W, levels)
    return _reconstruct(bands, h, w, H, W, levels, step, chroma)


# ---------------------------------------------------------------------------
# Ligne de commande (lecture/écriture PNG, JPEG, BMP... via OpenCV)
# ---------------------------------------------------------------------------


def read_image(path):
    import cv2
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise SystemExit("Impossible de lire l'image : " + path)
    if img.dtype != np.uint8:
        img = (img / 257.0).round().astype(np.uint8)  # 16 bits -> 8 bits
    if img.ndim == 3:
        img = img[..., 0] if img.shape[2] == 1 else img[..., 2::-1]  # BGR(A) -> RGB
    return np.ascontiguousarray(img)


def write_image(path, img):
    import cv2
    ext = os.path.splitext(path)[1] or ".png"
    bgr = img if img.ndim == 2 else img[..., ::-1]
    ok, buf = cv2.imencode(ext, bgr)
    if not ok:
        raise SystemExit("Impossible d'écrire l'image : " + path)
    buf.tofile(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="JPEG 3000 : encodeur/décodeur d'images .j3k")
    sub = ap.add_subparsers(dest="cmd")
    e = sub.add_parser("encode", help="image -> .j3k")
    e.add_argument("entree")
    e.add_argument("sortie")
    g = e.add_mutually_exclusive_group()
    g.add_argument("--quality", type=float, default=50, help="1 à 100 (défaut 50)")
    g.add_argument("--size", type=int, help="taille maximale du fichier, en octets")
    d = sub.add_parser("decode", help=".j3k -> image (PNG, BMP...)")
    d.add_argument("entree")
    d.add_argument("sortie")
    args = ap.parse_args(argv)

    t0 = time.time()
    if args.cmd == "encode":
        img = read_image(args.entree)
        h, w = img.shape[:2]
        if args.size:
            data, _ = encode_to_size(img, args.size)
        else:
            data = encode(img, quality_to_step(args.quality))
        with open(args.sortie, "wb") as f:
            f.write(data)
        print("{} ({}x{}) -> {} : {:,} octets ({:.2f} bit/pixel) en {:.1f} s".format(
            args.entree, w, h, args.sortie, len(data), 8 * len(data) / (w * h),
            time.time() - t0).replace(",", " "))
    elif args.cmd == "decode":
        with open(args.entree, "rb") as f:
            img = decode(f.read())
        write_image(args.sortie, img)
        print("{} -> {} ({}x{}) en {:.1f} s".format(
            args.entree, args.sortie, img.shape[1], img.shape[0], time.time() - t0))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
