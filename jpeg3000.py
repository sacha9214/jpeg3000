#!/usr/bin/env python3
"""JPEG 3000 : prototype de format d'image compressé (fichiers .j3k).

Comment ça marche, en 4 étapes :
  1. Couleurs : RGB -> YCbCr (luminosité + 2 couleurs), comme le JPEG.
  2. Transformée en ondelettes CDF 9/7 sur toute l'image (comme JPEG 2000), de n'importe
     quelle taille, au lieu de blocs 8x8 : plus d'effet « pavés » quand on compresse fort.
     Elle est directionnelle : par bloc de 16x16, elle filtre le long des contours en biais
     (7 directions choisies par l'encodeur et écrites dans le fichier).
  3. Quantification « dépendante » (comme dans VVC) : deux grilles de valeurs décalées, choisies
     par une petite machine à 4 états. L'encodeur cherche, ligne par ligne, la suite de valeurs
     qui pèse le mieux l'erreur contre le coût exact en bits (treillis de Viterbi).
  4. Codage arithmétique adaptatif avec contextes (voisins, coefficient parent, bandes
     voisines et, pour les couleurs, la luminosité au même endroit). Les deux décisions les plus
     fréquentes (« ce coefficient est-il nul ? », « son amplitude dépasse-t-elle 1 ? »)
     mélangent 4 modèles avec un petit réseau qui apprend pendant le codage, comme les
     compresseurs de la famille PAQ. Toutes les probabilités partent de valeurs apprises sur
     d'autres photos (table PRIORS, à la fin du fichier) au lieu de 50 %.

Format J3K5 (version 5). Les fichiers J3K4 et J3K3 (versions 4 et 3) se lisent toujours.

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

MAGIC = b"J3K5"
OLD_MAGICS = (b"J3K3", b"J3K4")  # versions encore lues (J3K3 : sans ondelette directionnelle ; J3K4 : v4)
# magie, largeur, hauteur, canaux (1 gris, 2 gris+alpha, 3 RGB, 4 RGBA), niveaux, pas, facteur chroma
HEADER = struct.Struct("<4sIIBBff")
MAX_PIXELS = 1 << 28

CHROMA = 1.7     # les couleurs sont quantifiées plus fort que la luminosité (l'œil y est moins sensible)...
CHROMA_GAMMA = -0.2  # ... un peu moins à forte compression, un peu plus à haute qualité (mesuré)
BIAS = 0.05      # anciens formats : point de reconstruction à l'intérieur de chaque intervalle
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


# Ondelette directionnelle (depuis le format J3K4) : chaque étape du lifting prédit un échantillon à partir
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
    """Encodeur : direction de chaque bloc, celle qui laisse le moins d'énergie dans les hautes
    fréquences après l'étape complète de lifting (calculée pour chaque direction)."""
    m, n = x.shape
    nbm, nbn = _grid(m, n)
    if n < 2:
        return np.full((nbm, nbn), ZERO, dtype=np.int64)
    costs = np.zeros((len(OFFS), nbm, nbn))
    full = np.zeros((nbm * DIR_BLOCK, nbn * DIR_BLOCK))
    for i in range(len(OFFS)):
        full[:m, 1:n:2] = np.abs(_fwd_dir(x, np.full(x.shape, i))[1])
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


def _dequantize(q, step, is_ll):
    """Bande basse fréquence, et hautes fréquences des anciens formats (J3K3, J3K4)."""
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
LEVEL1_STEP = 1.1  # depuis J3K4 : détails les plus fins quantifiés 10 % plus grossièrement (mesuré : un peu mieux)
# Quantification dépendante (J3K5). Le « pas fin » vaut DQ_SCALE fois le pas de la sous-bande ; un
# quantificateur reconstruit sur les multiples pairs du pas fin, l'autre sur les impairs (et zéro).
DQ_SCALE = 0.6     # réglé pour qu'une même qualité donne à peu près la même taille qu'en version 4
DQ_BIAS = 0.28     # point de reconstruction un peu au-delà du niveau (en pas fins ; mesuré)
DQ_NEXT = [0, 2, 2, 0, 1, 3, 3, 1]   # état suivant = DQ_NEXT[2 * état + parité de la valeur]
DQ_QS = [0, 0, 1, 2]                 # groupe de contextes de chaque état
# Encodeur seulement :
DQ_LAMBDA = 0.25   # poids du débit (en pas fins au carré par bit)
DQ_FLAG_T = 1.3    # un bloc est codé s'il contient une amplitude au moins égale à ce seuil (en pas fins)
DQ_ZB_MAX = 4      # un bloc qui n'a que quelques valeurs au-dessus du seuil (au plus 4)...
DQ_ZB_AMP = 3.0    # ... toutes petites...
DQ_ZB_BITS = 8.0   # ... est vidé si l'erreur ajoutée vaut moins que ces bits par valeur (et ses zéros)


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
    def __init__(self, dq=False, prior=None):
        """dq : contextes de la quantification dépendante (J3K5), qui dépendent aussi de l'état."""
        k = 2 if dq else 1
        self.blk = _probs(18)
        self.gt1 = _probs(20)   # J3K3 seulement
        self.gt2 = _probs(20)
        self.eg = _probs(400)
        self.sgn = _probs(27)
        # « Ce coefficient est-il nul ? » : 4 modèles mélangés. Le contexte principal à deux
        # vitesses, un motif fin des voisins proches, un contexte large (voisins à 2 pas,
        # bandes cousines, luminosité) ; probabilités de 1 sur 16 bits.
        self.sfa = [32768] * (144 if dq else 48)
        self.ssa = [32768] * (144 if dq else 48)
        self.smb = [32768] * (162 * k)
        self.smc = [32768] * (108 * k)
        self.mw = [MIX_W0, MIX_W0, MIX_W0, MIX_W0, 0] * (8 * k)
        # « Amplitude > 1 ? » : même principe.
        self.gfa = [32768] * (20 * k)
        self.gsa = [32768] * (20 * k)
        self.gmb = [32768] * (384 * k)
        self.gmc = [32768] * (108 * k)
        self.gw = [MIX_W0, MIX_W0, MIX_W0, MIX_W0, 0] * (10 * k)
        if prior is not None:
            for name, values in prior.items():
                setattr(self, name, list(values))


class _Models:
    def __init__(self, dq=False, prior=None):
        self.dc = list(prior["dc"]) if prior else _probs(60)
        self.bands = [_BandModel(dq, prior["bands"][i] if prior else None) for i in range(8)]


_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_PRIOR_CACHE = {}


def _priors():
    """Table PRIORS -> (luminosité, couleurs, directions) : les probabilités de départ du format J3K5.
    Un caractère par contexte (64 niveaux dans le domaine logistique), « . » + un caractère = une
    série de contextes neutres ; les poids des mélangeurs prennent deux caractères."""
    if PRIORS not in _PRIOR_CACHE:
        if not PRIORS:
            return None
        idx = {c: i for i, c in enumerate(_B64)}
        vals, it = [], iter(PRIORS)
        for c in it:
            if c == ".":
                vals.extend([32] * (idx[next(it)] + 2))
            else:
                vals.append(idx[c])
        pos = [0]

        def take(n):
            pos[0] += n
            return vals[pos[0] - n:pos[0]]

        def probs(n, pair=False):
            out = [SQUASH[max(64 * (v - 32), -2047) + 2047] << 4 for v in take(n)]
            return [p for p in out for _ in (0, 1)] if pair else out

        def weights(n):
            v = take(2 * n)
            return [(v[2 * i] * 64 + v[2 * i + 1] - 2048) * 64 for i in range(n)]
        planes = []
        for _ in (0, 1):
            m = {"dc": probs(60, True), "bands": []}
            for _ in range(8):
                b = {"blk": probs(18, True), "gt2": probs(20, True), "eg": probs(400, True), "sgn": probs(27, True)}
                b["ssa"] = probs(144)
                b["sfa"] = list(b["ssa"])
                b["smb"], b["smc"] = probs(324), probs(216)
                b["gsa"] = probs(40)
                b["gfa"] = list(b["gsa"])
                b["gmb"], b["gmc"] = probs(768), probs(216)
                b["mw"], b["gw"] = weights(80), weights(100)
                m["bands"].append(b)
            planes.append(m)
        dirs = probs(2 * len(OFFS) * 8, True)
        if pos[0] != len(vals):
            raise ValueError("table PRIORS incohérente")
        _PRIOR_CACHE.clear()
        _PRIOR_CACHE[PRIORS] = (planes[0], planes[1], dirs)
    return _PRIOR_CACHE[PRIORS]


def _start_state(dq):
    """État de départ du codeur : (modèles de la luminosité, modèles des couleurs, probabilités des directions)."""
    pr = _priors() if dq else None
    if pr is None:
        return _Models(dq), _Models(dq), _probs(2 * len(OFFS) * 8)
    return _Models(dq, pr[0]), _Models(dq, pr[1]), list(pr[2])


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


