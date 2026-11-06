# JPEG 3000 contre JPEG, JPEG 2000 et WebP : résultats

Pour chaque mesure, tous les fichiers ont au moins la même qualité d'image que le JPEG ; on compare leur taille (−50 % = deux fois plus petit que le JPEG).

- **PSNR** : fidélité pixel par pixel.
- **SSIM** : ressemblance perçue par l'œil (formes, textures), plus proche de ce qu'on voit.

**Taille moyenne par rapport au JPEG, à qualité égale :**

| Format | À PSNR égal | À SSIM égal |
|---|---:|---:|
| JPEG (1992) | référence | référence |
| JPEG 2000 (2000) | −41 % | −9 % |
| WebP (2010) | −32 % | −20 % |
| **JPEG 3000 (ce prototype)** | −51 % | −37 % |

**JPEG 3000 contre son prédécesseur JPEG 2000 : −17 % à PSNR égal, −29 % à SSIM égal.**

JPEG 3000 est réglé pour le PSNR : son avance est plus faible quand on mesure ce que l'œil perçoit (SSIM).

Encodeurs utilisés : JPEG = libjpeg (optimisation Huffman activée), JPEG 2000 = Jasper (intégré à OpenCV, ondelette 5/3, une seule couche), WebP = libwebp. Un encodeur JPEG 2000 réglé en ondelette 9/7 (OpenJPEG, Kakadu) ferait mieux que Jasper. Pour JPEG 2000 et WebP, dont le réglage est un nombre entier, la taille est interpolée entre les deux réglages voisins. « — » : qualité inatteignable pour ce format ; ces cas sont exclus des moyennes.

Les photos de test étaient déjà des JPEG : elles sont réduites (x2 ou x3) pour effacer leur grille 8x8 d'origine. Sans cela, un nouveau JPEG retombe pile sur cette grille et gagne injustement (mesuré : 43,9 dB au lieu de 36,8 dB en qualité 75 sur le raton laveur).

## Raton laveur (512x384)

Taille par rapport au JPEG : à PSNR égal / à SSIM égal.

| Qualité JPEG | JPEG | JPEG 2000 | WebP | **JPEG 3000** |
|---:|---:|---:|---:|---:|
| 10 | 9,5 Ko | −30 % / −8 % | −27 % / −9 % | **−44 % / −28 %** |
| 25 | 19,6 Ko | −26 % / −14 % | −24 % / −21 % | **−39 % / −28 %** |
| 50 | 31,2 Ko | −29 % / −18 % | −27 % / −27 % | **−40 % / −31 %** |
| 75 | 46,9 Ko | −29 % / −18 % | −29 % / −29 % | **−39 % / −31 %** |
| 90 | 76,5 Ko | −26 % / −15 % | −29 % / −30 % | **−36 % / −31 %** |

## Plage (640x400)

Taille par rapport au JPEG : à PSNR égal / à SSIM égal.

| Qualité JPEG | JPEG | JPEG 2000 | WebP | **JPEG 3000** |
|---:|---:|---:|---:|---:|
| 10 | 6,1 Ko | −50 % / −37 % | −32 % / −32 % | **−56 % / −54 %** |
| 25 | 12,8 Ko | −44 % / −9 % | −36 % / −26 % | **−49 % / −29 %** |
| 50 | 21,2 Ko | −47 % / +10 % | −38 % / −18 % | **−50 % / −20 %** |
| 75 | 32,7 Ko | −49 % / +18 % | −39 % / −16 % | **−52 % / −16 %** |
| 90 | 56,9 Ko | −52 % / +13 % | −37 % / −22 % | **−56 % / −19 %** |

## Rocher (640x400)

Taille par rapport au JPEG : à PSNR égal / à SSIM égal.

| Qualité JPEG | JPEG | JPEG 2000 | WebP | **JPEG 3000** |
|---:|---:|---:|---:|---:|
| 10 | 8,7 Ko | −35 % / −2 % | −27 % / +14 % | **−49 % / −13 %** |
| 25 | 20,9 Ko | −25 % / −0 % | −16 % / −7 % | **−38 % / −13 %** |
| 50 | 35,7 Ko | −23 % / −15 % | −18 % / −19 % | **−35 % / −21 %** |
| 75 | 57,7 Ko | −24 % / −18 % | −26 % / −29 % | **−35 % / −28 %** |
| 90 | 103,1 Ko | −26 % / −27 % | −32 % / −35 % | **−35 % / −33 %** |

## Tulipes (640x400)

Taille par rapport au JPEG : à PSNR égal / à SSIM égal.

| Qualité JPEG | JPEG | JPEG 2000 | WebP | **JPEG 3000** |
|---:|---:|---:|---:|---:|
| 10 | 3,0 Ko | −68 % / −75 % | −33 % / −33 % | **−78 % / −91 %** |
| 25 | 5,3 Ko | −61 % / −66 % | −48 % / −56 % | **−71 % / −80 %** |
| 50 | 8,5 Ko | −61 % / −39 % | −50 % / −39 % | **−69 % / −68 %** |
| 75 | 12,9 Ko | −60 % / +7 % | −48 % / −2 % | **−70 % / −61 %** |
| 90 | 23,7 Ko | −62 % / +142 % | −32 % / +39 % | **−74 % / −55 %** |
