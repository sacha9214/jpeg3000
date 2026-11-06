#!/usr/bin/env python3
"""Banc d'essai : JPEG 3000 contre JPEG, JPEG 2000 et WebP, à qualité d'image égale.

Pour chaque photo et chaque réglage JPEG, on cherche pour chaque format le plus petit
fichier qui atteint au moins la même qualité que le JPEG, puis on compare les tailles.
La qualité est mesurée de deux façons : PSNR (fidélité pixel par pixel) et SSIM
(ressemblance perçue). Chaque fichier JPEG 3000 est réellement décodé pour la mesure.

Écrit resultats.md et comparaison.png (zoom sur une photo très compressée,
à taille de fichier égale).

Usage : python compare.py
"""
import math
import os
import time

os.environ.setdefault("OPENCV_IO_ENABLE_JASPER", "true")  # active JPEG 2000 dans OpenCV

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

import jpeg3000 as j3  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
JPEG_QUALITIES = [10, 25, 50, 75, 90]
WALLPAPERS = [
    ("Plage", r"C:\Windows\Web\Wallpaper\Theme1\img1.jpg"),
    ("Rocher", r"C:\Windows\Web\Wallpaper\Theme1\img3.jpg"),
    ("Tulipes", r"C:\Windows\Web\Wallpaper\Theme2\img10.jpg"),
]


def load_photos():
    """Toutes ces photos ont été des JPEG : on les réduit pour effacer la grille 8x8 d'origine,
    sur laquelle un nouveau JPEG retomberait pile (avantage injuste : +7 dB mesurés)."""
    photos = {}
    try:
        from scipy.misc import face
        photos["Raton laveur"] = cv2.resize(face(), (512, 384), interpolation=cv2.INTER_AREA)
    except ImportError:
        pass
    for name, path in WALLPAPERS:
        if os.path.exists(path):
            bgr = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
            bgr = cv2.resize(bgr, None, fx=1 / 3, fy=1 / 3, interpolation=cv2.INTER_AREA)
            photos[name] = np.ascontiguousarray(bgr[..., ::-1])
    return photos


# --- Mesures de qualité -----------------------------------------------------


def psnr(a, b):
    """Fidélité pixel par pixel, en dB (plus c'est haut, mieux c'est)."""
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 10 * np.log10(255.0 ** 2 / mse) if mse else 99.0


def ssim(a, b):
    """Ressemblance perçue sur la luminosité (1 = identique)."""
    x = a.astype(np.float64) @ [0.299, 0.587, 0.114]
    y = b.astype(np.float64) @ [0.299, 0.587, 0.114]

    def g(z):
        return ndimage.gaussian_filter(z, 1.5)

    mx, my = g(x), g(y)
    sxx, syy, sxy = g(x * x) - mx * mx, g(y * y) - my * my, g(x * y) - mx * my
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    m = ((2 * mx * my + c1) * (2 * sxy + c2)) / ((mx * mx + my * my + c1) * (sxx + syy + c2))
    return float(m.mean())


METRICS = [("PSNR", psnr), ("SSIM", ssim)]


# --- Encodeurs ----------------------------------------------------------------


def _opencv(img, ext, params):
    _, buf = cv2.imencode(ext, img[..., ::-1], params)
    return len(buf), cv2.imdecode(buf, cv2.IMREAD_COLOR)[..., ::-1]


def jpeg(img, quality):
    return _opencv(img, ".jpg", [cv2.IMWRITE_JPEG_QUALITY, quality, cv2.IMWRITE_JPEG_OPTIMIZE, 1])


def webp(img, quality):
    return _opencv(img, ".webp", [cv2.IMWRITE_WEBP_QUALITY, quality])


def jpeg2000(img, rate):
    """rate = taux de compression x 1000 (1 à 1000)."""
    return _opencv(img, ".jp2", [cv2.IMWRITE_JPEG2000_COMPRESSION_X1000, rate])


