#!/usr/bin/env python3
"""Tests : l'image décodée depuis le fichier doit être exactement celle prévue par l'encodeur.

Usage : python test_jpeg3000.py
"""
import numpy as np

import jpeg3000 as j3


def test_wavelet_is_invertible():
    x = np.random.RandomState(0).rand(64, 96) * 255
    for levels in (1, 3, 5):
        assert np.abs(j3.idwt2(j3.dwt2(x, levels), levels) - x).max() < 1e-9


def test_round_trip():
    rng = np.random.RandomState(0)
    cases = {
        "bruit 37x53": (rng.rand(37, 53, 3) * 255).astype(np.uint8),
        "gris 61x40": (rng.rand(61, 40) * 255).astype(np.uint8),
        "1x1": np.array([[[10, 200, 30]]], dtype=np.uint8),
        "2x5 gris": (rng.rand(2, 5) * 255).astype(np.uint8),
        "uni 33x33": np.full((33, 33, 3), 77, np.uint8),
        "dégradé 100x150": np.dstack(np.meshgrid(np.arange(150), np.arange(100)) + [np.full((100, 150), 90)])
        .astype(np.uint8),
    }
    try:
        from scipy.misc import face
        cases["photo 200x300"] = face()[100:300, 300:600].copy()
    except ImportError:
        pass
    for name, img in cases.items():
        for step in (0.3, 4, 30, 400):
            out = j3.decode(j3.encode(img, step))
            assert out.shape == img.shape, name
            assert np.array_equal(out, j3.simulate(img, step)), (name, step)


if __name__ == "__main__":
    test_wavelet_is_invertible()
    test_round_trip()
    print("Tous les tests passent.")