def _code_band(rc, model, R, P, C, X, h, w, v4=True):
    """Bande haute fréquence des anciens formats (J3K3, J3K4), en lecture seule.
    Contextes déjà connus avant de la décoder :
    P = bande « parente » (même orientation, échelle 2 fois plus grossière),
    C = bandes « cousines » (même échelle, orientations déjà codées),
    X = même bande de la luminosité (pour les couleurs ; None pour la luminosité)."""
    if not h or not w:
        return
    bit, code, ue = rc.bit, rc.code, rc.ue
    blk, gt1, gt2, eg, sgn = model.blk, model.gt1, model.gt2, model.eg, model.sgn
    sfa, ssa, smb, smc, mw = model.sfa, model.ssa, model.smb, model.smc, model.mw
    gfa, gsa, gmb, gmc, gw = model.gfa, model.gsa, model.gmb, model.gmc, model.gw
    ST, SQ, SH = STRETCH, SQUASH, MIX_SHIFT
    zero = np.zeros((h, w), dtype=np.int64)
    pv = _parent_map(np.asarray(P, dtype=np.int64), h, w) if P and P[0] else None
    xv = _same_scale(X, h, w) if X is not None else None
    cv = zero
    for c in C:
        if c and c[0]:
            cv = cv + np.abs(_same_scale(c, h, w))
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
            nz = flags[by][bx] = bit(blk, 2 * (3 * c + pc) + lb)
            if not nz:
                continue
            # b) Les coefficients du bloc, un par un.
            for y in range(y0, y1):
                row = R[y]
                up = R[y - 1] if y else None
                up2 = R[y - 2] if y > 1 else None
                pmr, cmr, xmr = pm[y], cm[y], xm[y]
                xsr = xs[y] if xs is not None else None
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
                    b = code(4096 - p1)
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
                    if v4:  # « amplitude > 1 ? » : mélange de 4 modèles (J3K3 : un seul contexte)
                        ga = mc
                        gb = ((((l1 if l1 < 3 else 3) * 4 + (u1 if u1 < 3 else 3)) * 4 + (dg if dg < 3 else 3)) * 3
                              + p) * 2 + lc
                        qa, qs, qb_, qc_ = gfa[ga], gsa[ga], gmb[gb], gmc[cc]
                        t0, t1, t2, t3 = ST[qa >> 4], ST[qs >> 4], ST[qb_ >> 4], ST[qc_ >> 4]
                        kg = 5 * (mc >> 1)
                        dot = (gw[kg] * t0 + gw[kg + 1] * t1 + gw[kg + 2] * t2 + gw[kg + 3] * t3 + gw[kg + 4] * 256) >> 16
                        g1p = SQ[(dot if dot < 2047 else 2047) + 2047] if dot > -2047 else 1
                        gb1 = code(4096 - g1p)
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
                            gmc[cc] = qc_ + ((65536 - qc_) >> 5)
                        else:
                            gfa[ga] = qa - (qa >> 4)
                            gsa[ga] = qs - (qs >> 7)
                            gmb[gb] = qb_ - (qb_ >> 5)
                            gmc[cc] = qc_ - (qc_ >> 5)
                    else:
                        gb1 = bit(gt1, mc)
                    if gb1:
                        m = 3 + ue(eg, 20 * mc) if bit(gt2, mc) else 2
                    else:
                        m = 1
                    # Signe : prédit par les voisins de gauche et du dessus (et la luminosité).
                    sl = row[x - 1] if x else 0
                    su = up[x] if up is not None else 0
                    sc = 3 * ((sl > 0) - (sl < 0) + 1) + (su > 0) - (su < 0) + 1
                    if xsr is not None:
                        sc = 3 * sc + xsr[x]
                    row[x] = -m if bit(sgn, sc) else m


