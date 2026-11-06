# JPEG 3000 (prototype)

Un format d'image expérimental (`.j3k`) qui produit des fichiers plus légers que le JPEG
pour la même qualité d'image. Résultats chiffrés : voir `resultats.md` et `comparaison.png`.

## Utilisation

Il faut Python 3 avec `numpy` et `opencv-python` (et `scipy` pour le banc d'essai).

```
python jpeg3000.py encode photo.png photo.j3k --quality 50
python jpeg3000.py encode photo.png photo.j3k --size 50000
python jpeg3000.py decode photo.j3k photo_decodee.png
python compare.py
```

- `--quality` : de 1 (tout petit fichier) à 100 (presque sans perte), 50 par défaut.
- `--size` : taille maximale du fichier en octets ; la meilleure qualité qui tient est choisie.

## Ce qui change par rapport au JPEG

| | JPEG (1992) | JPEG 3000 |
|---|---|---|
| Découpage | blocs de 8x8 pixels | ondelettes sur toute l'image : pas d'effet « pavés » |
| Quantification | tables fixes | zone morte, pas adapté à chaque sous-bande |
| Codage final | Huffman | codage arithmétique adaptatif avec contextes |

## Limites (à connaître avant d'en parler à quelqu'un)

- C'est un prototype en Python : environ 2 s pour encoder ou décoder une photo d'un mégapixel.
- Aucun navigateur, téléphone ou logiciel ne sait ouvrir un `.j3k`.
- JPEG XL et AVIF existent déjà, sont gratuits, et compressent mieux.
- « JPEG » est une marque du comité JPEG : il faudrait un autre nom pour le diffuser.
