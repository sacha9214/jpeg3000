#!/usr/bin/env python3
"""Banc d'essai : JPEG 3000 contre JPEG, JPEG 2000, WebP et AVIF, à qualité d'image égale.

Pour chaque photo et chaque réglage JPEG, on cherche pour chaque format le plus petit
fichier qui atteint au moins la même qualité que le JPEG, puis on compare les tailles.
La qualité est mesurée de deux façons : PSNR (fidélité pixel par pixel) et SSIM
(ressemblance perçue). Chaque fichier JPEG 3000 est réellement décodé pour la mesure.

Écrit results.md et comparaison.png (zoom sur une photo très compressée, à taille de
fichier égale). Il faut opencv-python et scipy ; un format absent de votre OpenCV est sauté.

Usage : python compare.py
"""
import io
import math
import os
import time

import cv2
import numpy as np
from scipy import ndimage

import jpeg3000 as j3

HERE = os.path.dirname(os.path.abspath(__file__))
JPEG_QUALITIES = [10, 25, 50, 75, 90]
PHOTOS = [("Astronaut", "astronaut.png"), ("Cat", "chelsea.png"), ("Coffee", "coffee.png"),
          ("Camera (grayscale)", "camera.png")]


def load_photos():
    """Photos libres de droits (voir test-images/README.md), jamais compressées en JPEG :
    aucun format n'est avantagé par une ancienne grille 8x8."""
    return {name: j3.read_image(os.path.join(HERE, "test-images", f)) for name, f in PHOTOS}


# --- Mesures de qualité -----------------------------------------------------


def psnr(a, b):
    """Fidélité pixel par pixel, en dB (plus c'est haut, mieux c'est)."""
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 10 * np.log10(255.0 ** 2 / mse) if mse else 99.0


def ssim(a, b):
    """Ressemblance perçue sur la luminosité (1 = identique)."""
    if a.ndim == 3:
        x = a.astype(np.float64) @ [0.299, 0.587, 0.114]
        y = b.astype(np.float64) @ [0.299, 0.587, 0.114]
    else:
        x, y = a.astype(np.float64), b.astype(np.float64)

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
    bgr = img if img.ndim == 2 else img[..., ::-1]
    ok, buf = cv2.imencode(ext, bgr, params)
    if not ok:
        raise RuntimeError("OpenCV ne sait pas écrire " + ext)
    out = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE if img.ndim == 2 else cv2.IMREAD_COLOR)
    return len(buf), out if img.ndim == 2 else out[..., ::-1]


def jpeg(img, quality):
    return _opencv(img, ".jpg", [cv2.IMWRITE_JPEG_QUALITY, quality, cv2.IMWRITE_JPEG_OPTIMIZE, 1])


def jpeg2000(img, rate):
    """OpenJPEG via Pillow, ondelette 9/7 (irréversible) ; rate = 1000 / taux de compression (1 à 1000).
    (Le JPEG 2000 d'OpenCV est mal réglé : à taille égale il perd 3 dB, pire que le JPEG.)"""
    from PIL import Image
    b = io.BytesIO()
    Image.fromarray(img).save(b, "JPEG2000", quality_mode="rates", quality_layers=[1000.0 / rate],
                              irreversible=True, mct=1 if img.ndim == 3 else 0)
    out = np.asarray(Image.open(io.BytesIO(b.getvalue())).convert("RGB" if img.ndim == 3 else "L"))
    return b.tell(), out


def webp(img, quality):
    return _opencv(img, ".webp", [cv2.IMWRITE_WEBP_QUALITY, quality])


def avif(img, quality):
    """libavif (AV1), vitesse 6 = réglage par défaut de libavif."""
    return _opencv(img, ".avif", [cv2.IMWRITE_AVIF_QUALITY, quality, cv2.IMWRITE_AVIF_SPEED, 6])


def available(codec):
    try:
        codec(np.zeros((128, 128, 3), np.uint8), 50)
        return True
    except (cv2.error, RuntimeError, AttributeError, OSError, ImportError):
        return False


CODECS = [("JPEG 2000", jpeg2000, 1, 1000), ("WebP", webp, 1, 100), ("AVIF", avif, 1, 100)]


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
    if m_hi <= m_lo or s_lo >= s_hi:
        return s_hi
    t = (target - m_lo) / (m_hi - m_lo)
    return math.exp(math.log(s_lo) + t * (math.log(s_hi) - math.log(s_lo)))


def j3k_for(prep, img, metric, target):
    """Taille du plus petit fichier JPEG 3000 qui atteint la qualité visée (réellement décodé)."""
    lo, hi = -2.0, 11.0  # log2 du pas
    for _ in range(15):
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


def kb(n):
    return "{:.1f} KB".format(n / 1000)


def pct(x):
    return "{:+.0f} %".format(100 * x).replace("-", "−")


def rel(size, ref):
    return "—" if size is None else pct(size / ref - 1)


