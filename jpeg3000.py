#!/usr/bin/env python3
"""JPEG 3000 : prototype de format d'image compressé (fichiers .j3k).

Comment ça marche, en 4 étapes :
  1. Couleurs : RGB -> YCbCr (luminosité + 2 couleurs), comme le JPEG.
  2. Transformée en ondelettes CDF 9/7 sur toute l'image (comme JPEG 2000), de n'importe
     quelle taille, au lieu de blocs 8x8 : plus d'effet « pavés » quand on compresse fort.
     Elle est directionnelle : par bloc de 16x16, elle filtre le long des contours en biais
     (7 directions choisies par l'encodeur et écrites dans le fichier).
  3. Quantification à zone morte, avec un pas adapté à chaque sous-bande et une
     zone morte qui dépend du voisinage (on garde la texture, on jette le bruit isolé),
     puis choix final de chaque valeur en pesant l'erreur contre son coût exact en bits (RDO).
  4. Codage arithmétique adaptatif avec contextes (voisins, coefficient parent, bandes
     voisines et, pour les couleurs, la luminosité au même endroit). Les deux décisions les plus
     fréquentes (« ce coefficient est-il nul ? », « son amplitude dépasse-t-elle 1 ? »)
     mélangent 4 modèles avec un petit réseau qui apprend pendant le codage, comme les
     compresseurs de la famille PAQ.

Format J3K4 (version 4). Les fichiers J3K3 (version 3) se lisent toujours.

La transparence (canal alpha) est gardée telle quelle, sans perte.

Usage :
  python jpeg3000.py encode photo.png photo.j3k --quality 50
  python jpeg3000.py encode photo.png photo.j3k --size 50000     (taille visée en octets)
  python jpeg3000.py decode photo.j3k photo_decodee.png
  python jpeg3000.py info photo.j3k

Après `import jpeg3000`, Pillow sait aussi ouvrir les .j3k : Image.open("photo.j3k").
"""
import argparse
import os
import struct
import time

import numpy as np

MAGIC = b"J3K4"
OLD_MAGICS = (b"J3K3",)  # versions encore lues (J3K3 = v3, sans ondelette directionnelle)
# magie, largeur, hauteur, canaux (1 gris, 2 gris+alpha, 3 RGB, 4 RGBA), niveaux, pas, facteur chroma
HEADER = struct.Struct("<4sIIBBff")
MAX_PIXELS = 1 << 28

CHROMA = 1.6     # les couleurs sont quantifiées plus fort que la luminosité (l'œil y est moins sensible)
RHO = 0.25       # zone morte : arrondi vers zéro des petits coefficients
RHO_ISO = 0.10   # ... plus forte pour un coefficient isolé (cher à coder, peu visible)
RHO_BUSY = 0.40  # ... plus faible au milieu d'une texture (peu cher à coder, garde le détail)
BIAS = 0.05      # point de reconstruction à l'intérieur de chaque intervalle
BLOCK = 32       # côté des blocs testés « tout à zéro » d'un seul coup
MAX_LEVELS = 6   # niveaux d'ondelette au plus...
MIN_LL = 8       # ... tant que la bande basse fréquence garde au moins 8 pixels de côté

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


def _sides(s, d):
    """Voisins avec extension symétrique (comme JPEG 2000), pour une longueur paire ou impaire.
    Renvoie s[k+1] pour chaque d[k], puis d[k-1] et d[k] pour chaque s[k]."""
    ns, nd = s.shape[-1], d.shape[-1]
    s_next = s[..., 1:] if ns > nd else np.concatenate([s[..., 1:], s[..., -1:]], axis=-1)
    d_prev = np.concatenate([d[..., :1], d[..., :ns - 1]], axis=-1)
    d_cur = np.concatenate([d, d[..., -1:]], axis=-1) if ns > nd else d
    return s_next, d_prev, d_cur


def _fwd(x):
    """Une étape d'analyse le long du dernier axe (toute longueur) -> (basses, hautes fréquences)."""
    s = x[..., 0::2].copy()
    d = x[..., 1::2].copy()
    nd = d.shape[-1]
    if not nd:  # un seul échantillon : rien à transformer
        return s, d
    d += ALPHA * (s[..., :nd] + _sides(s, d)[0])
    _, dp, dc = _sides(s, d)
    s += BETA * (dp + dc)
    d += GAMMA * (s[..., :nd] + _sides(s, d)[0])
    _, dp, dc = _sides(s, d)
    s += DELTA * (dp + dc)
    return s / K, d * K


def _inv(s, d):
    nd = d.shape[-1]
    if not nd:
        return s.copy()
    s = s * K
    d = d / K
    _, dp, dc = _sides(s, d)
    s -= DELTA * (dp + dc)
    d -= GAMMA * (s[..., :nd] + _sides(s, d)[0])
    _, dp, dc = _sides(s, d)
    s -= BETA * (dp + dc)
    d -= ALPHA * (s[..., :nd] + _sides(s, d)[0])
    x = np.empty(s.shape[:-1] + (s.shape[-1] + nd,))
    x[..., 0::2] = s
    x[..., 1::2] = d
    return x


def level_sizes(h, w, levels):
    """Taille de la zone basse fréquence à chaque niveau : (h, w), (⌈h/2⌉, ⌈w/2⌉)..."""
    sizes = [(h, w)]
    for _ in range(levels):
        h, w = (h + 1) >> 1, (w + 1) >> 1
        sizes.append((h, w))
    return sizes


def dwt2(img, levels):
    a = img.astype(np.float64)
    for h, w in level_sizes(*a.shape, levels)[:-1]:
        lo, hi = _fwd(a[:h, :w])                          # le long des lignes
        lo, hi = _fwd(np.concatenate([lo, hi], axis=1).T)  # le long des colonnes
        a[:h, :w] = np.concatenate([lo, hi], axis=1).T
    return a


def idwt2(a, levels):
    a = a.copy()
    sizes = level_sizes(*a.shape, levels)
    for lev in range(levels, 0, -1):
        (h, w), (lh, lw) = sizes[lev - 1], sizes[lev]
        t = a[:h, :w].T
        sub = _inv(t[:, :lh], t[:, lh:]).T
        a[:h, :w] = _inv(sub[:, :lw], sub[:, lw:])
    return a


