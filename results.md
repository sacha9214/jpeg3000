# JPEG 3000 vs JPEG, JPEG 2000, WebP and AVIF: results

For each measurement, every file has at least the image quality of the JPEG file; we compare their sizes (−50 % = half the size of the JPEG).

- **PSNR**: pixel-by-pixel fidelity.
- **SSIM**: perceived similarity (shapes, textures), closer to what the eye sees.

**Average file size relative to JPEG, at equal quality:**

| Format | At equal PSNR | At equal SSIM |
|---|---:|---:|
| JPEG (1992) | reference | reference |
| JPEG 2000 (2000) | −43 % | −22 % |
| WebP (2010) | −35 % | −26 % |
| AVIF (2019) | −55 % | −46 % |
| **JPEG 3000 v3 (this prototype)** | −49 % | −34 % |

Encoders: JPEG = libjpeg-turbo (optimized Huffman tables), JPEG 2000 = OpenJPEG through Pillow (irreversible 9/7 wavelet; the OpenJPEG writer built into OpenCV is misconfigured: about 3 dB worse at equal size in our test), WebP = libwebp, AVIF = libavif with the AV1 encoder bundled in OpenCV 5.0.0 (speed 6, its default). For the other formats, whose setting is an integer, the size is interpolated between the two neighbouring settings. "—": quality out of reach for that format; excluded from the averages.

Test photos: public domain / CC0 images shipped with scikit-image (see test-images/README.md), never JPEG-compressed, so no format benefits from an old 8x8 grid. JPEG XL could not be tested here.

## Astronaut (512x512)

File size relative to JPEG: at equal PSNR / at equal SSIM.

| JPEG quality | JPEG | JPEG 2000 | WebP | AVIF | **JPEG 3000** |
|---:|---:|---:|---:|---:|---:|
| 10 | 9.8 KB | −43 % / −25 % | −35 % / −27 % | −57 % / −52 % | **−48 % / −32 %** |
| 25 | 17.8 KB | −41 % / −17 % | −39 % / −29 % | −57 % / −47 % | **−46 % / −25 %** |
| 50 | 27.1 KB | −43 % / −13 % | −42 % / −30 % | −58 % / −46 % | **−48 % / −27 %** |
| 75 | 39.7 KB | −46 % / −12 % | −43 % / −28 % | −58 % / −45 % | **−50 % / −29 %** |
| 90 | 66.5 KB | −50 % / −11 % | −39 % / −26 % | −54 % / −39 % | **−53 % / −28 %** |

## Cat (451x300)

File size relative to JPEG: at equal PSNR / at equal SSIM.

| JPEG quality | JPEG | JPEG 2000 | WebP | AVIF | **JPEG 3000** |
|---:|---:|---:|---:|---:|---:|
| 10 | 3.9 KB | −59 % / −45 % | −41 % / −23 % | −60 % / −52 % | **−66 % / −57 %** |
| 25 | 8.0 KB | −43 % / −28 % | −26 % / −15 % | −50 % / −43 % | **−51 % / −41 %** |
| 50 | 13.0 KB | −42 % / −28 % | −24 % / −21 % | −47 % / −44 % | **−47 % / −41 %** |
| 75 | 20.1 KB | −42 % / −31 % | −25 % / −24 % | −47 % / −45 % | **−47 % / −42 %** |
| 90 | 34.3 KB | −41 % / −34 % | −27 % / −25 % | −45 % / −44 % | **−47 % / −42 %** |

## Coffee (600x400)

File size relative to JPEG: at equal PSNR / at equal SSIM.

| JPEG quality | JPEG | JPEG 2000 | WebP | AVIF | **JPEG 3000** |
|---:|---:|---:|---:|---:|---:|
| 10 | 7.6 KB | −50 % / −27 % | −31 % / −19 % | −66 % / −58 % | **−56 % / −40 %** |
| 25 | 16.1 KB | −44 % / −15 % | −34 % / −18 % | −64 % / −51 % | **−51 % / −28 %** |
| 50 | 26.4 KB | −45 % / −16 % | −36 % / −25 % | −62 % / −51 % | **−51 % / −30 %** |
| 75 | 40.9 KB | −45 % / −20 % | −39 % / −31 % | −60 % / −50 % | **−51 % / −33 %** |
| 90 | 71.3 KB | −45 % / −19 % | −34 % / −31 % | −53 % / −45 % | **−49 % / −33 %** |

## Camera (grayscale) (512x512)

File size relative to JPEG: at equal PSNR / at equal SSIM.

| JPEG quality | JPEG | JPEG 2000 | WebP | AVIF | **JPEG 3000** |
|---:|---:|---:|---:|---:|---:|
| 10 | 5.9 KB | −35 % / −15 % | −43 % / −25 % | −57 % / −47 % | **−44 % / −26 %** |
| 25 | 12.7 KB | −31 % / −13 % | −31 % / −20 % | −51 % / −40 % | **−39 % / −20 %** |
| 50 | 21.3 KB | −36 % / −19 % | −35 % / −30 % | −51 % / −44 % | **−43 % / −28 %** |
| 75 | 34.1 KB | −39 % / −26 % | −39 % / −35 % | −52 % / −45 % | **−46 % / −34 %** |
| 90 | 59.2 KB | −37 % / −28 % | −39 % / −37 % | −48 % / −42 % | **−43 % / −35 %** |