def _code_band_dq(rc, model, qb, R, N, P, C, X, h, w, A=None):
    """Bande haute fréquence en quantification dépendante : deux quantificateurs décalés (niveaux pairs /
    impairs du pas fin) choisis par une machine à 4 états que suit la parité de chaque valeur.
    R = valeurs codées (servent de contexte), N = niveaux de reconstruction (en pas fins).
    Encodeur : qb = blocs à coder, A = amplitudes exactes en pas fins ; chaque ligne d'un bloc est
    optimisée par un treillis (Viterbi) qui pèse l'erreur contre le coût en bits."""
    if not h or not w:
        return
    enc = A is not None
    bit, code, ue = rc.bit, rc.code, rc.ue
    blk, gt2, eg, sgn = model.blk, model.gt2, model.eg, model.sgn
    sfa, ssa, smb, smc, mw = model.sfa, model.ssa, model.smb, model.smc, model.mw
    gfa, gsa, gmb, gmc, gw = model.gfa, model.gsa, model.gmb, model.gmc, model.gw
    ST, SQ, SH, NEXT, QS = STRETCH, SQUASH, MIX_SHIFT, DQ_NEXT, DQ_QS
    zero = np.zeros((h, w), dtype=np.int64)
    pv = _parent_map(np.asarray(P, dtype=np.int64), h, w) if P and P[0] else None
    xv = _same_scale(X, h, w) if X is not None else None
    cv = zero
    for c in C:
        if c and c[0]:
            cv = cv + np.abs(_same_scale(c, h, w))
    pb = _block_any(pv, h, w) if pv is not None else None
    xb = _block_any(xv, h, w) if xv is not None else None
    pm = np.minimum(np.abs(pv if pv is not None else zero), 2).tolist()
    xs = (np.sign(xv) + 1).tolist() if xv is not None else None
    xm = np.minimum(np.abs(xv if xv is not None else zero), 2).tolist()
    cm = np.minimum(cv, 2).tolist()
    INF, lam = 1e30, DQ_LAMBDA
    if enc:  # plus grande amplitude de chaque ligne de chaque bloc (pour sauter les lignes vides)
        t = np.abs(np.asarray(A, dtype=np.float64))
        nbw_ = -(-w // BLOCK)
        t = np.pad(t, ((0, 0), (0, nbw_ * BLOCK - w)))
        amax = t.reshape(h, nbw_, BLOCK).max(axis=2).tolist()

    def trellis(y, x0, x1, st0, row, up, up2):
        """Meilleure suite de valeurs pour la ligne y du bloc (colonnes x0..x1-1), en partant de l'état st0."""
        arow = A[y]
        if amax[y][x0 // BLOCK] <= 0.5:
            return [0] * (x1 - x0)
        pmr, cmr, xmr = pm[y], cm[y], xm[y]
        xsr = xs[y] if xs is not None else None
        cost = [INF, INF, INF, INF]
        cost[st0] = 0.0
        l0 = row[x0 - 1] if x0 else 0
        l00 = abs(row[x0 - 2]) if x0 > 1 else 0
        m1 = [abs(l0)] * 4
        m2 = [l00] * 4
        back = []
        for x in range(x0, x1):
            a = arow[x]
            neg = a < 0
            if neg:
                a = -a
            u1 = dg = f2 = 0
            su = 1
            if up is not None:
                t = up[x]
                u1 = t if t >= 0 else -t
                su = (t > 0) - (t < 0) + 1
                if x:
                    dg = abs(up[x - 1])
                if x + 1 < w:
                    dg += abs(up[x + 1])
                if up2 is not None:
                    f2 = abs(up2[x])
            p = pmr[x]
            lc = 1 if xsr is not None and xsr[x] != 1 else 0
            cmx, xmx = cmr[x], xmr[x]
            if x > x0:
                t = arow[x - 1]
                sl1 = 0 if t < 0 else 2
            else:
                sl1 = (l0 > 0) - (l0 < 0) + 1
            zonly = a <= 0.5
            ncost = [INF, INF, INF, INF]
            nprev = [0, 0, 0, 0]
            nk = [0, 0, 0, 0]
            memo = {}
            for s in (0, 1, 2, 3):
                c0 = cost[s]
                if c0 >= INF:
                    continue
                l1 = m1[s]
                qq = s >> 1
                key = (l1, m2[s], QS[s])
                e = memo.get(key)
                if e is None:
                    sn = 2 * (l1 + u1) + dg
                    sb = SBIN[sn if sn < 15 else 15]
                    ca = 2 * (3 * sb + p) + lc + 48 * QS[s]
                    cb = (((((l1 if l1 < 2 else 2) * 3 + (u1 if u1 < 2 else 2)) * 3 + (dg if dg < 2 else 2)) * 3
                           + p) * 2 + lc) * 2 + qq
                    fr = m2[s] + f2
                    cc = ((((fr if fr < 3 else 3) * 3 + cmx) * 3 + xmx) * 3 + p) * 2 + qq
                    k = 5 * (sb + 8 * qq)
                    dot = (mw[k] * ST[sfa[ca] >> 4] + mw[k + 1] * ST[ssa[ca] >> 4] + mw[k + 2] * ST[smb[cb] >> 4]
                           + mw[k + 3] * ST[smc[cc] >> 4] + mw[k + 4] * 256) >> 16
                    p1 = SQ[(dot if dot < 2047 else 2047) + 2047] if dot > -2047 else 1
                    if zonly:
                        e = (lam * COST[4096 - p1],)
                    else:
                        mc = sn + 2 * p
                        mc = 2 * MLOG[mc if mc < 64 else 63] + lc
                        ga = mc + 20 * qq
                        gb = (((((l1 if l1 < 3 else 3) * 4 + (u1 if u1 < 3 else 3)) * 4 + (dg if dg < 3 else 3)) * 3
                               + p) * 2 + lc) * 2 + qq
                        gc = ((((fr if fr < 3 else 3) * 3 + cmx) * 3 + xmx) * 3 + p) * 2 + qq
                        kg = 5 * ((mc >> 1) + 10 * qq)
                        dot = (gw[kg] * ST[gfa[ga] >> 4] + gw[kg + 1] * ST[gsa[ga] >> 4] + gw[kg + 2] * ST[gmb[gb] >> 4]
                               + gw[kg + 3] * ST[gmc[gc] >> 4] + gw[kg + 4] * 256) >> 16
                        g1p = SQ[(dot if dot < 2047 else 2047) + 2047] if dot > -2047 else 1
                        sc = 3 * (sl1 if l1 else 1) + su
                        if xsr is not None:
                            sc = 3 * sc + xsr[x]
                        ps0 = (sgn[2 * sc] + sgn[2 * sc + 1]) >> 5
                        g2 = (gt2[2 * mc] + gt2[2 * mc + 1]) >> 5
                        e = (lam * COST[4096 - p1], lam * (COST[p1] + COST[4096 - ps0 if neg else ps0]),
                             lam * COST[4096 - g1p], lam * COST[g1p], lam * COST[g2], lam * COST[4096 - g2], mc)
                    memo[key] = e
                # valeur nulle
                j = c0 + a * a + e[0]
                t = NEXT[2 * s]
                if j < ncost[t]:
                    ncost[t], nprev[t], nk[t] = j, s, 0
                if zonly:
                    continue
                # les deux niveaux non nuls les plus proches de ce quantificateur
                kn = int((a + qq) * 0.5 + 0.5)
                if kn < 1:
                    kn = 1
                for kk in (kn - 1, kn):
                    if kk < 1:
                        continue
                    d = a - (2 * kk - qq) - DQ_BIAS
                    if kk == 1:
                        j = c0 + d * d + e[1] + e[2]
                    elif kk == 2:
                        j = c0 + d * d + e[1] + e[3] + e[4]
                    else:
                        j = c0 + d * d + e[1] + e[3] + e[5] + lam * _ue_cost(eg, 20 * e[6], kk - 3)
                    t = NEXT[2 * s + (kk & 1)]
                    if j < ncost[t]:
                        ncost[t], nprev[t], nk[t] = j, s, kk
            back.append((nprev, nk))
            m2n = [0, 0, 0, 0]
            for t in (0, 1, 2, 3):
                if ncost[t] < INF:
                    m2n[t] = m1[nprev[t]]
            m1, m2, cost = nk, m2n, ncost
        s = min((0, 1, 2, 3), key=cost.__getitem__)
        out = [0] * (x1 - x0)
        for i in range(x1 - x0 - 1, -1, -1):
            nprev, nk = back[i]
            k = nk[s]
            out[i] = -k if arow[x0 + i] < 0 else k
            s = nprev[s]
        return out

    nbh, nbw = -(-h // BLOCK), -(-w // BLOCK)
    flags = [[0] * nbw for _ in range(nbh)]
    for by in range(nbh):
        y0, y1 = by * BLOCK, min(by * BLOCK + BLOCK, h)
        for bx in range(nbw):
            x0, x1 = bx * BLOCK, min(bx * BLOCK + BLOCK, w)
            c = (flags[by - 1][bx] if by else 0) + (flags[by][bx - 1] if bx else 0)
            pc = pb[by][bx] if pb is not None else 2
            lb = xb[by][bx] if xb is not None else 0
            nz = flags[by][bx] = bit(blk, 2 * (3 * c + pc) + lb, qb[by][bx] if enc else 0)
            if not nz:
                continue
            state = 0
            for y in range(y0, y1):
                row, nrow = R[y], N[y]
                up = R[y - 1] if y else None
                up2 = R[y - 2] if y > 1 else None
                pmr, cmr, xmr = pm[y], cm[y], xm[y]
                xsr = xs[y] if xs is not None else None
                ks = trellis(y, x0, x1, state, row, up, up2) if enc else None
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
                    v = ks[x - x0] if enc else 0
                    m = -v if v < 0 else v
                    qq = state >> 1
                    sb = SBIN[sn if sn < 15 else 15]
                    ca = 2 * (3 * sb + p) + lc + 48 * QS[state]
                    cb = (((((l1 if l1 < 2 else 2) * 3 + (u1 if u1 < 2 else 2)) * 3 + (dg if dg < 2 else 2)) * 3
                           + p) * 2 + lc) * 2 + qq
                    fr = (abs(row[x - 2]) if x > 1 else 0) + (abs(up2[x]) if up2 is not None else 0)
                    cc = ((((fr if fr < 3 else 3) * 3 + cmr[x]) * 3 + xmr[x]) * 3 + p) * 2 + qq
                    pa, ps, pb_, pc_ = sfa[ca], ssa[ca], smb[cb], smc[cc]
                    s0, s1, s2, s3 = ST[pa >> 4], ST[ps >> 4], ST[pb_ >> 4], ST[pc_ >> 4]
                    k = 5 * (sb + 8 * qq)
                    dot = (mw[k] * s0 + mw[k + 1] * s1 + mw[k + 2] * s2 + mw[k + 3] * s3 + mw[k + 4] * 256) >> 16
                    p1 = SQ[(dot if dot < 2047 else 2047) + 2047] if dot > -2047 else 1
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
                        state = NEXT[2 * state]
                        continue
                    sfa[ca] = pa + ((65536 - pa) >> 4)
                    ssa[ca] = ps + ((65536 - ps) >> 7)
                    smb[cb] = pb_ + ((65536 - pb_) >> 5)
                    smc[cc] = pc_ + ((65536 - pc_) >> 5)
                    mc = sn + 2 * p
                    mc = 2 * MLOG[mc if mc < 64 else 63] + lc
                    ga = mc + 20 * qq
                    gb = (((((l1 if l1 < 3 else 3) * 4 + (u1 if u1 < 3 else 3)) * 4 + (dg if dg < 3 else 3)) * 3
                           + p) * 2 + lc) * 2 + qq
                    gc = ((((fr if fr < 3 else 3) * 3 + cmr[x]) * 3 + xmr[x]) * 3 + p) * 2 + qq
                    qa, qs_, qb_, qc_ = gfa[ga], gsa[ga], gmb[gb], gmc[gc]
                    t0, t1, t2, t3 = ST[qa >> 4], ST[qs_ >> 4], ST[qb_ >> 4], ST[qc_ >> 4]
                    kg = 5 * ((mc >> 1) + 10 * qq)
                    dot = (gw[kg] * t0 + gw[kg + 1] * t1 + gw[kg + 2] * t2 + gw[kg + 3] * t3 + gw[kg + 4] * 256) >> 16
                    g1p = SQ[(dot if dot < 2047 else 2047) + 2047] if dot > -2047 else 1
                    gb1 = code(4096 - g1p, 1 if m > 1 else 0)
                    err = (gb1 << 12) - g1p
                    gw[kg] += (t0 * err) >> SH
                    gw[kg + 1] += (t1 * err) >> SH
                    gw[kg + 2] += (t2 * err) >> SH
                    gw[kg + 3] += (t3 * err) >> SH
                    gw[kg + 4] += (256 * err) >> SH
                    if gb1:
                        gfa[ga] = qa + ((65536 - qa) >> 4)
                        gsa[ga] = qs_ + ((65536 - qs_) >> 7)
                        gmb[gb] = qb_ + ((65536 - qb_) >> 5)
                        gmc[gc] = qc_ + ((65536 - qc_) >> 5)
                        if bit(gt2, mc, m > 2):
                            m = 3 + ue(eg, 20 * mc, m - 3)
                        else:
                            m = 2
                    else:
                        gfa[ga] = qa - (qa >> 4)
                        gsa[ga] = qs_ - (qs_ >> 7)
                        gmb[gb] = qb_ - (qb_ >> 5)
                        gmc[gc] = qc_ - (qc_ >> 5)
                        m = 1
                    sl = row[x - 1] if x else 0
                    su = up[x] if up is not None else 0
                    sc = 3 * ((sl > 0) - (sl < 0) + 1) + (su > 0) - (su < 0) + 1
                    if xsr is not None:
                        sc = 3 * sc + xsr[x]
                    n = 2 * m - qq
                    if bit(sgn, sc, v < 0):
                        row[x], nrow[x] = -m, -n
                    else:
                        row[x], nrow[x] = m, n
                    state = NEXT[2 * state + (m & 1)]


def _code_planes(rc, models, ncolor, h, w, levels, version, flags=None, amps=None):
    """Code (flags et amps donnés) ou décode toutes les sous-bandes. Renvoie, par plan, les valeurs à
    déquantifier : niveaux de reconstruction en J3K5, valeurs quantifiées dans les anciens formats."""
    specs = band_specs(h, w, levels)
    out, ctx = [], []
    for ci in range(ncolor):
        md = models[1 if ci else 0]
        parents, bands, lv = {}, [], []
        for bi, (lev, o, ys, xs, _) in enumerate(specs):
            bh, bw = ys.stop - ys.start, xs.stop - xs.start
            f = flags[ci][bi] if flags is not None else None
            R = [[0] * bw for _ in range(bh)]
            if o == LL:
                _code_ll(rc, md.dc, f, R, bh, bw)
                lv.append(R)
            else:
                model = md.bands[2 * (min(lev, 4) - 1) + (o == HH)]
                X = ctx[0][bi] if ci else None
                C = [bands[bi - k] for k in range(1, o)]
                if version == 5:
                    N = [[0] * bw for _ in range(bh)]
                    _code_band_dq(rc, model, f, R, N, parents.get(o), C, X, bh, bw,
                                  amps[ci][bi] if amps is not None else None)
                    lv.append(N)
                else:
                    _code_band(rc, model, R, parents.get(o), C, X, bh, bw, version == 4)
                    lv.append(R)
                parents[o] = R
            bands.append(R)
        ctx.append(bands)
        out.append(lv)
    return out


def _code_dirs(rc, probs, shapes, dirs=None):
    """Directions de l'ondelette : un symbole par bloc, en 3 bits adaptatifs (arbre binaire),
    selon la direction du bloc précédent et le type de passe (verticale ou horizontale)."""
    enc = dirs is not None
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


def _dq_flags(av):
    """Encodeur : blocs BLOCK x BLOCK à coder (av = amplitudes en pas fins). Un bloc qui n'aurait que
    quelques petites valeurs coûte son drapeau, ses zéros et ces valeurs ; on le laisse vide si
    l'erreur ajoutée vaut moins que les bits gagnés."""
    h, w = av.shape
    flags = _block_any(av >= DQ_FLAG_T, h, w)
    for by, y0 in enumerate(range(0, h, BLOCK)):
        for bx, x0 in enumerate(range(0, w, BLOCK)):
            if not flags[by][bx]:
                continue
            b = av[y0:y0 + BLOCK, x0:x0 + BLOCK]
            a = b[b >= DQ_FLAG_T]
            if a.size > DQ_ZB_MAX or a.max() >= DQ_ZB_AMP:
                continue
            if np.sum(a * a - (a - 2 - DQ_BIAS) ** 2) < DQ_LAMBDA * (DQ_ZB_BITS * a.size + 0.02 * b.size):
                flags[by][bx] = 0
    return flags


def _quantize_all(prep, step, chroma):
    """Encodeur. Par plan et par sous-bande : (bande basse quantifiée, ou blocs à coder ;
    amplitudes exactes en pas fins, que le treillis quantifiera pendant le codage)."""
    specs = band_specs(prep["h"], prep["w"], prep["levels"])
    flags, amps = [], []
    for ci, a in enumerate(prep["coeffs"]):
        st = step * (chroma if ci else 1.0)
        f, e = [], []
        for lev, o, ys, xs, norm in specs:
            if o == LL:
                f.append(np.round(a[ys, xs] / (st / norm)).astype(np.int64).tolist())
                e.append(None)
            else:
                exact = a[ys, xs] / (_band_step(st, norm, lev, o) * DQ_SCALE)
                f.append(_dq_flags(np.abs(exact)))
                e.append(exact.tolist())
        flags.append(f)
        amps.append(e)
    return flags, amps


def _reconstruct(values, h, w, levels, step, chroma, alpha=None, dirs=None, version=5):
    specs = band_specs(h, w, levels)
    planes = []
    for ci, bands in enumerate(values):
        st = step * (chroma if ci else 1.0)
        a = np.zeros((h, w))
        for (lev, o, ys, xs, norm), q in zip(specs, bands):
            q = np.asarray(q, dtype=np.float64).reshape(ys.stop - ys.start, xs.stop - xs.start)
            sb = _band_step(st, norm, lev, o, version >= 4)
            if version == 5 and o != LL:
                a[ys, xs] = np.sign(q) * (np.abs(q) + DQ_BIAS) * (sb * DQ_SCALE)
            else:
                a[ys, xs] = _dequantize(q, sb, o == LL)
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


def chroma_for(step):
    """Facteur des couleurs choisi par l'encodeur (écrit dans le fichier : le décodeur n'a rien à savoir)."""
    return CHROMA * (step / 12.0) ** CHROMA_GAMMA


def encode_full(img_or_prep, step):
    """Image -> (octets .j3k, image telle qu'elle sera décodée)."""
    prep = img_or_prep if isinstance(img_or_prep, dict) else prepare(img_or_prep)
    step, chroma = _f32(step), _f32(chroma_for(step))
    if not (step > 0 and np.isfinite(step)):
        raise ValueError("pas de quantification invalide : {}".format(step))
    h, w, levels = prep["h"], prep["w"], prep["levels"]
    flags, amps = _quantize_all(prep, step, chroma)
    rc = Encoder()
    luma, colors, dir_probs = _start_state(True)
    _code_dirs(rc, dir_probs, dir_shapes(h, w, levels), prep["dirs"])
    values = _code_planes(rc, (luma, colors), len(flags), h, w, levels, 5, flags, amps)
    alpha = prep["alpha"]
    if alpha is not None:
        _code_ll(rc, _probs(60), alpha.astype(np.int64).tolist(), [[0] * w for _ in range(h)], h, w)
    nch = len(flags) + (alpha is not None)
    header = HEADER.pack(MAGIC, w, h, nch, levels, step, chroma)
    image = _reconstruct(values, h, w, levels, step, chroma, alpha, prep["dirs"])
    return header + rc.finish(), image


def encode(img_or_prep, step):
    """Image (uint8 : gris, gris+alpha, RGB ou RGBA) -> octets .j3k."""
    return encode_full(img_or_prep, step)[0]


def simulate(img_or_prep, step):
    """Image telle qu'elle sera après décodage."""
    return encode_full(img_or_prep, step)[1]


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
    w, h, nch, levels = hd["width"], hd["height"], hd["channels"], hd["levels"]
    ncolor = 1 if nch <= 2 else 3
    version = int(hd["version"][3])
    rc = Decoder(data, HEADER.size)
    luma, colors, dir_probs = _start_state(version == 5)
    dirs = _code_dirs(rc, dir_probs, dir_shapes(h, w, levels)) if version >= 4 else None
    values = _code_planes(rc, (luma, colors), ncolor, h, w, levels, version)
    alpha = None
    if nch in (2, 4):
        alpha = [[0] * w for _ in range(h)]
        _code_ll(rc, _probs(60), None, alpha, h, w)
        alpha = np.clip(np.array(alpha, dtype=np.int64), 0, 255)
    return _reconstruct(values, h, w, levels, hd["step"], hd["chroma"], alpha, dirs, version)


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


# --- priors: begin (table produite par tools/train_priors.py ; fait partie du format J3K5)
PRIORS = (
    ".Seff.Pccddeff.Lh.Ff.Df.Fhgigjgighgegdgd._.2h.lhh.kiih.jhjjh.ifhjih.hdfiji.hdegiih.ghedigejif.QQgUgZgbgbgdgd"
    "gdgfgdgdgfgfgf.BhghgighghgjgigigkgUgZgdgege.Bf.Bh.DhghghgjghgigjghgigkghgiglgWgagegege.Fh.Dighgigjghgigjghgi"
    "gkghgiglgRV.AUY.AZd.AZb.Aad.Acf.Adf.Ad.Beh.Acf.Acf.Aeh.Acf.Acf.Adh.Ad.Bd.Beh.Fh.Ahi.Bh.Bh.Ahj.Bh.Bi.Ahj.Ac.B"
    "dh.Afj.Ad.Bdh.Afi.Afh.Afh.Bj.Afh.Afh.Bi.Afi.Afi.Bj.Bi.Bi.Ahk.Bh.Bh.Ahi.Bh.Ahi.Aij.Ahi.Aij.Ajl.Bh.Bi.Aik.Bh.B"
    "i.Aik.Ahh.Ahi.Aik.Bh.Bh.Aij.Ahh.Ahi.Ajk.Aii.Aij.Akl.Ehh.Aii.Ahh.Ahi.Ajk.Ajj.Ajk.Alm.ATXWabf.Kbdcffh.Kdee.Ai."
    "KZbadeh.Kege.Aj.Kf.Ahhj.Kdfdfgi.Lhghhj.Lhhiik.Kfhfhhk.Khihjil.Kiiijjl.Kfgfgdgcgcgd.Bigjgjgegegbgbgbgd.Bigjgj"
    "gcc.Aba.Aba.Aec.Adb.Acc.Aed.Aed.Aed.Aff.Aff.Aff.Aed.Adc.Adc.Aec.Adc.Adc.Aee.Aed.Add.Aff.Aff.Aff.Eff.Aff.Aff."
    "Aff.Aff.Aff.Aff.Aff.Aff.Aff.Aff.wdb.Aca.Abb.Aed.Adc.Add.Aee.Aed.Aee.Aff.Aff.Aff.Aee.Aed.Aee.Aed.Add.Add.Aee."
    "Aee.Ade.Aff.Aff.Af.Rff.Af.Bf.Bff.Af.Bf.Bf.Bf.Vhh.Ihh.Jh.Aff.Aff.Af.Bff.Aff.Aff.Eff.Af.Zf.Bff.Af.Ff.Oh.hh.Fh."
    "Ahh.Uhh.Ihh.Bh.Ahh.Aij.Ihh.Ihh.Ihh.Fh.Ahi.Ihh.Ihi.Fh.Aii.Ahh.Ahh.Aii.Uhh.Ihi.Ahh.Ahh.Ajj.Ihh.Ihi.Fh.Aii.Ahi."
    "Aii.Akk.Abbaabb.Keeedee.Kffffff.Kcbbbcc.Keeedee.Kffff.Mddddee.Kfeeeff.Mf.Nffff.Of.Ah.Lhghhi.KhzmYiGlXZYj9kwk"
    "PlaeijtkXkej7e1jej0lpkNe5jskIk4jegPkAkVk-jOhMkbkjk5jjhXkVkwlNj_hxiwlsiqmFazkDkmkPk0epj0kZlPiPgzjgkHlIjKg5kQk"
    "jk2iwiSkgkhkujniKkqkkk6kVh0kYkslMkqh6lUk4lGlOfalBk1k7lEfql0k_lHlYfAlvk7k5lSewlWkyk4lNevkskTlHk2eykFkOlDkRgCk"
    "RknlBkLgykqk0lCkogdk0k1k_k4gNlLk3lAlQfelHk3k_lOfglUk0k2lQe9lFknkxlJenkqkbk0lCeqkOj2lWkge7j5kKlEj6gekQkslFkHg"
    "6ksk4k_kr.Ak1k1lAk4gNh.Lf.Fhghgigighgfgfgf._._.dhh.khhh.jhihh.jhih.ifghhh.hf.ljjhigeged.QQgWgagdgdgegdgdgege"
    "gegfgfgf.FhghghgighghgigVgagegegf.Bf.Bh.DhghghgighghgighghgighghgjgXgbgegfgf.Fh.Dh.BhgighghgighghgighghgjgRV"
    ".AUY.AYc.Aac.Aad.Acf.Aef.Ad.Beh.Ade.Acf.Ae.Bdf.Adf.Aeh.Af.Be.Bfi.Jh.Fh.Bi.Bh.Bh.Ahj.Acf.Ac.Beh.Ae.Bd.Bfh.Afh"
    ".Afh.Bi.Af.Bfh.Afi.Afh.Afh.Afi.Bh.Bh.Bi.Fh.Ahh.Bh.Bh.Ahi.Ahh.Ahi.Aij.Fh.Bi.Bh.Bh.Ahi.Bh.Ahh.Ahj.Fh.Ahi.Bh.Ah"
    "h.Ahj.Ahh.Ahi.Aik.Fh.Ahh.Ahh.Ahh.Aii.Ahi.Aii.Ajk.APUTYYc.KVZXbaf.Kadbedh.Kabaccf.Kcdbed.Lefegfi.Keeefe.Lefeg"
    "eh.Kfhfhgj.Kfffgf.Lfgf.Ah.Khihihj.Kf.Begdgdgdgfghghghgegfgdgcgcgd.Bhghghgec.Adb.Aca.Afd.Aec.Adc.Afe.Aed.Aed."
    "Bf.Aff.Aff.Afd.Aed.Aed.Aed.Aec.Add.Afe.Aee.Aee.Aff.Aff.Aff.Iff.Bf.Aff.Aff.Eff.Aff.Iff.wed.Adc.Adc.Aee.Aed.Ad"
    "d.Afe.Afe.Aee.Bf.Aff.Aff.Aff.Afe.Aee.Aee.Aed.Add.Aff.Aee.Aee.Af.Bff.Aff.Qff.Aff.Eff.Af.6h.Ff.Aff.Eff.Aff.Ff."
    "Aff.cff.Af.Fff.Af.6h.hh.Ihh.th.hh.Ihh.Vh.Jh.Ihh.Uhh.Ihh.Bh.Ahh.Aii.Afeecec.Kecdcdb.Keddcdc.Kfeeded.Keddcdc.K"
    "eddcdd.Kfffefe.Kfeeded.Keeedee.Lfffff.Kfffffe.Kffffff.KjqkWjUmPctkpktknlrfGkjklkhlKfEkSkakmlIfRkVkiklksf-kfk"
    "qkokfghkvkwkvkrgjkykyk0kxghj0kgjwnDdIkfkrkjlbfTkPkkkfkkgNkHkdkbkrghkjkpklkihBkukskpkyg-kxkvkyk6gtkwkxk4lBgkl"
    "Ck1k7k6fsk5k0k2k4f1lck6lAk_fYlulAk-k9fIlbk7k2k7fMlDkyk0kzfKklkpkukrf4krkxkwkpgRk1k1k1kzgJk0kzk1k0gClKk3lBk_f"
    "nlEk2k9k_fqlgk6lAk-fRldk0k1k6fElNkwkzk6fGkskikzkvfPkekmkukkgHkukzkykpgXk1k0k2kzgIkzkzk0k0gB.Uhghghghgfgegcgc"
    "._._._.Fhh.jfhih.iegiih.hdehiih.gcdfhiih.gfehgfih.RSgXgbgdgdgegegegfgegegf.Dh.BhgighghgjghgigkgWgbgegfgf.Fh."
    "Dh.Bhgi.Bhgj.BhgjghgiglgYgcgfgfgf.Fh.Dh.Bhgighghgj.BhgjghgiglgRW.AVZ.Aad.Aac.Abe.Adf.Aef.Ae.Bfh.Adf.Adf.Afh."
    "Adf.Ad.Beh.Ae.Beh.Afi.Ihh.Fh.Ahi.Bh.Bh.Ahj.Ad.Bdh.Afi.Ae.Be.Bfi.Bh.Afh.Ahj.Af.Bfh.Bh.Afh.Afh.Ahj.Bh.Bi.Aik.I"
    "hh.Bh.Bh.Ahi.Ahi.Aij.Ajl.Fh.Aij.Fh.Aij.Bh.Ahi.Ajk.Fh.Ahi.Bh.Ahh.Aij.Ahi.Aij.Akm.Ihh.Ehh.Ajj.Aij.Ajk.Alm.AUYX"
    "cc.Ldeeffh.Keff.Ah.Kacbefi.Kfgfghi.Nhij.Kefeghj.Nhhj.Lhghij.Lhghik.Khhhijl.Khiijkl.Kf.Begdgdge.Bigjgkgfgfgdg"
    "cgdgeghgigkgkged.Adb.Adc.Afd.Aed.Aed.Aff.Afe.Afe.Ff.Efe.Afe.Afe.Afe.Aed.Afe.Aff.Afe.Aff.Bf.Aff.Bf._.bhh.Aed."
    "Aec.Aed.Afe.Aee.Aee.Aff.Aff.Aff.Mff.Aff.Aff.Afe.Aee.Aff.Aff.Aff.Aff.5h.Uhh.Ihh.Fh.Ahi.shh.shh.shh.Vh.Ihh.Bh."
    "Ahh.Aij.Ihh.Ihh.Ihh.Fh.Aii.Jh.Ihh.Iii.Bh.Ahh.Ajj.Uhh.Ihh.Bh.Ahh.Ajj.Ihh.Ihh.Iii.Ahi.Aii.Akl.Adccbdd.Kffffff."
    "Nf.Mdddcee.Kffffgf.Ph.Keeeeff.Kffff.Qhh.Ohh.Oii.Khhhhjj.KjqkZjTlCb5lFk0k7ljemkpklkqkVe7kYkelOkffAkSkik3kGgHk"
    "dkkkxjwg6kpkjkukKhEkWkilCj-iAj9kmj2lVcekukvk0lDe5kekpk8jUgskXkkk-jyg2k1kuk5j8hwlAksk1kTh5lAktk5krhpkmknlCkmi"
    "elEk2k8lAflk6k0k3k7fwljk8lAlKfJmBlGlAlMetlwlDk8lJeslEkxk7k6e7kZkikzkkf9kakokrkYglklkvk2km.Akok2k8kzgalMk3lCl"
    "IfhlGk2k-lHfklwk_lFlUe8lzk-k8lNeolXk2k2lEexktkelEk0fKkTkgkxkTgPkbkrkvkTgskpkzkykoglksk5k8k2gb.Whghgh.Begege."
    "_._._.Fh.lhhh.ifghh.iefhhh.hefghhh.ghh.Dff.QSgZgdgegegfgfgegfgfgf.Fh.Dh.BhgighghgjgYgdgfgf.Nh.Dh.Bhgi.Di.Bhg"
    "jgagdgfgf.Nh.Dh.Bhgh.DighghgjgSV.AWa.Abe.Acd.Ace.Ae.Bef.Af.Ch.Aef.Aef.Af.Bef.Ae.Bfh.Af.Bf.Ci.Jh.Jh.Bh.Bh.Aij"
    ".Aef.Ae.Bf.Bf.Bf.Ch.Fh.Ahi.Af.Bf.Ch.Af.Bfh.Bh.Bh.Bh.Ahj.Jh.Fh.Ahh.Ahh.Ahi.Ajk.Ihh.Ihh.Bh.Ahh.Aij.Ihh.Fh.Ahi."
    "Ahh.Ahi.Ajk.Ihh.Fh.Aii.Ahi.Aij.Akl.ARVWabe.KYbaddf.Kbed.Ai.Kbccdef.Kdedff.Lfgfhhj.Keffff.Lfgf.Ohghij.Kfffff."
    "Lfgf.Ah.Khiijjk.Ofgfgfgf.Bhgigigfgfgegegegf.Bigigigfe.Aed.Aee.Afe.Afe.Afe.Aff.Aff.Aff.Mff.Afe.Aff.Aff.Afe.Af"
    "f.Bf.Aff.Aff._.nhh.Aff.Afe.Aff.Aff.Aff.Aff.Aff.Aff.Aff.Mff.Aff.Aff.Aff.Aff.Aff.Aff.Aff.Af.6h.shi.th.shh.shh."
    "ph.Aii.shh.Vh.Jh.Fh.Aii.hh.Fh.Aii.Uhh.Ihh.Ahh.Ahh.Ajj.Afefeff.Kfefefe.Kfeeeff.Kfefeff.Kfefefe.Kfeeeff.Lfgfgf"
    ".Kffffff.Kffff.gffgf.Ohh.KlCkAkLl7dJlPk3lClqe7lJk1k2lce4k4kvkzlYfHkpktkvk_f7kskskrkygckzkukuk1gkkwkskukxg8k-"
    "kfkpmcdkk1kyk3ldfTkrkvktlEf4krkukvlKgbk2kxkxlCg4k9kykylIhBk8kyk0lIg4k3kskylIhMk5k0k2k2fyk2k0k1k1f3lLk3k5k5ff"
    "lkk-k6k7fJlZk9k3k6fMlHk3k0k0fNkpkskvkvf3kqkvksktgUkykzkxk0gRk1k0k2k3gKlFk2k-k6fpk-k1k5k6ftllk9lCk-fOl0lCk9k-"
    "e6lhk_k5k9fBlBk0k0k3fOkmkrkuktgDkukwkrktgZkzkzkxk0gSk2k1k4k4gL.Yh.Bfgegdgb._._._.sfhh.jeghh.ideghh.hcdeghiih"
    ".ffehgfh.SYgbgegfgfgfgfgf.Bfgf.Fh.Dh.Dighghglgbgegf.Ph.Dh.Dh.Dh.Bhglgcgegf.Ph.Dh.Dh.Di.BhglgWY.AYb.Abe.Ade.A"
    "df.Ae.Bf.Bf.Ch.Af.Bf.Bf.Bf.Bf.Bf.Bf.Bf.Ch.Jh.Jh.Fh.Aij.Af.Bf.Ch.Af.Bf.Ch.Ihi.Jh.Fh.Bh.Bh.Bh.Aij.Uhh.Ahh.Ahh."
    "Akl.Iii.Ihh.Fh.Ajj.Ihh.Ihi.Ahh.Ahh.Akl.Ihh.Iii.Aii.Aij.Amn.AXaZdd.Lfff.Ah.Kfff.Ah.Kdedfgi.Ohh.Ohi.Kfgfghj.Oh"
    "h.Oii.Lhhijk.Lhhhjl.Khhhilm.Qfgfgfghgigjgk.Dfgegegfghgigjgkgff.Afe.Afe.Aff.Aff.Aff.Bf.Bf.Bf.Nf.Bf.Bf.Aff.Aff"
    ".Aff.Ff.Bf._.nhh.Aff.Aff.Aff.Bf.Aff.Aff.df.Ff.Aff.Aff.Vh.th.sii._.bhh.shh.sij.Ihh.Jh.Uii.Uhh.Ihh.Iij.hh.Fh.A"
    "jj.Uhh.Ihh.Ahh.Ahi.All.Afefdff.ufefeff.Nf.Qhh.Kffff.ihh.Oii.Oii.Khhhhjk.Klukhk4lRdelVk4lHlae7lGk1k5k-fIk8kzk"
    "9kzfRkvkxk3kngNk4kyk4kngslDk0k4kvg4k6kkkxkWh0lpk1lclddvk-k0k-lBfRk0kyk5kngOk4kzk6kxghlFk1k-k2hIlRk3lBlEhZlSk"
    "3lFlOhVlMkulElCiWk1kzk0k1f3k0kzk0k0f6k_k1k2k3folTk5k3k6fSlUk6k3k4fNlDk3k1kwfPkskvk0ktgDkykxktksghk7kzkzk1gik"
    "5k5k9k5glk9k1k4k5fuk4k0k2k4fylTk5k8lAfXlslAk9lCe-lck-k4k9fCk-k0k4kzfTkukwk0kpgQk7k0kxkqgolAk1k1k0gmk7k6k6k4g"
    "l.eegegd._._._._.Tfghh.iefghh.hdefghh.7Ygdgfgfgf.Bfgf.Rh.Dh.Bhgjgdgf.dh.Dh.Djgegf.dh.Dh.DjgVZ.AZc.Ade.Aef.Ae"
    "f.Af.Bf.Bf.Fff.Af.Ff.Bf.Oh.Vh.Iii.Af.Bf.Jf.Oh.Vh.Ihi.Uhh.Ehh.Ajj.Ihh.Jh.Iii.Uhh.Ehh.Ajj.Uhh.Ahh.Ahi.Akl.AWZa"
    "ddf.Kbddfe.Ldfe.Ai.Keeefff.Kfffgf.Ohhi.Kfff.ihij.uhhhijl.Qfgfgf.Bhgigi.Dfgfgfgfghghgigi.Af.Aff.Aff.Bf.Bf.Bf."
    "Zf.Bf.Ff.Bf._.3hh.Bf.Bf.Jf._.4h.sii._.ch.th.sii.shh.sii.sii.ghh.Bh.Ahh.Ajk.Bfgfgf.Kffffff.Lfff.Nfgf.Nfffgf.K"
    "ffff.hf.Qhh.xhii.Kmqk5lolxdmlIk2k_lSfNlGk2k4lKfMk7k0k1lGfbkzkzk0k7gCk5k0k1k8gYk7k0k3k8gflCktktk2g-mFk9l4lwd5"
    "k5k0k5lEffk0kzk0lAf2k1kzk0lCgOk6k0k2lDglk_k1k4lIgvlDk1k-lPgvlLk0k7lPhTk0kzk0k0f8k0kzkzk0f8k5k0k0k0fwlEk2k1k1"
    "fjlEk2k0k1fik_k1kzkzfjkykykzk1gDk3kzkyk2gWk8k1k0k6gWk8k2k2k8gTk5k0k2k1fyk2kzk0k0f3lGk2k4k3fjlWk6k3k4fUlMk4k1"
    "k2fZk7k1k0k0fkkzkykzk1gLk7k1kyk3galAk2k1k-gYk9k2k3k9gS.cfgegdgb._._._.sf.lef.kdfgh.ibcdfghhh.gf.Bh.Sdgegf.bh"
    ".Dhgjghglgfgf.dh.Dhgi.Bkgf.lhgi.Bkgbc.Ace.Aef.Aff.Aff.xh.Yii.Eij.Jh.Vh.Zh.Ehh.Yhh.Bh.Ajj.Ahh.Ehh.Jh.Ahh.Eii."
    "Uhh.Ahh.Bh.Ajj.Uhh.Ann.Ahh.Ano.Acecefh.hh.Kfgfghi.Ph.Ohh.Ohi.Ohh.Ohh.Kklhhlm.Khh.Ajj.Kllhhmm.Whghgigk.Lhgigi"
    "gk.Af.Bf.Bf._._.mhh.Ehi._._.yhh.Ehi._._.yhh.Eii.Ahh.Ehh.ghh.khh.Eii.khh.Eii.hh.All.Ahh.Alm.Cff.zf.1h.ghh.Kjk"
    ".Ajk.Khh.Aii.Kkkhhkk.KmHlBljlbegk6k0k3k7fok4k0k0k5fvk2kzk2k1f5k2k0k1k2gSk9k1k3k5gmlGk2k8lBgwmWk6lFlIiOlgk6lZ"
    "lHe2k2kzk1k1fwk1kzk1k0gNk2kzk1k5gVk5k0k2k-gmlCk1k6lIg3lGk2lClRhAmvlCltl4jAk0kzkzkzf_kzkzkzkzf-k1kzk0k0f4k3k0"
    "k0kzf0k5k0k0kzfwk1kzk0kyf3k1kzk0k1gQlGk3k3k9gqlWk6k9lHgwlpk8lDlGg8k1kzk0k0f3k0kzk0kzf7k5k0k1k1fxk_k1k0k1fpk7"
    "k0k0kzfuk2kzk0kwf4k6k0k0k4galOk4k3k-gwlck7k_lIg0lpk8lDlFg8.efgegd._._._._.Tf.lff.kdde.Ahh.6dgf.li.Bjgf.nh.Bi"
    "gf.nh.Bigbd.Ade.Aff.Aff.Af.yh.Yhh.Ehh._.Dhh.Yhh.Eii.Ihh.Nh.Ehh.Yhh.Eii.Vh.Akk.Ahh.All.Aadeff.Ldffff.Lfgf.Ah."
    "Kfff.gh.Ahh.uhh.Ahh.ukkhhll.Yhghgj.Nhghgi.Af.Bf._._.rh.Ehh._._.6hh._._.6hh.shh.shh.shh.kjj.Ahh.Akk._._.Th.uj"
    "jghjj.KmMlBlmlTehk3k0k1k6fuk3k0k0k4fwk1kzk0k4f4k0kzkzk3gIk3k0k0k6gUk7k0k2k-gcl-k8lFlXhjlWk4lPlFfBk1kzk0k2f0k"
    "zkzkzk2f_k0kzk0k4gKk1kzk0k6gUk4k0k2k_gek7k0k4lEgjl5k9lVlxh8kzkzkzkzf-kzkzkzkzf_k0kzkzkzf6k1kzkzkzf3k2k0kzk0f"
    "3k0kzkzkzf5k0kzkzk2gIk7k0k0k7gYlEk2k3lBgclZk6k9lHgmk0kzk0kzf5kzkzkzkzf-k2k0k0kzf2k5k0k0k0fyk3k0kzk0f1k1kzkzk"
    "0f6k1kzkzk3gOlBk2k1k8gclGk3k3lCgelak6k8lHgl.Scef.Pdeeff.Njigi.Vhghgh._._._._._._.Tf.Gfh.Dh.DPXYZbafffffeffff"
    "ffffffff.WWbbbddffffgf.iXbccedffffgf.iOTTXUYWZYaYaccdedddededefffgfffgfgegfffffffgfffgeffgfffgfgegfffgfgfgfg"
    "f.Kh.Weffgfgegfgehfgfgfgfgfge.Ffgfgf.Bf.Bf.Bfh.Bh.Bh.Bh.Bh.Fh.Vh.Ihh.Jh.th.Ihh.ghhOUVYYbVYXaZcWaXbadadddefce"
    "deefefefegeffffffffffffgfgfgbbccdededeefefefegfffffffffffffgf.Cf.Cfgf.Df.Bfffffffffff.Bf.Ah.Iff.Rh.Th.Kf.Wf."
    "Bffefefe.Hfgffffefefe.Gffeeffeefeddfffegffefffegfgf.Aff.Afe.Jfffffgffeffeefffffffeffeegfff.Affgfff.Jf.Vf.If."
    "7fffeffeeffedffffgffffffe.Bf.Aff.Aff.Mff.Aff.Affffffgfffffee.Aff.Aff.Aff._._._._._._._.jeefeedfefeedeeeddcgf"
    ".Bfgfgfgffffefe.Hfgf.Afffffefeeefefeeefeeddd.Ffgfgffefffe.Jf.Bfffffffffgfffffffffee.Kffffff.gff.ik7k2kllCeMk"
    "3kzk0lHftk1kxkykvfwk0kxkzk3fvkykykzklgDk0kzkzkrgGk0kzk0kxgDkzkzkzkzgBlCktk5lZeskykxkzlAfwkwkxkzkigJkykyk0kqg"
    "Gk1kzk0kigPk1kzk0kugJk0kzk0kzgDkzkzkzkzgBk3k0k1k2f5k0kzk0k0f9k9k1k2k3f0lFk2k2k4fulDk2k1k4fvk_k1k0k3fxk0kzkzk"
    "0f9kzkzkzkzgAkzkzkzkzgAkzkzkzkzgAk3k0k1k2f6k2k0k1k2f7k_k1k2k4fxlIk2k2k5fplEk2k1k4frk-k1k1k3fvkzkzkzkygAkzkzk"
    "zkzgAkzkzkzkzgAkzkzkzkzgAihgh._._._._._._._.EPZaadcffgfgfffgf.Af.Af.YXcdcfe.oadeefe.oOQWZXaYaacaceeefffeffgf"
    ".Ff.Bfgfffffffffgfgfgf.Bf.Bf.veffgfgfgfgf.Bf.Bf.Pf.Df.Bf._._.lORXbbdXaYbbdZbacbdZcceefcdceefdedeefefefffefef"
    "fgfffffgcdeffgefffffffffffffff.Afgfgfgfffff.Ffgf.Ffgfff.Jf._.ffgfgfgf.Hf.Bfffgfgf.Gffffgfffgfffffffgfffgfffg"
    "f.Df.Aff.Mff.Aff.Affffffgfff.Afff._.0ffff.Aff.Affffff.Aff.Bf.Xfgf.Bf.Bfgfgf.Aff.Aff._._._._._._._.veefffffff"
    "fffffffff.Dfgfffgffffeff.Dfgfgfgfffffffffffgfffgfffgfffff.Ffffgfffffff.Ffgfgfffffffgf.Nf.Nfgf.Pf.0lDkukyk7fM"
    "k0kzk0k3f5k1kzk0k2f4k0kzkzkwf7kzkzkzkxgAkzkzkzkygAkzkzkzkzgAkzkzkzkzgAlKkxlDlEfYkzkzkzk1f8kzkzkzkxf-kzkzkzks"
    "gDkzkzkzkwgCkzkzkzkygBkzkzkzkzgAkzkzkzkzgAk0kzk0k0f-k0kzkzk0f-k1k0k0k0f8k4k0k0k0f5k2k0kzk0f8k1k0kzk0f8kzkzkz"
    "kzgAkzkzkzkzgAkzkzkzkzgAkzkzkzkzgAk1kzk0k0f9k1kzk0k0f9k2k0k0k0f7k6k0k0k0f3k3k0k0k0f6k1k0kzk0f8kzkzkzkzgAkzkz"
    "kzkzgAkzkzkzkzgAkzkzkzkzgAhh.Vhghgi.Bfgfgf._._._.Xhh.lhh.jfhh.jfghh.5fgffgf.Hhghhgh.BMWZZddfefefffffefffffef"
    "f.Jh.Dh.BhgiWZcceffffff.Eh.Dh.Dh.Dh.Dh.DiXacdeffffffgfgf.Ah.Dh.Dh.Dh.Dh.BhgiKRTYTXWaacbeddcdddcededfffefffeg"
    "fffgffefffeffffhffdfefdfffegffefffegfffh.Jh.Jh.Cfgfgf.Ahefegefehfffifffgffegfffh.Afgfgfgf.Ahf.Bfgf.Ch.Bhfgfh"
    "f.Ai.Bhf.Ahfghi.Ihh.Fh.Ahi.Bh.Bh.Ahi.Fh.Ahi.Fh.Ahi.Fh.Ahi.Ihh.Fh.Ahi.Ahh.Ahh.Aij.Jh.Ehh.Ahi.Ahh.AhighijKSUYa"
    "cTXWabeXbaddhccdeffdeeffgefe.Ahdeeffgeffffgfgf.Ahbbccdecdcddfdee.Aifffffgfffgfgfgf.Aigffffgfgfgf.Dhheeeeefff"
    "effgfgfghifgf.Dfgf.Chhi.Nhhiffefeffgfgf.Ahghij.Lhghhi.Jhhhhhiigf.Bfgegege.Bhgh.Bfgfgefdfdfe.Bhgh.Cedgfdcffdc"
    ".Afegfedgfed.Aff.Aff.Afe.Ff.Aff.Afe.Afegfee.Afegffegfee.Aff.Aff.Aff.Ff.Aff.Uf.Kf.Ff.Bf.weeffedffdd.Aff.Afegf"
    "ee.Aff.Aff.Aff.Ff.Eff.Aff.Aff.Aff.Afegfee.Aff.Aff.Aff.Ef.Bf._._._._.Zhh.Jh.4hh.Ihh.th.Fh.Ahh.Vh.Ihh.Bh.Ahh.A"
    "higffffefefeedddccdc.Ffgfffffffff.Hfgfgfgfgfgffffffffeeeeddddd.Ffgfffffffff.Hfff.Aff.Dfffgfffffffeeff.Hfgf.A"
    "ff.Lf.Af.Efff.Affff.Aff.Jfgf.Nf.FhlFkYkNlwcDlEkvk2lze8k5kpkqlUfLk6knk8ltfQktkvk0lBf8kykxk0k3gQk2kyk0k1gRk4ky"
    "k2k0gRlCkXkzmldDk5kwk6lxfFkzkwk-k7gQkwkuk7lYf6k5kyk2kzgok-kzk1k3gmk6kzk2k4gbk5kzk5k5gVk-k1k5k6fuk2k0k1k2f4lV"
    "k5k9lAfcljk8k8lBfMlck6k5lAfQlIk0k2k6fbkykxk1kzf8kzkzk1kygGk1k0k0k0gDkzkzk0kzgBlEk2k9k9fqk7k0k3k6fxlck6lAlBfR"
    "lok4k5lBfAlXk0kzk-fJlCkuk2k5fbkvkwk2k0f_k0kzk1kygIk1k0k0k0gEkzkzk0kzgB.Xhghgh._._._._._._.rRXaZedffffgfgfffg"
    "fgfgfgf.Ph.DhYbddffgf.Th.Dh.Kacedff.Vh.Dh.KOTUYWZXabcaeeedeeedeefeffffgfffgfgfgffffffeffffgffefffegfgf.Dfgfg"
    "ffgh.Vh.Jhffefffegfffgfgfgfgfgfgf.Dfgf.Ch.Ef.Oh.Fh.Bh.Vh.Ihh.Vh.Jh.Jh.Jh.Ihh.Zh.Ah.BhhOTXacdUYYbdeXZZccfZbbd"
    "eeacbddfadbddgcddeefdedeefdfdffhededefeeeeffeeeffgfffeffffefffefeffgfffffffffffgfgf.Ahffffffgffffgfgf.Cfffff"
    ".Afgf.Bf.Dfgfgfgfgf.Chgh.Uf.Df.Uhhi.Dfgfgfgf.Hf.Bfgegegf.Ifegfedgfed.Affgffe.Afe.Bf.Aff.Aff.Nf.Afe.Aff.Bf.Af"
    "f.Aff.Bf.Aff.Aff.Jf.Uf.3f.Pff.Afe.Afe.Aff.Aff.Aff.Aff.Aff.Aff.Nf.Bf.Aff.Aff.Aff.Aff.Eff.Aff.If._._.jf._._._."
    "_.8fgffffefe.Dfgfgffffffefe.Bfffgffffffffeee.Hfgfgfgfff.Dfgfgfgfffffff.Bfgfgffffffffefe.Nf.Jfgfgfffff.Bfff.B"
    "fffffffff.nf.Cff.Afgfflhkkk2lSeGlBk0k5lOfelBk0k1lEfdk9kykzlEflkzkzkzk3f6kzkzkzkzgCk0kzkzkzgDk0kzk0kzgClZktlM"
    "lgelk4kzk4lJfkk0kzkzlBfxk0kzkzk9f7k0kzk0k0gIk1kzk0k1gIk0kzk0k1gEk0kzk0k0gCk1kzk0k0f6k0kzkzkzf-k7k0k2k1f0lCk2"
    "k2k1fvk_k1k0k0fxk7k0kzkzfzkzkzkzkzf9kzkzkzkzgAkzkzkzkzgAkzkzkzkzgAk4k0k2k0f4k1kzk0k0f7lEk2k5k1ftlNk4k3k2fmlE"
    "k2k0k1frk6kzkzkzfykzkzkzkzf-kzkzkzkzgBkzkzkzkzgAkzkzkzkzgA.Xhghgh.Bfgegf._._._.Yh.lhh.jfghh.iffhh.iff.Ah.hh."
    "ETYaaeefffffffffff.Affff.Kh.Dh.BhgiZbddf.Qh.Dh.Dh.Dh.Dibceef.Qh.Dh.Dh.Dh.DiOSVYVZXbbdcfeedeeedfeeegfffgfffgf"
    "fghfffffffgfffgfffgffegfffhfgfgfffgffgh.Jh.Jh.Fhfghifffgffegffgifffgfffgffgh.Fhf.Ai.Jh.Fh.Bh.Bh.Bh.Ahi.Jh.Ih"
    "h.Bh.Ahh.Aij.Ihh.Ihh.Fh.Aii.Jh.Fh.Ahi.Bh.Ahh.Aij.Jh.Ihh.Ahh.AhighjjPTVZbdVYXbceZcbefhdeeffffffffgfgf.Aheffff"
    "ffffgfgf.Chccddeededeefefeghi.Af.Lhh.Af.Lhiffeffffffffgf.Ahhj.Ohi.Ohiffffeffgfgf.Ahhhij.Lhhhij.Jhghhhij.Dfgf"
    "gfgf.Bhghghgf.Begegegf.Bhgigh.Afegfedgfed.Aff.Afe.Afe.Bf.Aff.Aff.Nf.Aff.Aff.Aff.Aff.Aff.Bf.Aff.Aff._.rff.Afe"
    "gfee.Bf.Aff.Aff.Ff.Aff.Rf.Af.Bff.Aff.Aff.Eff.Aff._.oh._._.Lh.shh.shh.Vh.Ihh.Ihh.shi.ghh.Ehh.Aii.Bffffffefeee"
    "edee.Nff.Ufffgffffffeeeee.Nf.Xfgfgffffffff.of.Bfff.Dh.Ohh.OhhmKkSkyl8cHlfk5lLmDeglRk2k4lqeulHkzk8lte5kykyk0l"
    "Ff4k4kzk0k8gYk9k0k2k6gdlCkyk2k7gmmIktlwmucmlHk1lHlze0k9kzk9lUf4lBk0k6ligClDk2k5lEgzlHk2k5lEg5lDk1k7lEgtlEkzk"
    "-lDgyk4k0k2k2fxk0kzk0k0f5lFk2k4k6fhlgk8k6k-fJldk8k5k9fHlPk5k1k6fMkvkxk0kzf7kzkzkzkygMk3k0k0k1gKk3k0k3k2gFk_k"
    "1k6k6fpk5k0k2k3fxlgk7lBlCfKl3lCk-lEe0lmk_k6lCe5lFk1k2k9fOkvkxk0k0gAk3k0kzk0gRk7k1k1k2gMk2k0k2k2gF.bh.Bfgf._."
    "_._._.qh.jf._.TVadcffgfgf.Bfgf.Xh.Dhbdfef.ch.Dh.Dhdefff.ih.DhRVWZYbZcdedfffefffeffffgf.Bfgfgf.Bfgfffffgfgfgf"
    "ffgfgfgf.Mh.ghhfffgfffgfgf.Ff.Ch.Jh.Vh.Ihh.Vh.Fh.Ahi.Vh.Ihh.Vh.Fh.Ahh.Vh.Ahh.Ahh.AiiSVZcdeYbcdefacceegbddeef"
    "deefffcedffgefefffefeffgegf.Aheefffffffffgfffffgfffffffgfffgfgf.Bfgfgfgfgfgf.Chhiffffff.Of.ahhi.Cf.qhhhii.Ff"
    "gfgf.Dh.Ffgfgfgf.Bhgh.Cff.Afe.Afe.Bf.Aff.Aff.Jf.Nf.Bf.Aff.Bf.Aff.Aff._.4f.Aff.Aff.Ff.Aff._._.jh._._.7h._.bhh"
    ".sh.qh.Ahh.Hf.Bfffff.Ffgf.Affffff.Hfffffffff.Lfgfgf.Lfffff.Ifffffff._.jf.Em8k9lylydLlLk3lAlUfHlKk2k4lNfFlBk1"
    "k1lIfVk0kzkzk6f4k1kzk0k3gGk2k0k0k2gIk3kzk0k2gKmelAmLl6dfk8k0k7lLfWk3kzk1lEfnk4k0k1lCf7k4k0k1k7gPk4k0k1k7gRk4"
    "k0k1k5gOk5k0k3k6gOk0kzk0k0f7k0kzkzkzf9k5k0k1k0fylDk2k1k1fmlCk2k0k1fok9k1k0k0fqkykzkzk0f9kzkzkzkzgDk0kzk0k0gC"
    "kzkzkzkzgBk4k0k2k0f0k1kzk0k0f5lEk2k4k2fmlRk5k3k2fblIk4k1k2fgk-k1k0k2fokykzkzk0f_k0k0kzk0gFk1k0kzk0gDkzkzk0k0"
    "gB.dfgegege._._._._.mfghh.iefgh.ieefghh.Uf.Hh.GZbedffgf.Df.Ph.Bhgh.Bhgjghgjdeff.Xh.Dh.Dhgi.Biefff.dh.Dhgi.Bi"
    "WZYbacadefefffefffeffff.Hf.Bfff.Bf.Dfgf.Bf.Lf.Ah.Vh.Ahi.Ehifgfhfgfgf.Ah.Jh.Bh.Fh.Nh.Fh.Ahh.Ehi.Vh.Aii.Bh.Aij"
    ".Ahh.Ehh.Bh.Ehh.Ahh.Ehi.Mhh.Ehh.Aii.Ahh.Aij.Mhh.Ehh.Akk.Ahh.AkkWZadefZcbeefcfcfgiffff.Afgf.Ghffff.Afgf.Ch.Ah"
    "heeffffeffffgfhfghi.Ohh.Khh.Ahhfffffgfgf.Bhhghii.Lh.Ahh.Khh.Ahh.Cf.Fjkhhjk.Kiighij.Kjjhhjj.Ffgfgf.Bhgigigf.B"
    "fgfgf.Bhghgigi.Afe.Afe.Aff.Aff.Aff.Aff.Zf.Bf.Ff.Bf._.vhh.Fh.Aff.Aff.Aff.Bf._._.ihh.Ehh._._.yhh.Ehh.Bh.hh.Ehh"
    ".hh.Ahh.Ehh.khh.Ehh.ghh.Ajj.Ahh.Ajj.Ffgfgffefeff.pfgfgfffffff._.jii.Ahh.Khh.Ahh.Kii.AiinJk5l0mCcclMk3lBlfe4l"
    "Hk2k4lVe_lCk1k5lVfMk4k0k1lAgIlDk2k4lAgqlNk4k9lDg2ltk6lBlIhYmtlDmamPc1k7k0k7lOfOk5kzk5lGgEk_k0k5lOgQlGk2k6lHg"
    "-lOk3k-lOhNlRk4lGlQhLl9k-lXldh2k5k0k2k2ftk0kzk0k0f4k-k1k1k2fllJk3k2k3fXlFk2k1k3fZk9k1k0k4fgkzkyk0k2gJlBk3k2k"
    "5.AlFk3k5k8gblCk1k6k8gTlAk1k7k4fhk2k0k1k2fxlQk4k6k7fQlck8k5k9fDlLk4k2k6fPk7k0k1k6fkk2kzk0k4gRlHk4k2k6gklIk3k"
    "5k8gdlEk2k8k9gV.ffgfgf._._._._.mf.lf.lff.radfe.jhgh.Bhefff.lh.Bhff.nh.BhXaacceceeffgfffffffgf.Nfgf.eh.Yhh.Eh"
    "hf.Df.2h.Fh.Yhh.Ehh.Yhh.Ehh.Yhh.Ehh.Vh.Aij.Ahh.AiiXbdeffcdeffgdeef.Adffffgefff.Aeff.Bfffffgfgf.Chfghhfffffgf"
    "ff.Bfgf.Bff.Cf.Vhhghhi.uhhghhi.ujjhhij.Nhghgh.Ff.Fhghgh.Aff.Aff.Aff.Bf.Bf._._.rf.Bf._._.yhh._._.6hh._.Uh.Ehh"
    ".lh.Ehh.kii.Eii.Lfgf.Nfgf.Pf._._.ahh.AhhnclMmPltdCk-k1k5lDfUk_k1k1lAfSk5k0k0k-fhk0kzk0k6gAk4k0k0k6gRk6k0k2k7"
    "gVlNk3k5lBglmZlCmIlndnk3kzk3k9fhk1kzk0k8fzk2kzk0k9gEk3k0k0k8gVk6k0k2k_gek7k0k4lAgdlLk3k_lJgvk1kzk0k0f2k0kzkz"
    "kzf7k2k0k0k0fzk7k0k0k0frk5k0kzk0fvk3k0kzk0fwkzkzkzk1gDk4k0k0k2gMk5k0k1k3gKk3k0k1k3gGk4k0k2k0fvk1kzk0k0f3k-k1"
    "k2k0fnlDk2k1k1fik7k0k0k0fqk3k0kzk1fwk1kzkzk2gIk7k1k0k3gPk6k1k1k4gLk4k0k2k3gGgh.Bf.Bh.Bf.JpW.ASfh.Ifhgff.Bf.A"
    "fghgh.Ah.Ch.NtS.AQ.Kfhgff.Gh"
)
# --- priors: end


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
