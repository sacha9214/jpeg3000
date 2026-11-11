#!/usr/bin/env python3
"""Apprend les probabilités de départ du format J3K5 (la table PRIORS) sur un dossier de photos.

Chaque photo est encodée à plusieurs qualités en partant de 50 % partout ; on note où les
probabilités ont fini, on fait la moyenne sur toutes les photos, et on écrit la table dans
jpeg3000.py et web/j3k.js (les deux décodeurs doivent avoir exactement la même).

ATTENTION : la table fait partie du format. La changer rend illisibles les fichiers J3K5 déjà
créés ; il faut alors changer aussi le numéro de version du format.

Usage :
  python tools/train_priors.py dossier_de_photos [autre_dossier ...]            (essai, n'écrit rien)
  python tools/train_priors.py dossier_de_photos --write                        (met à jour les deux fichiers)

Ne pas y mettre les photos qui servent à mesurer le codec (test-images/).
"""
import argparse
import glob
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import jpeg3000 as j3  # noqa: E402

STEPS = [2.5, 4.0, 6.0, 9.0, 14.0, 22.0, 34.0, 52.0, 80.0]
PAIRS = ("blk", "gt2", "eg", "sgn")            # [rapide, lente] entrelacées
SINGLE = ("ssa", "smb", "smc", "gsa", "gmb", "gmc")
MIXERS = ("mw", "gw")
ORDER = ("blk", "gt2", "eg", "sgn", "ssa", "smb", "smc", "gsa", "gmb", "gmc", "mw", "gw")  # ordre dans la table
EXTS = (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".heic")


def final_states(path):
    """États finaux des modèles après avoir encodé une photo, pour chaque qualité, en partant de 50 %."""
    j3.PRIORS = ""
    made = []
    start = j3._start_state

    def spy(dq):
        made.append(start(dq))
        return made[-1]
    j3._start_state = spy
    img = j3.read_image(path)
    if img.ndim == 3 and img.shape[2] in (2, 4):
        img = img[..., :-1]
    prep = j3.prepare(np.ascontiguousarray(img))
    runs = []
    for step in STEPS:
        made.clear()
        j3.encode(prep, step)
        luma, colors, dirs = made[0]
        run = [{"dc": list(m.dc), "bands": [{f: list(getattr(b, f)) for f in PAIRS + SINGLE + MIXERS} for b in m.bands]}
               for m in (luma, colors)]
        runs.append((run[0], run[1] if prep["coeffs"][1:] else None, list(dirs)))
    j3._start_state = start
    return runs


def mean_pairs(rows):
    """Moyenne de la probabilité lente, sur les encodages où le contexte a servi (sinon 50 %)."""
    a = np.array(rows, dtype=np.float64)
    fast, slow = a[:, 0::2], a[:, 1::2]
    used = (fast != 32768) | (slow != 32768)
    return (np.where(used, slow, 0).sum(0) + 32768) / (used.sum(0) + 1)


def mean_single(rows):
    a = np.array(rows, dtype=np.float64)
    used = a != 32768
    return (np.where(used, a, 0).sum(0) + 32768) / (used.sum(0) + 1)


def symbols(runs):
    """Toutes les valeurs de la table, dans l'ordre où _priors() les relit (0..63 chacune)."""
    def prob(values):
        out = []
        for p in values:
            d = j3.STRETCH[min(max(int(round(p)) >> 4, 1), 4095)]
            out.append(min(max(32 + (d + 32) // 64, 0), 63))
        return out

    def weights(values):
        out = []
        for v in values:
            v = min(max(int(round(v / 64.0)) + 2048, 0), 4095)
            out += [v >> 6, v & 63]
        return out
    out = []
    for kind in (0, 1):
        rk = [r[kind] for r in runs if r[kind] is not None]
        out += prob(mean_pairs([r["dc"] for r in rk]))
        for bi in range(8):
            for f in ORDER:
                rows = [r["bands"][bi][f] for r in rk]
                if f in PAIRS:
                    out += prob(mean_pairs(rows))
                elif f in SINGLE:
                    out += prob(mean_single(rows))
                else:
                    out += weights(np.mean(np.array(rows, dtype=np.float64), axis=0))
    out += prob(mean_pairs([r[2] for r in runs]))
    return out


def pack(sym):
    """Un caractère par valeur ; une série de valeurs neutres (32) devient « . » + sa longueur."""
    out, i = [], 0
    while i < len(sym):
        n = 1
        while sym[i] == 32 and i + n < len(sym) and sym[i + n] == 32 and n < 65:
            n += 1
        if sym[i] == 32 and n >= 2:
            out.append("." + j3._B64[n - 2])
            i += n
        else:
            out.append(j3._B64[sym[i]])
            i += 1
    return "".join(out)


def write(table, width=108):
    lines = [table[i:i + width] for i in range(0, len(table), width)]
    py = "PRIORS = (\n" + "".join('    "{}"\n'.format(l) for l in lines) + ")\n"
    js = "  const PRIORS =\n" + " +\n".join('    "{}"'.format(l) for l in lines) + ";\n"
    for path, begin, block in ((os.path.join(ROOT, "jpeg3000.py"), "# --- priors: begin", py),
                               (os.path.join(ROOT, "web", "j3k.js"), "// --- priors: begin", js)):
        src = open(path, encoding="utf-8").read()
        end = begin.replace("begin", "end")
        pattern = re.compile(r"(%s[^\n]*\n).*?([ ]*%s)" % (re.escape(begin), re.escape(end)), re.S)
        assert len(pattern.findall(src)) == 1, path
        with open(path, "w", encoding="utf-8") as f:
            f.write(pattern.sub(lambda m: m.group(1) + block + m.group(2), src))
        print("table écrite dans", os.path.relpath(path, ROOT))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("folders", nargs="+", help="dossiers (ou fichiers) de photos d'entraînement")
    ap.add_argument("--write", action="store_true", help="écrire la table dans jpeg3000.py et web/j3k.js")
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    args = ap.parse_args()
    paths = []
    for f in args.folders:
        paths += sorted(p for p in glob.glob(os.path.join(f, "*")) if p.lower().endswith(EXTS)) if os.path.isdir(f) else [f]
    if not paths:
        sys.exit("aucune photo trouvée")
    print("{} photos, {} qualités chacune...".format(len(paths), len(STEPS)))
    with ProcessPoolExecutor(args.jobs) as ex:
        runs = [r for rs in ex.map(final_states, paths) for r in rs]
    table = pack(symbols(runs))
    print("table : {} caractères".format(len(table)))
    if args.write:
        write(table)
    else:
        print("(essai : relancer avec --write pour mettre à jour jpeg3000.py et web/j3k.js)")


if __name__ == "__main__":
    main()
