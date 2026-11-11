#!/usr/bin/env python3
"""Tests : l'image décodée depuis le fichier doit être exactement celle prévue par l'encodeur.

Usage : python test_jpeg3000.py
"""
import io
import os

import numpy as np

import jpeg3000 as j3

HERE = os.path.dirname(os.path.abspath(__file__))


def photo():
    """Une vraie photo si une est disponible (test-images/), sinon None."""
    path = os.path.join(HERE, "test-images", "astronaut.png")
    return j3.read_image(path)[100:300, 150:450].copy() if os.path.exists(path) else None


def test_wavelet_is_invertible():
    x = np.random.RandomState(0).rand(67, 95) * 255
    for levels in (1, 3, 5):
        assert np.abs(j3.idwt2(j3.dwt2(x, levels), levels) - x).max() < 1e-9
    for n in (1, 2, 3, 4, 5, 7, 8, 13):  # toutes les longueurs, paires et impaires
        v = np.random.RandomState(n).rand(3, n)
        assert np.abs(j3._inv(*j3._fwd(v)) - v).max() < 1e-9, n
    # Ondelette directionnelle : directions choisies par l'encodeur, puis directions au hasard.
    for shape in ((67, 95), (1, 9), (40, 1), (33, 64)):
        x = np.random.RandomState(1).rand(*shape) * 255
        a, dirs = j3.dwt2_dir(x, 3)
        assert np.abs(j3.idwt2_dir(a, 3, dirs) - x).max() < 1e-9, shape
        rnd = [np.random.RandomState(i).randint(0, len(j3.OFFS), g.shape) for i, g in enumerate(dirs)]
        a, _ = j3.dwt2_dir(x, 3, rnd)
        assert np.abs(j3.idwt2_dir(a, 3, rnd) - x).max() < 1e-9, shape


def test_round_trip():
    rng = np.random.RandomState(0)
    alpha = np.where(rng.rand(45, 31) < 0.5, 0, 255).astype(np.uint8)
    alpha[10:20, 5:25] = np.arange(20, dtype=np.uint8) * 13
    cases = {
        "bruit 37x53": (rng.rand(37, 53, 3) * 255).astype(np.uint8),
        "gris 61x40": (rng.rand(61, 40) * 255).astype(np.uint8),
        "1x1": np.array([[[10, 200, 30]]], dtype=np.uint8),
        "2x5 gris": (rng.rand(2, 5) * 255).astype(np.uint8),
        "1x40 gris": (rng.rand(1, 40) * 255).astype(np.uint8),
        "33x1": (rng.rand(33, 1, 3) * 255).astype(np.uint8),
        "uni 33x33": np.full((33, 33, 3), 77, np.uint8),
        "dégradé 100x150": np.dstack(list(np.meshgrid(np.arange(150), np.arange(100))) + [np.full((100, 150), 90)])
        .astype(np.uint8),
        "RGBA 45x31": np.dstack([(rng.rand(45, 31, 3) * 255).astype(np.uint8), alpha]),
        "gris+alpha 45x31": np.dstack([(rng.rand(45, 31) * 255).astype(np.uint8), alpha]),
    }
    if photo() is not None:
        cases["photo 200x300"] = photo()
    for name, img in cases.items():
        for step in (0.3, 4, 30, 400):
            out = j3.decode(j3.encode(img, step))
            assert out.shape == img.shape, (name, out.shape)
            assert np.array_equal(out, j3.simulate(img, step)), (name, step)
            if img.ndim == 3 and img.shape[2] in (2, 4):
                assert np.array_equal(out[..., -1], img[..., -1]), name  # transparence sans perte


OLD_SAMPLES = (("v3-sample", b"J3K3"), ("v4-sample", b"J3K4"))


def test_old_versions_still_decode():
    """Un fichier J3K3 ou J3K4 doit donner exactement ce que donnait le décodeur de sa version."""
    for name, magic in OLD_SAMPLES:
        with open(os.path.join(HERE, "test-images", name + ".j3k"), "rb") as f:
            data = f.read()
        assert data[:4] == magic, name
        assert np.array_equal(j3.decode(data), j3.read_image(os.path.join(HERE, "test-images", name + ".png"))), name