def size_for(codec, img, metric, target, lo, hi):
    """Taille du plus petit fichier qui atteint la qualité visée, pour un codec à réglage entier.

    Le réglage est entier, donc on interpole entre les deux réglages voisins
    (sinon le format serait pénalisé par le pas du réglage). None si inatteignable.
    """
    first, best = lo, None
    while lo <= hi:
        mid = (lo + hi) // 2
        size, out = codec(img, mid)
        m = metric(img, out)
        if m >= target:
            best, hi = (mid, size, m), mid - 1
        else:
            lo = mid + 1
    if best is None:
        return None
    setting, s_hi, m_hi = best
    if setting == first:
        return s_hi
    s_lo, out = codec(img, setting - 1)
    m_lo = metric(img, out)
    if m_hi <= m_lo:
        return s_hi
    t = (target - m_lo) / (m_hi - m_lo)
    return math.exp(math.log(s_lo) + t * (math.log(s_hi) - math.log(s_lo)))


def j3k_for(prep, img, metric, target):
    """Taille du plus petit fichier JPEG 3000 qui atteint la qualité visée (réellement décodé)."""
    lo, hi = -2.0, 11.0  # log2 du pas
    for _ in range(20):
        mid = (lo + hi) / 2
        if metric(img, j3.simulate(prep, 2 ** mid)) >= target:
            lo = mid
        else:
            hi = mid
    data = j3.encode(prep, 2 ** lo)
    if metric(img, j3.decode(data)) < target - 1e-9:
        raise AssertionError("le fichier décodé n'atteint pas la qualité prévue")
    return len(data)


# --- Programme principal --------------------------------------------------------

NAMES = ["JPEG 2000", "WebP", "JPEG 3000"]


def ko(n):
    return "{:.1f} Ko".format(n / 1000).replace(".", ",")


def pct(x):
    return "{:+.0f} %".format(100 * x).replace("-", "−")


def rel(size, ref):
    return "—" if size is None else pct(size / ref - 1)


