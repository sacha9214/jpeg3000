# JPEG 3000 (prototype)

Un format d'image expérimental (`.j3k`) qui produit des fichiers plus légers que le JPEG
pour la même qualité d'image, et sans l'effet « pavés » du JPEG quand on compresse fort.

![Comparaison à taille égale : original, JPEG, JPEG 2000, JPEG 3000](comparaison.png)

## Résultats

Taille moyenne des fichiers par rapport au JPEG, à qualité égale
(4 photos x 5 niveaux de compression, détails dans [resultats.md](resultats.md)) :

| Format | À PSNR égal | À SSIM égal |
|---|---:|---:|
| JPEG (1992) | référence | référence |
| JPEG 2000 (2000) | −41 % | −9 % |
| WebP (2010) | −32 % | −20 % |
| **JPEG 3000** | **−51 %** | **−37 %** |

- **PSNR** : fidélité pixel par pixel. **SSIM** : ressemblance perçue par l'œil.
- Contre son prédécesseur JPEG 2000 : **−17 %** à PSNR égal, **−29 %** à SSIM égal.
- JPEG 3000 fait mieux que JPEG, JPEG 2000 et WebP sur les 20 mesures en PSNR.
  En SSIM, WebP fait un peu mieux sur 3 mesures à haute qualité (Plage q90, Rocher q75 et q90).

À savoir pour lire ces chiffres honnêtement :

- L'encodeur JPEG 2000 utilisé (Jasper, inclus dans OpenCV) n'est pas le meilleur :
  il utilise l'ondelette 5/3. OpenJPEG ou Kakadu en 9/7 feraient mieux.
- JPEG XL et AVIF, les formats modernes les plus efficaces, n'ont pas pu être testés ici.
- Seulement 4 photos de test, toutes réduites pour effacer leur ancienne compression JPEG.

## Utilisation

Il faut Python 3 avec `numpy` et `opencv-python` (et `scipy` pour le banc d'essai).

```
python jpeg3000.py encode photo.png photo.j3k --quality 50
python jpeg3000.py encode photo.png photo.j3k --size 50000
python jpeg3000.py decode photo.j3k photo_decodee.png
python test_jpeg3000.py
python compare.py
```

- `--quality` : de 1 (tout petit fichier) à 100 (presque sans perte), 50 par défaut.
- `--size` : taille maximale du fichier en octets ; la meilleure qualité qui tient est choisie.

## Ce qui change par rapport au JPEG

| | JPEG (1992) | JPEG 3000 |
|---|---|---|
| Découpage | blocs de 8x8 pixels | ondelettes CDF 9/7 sur toute l'image : pas d'effet « pavés » |
| Quantification | tables fixes | pas adapté à chaque sous-bande, zone morte qui dépend du voisinage |
| Codage final | Huffman | codage arithmétique adaptatif avec contextes (voisins, échelle parente, luminosité pour les couleurs) |

Version 2 (format `J3K2`) par rapport à la version 1 : zone morte adaptative, contexte
« luminosité » pour coder les couleurs, probabilités à double vitesse dans le codeur
arithmétique, couleurs un peu plus compressées. Gain : fichiers environ 6 % plus petits à SSIM égal.

## Limites

- C'est un prototype en Python : 3 à 4 s pour encoder ou décoder une photo d'un mégapixel.
- Aucun navigateur, téléphone ou logiciel ne sait ouvrir un `.j3k`.
- « JPEG » est une marque du comité JPEG : il faudrait un autre nom pour diffuser ce format.