def benchmark(photos, codecs):
    names = [n for n, *_ in codecs] + ["JPEG 3000"]
    gains = {(n, m): [] for n in names for m, _ in METRICS}
    lines = []
    for name, img in photos.items():
        h, w = img.shape[:2]
        prep = j3.prepare(img)
        lines += [
            "## {} ({}x{})".format(name, w, h),
            "",
            "File size relative to JPEG: at equal PSNR / at equal SSIM.",
            "",
            "| JPEG quality | JPEG | " + " | ".join(names[:-1]) + " | **JPEG 3000** |",
            "|---:|---:|" + "---:|" * len(names),
        ]
        for q in JPEG_QUALITIES:
            t0 = time.time()
            sj, dj = jpeg(img, q)
            sizes = {}
            for m, f in METRICS:
                target = f(img, dj)
                for cname, codec, lo, hi in codecs:
                    sizes[cname, m] = size_for(codec, img, f, target, lo, hi)
                sizes["JPEG 3000", m] = j3k_for(prep, img, f, target)
            for key, s in sizes.items():
                if s is not None:
                    gains[key].append(s / sj - 1)
            cells = ["{} / {}".format(rel(sizes[n, "PSNR"], sj), rel(sizes[n, "SSIM"], sj)) for n in names]
            lines.append("| {} | {} | {} | **{}** |".format(q, kb(sj), " | ".join(cells[:-1]), cells[-1]))
            print("{:18s} JPEG q={:2d} {:>9} | {}  [{:.0f} s]".format(
                name, q, kb(sj), " | ".join("{} {}".format(n, c) for n, c in zip(names, cells)), time.time() - t0),
                flush=True)
        lines.append("")

    def avg(values):
        return pct(np.mean(values)) if values else "—"

    labels = {"JPEG 2000": "JPEG 2000 (2000)", "WebP": "WebP (2010)", "AVIF": "AVIF (2019)",
              "JPEG 3000": "**JPEG 3000 v4 (this prototype)**"}
    summary = [
        "# JPEG 3000 vs JPEG, JPEG 2000, WebP and AVIF: results",
        "",
        "For each measurement, every file has at least the image quality of the JPEG file; "
        "we compare their sizes (−50 % = half the size of the JPEG).",
        "",
        "- **PSNR**: pixel-by-pixel fidelity.",
        "- **SSIM**: perceived similarity (shapes, textures), closer to what the eye sees.",
        "",
        "**Average file size relative to JPEG, at equal quality:**",
        "",
        "| Format | At equal PSNR | At equal SSIM |",
        "|---|---:|---:|",
        "| JPEG (1992) | reference | reference |",
    ]
    for n in names:
        summary.append("| {} | {} | {} |".format(labels[n], avg(gains[n, "PSNR"]), avg(gains[n, "SSIM"])))
    summary += [
        "",
        "Encoders: JPEG = libjpeg-turbo (optimized Huffman tables), JPEG 2000 = OpenJPEG through Pillow (irreversible 9/7 wavelet; the OpenJPEG writer built into OpenCV is misconfigured: about 3 dB worse at equal size in our test), "
        "WebP = libwebp, AVIF = libavif with the AV1 encoder bundled in OpenCV {} (speed 6, its default). "
        "For the other formats, whose setting is an integer, the size is interpolated between the two "
        "neighbouring settings. \"—\": quality out of reach for that format; excluded from the averages.".format(
            cv2.__version__),
        "",
        "Test photos: public domain / CC0 images shipped with scikit-image (see test-images/README.md), "
        "never JPEG-compressed, so no format benefits from an old 8x8 grid. JPEG XL could not be tested here.",
        "",
    ]
    with open(os.path.join(HERE, "results.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(summary + lines))
    print("\n".join(summary[9:10 + len(names) + 2]))


def label(tile, text):
    tile = tile.copy()
    cv2.rectangle(tile, (0, 0), (tile.shape[1], 40), (0, 0, 0), -1)
    cv2.putText(tile, text, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (255, 255, 255), 2, cv2.LINE_AA)
    return tile


def same_size(codec, img, size, lo, hi):
    """Meilleur réglage d'un codec dont le fichier ne dépasse pas size."""
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if codec(img, mid)[0] <= size:
            lo = mid
        else:
            hi = mid - 1
    return codec(img, lo)


def visual(img, codecs, quality=8, box=(40, 160, 180), zoom=3):
    """Original / JPEG très compressé / les autres formats à la même taille, agrandis."""
    sj, dj = jpeg(img, quality)
    data, _ = j3.encode_to_size(img, sj)
    tiles = [(img, "Original"), (dj, "JPEG {}".format(kb(sj)))]
    for cname, codec, lo, hi in codecs:
        s, d = same_size(codec, img, sj, lo, hi)
        tiles.append((d, "{} {}".format(cname, kb(s))))
    tiles.append((j3.decode(data), "JPEG 3000 {}".format(kb(len(data)))))
    y, x, s = box
    row = []
    for im, text in tiles:
        crop = cv2.resize(im[y:y + s, x:x + s], None, fx=zoom, fy=zoom, interpolation=cv2.INTER_NEAREST)
        row.append(label(crop[..., ::-1], text))
        row.append(np.full((zoom * s, 8, 3), 255, np.uint8))
    out = os.path.join(HERE, "comparaison.png")
    cv2.imencode(".png", np.hstack(row[:-1]))[1].tofile(out)
    print("comparaison.png : " + ", ".join("{} ({:.1f} dB)".format(t, psnr(img, im)) for im, t in tiles[1:]))


if __name__ == "__main__":
    codecs = [c for c in CODECS if available(c[1])]
    missing = [c[0] for c in CODECS if c not in codecs]
    if missing:
        print("Formats indisponibles dans cet OpenCV (sautés) : " + ", ".join(missing))
    photos = load_photos()
    benchmark(photos, codecs)
    visual(photos["Astronaut"], codecs)
