# JPEG 3000 (prototype)

An experimental image format (`.j3k`) that makes files much smaller than JPEG at the same
image quality, without JPEG's blocky artifacts at high compression. Version 3 adds an
in-browser decoder, transparency and support for images of any size.

![Same file size, zoomed in: original, JPEG, JPEG 2000, WebP, AVIF, JPEG 3000](comparaison.png)

**Browser viewer:** [`web/index.html`](web/index.html) opens `.j3k` files in any browser
(decoding runs on your device, nothing is uploaded). Serve the `web` folder, for example with
`python -m http.server --directory web`.

## Results

Average file size relative to JPEG, at equal image quality
(4 photos x 5 compression levels, details in [results.md](results.md)):

| Format | At equal PSNR | At equal SSIM |
|---|---:|---:|
| JPEG (1992) | reference | reference |
| JPEG 2000 (2000) | −43 % | −22 % |
| WebP (2010) | −35 % | −26 % |
| AVIF (2019) | −55 % | −46 % |
| **JPEG 3000 v3** | **−49 %** | **−34 %** |

- **PSNR**: pixel-by-pixel fidelity. **SSIM**: perceived similarity, closer to what the eye sees.
- Against its predecessor JPEG 2000 (OpenJPEG, 9/7 wavelet): files **10 % smaller** at equal PSNR
  and **15 % smaller** at equal SSIM, smaller on all 20 measurements.
- Against WebP: smaller on all 20 measurements in PSNR, on 14 out of 20 in SSIM.
- **AVIF is still better**: its files are about 11 % smaller than JPEG 3000's at equal PSNR and
  18 % smaller at equal SSIM. JPEG 3000 only beats it on the cat photo (3 measurements out of 20
  in PSNR, 1 in SSIM). AV1 has tools this prototype doesn't: directional intra prediction and
  deringing filters.

To read these numbers honestly:

- Only 4 test photos (3 colour, 1 grayscale), all small (0.14 to 0.26 megapixels).
- JPEG XL, the other modern format, could not be tested here.
- These numbers can't be compared with version 2's: version 2 was measured on Windows
  wallpapers, against a weaker JPEG 2000 encoder (Jasper, 5/3 wavelet) and without AVIF.

## What's new in version 3

Format `J3K3`, about **6 % smaller files than version 2** at equal quality (Bjøntegaard delta
rate over the 4 test photos plus scikit-image's motorcycle photo, PSNR and SSIM alike;
up to −9.5 % on some photos):

| Change | Gain |
|---|---:|
| Wavelet transform of any image size, even odd: version 2 padded images to a multiple of 32 or 64 pixels and spent bits on pixels that don't exist | −2.1 % (up to −4.5 %) |
| Signs predicted from neighbouring coefficients (and from brightness, for colours) instead of stored raw | −1.5 % |
| 32×32 "all zero" blocks instead of 4×4 | −1.3 % |
| "Is this coefficient zero?" decided by mixing 4 models with a small online-learning network (logistic mixing, as in PAQ compressors), in integer arithmetic so every decoder gets the exact same result | −1.3 % |
| Logarithmic context classes for coefficient amplitudes | −0.1 % |

Measured and rejected because they gained nothing or made files bigger: quadtree zero blocks
(16 → 8 → 4), per-band reconstruction offsets (at most +0.02 dB even with perfect knowledge),
sibling subbands or farther neighbours added to the main context (too many states to learn;
they only pay off inside the mixer), parent sign as a sign context.

Compatibility:

- **Any browser** can open `.j3k` files with [`web/j3k.js`](web/j3k.js), a dependency-free
  decoder (about 0.2 s for a 1-megapixel photo). It gives exactly the same pixels as the
  Python decoder, which the tests check. On a web page:
  ```html
  <script src="j3k.js"></script>
  <img data-j3k="photo.j3k" alt="...">
  <script>J3K.polyfill();</script>
  ```
- **Transparency**: RGBA and grayscale+alpha images; the alpha channel is stored losslessly.
- **Any size**, from 1×1 pixel up to 268 million pixels, odd sizes included.
- **More input files**: PNG, JPEG, WebP, AVIF, TIFF, BMP, GIF, 16-bit images, palette images,
  and HEIC/HEIF (iPhone) if `pillow-heif` is installed. Phone photos are turned upright
  using their EXIF orientation.
- **Pillow plugin**: after `import jpeg3000`, `PIL.Image.open("photo.j3k")` works, so any
  Python program built on Pillow can read `.j3k`.
- Clear errors for damaged files or files from another version (`J3K2` files need version 2:
  `git checkout v2`).

## Usage

Python 3 with `numpy` and `pillow` (`pip install -r requirements.txt`).

```
python jpeg3000.py encode photo.png photo.j3k --quality 50
python jpeg3000.py encode photo.png photo.j3k --size 50000
python jpeg3000.py decode photo.j3k photo.png
python jpeg3000.py info photo.j3k
python test_jpeg3000.py
python compare.py
```

- `--quality`: from 1 (tiny file) to 100 (almost lossless), 50 by default.
- `--size`: maximum file size in bytes; the best quality that fits is chosen.
- `decode` writes PNG, JPEG, WebP, TIFF or BMP depending on the extension (transparent
  images are placed on white for formats without transparency).
- `compare.py` needs `opencv-python` and `scipy`; a format missing from your OpenCV build is skipped.
- The test suite also checks the JavaScript decoder against the Python one when Node.js is installed.

The command line messages are in French.

## How it works

| | JPEG (1992) | JPEG 3000 |
|---|---|---|
| Transform | 8x8 pixel blocks | CDF 9/7 wavelets over the whole image: no blocky artifacts |
| Quantization | fixed tables | step tuned to each subband, dead zone that depends on the neighbourhood |
| Entropy coding | Huffman | adaptive binary arithmetic coding with contexts (neighbours, parent scale, sibling subbands, brightness for colours) and logistic mixing of 4 models |

## Limitations

- It's a Python prototype: about 1.8 s to encode or decode a 1-megapixel photo at quality 50 on an Apple M3
  (twice as slow as version 2, the price of model mixing). The JavaScript decoder is 9 times faster.
- Only the decoder exists in JavaScript; encoding needs Python.
- Browsers, phones and image editors don't open `.j3k` natively: you need `j3k.js`, the
  Python tool or the Pillow plugin.
- "JPEG" is a trademark of the JPEG committee: the format would need another name to be distributed.