def benchmark(photos):
    gains = {(n, m): [] for n in NAMES for m, _ in METRICS}
    vs_jp2 = {m: [] for m, _ in METRICS}
    lines = []
    for name, img in photos.items():
        h, w = img.shape[:2]
        prep = j3.prepare(img)
        lines += [
            "## {} ({}x{})".format(name, w, h),
            "",
            "Taille par rapport au JPEG : à PSNR égal / à SSIM égal.",
            "",
            "| Qualité JPEG | JPEG | JPEG 2000 | WebP | **JPEG 3000** |",
            "|---:|---:|---:|---:|---:|",
        ]
        for q in JPEG_QUALITIES:
            t0 = time.time()
            sj, dj = jpeg(img, q)
            sizes = {}
            for m, f in METRICS:
                target = f(img, dj)
                sizes["JPEG 2000", m] = size_for(jpeg2000, img, f, target, 1, 1000)
                sizes["WebP", m] = size_for(webp, img, f, target, 1, 100)
                sizes["JPEG 3000", m] = j3k_for(prep, img, f, target)
                if sizes["JPEG 2000", m]:
                    vs_jp2[m].append(sizes["JPEG 3000", m] / sizes["JPEG 2000", m] - 1)
            for key, s in sizes.items():
                if s is not None:
                    gains[key].append(s / sj - 1)
            cells = ["{} / {}".format(rel(sizes[n, "PSNR"], sj), rel(sizes[n, "SSIM"], sj)) for n in NAMES]
            lines.append("| {} | {} | {} | {} | **{}** |".format(q, ko(sj), *cells))
            print("{:13s} JPEG q={:2d} {:>9} | JPEG 2000 {:>15} | WebP {:>15} | JPEG 3000 {:>15}  [{:.0f} s]".format(
                name, q, ko(sj), *cells, time.time() - t0), flush=True)
        lines.append("")

    def avg(values):
        return pct(np.mean(values)) if values else "—"

    summary = [
        "# JPEG 3000 contre JPEG, JPEG 2000 et WebP : résultats",
        "",
        "Pour chaque mesure, tous les fichiers ont au moins la même qualité d'image que le JPEG ; "
        "on compare leur taille (−50 % = deux fois plus petit que le JPEG).",
        "",
        "- **PSNR** : fidélité pixel par pixel.",
        "- **SSIM** : ressemblance perçue par l'œil (formes, textures), plus proche de ce qu'on voit.",
        "",
        "**Taille moyenne par rapport au JPEG, à qualité égale :**",
        "",
        "| Format | À PSNR égal | À SSIM égal |",
        "|---|---:|---:|",
        "| JPEG (1992) | référence | référence |",
    ]
    labels = {"JPEG 2000": "JPEG 2000 (2000)", "WebP": "WebP (2010)", "JPEG 3000": "**JPEG 3000 (ce prototype)**"}
    for n in NAMES:
        summary.append("| {} | {} | {} |".format(labels[n], avg(gains[n, "PSNR"]), avg(gains[n, "SSIM"])))
    summary += [
        "",
        "**JPEG 3000 contre son prédécesseur JPEG 2000 : {} à PSNR égal, {} à SSIM égal.**".format(
            avg(vs_jp2["PSNR"]), avg(vs_jp2["SSIM"])),
        "",
        "JPEG 3000 est réglé pour le PSNR : son avance est plus faible quand on mesure ce que l'œil perçoit (SSIM).",
        "",
        "Encodeurs utilisés : JPEG = libjpeg (optimisation Huffman activée), JPEG 2000 = Jasper "
        "(intégré à OpenCV, ondelette 5/3, une seule couche), WebP = libwebp. Un encodeur JPEG 2000 "
        "réglé en ondelette 9/7 (OpenJPEG, Kakadu) ferait mieux que Jasper. Pour JPEG 2000 et WebP, "
        "dont le réglage est un nombre entier, la taille est interpolée entre les deux réglages voisins. "
        "« — » : qualité inatteignable pour ce format ; ces cas sont exclus des moyennes.",
        "",
        "Les photos de test étaient déjà des JPEG : elles sont réduites (x2 ou x3) pour effacer leur "
        "grille 8x8 d'origine. Sans cela, un nouveau JPEG retombe pile sur cette grille et gagne "
        "injustement (mesuré : 43,9 dB au lieu de 36,8 dB en qualité 75 sur le raton laveur).",
        "",
    ]
    with open(os.path.join(HERE, "resultats.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(summary + lines))
    print("\n".join(summary[9:18]))


def label(tile, text):
    tile = tile.copy()
    cv2.rectangle(tile, (0, 0), (tile.shape[1], 44), (0, 0, 0), -1)
    cv2.putText(tile, text, (12, 31), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2, cv2.LINE_AA)
    return tile


def visual(img, quality=8, box=(85, 235, 150), zoom=4):
    """Original / JPEG très compressé / JPEG 2000 et JPEG 3000 de même taille, agrandis."""
    sj, dj = jpeg(img, quality)
    data, _ = j3.encode_to_size(img, sj)
    d3k = j3.decode(data)
    lo, hi = 1, 1000  # plus fort réglage JPEG 2000 qui ne dépasse pas la taille du JPEG
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if jpeg2000(img, mid)[0] <= sj:
            lo = mid
        else:
            hi = mid - 1
    s2k, d2k = jpeg2000(img, lo)
    y, x, s = box
    tiles = []
    for im, text in [(img, "Original"),
                     (dj, "JPEG : {}".format(ko(sj))),
                     (d2k, "JPEG 2000 : {}".format(ko(s2k))),
                     (d3k, "JPEG 3000 : {}".format(ko(len(data))))]:
        crop = cv2.resize(im[y:y + s, x:x + s], None, fx=zoom, fy=zoom, interpolation=cv2.INTER_NEAREST)
        tiles.append(label(crop[..., ::-1], text))
        tiles.append(np.full((zoom * s, 8, 3), 255, np.uint8))
    out = os.path.join(HERE, "comparaison.png")
    cv2.imencode(".png", np.hstack(tiles[:-1]))[1].tofile(out)
    print("comparaison.png : JPEG {} ({:.1f} dB), JPEG 2000 {} ({:.1f} dB), JPEG 3000 {} ({:.1f} dB)".format(
        ko(sj), psnr(img, dj), ko(s2k), psnr(img, d2k), ko(len(data)), psnr(img, d3k)))


if __name__ == "__main__":
    photos = load_photos()
    benchmark(photos)
    visual(photos.get("Raton laveur", next(iter(photos.values()))))
