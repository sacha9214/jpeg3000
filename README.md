# JPEG 3000 (prototype)

An experimental image format (`.j3k`) that makes files much smaller than JPEG at the same
image quality, without JPEG's blocky artifacts at high compression. Version 4 adds a
directional wavelet transform and rate-distortion optimized quantization to get closer to AVIF
(version 4.1 tunes the encoder further, same file format); version 3 added an in-browser
decoder, transparency and support for images of any size.

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
| JPEG 3000 v3 | −49 % | −34 % |
| JPEG 3000 v4 | −52 % | −38 % |
| **JPEG 3000 v4.1** | **−52 %** | **−38 %** |

- **PSNR**: pixel-by-pixel fidelity. **SSIM**: perceived similarity, closer to what the eye sees.
- Against its predecessor JPEG 2000 (OpenJPEG, 9/7 wavelet): files **16 % smaller** at equal PSNR
  and **21 % smaller** at equal SSIM, smaller on all 20 measurements.
- Against WebP: smaller on all 20 measurements in PSNR, on 17 out of 20 in SSIM (3 ties).
- **AVIF is still better on average**, but the gap was roughly halved: its files are about 6 %
  smaller than JPEG 3000's at equal PSNR (11 % for version 3) and 12 % smaller at equal SSIM
  (18 % for version 3). JPEG 3000 v4.1 produces smaller files than AVIF in 7 measurements out of
  20 in PSNR (the cat photo at every level, and 3 photos out of 4 at the highest quality), and
  in 4 out of 20 in SSIM (plus 1 tie).

How JPEG 3000 compares with AVIF, in more detail (Bjøntegaard delta rate, AVIF as the
reference, + means JPEG 3000 needs more bytes):

| Photo set | Version | At equal PSNR | At equal SSIM |
|---|---|---:|---:|
| 5 tuning photos (the 4 test photos + scikit-image's motorcycle) | v3 | +8.9 % | +21.6 % |
| | v4 | +3.2 % | +14.7 % |
| | **v4.1** | **+2.9 %** | **+13.5 %** |
| 9 photos never used for tuning | v3 | +6.4 % | +19.1 % |
| | v4 | +1.8 % | +12.7 % |
| | **v4.1** | **+1.7 %** | **+11.5 %** |

Correction: the version 4 README gave +4.0 % / +15.7 % for the photos never used for tuning.
One of them, scikit-image's `coins`, turned out to hide an old JPEG 8x8 grid: AVIF gains 1.5 dB
on it when the grid is aligned and loses that gain once the picture is shifted by 1 to 4 pixels.
It is now measured shifted by 3 pixels, which gives the numbers above.

- The gap depends on the bitrate. JPEG 3000 is at its best at high quality (on the astronaut it
  needs 14 % fewer bytes than AVIF at 37.8 dB) and at its worst at low bitrates: below about
  0.4 bit/pixel, AVIF needs 20 to 26 % fewer bytes on the astronaut and grayscale photos. That's
  where AV1's block prediction and deringing filters shine.
- It beats AVIF in PSNR on about half of the photos (a cat, a landscape at −24 %, gravel, a
  microscope slide...) and loses clearly on pictures with large flat or black areas or fine
  noise (Hubble deep field +23 %, retina +19 %, coins +9 %).
- The SSIM gap is larger partly because AVIF is tuned for perception: libavif's default output
  differs from both `tune=psnr` and `tune=ssim` (recent versions likely use libaom's perceptual
  `tune=iq`). JPEG 3000 is tuned for PSNR.

To read these numbers honestly:

- Only 4 test photos in the public benchmark (3 colour, 1 grayscale), all small (0.14 to 0.26 megapixels).
- JPEG XL, the other modern format, could not be tested here.
- These numbers can't be compared with version 2's: version 2 was measured on Windows
  wallpapers, against a weaker JPEG 2000 encoder (Jasper, 5/3 wavelet) and without AVIF.

## What's new in version 4.1

Only the encoder changes: files are still `J3K4` and every decoder (Python, browser) reads
them unchanged. About −0.3 % in PSNR and −1.2 % in SSIM against version 4, on photos never used
for tuning.

| Change | Effect |
|---|---|
| Directions chosen on the energy actually left after the full lifting step, instead of a simpler prediction error | small gain on both metrics |
| Colour quantization factor 1.7, slightly lower at strong compression and higher at high quality (the factor is stored in each file) | better in PSNR **and** SSIM |
| Nearly empty 32×32 blocks (at most four ±1 values) emptied when the added error is worth less than the bits saved | mostly SSIM |

Measured and rejected: perceptual quantization (errors weighted by local texture; it only trades
PSNR for SSIM), rate-dependent direction thresholds, a 4-tap half-pixel interpolation (+0.1 %,
not worth a file format change), richer contexts for signs (already 0.90 to 0.96 bit per sign)
and for "is this coefficient zero?".

## What's new in version 4

Format `J3K4`, about **4 % smaller files than version 3** at equal quality (−4.3 % in PSNR,
−4.9 % in SSIM over the 5 tuning photos, up to −13.7 % on the coffee photo). Version 3 files
(`J3K3`) can still be read, in Python and in the browser.

| Change | Gain |
|---|---:|
| Directional wavelet (adaptive directional lifting): in each 16×16 block, the encoder picks one of 7 directions and the wavelet filters along it, so slanted edges stay cheap instead of spreading over many coefficients. Directions are chosen on brightness, reused for colours, and coded in about 3 adaptive bits per block | about −5 % |
| Rate-distortion optimized quantization (encoder only): each value is chosen among "keep", "one less" and "zero" by weighing the error against its exact cost in bits under the current arithmetic coder state | −0.6 % (−2 % in SSIM) |
| "Is the amplitude above 1?" also decided by mixing 4 models | −0.3 % |
| Finest details quantized 10 % more coarsely | −0.2 % |

Measured and rejected: a Wiener restoration filter after decoding (+0.01 to +0.08 dB), predicting
colours from brightness like AV1's CfL (colours are only 10 to 22 % of the bytes; −0.5 %), a
secondary probability estimation stage (SSE, −0.14 %), a bit-cost criterion for directions, a
quantization strength that depends on the step, other per-level step adjustments.

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
  decoder (about 0.3 s for a 1-megapixel photo in version 4). It gives exactly the same pixels as the
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
  `git checkout v2`; version 4 still reads `J3K3` files).

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
| Transform | 8x8 pixel blocks | directional CDF 9/7 wavelets over the whole image: no blocky artifacts, edges followed in 7 directions |
| Quantization | fixed tables | step tuned to each subband, dead zone that depends on the neighbourhood, then rate-distortion optimized choice of each value |
| Entropy coding | Huffman | adaptive binary arithmetic coding with contexts (neighbours, parent scale, sibling subbands, brightness for colours) and logistic mixing of 4 models |

## Limitations

- It's a Python prototype: on an Apple M3, about 3 s to encode and 2.2 s to decode a
  1-megapixel photo at quality 50 (version 4's directional wavelet and optimized quantization
  cost time). The JavaScript decoder takes about 0.3 s.
- Only the decoder exists in JavaScript; encoding needs Python.
- Browsers, phones and image editors don't open `.j3k` natively: you need `j3k.js`, the
  Python tool or the Pillow plugin.
- "JPEG" is a trademark of the JPEG committee: the format would need another name to be distributed.