def test_starting_probabilities_are_used():
    """La table PRIORS (probabilités de départ apprises) doit se relire en entier et servir."""
    assert j3._priors() is not None
    img = photo() if photo() is not None else (np.random.RandomState(3).rand(64, 64, 3) * 255).astype(np.uint8)
    learned = len(j3.encode(img, 30))
    table, j3.PRIORS = j3.PRIORS, ""  # sans la table : tout repart de 50 %
    try:
        flat = len(j3.encode(img, 30))
    finally:
        j3.PRIORS = table
    if photo() is not None:
        assert learned < flat, (learned, flat)  # la table doit faire gagner des octets


def test_opaque_alpha_is_dropped():
    img = np.dstack([np.full((20, 20, 3), 100, np.uint8), np.full((20, 20), 255, np.uint8)])
    assert j3.decode(j3.encode(img, 4)).shape == (20, 20, 3)


def test_bad_files_are_rejected():
    good = j3.encode(np.zeros((8, 8), np.uint8), 4)
    for data, words in [(b"", "pas un fichier"), (b"PNG...." * 4, "pas un fichier"),
                        (b"J3K2" + good[4:], "autre version"), (b"J3K9" + good[4:], "autre version"),
                        (good[:4] + b"\x00" * 8 + good[12:], "abîmé")]:
        try:
            j3.decode(data)
        except ValueError as e:
            assert words in str(e), (words, str(e))
        else:
            raise AssertionError("fichier invalide accepté : " + words)


NODE_DECODE = """
const fs = require("fs"), path = require("path");
const J3K = require(process.argv[1]);
for (const f of fs.readdirSync(process.argv[2]).filter((f) => f.endsWith(".j3k"))) {
  const im = J3K.decode(fs.readFileSync(path.join(process.argv[2], f)));
  fs.writeFileSync(path.join(process.argv[2], f + ".raw"), im.data);
}
"""


def test_javascript_decoder_matches():
    """Le décodeur du navigateur (web/j3k.js) doit donner exactement les mêmes pixels."""
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    if not node:
        print("(node absent : test du décodeur JavaScript sauté)")
        return
    rng = np.random.RandomState(2)
    cases = {
        "rgb": (rng.rand(37, 53, 3) * 255).astype(np.uint8),
        "gris": (rng.rand(61, 40) * 255).astype(np.uint8),
        "1x1": np.array([[[10, 200, 30]]], dtype=np.uint8),
        "1x40": (rng.rand(1, 40) * 255).astype(np.uint8),
        "rgba": np.dstack([(rng.rand(45, 31, 3) * 255).astype(np.uint8), (rng.rand(45, 31) * 255).astype(np.uint8)]),
        "gris+alpha": np.dstack([(rng.rand(20, 9) * 255).astype(np.uint8), np.full((20, 9), 9, np.uint8)]),
    }
    if photo() is not None:
        cases["photo"] = photo()
    expected = {}
    with tempfile.TemporaryDirectory() as tmp:
        for old, _ in OLD_SAMPLES:
            shutil.copy(os.path.join(HERE, "test-images", old + ".j3k"), tmp)
            expected[old + ".j3k"] = j3.read_image(os.path.join(HERE, "test-images", old + ".png"))
        for name, img in cases.items():
            for step in (0.3, 4, 30, 400):
                data = j3.encode(img, step)
                fname = "{}-{}.j3k".format(name, step)
                with open(os.path.join(tmp, fname), "wb") as f:
                    f.write(data)
                expected[fname] = j3.decode(data)
        subprocess.run([node, "-e", NODE_DECODE, os.path.join(HERE, "web", "j3k.js"), tmp], check=True)
        for fname, img in expected.items():
            with open(os.path.join(tmp, fname + ".raw"), "rb") as f:
                got = np.frombuffer(f.read(), np.uint8)
            assert np.array_equal(got, img.reshape(-1)), fname


def test_pillow_opens_j3k():
    try:
        from PIL import Image
    except ImportError:
        return
    img = np.dstack([(np.random.RandomState(1).rand(30, 41, 3) * 255).astype(np.uint8),
                     np.full((30, 41), 128, np.uint8)])
    data = j3.encode(img, 8)
    with Image.open(io.BytesIO(data)) as im:
        assert im.format == "J3K" and im.mode == "RGBA" and im.size == (41, 30)
        assert np.array_equal(np.asarray(im), j3.decode(data))


if __name__ == "__main__":
    test_wavelet_is_invertible()
    test_round_trip()
    test_old_versions_still_decode()
    test_starting_probabilities_are_used()
    test_opaque_alpha_is_dropped()
    test_bad_files_are_rejected()
    test_pillow_opens_j3k()
    test_javascript_decoder_matches()
    print("Tous les tests passent.")