# Ondelette directionnelle (format J3K4) : chaque étape du lifting prédit un échantillon à partir
# de ses voisins pris le long d'une direction choisie par bloc (un contour en biais reste alors
# « lisse » et coûte peu), au lieu de toujours prendre les voisins tout droit.
# OFFS = décalage latéral par échantillon (0 = tout droit, 1 = 45°, 0,5 = interpolé).
OFFS = [-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0]
ZERO = OFFS.index(0.0)
DIR_BLOCK = 16       # côté des blocs de direction (dans la zone du niveau)
DIR_MIN_GAIN = 0.3   # on ne quitte « tout droit » que si l'énergie restante baisse d'au moins 30 %


def _shift0(a, off):
    """b[i, k] = a[i + off, k] (le long de l'axe 0, interpolation linéaire, bords répétés)."""
    m = a.shape[0]
    lo = int(np.floor(off))
    f = off - lo
    i0 = np.clip(np.arange(m) + lo, 0, m - 1)
    if f == 0:
        return a[i0]
    i1 = np.clip(np.arange(m) + lo + 1, 0, m - 1)
    return a[i0] * (1 - f) + a[i1] * f


def _sel(a, dm, sign):
    """Pour chaque échantillon, a décalé de sign * OFFS[direction de cet échantillon]."""
    if (dm == ZERO).all():
        return a
    out = np.empty_like(a)
    for i, off in enumerate(OFFS):
        mask = dm == i
        if mask.any():
            out[mask] = _shift0(a, sign * off)[mask]
    return out


def _fwd_dir(x, dm):
    """Comme _fwd (lifting le long du dernier axe), voisins pris selon la direction dm (même forme que x)."""
    s, d = x[:, 0::2].copy(), x[:, 1::2].copy()
    nd = d.shape[1]
    if not nd:
        return s, d
    de, do = dm[:, 0::2], dm[:, 1::2]
    d += ALPHA * (_sel(s[:, :nd], do, -1) + _sel(_sides(s, d)[0], do, 1))
    _, dp, dc = _sides(s, d)
    s += BETA * (_sel(dp, de, -1) + _sel(dc, de, 1))
    d += GAMMA * (_sel(s[:, :nd], do, -1) + _sel(_sides(s, d)[0], do, 1))
    _, dp, dc = _sides(s, d)
    s += DELTA * (_sel(dp, de, -1) + _sel(dc, de, 1))
    return s / K, d * K


def _inv_dir(s, d, dm):
    nd = d.shape[1]
    if not nd:
        return s.copy()
    s = s * K
    d = d / K
    de, do = dm[:, 0::2], dm[:, 1::2]
    _, dp, dc = _sides(s, d)
    s -= DELTA * (_sel(dp, de, -1) + _sel(dc, de, 1))
    d -= GAMMA * (_sel(s[:, :nd], do, -1) + _sel(_sides(s, d)[0], do, 1))
    _, dp, dc = _sides(s, d)
    s -= BETA * (_sel(dp, de, -1) + _sel(dc, de, 1))
    d -= ALPHA * (_sel(s[:, :nd], do, -1) + _sel(_sides(s, d)[0], do, 1))
    x = np.empty((s.shape[0], s.shape[1] + nd))
    x[:, 0::2] = s
    x[:, 1::2] = d
    return x


