# Data attribution — vocadito

The audio and annotation files in this directory are excerpts from the **vocadito**
dataset. They are **not** covered by this repository's MIT licence; they are used under
CC BY 4.0, and this attribution must travel with them in any redistribution.

## Dataset

**vocadito: A dataset of solo vocals with f0, note, and lyric annotations**

**Authors:** Rachel M. Bittner, Katherine Pasalo, Juan José Bosch, Gabriel
Meseguer-Brocal, David Rubinstein

**Source:** Zenodo record 5557945 — https://doi.org/10.5281/zenodo.5557945

## Licence

Creative Commons Attribution 4.0 International (**CC BY 4.0**)

- Licence deed: https://creativecommons.org/licenses/by/4.0/
- Full legal text: https://creativecommons.org/licenses/by/4.0/legalcode

You are free to share and adapt the material, including commercially, provided you give
appropriate credit, link to the licence, and indicate if changes were made.

The archive distributed on Zenodo contains no `LICENSE` file. The CC BY 4.0 terms above
are taken from the Zenodo record's own licence metadata, and the same licence is listed for
vocadito by [mirdata](https://mirdata.readthedocs.io/). The files shipped with the data —
`Annotations/README.txt` and `vocadito_metadata.csv` — document column formats only and
state no terms of their own, so nothing in the distribution contradicts the record.

## What is included here

Five of the dataset's forty excerpts, chosen because their two independent note annotations
disagree in different ways:

| Excerpt | Files |
|---|---|
| `vocadito_8` | audio, f0, notesA1, notesA2 |
| `vocadito_16` | audio, f0, notesA1, notesA2 |
| `vocadito_17` | audio, f0, notesA1, notesA2 |
| `vocadito_21` | audio, f0, notesA1, notesA2 |
| `vocadito_34` | audio, f0, notesA1, notesA2 |

Laid out as in the original release:

```
Audio/vocadito_{n}.wav                        mono, 44.1 kHz
Annotations/F0/vocadito_{n}_f0.csv            time_sec, freq_hz  (0 = unvoiced)
Annotations/Notes/vocadito_{n}_notesA1.csv    onset_sec, pitch_hz, duration_sec
Annotations/Notes/vocadito_{n}_notesA2.csv    onset_sec, pitch_hz, duration_sec
Annotations/README.txt                        the upstream column documentation, verbatim
vocadito_metadata.csv                         the five matching rows of the original CSV
```

## Modifications

**The audio and annotation files are unmodified byte-for-byte copies** of the
corresponding files in the Zenodo release. `Annotations/README.txt` is likewise a verbatim
copy.

One file is derived rather than copied: `vocadito_metadata.csv` contains the original
header row and only the five rows whose `track_id` matches the excerpts above. No field
within those rows was altered. The dataset's lyrics annotations are not included here.

## Citation

```bibtex
@dataset{bittner_vocadito_2021,
  title     = {vocadito: A dataset of solo vocals with $f_0$, note, and lyric annotations},
  author    = {Bittner, Rachel M. and Pasalo, Katherine and Bosch, Juan Jos\'e and
               Meseguer-Brocal, Gabriel and Rubinstein, David},
  year      = {2021},
  publisher = {Zenodo},
  doi       = {10.5281/zenodo.5557945},
  url       = {https://doi.org/10.5281/zenodo.5557945}
}
```
