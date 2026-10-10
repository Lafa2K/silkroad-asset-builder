# Silkroad Asset Builder

A lightweight Python tool for common Silkroad Online asset tasks.

## Features

- extract one or multiple `.pk2` archives;
- convert PNG, JPG, BMP, and DDS textures to `.ddj`;
- turn an OBJ and texture into an effect mesh (`.bms` + `.ddj`);
- build a custom `.bsr/.bmt/.bms/.ddj` package.

The PK2 extractor streams files and updates only the progress display. It does
not load entire archives into memory or open File Explorer while working.

## Installation

Requires Python 3.10 or newer.

```powershell
python -m pip install -r requirements.txt
```

## Usage

Open `Open Silkroad Asset Builder.cmd` or run:

```powershell
python .\sro_asset_builder.py
```

### Extract PK2

1. Open the **PK2 EXTRACTOR** tab.
2. Click **Select PK2 files** and choose one or more archives.
3. Choose the destination folder.
4. Keep **Create one folder per PK2** enabled to generate folders such as
   `Data`, `Media`, and `Particles` without mixing their contents.
5. Click **EXTRACT ARCHIVES**.

The reader uses the standard Silkroad/vSRO client key. Archives protected with
a custom key are not currently supported.

For safety, extraction stops if a client PK2 contains recognizable server
executables or configuration files. Source PK2 files are always opened read-only.

### DDJ Textures

On the **DDJ TEXTURES** tab, select individual files or an entire folder. The
destination is optional; when empty, `.ddj` files are saved beside the originals.

### Effect / Prop

The **EFFECT / PROP** tab accepts:

- a Wavefront OBJ model;
- a PNG, DDS, JPG, or BMP texture;
- an extracted root containing `Data`, `Media`, or `Particles`.

`PROP SKILL` writes the mesh to `Particles/meshes` and the texture to
`Particles/textures`. `CUSTOM PROP` creates the complete set under
`Data/res/custom/<name>`.

## Note

Back up files before replacing anything in a client. The extractor only reads
PK2 archives and never modifies the original file.