def _grid(m, n):
    return -(-m // DIR_BLOCK), -(-n // DIR_BLOCK)


def _expand(blocks, m, n):
    return np.repeat(np.repeat(np.asarray(blocks), DIR_BLOCK, 0), DIR_BLOCK, 1)[:m, :n]


def _choose(x):
    """Encodeur : direction de chaque bloc, celle qui prédit le mieux les échantillons impairs."""
    m, n = x.shape
    nbm, nbn = _grid(m, n)
    s, d = x[:, 0::2], x[:, 1::2]
    nd = d.shape[1]
    if not nd:
        return np.full((nbm, nbn), ZERO, dtype=np.int64)
    sn = _sides(s, d)[0]
    costs = np.zeros((len(OFFS), nbm, nbn))
    full = np.zeros((nbm * DIR_BLOCK, nbn * DIR_BLOCK))
    for i, off in enumerate(OFFS):
        full[:m, 1:n:2] = np.abs(d - 0.5 * (_shift0(s[:, :nd], -off) + _shift0(sn, off)))
        costs[i] = full.reshape(nbm, DIR_BLOCK, nbn, DIR_BLOCK).sum(axis=(1, 3))
    best = costs.argmin(axis=0)
    best[costs[ZERO] <= costs.min(axis=0) * (1 + DIR_MIN_GAIN)] = ZERO
    return best


def dir_shapes(h, w, levels):
    """Taille des grilles de directions : pour chaque niveau, passe verticale puis horizontale."""
    shapes = []
    for hh, ww in level_sizes(h, w, levels)[:-1]:
        shapes += [_grid(ww, hh), _grid(hh, ww)]
    return shapes


def dwt2_dir(img, levels, dirs=None):
    """Ondelette directionnelle. dirs = None : l'encodeur choisit les directions (renvoyées)."""
    a = img.astype(np.float64)
    out = []
    for li, (h, w) in enumerate(level_sizes(*a.shape, levels)[:-1]):
        t = a[:h, :w].T                                   # passe verticale (le long des colonnes)
        vb = dirs[2 * li] if dirs is not None else _choose(t)
        lo, hi = _fwd_dir(t, _expand(vb, w, h))
        v = np.concatenate([lo, hi], axis=1).T
        hb = dirs[2 * li + 1] if dirs is not None else _choose(v)  # passe horizontale
        lo, hi = _fwd_dir(v, _expand(hb, h, w))
        a[:h, :w] = np.concatenate([lo, hi], axis=1)
        out += [vb, hb]
    return a, out


def idwt2_dir(a, levels, dirs):
    a = a.copy()
    sizes = level_sizes(*a.shape, levels)
    for lev in range(levels, 0, -1):
        (h, w), (lh, lw) = sizes[lev - 1], sizes[lev]
        vb, hb = dirs[2 * (lev - 1)], dirs[2 * (lev - 1) + 1]
        z = a[:h, :w]
        v = _inv_dir(z[:, :lw], z[:, lw:], _expand(hb, h, w))
        t = v.T
        a[:h, :w] = _inv_dir(t[:, :lh], t[:, lh:], _expand(vb, w, h)).T
    return a


# Énergie des fonctions de base de chaque niveau (basse, haute fréquence), calculée une fois
# pour toutes : elle donne à chaque sous-bande le bon pas de quantification. En dur pour que
# tous les décodeurs (Python, JavaScript...) utilisent exactement les mêmes nombres.
_LOW = [1.0, 1.4021081679297438, 2.030371856081801, 2.9011625562785768, 4.115285175175844,
        5.824510863772892, 8.238759934572657, 11.651954647921327, 16.47856064706484]
_HIGH = [1.0, 0.721261382508076, 0.9834713041227895, 1.4419624041394556, 2.0737604196716712,
         2.94732487653393, 4.173589458929619, 5.904302327552499, 8.350639020782582]

LL, HL, LH, HH = range(4)


def band_specs(H, W, levels):
    """Sous-bandes dans l'ordre de codage : (niveau, orientation, lignes, colonnes, norme)."""
    sizes = level_sizes(H, W, levels)
    h, w = sizes[levels]
    specs = [(levels, LL, slice(0, h), slice(0, w), _LOW[levels] * _LOW[levels])]
    for lev in range(levels, 0, -1):
        (h, w), (lh, lw) = sizes[lev - 1], sizes[lev]
        specs.append((lev, HL, slice(0, lh), slice(lw, w), _HIGH[lev] * _LOW[lev]))
        specs.append((lev, LH, slice(lh, h), slice(0, lw), _LOW[lev] * _HIGH[lev]))
        specs.append((lev, HH, slice(lh, h), slice(lw, w), _HIGH[lev] * _HIGH[lev]))
    return specs


# ---------------------------------------------------------------------------
# 3. Quantification
# ---------------------------------------------------------------------------


def _parent_map(parent, h, w):
    """Valeur du coefficient parent (échelle 2 fois plus grossière) en face de chaque position."""
    ph, pw = parent.shape
    return parent[np.minimum(np.arange(h) >> 1, ph - 1)][:, np.minimum(np.arange(w) >> 1, pw - 1)]


def _same_scale(band, h, w):
    """Une bande de la même échelle (autre orientation, ou luminosité), recadrée sur h x w."""
    b = np.asarray(band, dtype=np.int64).reshape(len(band), -1)
    bh, bw = b.shape
    return b[np.minimum(np.arange(h), bh - 1)][:, np.minimum(np.arange(w), bw - 1)]


def _quantize_detail(c, step, parent):
    """Zone morte adaptée au voisinage (8 voisins + parent) : décision côté encodeur seulement."""
    a = np.abs(c) / step
    q = np.floor(a + RHO)
    h, w = q.shape
    p = np.pad((q > 0).astype(np.int8), 1)
    n = sum(p[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
            for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx)
    if parent is not None and parent.size:
        n = n + _parent_map(parent != 0, h, w)
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

    def code(self, p0, b):
        """Bit b avec une probabilité de 0 donnée sur 12 bits (1 à 4095)."""
        bound = (self.range >> 12) * p0
        if b:
            self.low += bound
            self.range -= bound
        else:
            self.range = bound
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
        self.val = int.from_bytes(data[pos:pos + 4].ljust(4, b"\x00"), "big")

    def _byte(self):
        p = self.pos
        self.pos = p + 1
        return self.data[p] if p < len(self.data) else 0

    def bit(self, probs, i, _b=0):
        i += i
        pf, ps = probs[i], probs[i + 1]
        bound = (self.range >> 12) * ((pf + ps) >> 5)
        if self.val < bound:
            self.range = bound
            probs[i] = pf + ((65536 - pf) >> 4)
            probs[i + 1] = ps + ((65536 - ps) >> 7)
            b = 0
        else:
            self.val -= bound
            self.range -= bound
            probs[i] = pf - (pf >> 4)
            probs[i + 1] = ps - (ps >> 7)
            b = 1
        while self.range < TOP:
            self.range <<= 8
            self.val = (self.val << 8) | self._byte()
        return b

    def code(self, p0, _b=0):
        bound = (self.range >> 12) * p0
        if self.val < bound:
            self.range = bound
            b = 0
        else:
            self.val -= bound
            self.range -= bound
            b = 1
        while self.range < TOP:
            self.range <<= 8
            self.val = (self.val << 8) | self._byte()
        return b

    def bypass(self, _b=0):
        self.range >>= 1
        if self.val >= self.range:
            self.val -= self.range
            b = 1
        else:
            b = 0
        while self.range < TOP:
            self.range <<= 8
            self.val = (self.val << 8) | self._byte()
        return b

    def ue(self, probs, base, _v=0):
        n = 0
        while self.bit(probs, base + (n if n < 19 else 19)):
            n += 1
            if n > 40:
                raise ValueError("fichier .j3k abîmé (valeur impossible)")
        v = 1
        for _ in range(n):
            v = (v << 1) | self.bypass()
        return v - 1


# Mélange logistique (comme les compresseurs PAQ) : plusieurs modèles donnent chacun une
# probabilité, un petit réseau apprend en continu comment les combiner. Uniquement des
# calculs entiers, pour que tous les décodeurs (Python, JavaScript...) tombent pile pareil.
_SQT = [1, 2, 3, 6, 10, 16, 27, 45, 73, 120, 194, 310, 488, 747, 1101, 1546, 2047, 2549, 2994,
        3348, 3607, 3785, 3901, 3975, 4022, 4050, 4068, 4079, 4085, 4089, 4092, 4093, 4094]


def _squash(d):
    """Domaine logistique (-2047..2047) -> probabilité sur 12 bits (1..4095)."""
    w, i = d & 127, (d >> 7) + 16
    return max(1, (_SQT[i] * (128 - w) + _SQT[i + 1] * w + 64) >> 7)


SQUASH = [_squash(d) for d in range(-2047, 2048)]  # SQUASH[d + 2047]
STRETCH = [2047] * 4096                             # l'inverse : probabilité -> domaine logistique
_lo = 0
for _d in range(-2047, 2048):
    for _p in range(_lo, SQUASH[_d + 2047] + 1):
        STRETCH[_p] = _d
    _lo = max(_lo, SQUASH[_d + 2047] + 1)
COST = [32.0] + [-np.log2(p / 4096.0) for p in range(1, 4096)] + [0.0]  # bits pour une probabilité 12 bits
RDO_LAMBDA = 0.08  # poids du débit dans la quantification RDO (0 = désactivée)
LEVEL1_STEP = 1.1  # J3K4 : détails les plus fins quantifiés 10 % plus grossièrement (mesuré : un peu mieux)


def _band_step(st, norm, lev, o, v4=True):
    """Pas de quantification d'une sous-bande."""
    if o != LL and lev == 1 and v4:
        return st * LEVEL1_STEP / norm
    return st / norm
MIX_W0 = 19660   # poids de départ de chaque modèle dans le mélange (0,3 en virgule fixe 16 bits)
MIX_SHIFT = 11   # vitesse d'apprentissage du mélangeur (plus grand = plus lent)


# ---------------------------------------------------------------------------
# Modèle de contexte : le même code sert à l'encodage et au décodage
# (q = valeurs à coder, ou None pour décoder ; R se remplit au fur et à mesure).
# ---------------------------------------------------------------------------

# Regroupement de l'« activité » des voisins déjà codés en quelques classes.
SBIN = [0, 1, 2, 3, 3, 4, 4, 5, 5, 5, 6, 6, 6, 6, 7, 7]
# Classes d'amplitude à échelle logarithmique : 0, 1, 2, 3-4, 5-6, 7-10, 11-16, 17-28, 29-48, 49 et plus.
MLOG = [0, 1, 2, 3, 3, 4, 4, 5, 5, 5, 5, 6, 6, 6, 6, 6, 6] + [7] * 12 + [8] * 20 + [9] * 15


class _BandModel:
    def __init__(self):
        self.blk = _probs(18)
        self.gt1 = _probs(20)
        self.gt2 = _probs(20)
        self.eg = _probs(400)
        self.sgn = _probs(27)
        # « Ce coefficient est-il nul ? » : 4 modèles mélangés. Le contexte principal à deux
        # vitesses, un motif fin des voisins proches, un contexte large (voisins à 2 pas,
        # bandes cousines, luminosité) ; probabilités de 1 sur 16 bits.
        self.sfa = [32768] * 48
        self.ssa = [32768] * 48
        self.smb = [32768] * 162
        self.smc = [32768] * 108
        self.mw = [MIX_W0, MIX_W0, MIX_W0, MIX_W0, 0] * 8
        # « Amplitude > 1 ? » : même principe.
        self.gfa = [32768] * 20
        self.gsa = [32768] * 20
        self.gmb = [32768] * 384
        self.gmc = [32768] * 108
        self.gw = [MIX_W0, MIX_W0, MIX_W0, MIX_W0, 0] * 10


class _Models:
    def __init__(self):
        self.dc = _probs(60)
        self.bands = [_BandModel() for _ in range(8)]


def _code_ll(rc, probs, Q, R, h, w):
    """Prédiction à partir des voisins (comme JPEG-LS), puis le résidu. Sert pour la bande
    basse fréquence et pour la transparence (sans perte)."""
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


def _block_any(m, h, w):
    """Pour chaque bloc BLOCK x BLOCK : y a-t-il au moins une valeur non nulle ?"""
    nbh, nbw = -(-h // BLOCK), -(-w // BLOCK)
    p = np.zeros((nbh * BLOCK, nbw * BLOCK), dtype=bool)
    p[:h, :w] = m != 0
    return p.reshape(nbh, BLOCK, nbw, BLOCK).any(axis=(1, 3)).astype(np.int64).tolist()


def _ue_cost(probs, base, v):
    """Coût en bits de ue(v) avec l'état actuel des probabilités (encodeur seulement)."""
    v += 1
    n = v.bit_length() - 1
    c = float(n)
    for i in range(n):
        j = 2 * (base + (i if i < 19 else 19))
        c += COST[4096 - ((probs[j] + probs[j + 1]) >> 5)]
    j = 2 * (base + (n if n < 19 else 19))
    return c + COST[(probs[j] + probs[j + 1]) >> 5]


def _code_band(rc, model, q, R, P, C, X, h, w, A=None, lam=0.0, v4=True):
    """Bande haute fréquence. Contextes déjà connus du décodeur avant de la coder :
    P = bande « parente » (même orientation, échelle 2 fois plus grossière),
    C = bandes « cousines » (même échelle, orientations déjà codées),
    X = même bande de la luminosité (pour les couleurs ; None pour la luminosité).
    Encodeur : A = amplitudes exactes (en pas de quantification) ; si lam > 0, chaque valeur
    est choisie en pesant l'erreur contre le coût exact en bits (quantification « RDO »)."""
    if not h or not w:
        return
    enc = q is not None
    rdo = enc and lam > 0 and A is not None

    def qrow_sign(y, x):
        return A[y][x] < 0
    bit, code, ue = rc.bit, rc.code, rc.ue
    blk, gt1, gt2, eg, sgn = model.blk, model.gt1, model.gt2, model.eg, model.sgn
    sfa, ssa, smb, smc, mw = model.sfa, model.ssa, model.smb, model.smc, model.mw
    gfa, gsa, gmb, gmc, gw = model.gfa, model.gsa, model.gmb, model.gmc, model.gw
    g1mix = v4  # J3K3 : « amplitude > 1 » codé sans mélange

    def g1pred(mc, l1, u1, dg, fr, cmx, xmx, p, lc):
        """Probabilité mélangée de « amplitude > 1 » (et ce qu'il faut pour mettre à jour)."""
        ga = mc
        gb = ((((l1 if l1 < 3 else 3) * 4 + (u1 if u1 < 3 else 3)) * 4 + (dg if dg < 3 else 3)) * 3 + p) * 2 + lc
        gc = (((fr if fr < 3 else 3) * 3 + cmx) * 3 + xmx) * 3 + p
        t0, t1, t2, t3 = ST[gfa[ga] >> 4], ST[gsa[ga] >> 4], ST[gmb[gb] >> 4], ST[gmc[gc] >> 4]
        kg = 5 * (mc >> 1)
        dot = (gw[kg] * t0 + gw[kg + 1] * t1 + gw[kg + 2] * t2 + gw[kg + 3] * t3 + gw[kg + 4] * 256) >> 16
        return ga, gb, gc, SQ[(dot if dot < 2047 else 2047) + 2047] if dot > -2047 else 1, t0, t1, t2, t3, kg
    ST, SQ, SH = STRETCH, SQUASH, MIX_SHIFT
    Q = q.tolist() if enc else None
    zero = np.zeros((h, w), dtype=np.int64)
    pv = _parent_map(np.asarray(P, dtype=np.int64), h, w) if P and P[0] else None
    xv = _same_scale(X, h, w) if X is not None else None
    cv = zero
    for c in C:
        if c and c[0]:
            cv = cv + np.abs(_same_scale(c, h, w))
    qb = _block_any(q, h, w) if enc else None
    pb = _block_any(pv, h, w) if pv is not None else None
    xb = _block_any(xv, h, w) if xv is not None else None
    pm = np.minimum(np.abs(pv if pv is not None else zero), 2).tolist()
    xs = (np.sign(xv) + 1).tolist() if xv is not None else None
    xm = np.minimum(np.abs(xv if xv is not None else zero), 2).tolist()
    cm = np.minimum(cv, 2).tolist()
    nbh, nbw = -(-h // BLOCK), -(-w // BLOCK)
    flags = [[0] * nbw for _ in range(nbh)]
    for by in range(nbh):
        y0, y1 = by * BLOCK, min(by * BLOCK + BLOCK, h)
        for bx in range(nbw):
            x0, x1 = bx * BLOCK, min(bx * BLOCK + BLOCK, w)
            # a) Le bloc contient-il au moins un coefficient non nul ?
            c = (flags[by - 1][bx] if by else 0) + (flags[by][bx - 1] if bx else 0)
            pc = pb[by][bx] if pb is not None else 2
            lb = xb[by][bx] if xb is not None else 0
            nz = flags[by][bx] = bit(blk, 2 * (3 * c + pc) + lb, qb[by][bx] if enc else 0)
            if not nz:
                continue
            # b) Les coefficients du bloc, un par un.
            for y in range(y0, y1):
                row = R[y]
                up = R[y - 1] if y else None
                up2 = R[y - 2] if y > 1 else None
                pmr, cmr, xmr = pm[y], cm[y], xm[y]
                xsr = xs[y] if xs is not None else None
                qrow = Q[y] if enc else None
                arow = A[y] if rdo else None
                for x in range(x0, x1):
                    l1 = abs(row[x - 1]) if x else 0
                    u1 = dg = 0
                    if up is not None:
                        u1 = abs(up[x])
                        if x:
                            dg = abs(up[x - 1])
                        if x + 1 < w:
                            dg += abs(up[x + 1])
                    sn = 2 * (l1 + u1) + dg
                    p = pmr[x]
                    lc = 1 if xsr is not None and xsr[x] != 1 else 0
                    v = qrow[x] if enc else 0
                    m = abs(v)
                    # Nul ou pas ? Mélange de 4 prédictions, chacune issue d'un contexte différent.
                    sb = SBIN[sn if sn < 15 else 15]
                    ca = 2 * (3 * sb + p) + lc
                    cb = ((((l1 if l1 < 2 else 2) * 3 + (u1 if u1 < 2 else 2)) * 3 + (dg if dg < 2 else 2)) * 3
                          + p) * 2 + lc
                    fr = (abs(row[x - 2]) if x > 1 else 0) + (abs(up2[x]) if up2 is not None else 0)
                    cc = (((fr if fr < 3 else 3) * 3 + cmr[x]) * 3 + xmr[x]) * 3 + p
                    pa, ps, pb_, pc_ = sfa[ca], ssa[ca], smb[cb], smc[cc]
                    s0, s1, s2, s3 = ST[pa >> 4], ST[ps >> 4], ST[pb_ >> 4], ST[pc_ >> 4]
                    k = 5 * sb
                    dot = (mw[k] * s0 + mw[k + 1] * s1 + mw[k + 2] * s2 + mw[k + 3] * s3 + mw[k + 4] * 256) >> 16
                    p1 = SQ[(dot if dot < 2047 else 2047) + 2047] if dot > -2047 else 1
                    g1p = -1
                    if rdo:
                        av = arow[x]
                        av = -av if av < 0 else av
                        kmax = int(av - BIAS + 0.5)
                        if kmax >= 1 or m:
                            kmax = kmax if kmax >= 1 else 1
                            neg = v < 0 if v else qrow_sign(y, x)
                            # Coût de chaque choix : 0, kmax-1 ou kmax, avec les probabilités actuelles.
                            mc = sn + 2 * p
                            mc = 2 * MLOG[mc if mc < 64 else 63] + lc
                            sl = row[x - 1] if x else 0
                            su = up[x] if up is not None else 0
                            sc = 3 * ((sl > 0) - (sl < 0) + 1) + (su > 0) - (su < 0) + 1
                            if xsr is not None:
                                sc = 3 * sc + xsr[x]
                            ps0 = (sgn[2 * sc] + sgn[2 * sc + 1]) >> 5
                            if g1mix:
                                ga, gb, gc, g1p, t0, t1, t2, t3, kg = g1pred(mc, l1, u1, dg, fr, cmr[x], xmr[x], p, lc)
                                g1 = 4096 - g1p
                            else:
                                g1 = (gt1[2 * mc] + gt1[2 * mc + 1]) >> 5
                            g2 = (gt2[2 * mc] + gt2[2 * mc + 1]) >> 5
                            base = COST[p1] + COST[4096 - ps0 if neg else ps0]
                            best_k, best_j = 0, av * av + lam * COST[4096 - p1]
                            for kk in (kmax - 1, kmax):
                                if kk < 1:
                                    continue
                                if kk == 1:
                                    bits = base + COST[g1]
                                elif kk == 2:
                                    bits = base + COST[4096 - g1] + COST[g2]
                                else:
                                    bits = base + COST[4096 - g1] + COST[4096 - g2] + _ue_cost(eg, 20 * mc, kk - 3)
                                e = av - kk - BIAS
                                jj = e * e + lam * bits
                                if jj < best_j:
                                    best_k, best_j = kk, jj
                            m = best_k
                            v = -m if neg else m
                    b = code(4096 - p1, 1 if m else 0)
                    err = (b << 12) - p1
                    mw[k] += (s0 * err) >> SH
                    mw[k + 1] += (s1 * err) >> SH
                    mw[k + 2] += (s2 * err) >> SH
                    mw[k + 3] += (s3 * err) >> SH
                    mw[k + 4] += (256 * err) >> SH
                    if not b:
                        sfa[ca] = pa - (pa >> 4)
                        ssa[ca] = ps - (ps >> 7)
                        smb[cb] = pb_ - (pb_ >> 5)
                        smc[cc] = pc_ - (pc_ >> 5)
                        continue
                    sfa[ca] = pa + ((65536 - pa) >> 4)
                    ssa[ca] = ps + ((65536 - ps) >> 7)
                    smb[cb] = pb_ + ((65536 - pb_) >> 5)
                    smc[cc] = pc_ + ((65536 - pc_) >> 5)
                    # Amplitude : 1, 2, puis Exp-Golomb, selon l'activité autour.
                    mc = sn + 2 * p
                    mc = 2 * MLOG[mc if mc < 64 else 63] + lc
                    if g1mix:
                        if g1p < 0:
                            ga, gb, gc, g1p, t0, t1, t2, t3, kg = g1pred(mc, l1, u1, dg, fr, cmr[x], xmr[x], p, lc)
                        qa, qs, qb_, qc_ = gfa[ga], gsa[ga], gmb[gb], gmc[gc]
                        gb1 = code(4096 - g1p, 1 if m > 1 else 0)
                        err = (gb1 << 12) - g1p
                        gw[kg] += (t0 * err) >> SH
                        gw[kg + 1] += (t1 * err) >> SH
                        gw[kg + 2] += (t2 * err) >> SH
                        gw[kg + 3] += (t3 * err) >> SH
                        gw[kg + 4] += (256 * err) >> SH
                        if gb1:
                            gfa[ga] = qa + ((65536 - qa) >> 4)
                            gsa[ga] = qs + ((65536 - qs) >> 7)
                            gmb[gb] = qb_ + ((65536 - qb_) >> 5)
                            gmc[gc] = qc_ + ((65536 - qc_) >> 5)
                        else:
                            gfa[ga] = qa - (qa >> 4)
                            gsa[ga] = qs - (qs >> 7)
                            gmb[gb] = qb_ - (qb_ >> 5)
                            gmc[gc] = qc_ - (qc_ >> 5)
                    else:
                        gb1 = bit(gt1, mc, m > 1)
                    if gb1:
                        if bit(gt2, mc, m > 2):
                            m = 3 + ue(eg, 20 * mc, m - 3)
                        else:
                            m = 2
                    else:
                        m = 1
                    # Signe : prédit par les voisins de gauche et du dessus (et la luminosité).
                    sl = row[x - 1] if x else 0
                    su = up[x] if up is not None else 0
                    sc = 3 * ((sl > 0) - (sl < 0) + 1) + (su > 0) - (su < 0) + 1
                    if xsr is not None:
                        sc = 3 * sc + xsr[x]
                    row[x] = -m if bit(sgn, sc, v < 0) else m


def _code_planes(rc, ncolor, h, w, levels, qbands=None, amps=None, lam=0.0, v4=True):
    specs = band_specs(h, w, levels)
    luma, chroma = _Models(), _Models()
    out = []
    for ci in range(ncolor):
        models = chroma if ci else luma
        parents = {}
        bands = []
        for bi, (lev, o, ys, xs, _) in enumerate(specs):
            bh, bw = ys.stop - ys.start, xs.stop - xs.start
            q = qbands[ci][bi] if qbands is not None else None
            R = [[0] * bw for _ in range(bh)]
            if o == LL:
                _code_ll(rc, models.dc, q.tolist() if q is not None else None, R, bh, bw)
            else:
                model = models.bands[2 * (min(lev, 4) - 1) + (o == HH)]
                X = out[0][bi] if ci else None
                C = [bands[bi - k] for k in range(1, o)]
                A = amps[ci][bi] if amps is not None else None
                _code_band(rc, model, q, R, parents.get(o), C, X, bh, bw, A, lam, v4)
                parents[o] = R
            bands.append(R)
        out.append(bands)
    return out


def _code_dirs(rc, shapes, dirs=None):
    """Directions de l'ondelette : un symbole par bloc, en 3 bits adaptatifs (arbre binaire),
    selon la direction du bloc précédent et le type de passe (verticale ou horizontale)."""
    enc = dirs is not None
    probs = _probs(2 * len(OFFS) * 8)
    out = []
    for gi, (nr, nc) in enumerate(shapes):
        g = dirs[gi].tolist() if enc else [[0] * nc for _ in range(nr)]
        prev = ZERO
        for r in range(nr):
            row = g[r]
            for c in range(nc):
                base = ((gi & 1) * len(OFFS) + prev) * 8
                sym = row[c] if enc else 0
                node = 1
                for i in (2, 1, 0):
                    node = 2 * node + rc.bit(probs, base + node, (sym >> i) & 1)
                sym = node - 8
                if sym >= len(OFFS):
                    raise ValueError("fichier .j3k abîmé (direction impossible)")
                row[c] = prev = sym
        out.append(np.array(g, dtype=np.int64).reshape(nr, nc))
    return out


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _levels_for(h, w):
    m, levels = min(h, w), 1
    while levels < MAX_LEVELS and (m >> (levels + 1)) >= MIN_LL:
        levels += 1
    return levels


def prepare(img):
    """Transforme l'image une seule fois (pratique pour essayer plusieurs niveaux de compression).
    Image : tableau uint8 HxW (gris), HxWx2 (gris + alpha), HxWx3 (RGB) ou HxWx4 (RGBA)."""
    img = np.asarray(img)
    if img.dtype != np.uint8 or img.ndim not in (2, 3) or (img.ndim == 3 and not 1 <= img.shape[2] <= 4):
        raise ValueError("image attendue : tableau uint8 HxW (gris), HxWx2 (gris+alpha), "
                         "HxWx3 (RGB) ou HxWx4 (RGBA)")
    h, w = img.shape[:2]
    if not h or not w or h * w > MAX_PIXELS:
        raise ValueError("taille d'image non prise en charge : {}x{}".format(w, h))
    if img.ndim == 2:
        img = img[..., None]
    color, alpha = (img[..., :-1], img[..., -1]) if img.shape[2] in (2, 4) else (img, None)
    if alpha is not None and alpha.min() == 255:
        alpha = None  # entièrement opaque : inutile de garder la transparence
    levels = _levels_for(h, w)
    planes = [color[..., 0].astype(np.float64) - 128.0] if color.shape[2] == 1 else rgb_to_ycc(color)
    luma, dirs = dwt2_dir(planes[0], levels)       # directions choisies sur la luminosité...
    coeffs = [luma] + [dwt2_dir(p, levels, dirs)[0] for p in planes[1:]]  # ... et reprises pour les couleurs
    return {"h": h, "w": w, "levels": levels, "coeffs": coeffs, "alpha": alpha, "dirs": dirs}


def _quantize_all(prep, step, chroma, amps=None):
    """Quantification de départ ; amps (liste) reçoit les coefficients exacts en pas de quantification."""
    specs = band_specs(prep["h"], prep["w"], prep["levels"])
    out = []
    for ci, a in enumerate(prep["coeffs"]):
        st = step * (chroma if ci else 1.0)
        bands, parents, exact = [], {}, []
        for lev, o, ys, xs, norm in specs:
            if o == LL:
                q = np.round(a[ys, xs] / (st / norm)).astype(np.int64)
                exact.append(None)
            else:
                sb_ = _band_step(st, norm, lev, o)
                q = _quantize_detail(a[ys, xs], sb_, parents.get(o))
                parents[o] = q
                exact.append((a[ys, xs] / sb_).tolist() if amps is not None else None)
            bands.append(q)
        out.append(bands)
        if amps is not None:
            amps.append(exact)
    return out


def _reconstruct(qbands, h, w, levels, step, chroma, alpha=None, dirs=None, v4=True):
    specs = band_specs(h, w, levels)
    planes = []
    for ci, bands in enumerate(qbands):
        st = step * (chroma if ci else 1.0)
        a = np.zeros((h, w))
        for (lev, o, ys, xs, norm), q in zip(specs, bands):
            q = np.asarray(q, dtype=np.float64).reshape(ys.stop - ys.start, xs.stop - xs.start)
            a[ys, xs] = _dequantize(q, _band_step(st, norm, lev, o, v4), o == LL)
        planes.append(idwt2_dir(a, levels, dirs) if dirs is not None else idwt2(a, levels))
    if len(planes) == 1:
        img = np.clip(np.round(planes[0] + 128.0), 0, 255).astype(np.uint8)
    else:
        img = ycc_to_rgb(*planes)
    if alpha is None:
        return img
    return np.dstack([img, np.asarray(alpha, dtype=np.uint8)])


def quality_to_step(quality):
    """Qualité 1..100 (100 = presque sans perte) -> pas de quantification."""
    return 0.5 * 2.0 ** ((100.0 - quality) / 10.0)


def step_to_quality(step):
    return 100.0 - 10.0 * np.log2(step / 0.5)


def _f32(x):
    return float(np.float32(x))


def encode_full(img_or_prep, step):
    """Image -> (octets .j3k, image telle qu'elle sera décodée)."""
    prep = img_or_prep if isinstance(img_or_prep, dict) else prepare(img_or_prep)
    step, chroma = _f32(step), _f32(CHROMA)
    if not (step > 0 and np.isfinite(step)):
        raise ValueError("pas de quantification invalide : {}".format(step))
    amps = [] if RDO_LAMBDA > 0 else None
    q = _quantize_all(prep, step, chroma, amps)
    rc = Encoder()
    _code_dirs(rc, dir_shapes(prep["h"], prep["w"], prep["levels"]), prep["dirs"])
    bands = _code_planes(rc, len(q), prep["h"], prep["w"], prep["levels"], q, amps, RDO_LAMBDA)
    alpha = prep["alpha"]
    if alpha is not None:
        _code_ll(rc, _probs(60), alpha.astype(np.int64).tolist(), [[0] * prep["w"] for _ in range(prep["h"])],
                 prep["h"], prep["w"])
    nch = len(q) + (alpha is not None)
    header = HEADER.pack(MAGIC, prep["w"], prep["h"], nch, prep["levels"], step, chroma)
    image = _reconstruct(bands, prep["h"], prep["w"], prep["levels"], step, chroma, alpha, prep["dirs"])
    return header + rc.finish(), image


def encode(img_or_prep, step):
    """Image (uint8 : gris, gris+alpha, RGB ou RGBA) -> octets .j3k."""
    return encode_full(img_or_prep, step)[0]


def simulate(img_or_prep, step):
    """Image telle qu'elle sera après décodage (sans quantification RDO : rapide, sans coder)."""
    prep = img_or_prep if isinstance(img_or_prep, dict) else prepare(img_or_prep)
    if RDO_LAMBDA > 0:
        return encode_full(prep, step)[1]
    step, chroma = _f32(step), _f32(CHROMA)
    q = _quantize_all(prep, step, chroma)
    return _reconstruct(q, prep["h"], prep["w"], prep["levels"], step, chroma, prep["alpha"], prep["dirs"])


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


def read_header(data):
    """En-tête d'un .j3k -> dict (largeur, hauteur, canaux, niveaux, pas, chroma)."""
    if len(data) < HEADER.size or (data[:4] != MAGIC and data[:4] not in OLD_MAGICS):
        if data[:3] == b"J3K" and len(data) >= 4:
            raise ValueError("fichier JPEG 3000 d'une autre version ({}) : ce décodeur lit {}".format(
                data[:4].decode("ascii", "replace"), MAGIC.decode()))
        raise ValueError("ce n'est pas un fichier JPEG 3000 (.j3k)")
    _, w, h, nch, levels, step, chroma = HEADER.unpack_from(data)
    if not (1 <= nch <= 4 and 1 <= levels <= 8 and w and h and w * h <= MAX_PIXELS
            and step > 0 and np.isfinite(step) and chroma > 0 and np.isfinite(chroma)):
        raise ValueError("fichier .j3k abîmé (en-tête incohérent)")
    return {"width": w, "height": h, "channels": nch, "levels": levels, "step": step, "chroma": chroma,
            "version": data[:4].decode()}


def decode(data):
    """Octets .j3k -> image uint8 (HxW, HxWx2, HxWx3 ou HxWx4)."""
    hd = read_header(data)
    w, h, nch = hd["width"], hd["height"], hd["channels"]
    ncolor = 1 if nch <= 2 else 3
    v4 = hd["version"] == MAGIC.decode()
    rc = Decoder(data, HEADER.size)
    dirs = _code_dirs(rc, dir_shapes(h, w, hd["levels"])) if v4 else None
    bands = _code_planes(rc, ncolor, h, w, hd["levels"], v4=v4)
    alpha = None
    if nch in (2, 4):
        alpha = [[0] * w for _ in range(h)]
        _code_ll(rc, _probs(60), None, alpha, h, w)
        alpha = np.clip(np.array(alpha, dtype=np.int64), 0, 255)
    return _reconstruct(bands, h, w, hd["levels"], hd["step"], hd["chroma"], alpha, dirs, v4)


# ---------------------------------------------------------------------------
# Lecture / écriture des images classiques (Pillow de préférence, sinon OpenCV)
# ---------------------------------------------------------------------------


def _from_pil(im):
    """Image Pillow -> tableau uint8 gris / gris+alpha / RGB / RGBA, dans le bon sens (EXIF)."""
    from PIL import ImageOps
    im = ImageOps.exif_transpose(im)  # photos de téléphone : on applique l'orientation EXIF
    has_alpha = "A" in im.getbands() or "transparency" in im.info
    if im.mode in ("I;16", "I;16L", "I;16B", "I", "F"):  # gris 16 ou 32 bits -> 8 bits
        a = np.asarray(im, dtype=np.float64)
        top = 65535.0 if a.max() > 255 else 255.0
        return np.clip(np.round(a * (255.0 / top)), 0, 255).astype(np.uint8)
    if im.mode == "1" or im.mode == "L":
        mode = "LA" if has_alpha else "L"
    elif im.mode in ("LA", "La", "PA"):
        mode = "LA"
    else:
        mode = "RGBA" if has_alpha else "RGB"
    if im.mode != mode:
        im = im.convert(mode)
    return np.ascontiguousarray(np.asarray(im, dtype=np.uint8))


def read_image(path):
    """Lit PNG, JPEG, WebP, TIFF, BMP, GIF, AVIF (et HEIC si pillow-heif est installé)."""
    try:
        from PIL import Image
    except ImportError:
        Image = None
    if Image is not None:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        try:
            with Image.open(path) as im:
                return _from_pil(im)
        except (OSError, ValueError, SyntaxError) as e:
            error = e
    try:
        import cv2
    except ImportError:
        raise SystemExit("Impossible de lire l'image : " + path +
                         (" ({})".format(error) if Image is not None else " (installez Pillow : pip install pillow)"))
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise SystemExit("Impossible de lire l'image : " + path)
    if img.dtype != np.uint8:
        img = (img / 257.0).round().astype(np.uint8)  # 16 bits -> 8 bits
    if img.ndim == 3:
        img = img[..., 0] if img.shape[2] == 1 else img[..., [2, 1, 0, 3][:img.shape[2]]]  # BGR(A) -> RGB(A)
    return np.ascontiguousarray(img)


def write_image(path, img):
    """Écrit PNG, JPEG, WebP, TIFF, BMP... selon l'extension (PNG si aucune)."""
    ext = (os.path.splitext(path)[1] or ".png").lower()
    if img.ndim == 3 and img.shape[2] in (2, 4) and ext in (".jpg", ".jpeg", ".bmp"):
        # Format sans transparence : on pose l'image sur un fond blanc.
        a = img[..., -1:].astype(np.float64) / 255.0
        img = np.round(img[..., :-1] * a + 255.0 * (1.0 - a)).astype(np.uint8)
        img = img[..., 0] if img.shape[2] == 1 else img
    try:
        from PIL import Image
        options = {"quality": 95} if ext in (".jpg", ".jpeg", ".webp") else {}
        Image.fromarray(img).save(path, **options)
        return
    except ImportError:
        pass
    import cv2
    if img.ndim == 3:
        img = img[..., [2, 1, 0, 3][:img.shape[2]]] if img.shape[2] in (3, 4) else img[..., :1]
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        raise SystemExit("Impossible d'écrire l'image : " + path)
    buf.tofile(path)


def register_pillow():
    """Apprend à Pillow à ouvrir les .j3k (Image.open("photo.j3k"))."""
    from PIL import Image, ImageFile

    class J3KImageFile(ImageFile.ImageFile):
        format = "J3K"
        format_description = "JPEG 3000"

        def _open(self):
            hd = read_header(self.fp.read(HEADER.size))
            mode = ("L", "LA", "RGB", "RGBA")[hd["channels"] - 1]
            if isinstance(getattr(type(self), "mode", None), property):
                self._mode = mode
            else:
                self.mode = mode
            self._size = (hd["width"], hd["height"])
            tile = ("j3k", (0, 0) + self._size, 0, None)
            self.tile = [ImageFile._Tile(*tile) if hasattr(ImageFile, "_Tile") else tile]

    class J3KDecoder(ImageFile.PyDecoder):
        _pulls_fd = True

        def decode(self, buffer):
            self.fd.seek(0)
            self.set_as_raw(np.ascontiguousarray(decode(self.fd.read())).tobytes())
            return -1, 0

    Image.register_open(J3KImageFile.format, J3KImageFile, lambda prefix: prefix[:4] == MAGIC)
    Image.register_extension(J3KImageFile.format, ".j3k")
    Image.register_decoder("j3k", J3KDecoder)


try:
    register_pillow()
except ImportError:
    pass


def _n(n):
    return "{:,}".format(n).replace(",", " ")


def main(argv=None):
    ap = argparse.ArgumentParser(description="JPEG 3000 : encodeur/décodeur d'images .j3k")
    sub = ap.add_subparsers(dest="cmd")
    e = sub.add_parser("encode", help="image -> .j3k")
    e.add_argument("entree")
    e.add_argument("sortie")
    g = e.add_mutually_exclusive_group()
    g.add_argument("--quality", type=float, default=50, help="1 à 100 (défaut 50)")
    g.add_argument("--size", type=int, help="taille maximale du fichier, en octets")
    d = sub.add_parser("decode", help=".j3k -> image (PNG, JPEG, WebP, BMP...)")
    d.add_argument("entree")
    d.add_argument("sortie")
    i = sub.add_parser("info", help="décrit un fichier .j3k sans le décoder")
    i.add_argument("entree")
    args = ap.parse_args(argv)

    t0 = time.time()
    if args.cmd == "encode":
        img = read_image(args.entree)
        h, w = img.shape[:2]
        if args.size:
            data, _ = encode_to_size(img, args.size)
        else:
            data = encode(img, quality_to_step(min(max(args.quality, 1.0), 100.0)))
        with open(args.sortie, "wb") as f:
            f.write(data)
        print("{} ({}x{}) -> {} : {} octets ({:.2f} bit/pixel) en {:.1f} s".format(
            args.entree, w, h, args.sortie, _n(len(data)), 8 * len(data) / (w * h), time.time() - t0))
    elif args.cmd == "decode":
        with open(args.entree, "rb") as f:
            try:
                img = decode(f.read())
            except ValueError as err:
                raise SystemExit("{} : {}".format(args.entree, err))
        write_image(args.sortie, img)
        print("{} -> {} ({}x{}) en {:.1f} s".format(
            args.entree, args.sortie, img.shape[1], img.shape[0], time.time() - t0))
    elif args.cmd == "info":
        with open(args.entree, "rb") as f:
            data = f.read()
        try:
            hd = read_header(data)
        except ValueError as err:
            raise SystemExit("{} : {}".format(args.entree, err))
        kind = ("gris", "gris + transparence", "couleur", "couleur + transparence")[hd["channels"] - 1]
        print("{} : JPEG 3000 ({}), {}x{}, {}, qualité {:.0f}, {} octets ({:.2f} bit/pixel)".format(
            args.entree, hd["version"], hd["width"], hd["height"], kind, step_to_quality(hd["step"]),
            _n(len(data)), 8 * len(data) / (hd["width"] * hd["height"])))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
