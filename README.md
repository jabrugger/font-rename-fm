# font-rename-fm

Rename fonts using their internal names, with a preview before changing files.

This MIT-licensed fork extends [i-defranca/font-rename-fm](https://github.com/i-defranca/font-rename-fm), originally derived from [whtsky/font-rename](https://github.com/whtsky/font-rename). Original authorship and license are preserved. This fork's releases are distributed on GitHub; `pip install font-rename-fm` from PyPI installs the upstream package, not this fork.

## Install

Requires Python 3.12 or later. Download the wheel from [GitHub Releases](https://github.com/jabrugger/font-rename-fm/releases), then install it:

```console
python -m pip install ./font_rename_fm-0.2.9-py3-none-any.whl
```

Alternatively, install this repository checkout with `python -m pip install .`.

On Windows, use `py -3.14` instead of `python` to select Python 3.14 explicitly. The `font-rename` entry point is available when Python's Scripts directory is on PATH; the module command below avoids that requirement.

## Preview and apply

```console
python -m font_rename_fm.rename "C:\Fonts"
python -m font_rename_fm.rename "C:\Fonts" "D:\MoreFonts"
```

The default is a preview. It shows current and proposed filenames on separate lines. No fonts are changed until `--apply` is supplied:

```console
python -m font_rename_fm.rename "C:\Fonts" --apply
```

Test on a copy first. `--apply` can rename files, extract collection members and delete exact duplicates. Run while the input folders are otherwise idle. There is no general undo function; internal normalization has per-font backups, but ordinary renames and duplicate removal do not.

## Behavior

- Recursively processes TTF, OTF, TTC and OTC files. Symlinks and hidden paths beginning with `.` are skipped.
- Uses internal full names, preferring valid native-language records where available. English Windows records take priority over legacy Macintosh records when selecting a Latin name. Detects some legacy records containing ASCII bytes incorrectly labeled as Unicode.
- Cleans forbidden Windows filename characters, repeated whitespace, trailing spaces/periods and reserved device names. Removes leading `☞` markers when followed by a name. Symbols within meaningful names are preserved.
- For non-Latin names, produces `Transliteration [Original]`. Transliteration is automatic, not translation; language-specific readings and missing Arabic vowels cannot be inferred reliably.
- SHA-256 narrows duplicate candidates, then exact byte comparison decides. Only byte-identical files are removed. Deduplication spans all supplied folders; the retained copy can therefore be in another input folder.
- Different contents sharing a name are preserved. Differing style, version, manufacturer, weight, width, PostScript name or unique identifier can be added in brackets. Up to two descriptive fields are combined; a 12-digit SHA-256 prefix is the fallback. Numbers are a final fallback for retained identical copies or occupied destinations. The first retained font keeps the plain name.
- Names are limited to 180 UTF-16 units before the extension. This bounds the filename, not the full path length.
- Extracts TTC/OTC members and retains an original collection. Byte-identical collections can be consolidated. A member without a usable name does not block other members when the collection can be opened.
- Keeps unreadable or unusable fonts and reports errors while continuing with other inputs. A nonzero exit status means at least one operation was skipped or failed; it does not roll back earlier successful operations.

The tool does not consult web catalogs, infer canonical commercial names, move families between alphabetical folders, or automatically correct extensions from the font's outline format. WOFF/WOFF2, FOT, FON/FNT and Type 1 PFB/PFM files are outside its current processing scope. A FOT file may refer to a TTF's old filename.

## Optional internal-name normalization

```console
python -m font_rename_fm.rename "C:\Fonts" --normalize-internal
python -m font_rename_fm.rename "C:\Fonts" --normalize-internal --apply
```

Normalizes display family, style and full-name records: whitespace, forbidden filename characters and leading `☞` markers. A style containing only `☞` becomes empty. Also removes leading markers from CFF `FamilyName` and `FullName`, including the UTF-8 marker exposed as Latin-1 text. Technical PostScript and unique identifiers are preserved.

Each edited font receives a sibling `filename.ttf.original.bak` or `filename.otf.original.bak`. Existing backups are never overwritten. Unreadable localized records are kept; an operation that would leave a nonempty display name empty is rejected. The edits change font bytes and remove an invalidated DSIG signature. They do not harmonize contradictory commercial names. Extracted members can be normalized; the original TTC/OTC metadata stays unchanged.

Deduplication runs before normalization. A backup is trusted as provenance only when normalizing it reproduces the current font bytes exactly. This keeps collision hashes stable and recognizes previously extracted, normalized collection members on repeat runs. It never authorizes deleting files with different bytes.

## Options

| Option | Effect |
| --- | --- |
| `--apply` | Apply file operations and optional normalization. |
| `--dry-run` | Explicit preview; incompatible with `--apply`. |
| `--keep-duplicates` | Keep exact copies too, with distinct filenames. |
| `--no-transliterate` | Use only the original internal name. |
| `--normalize-internal` | Also clean internal display names; applying creates backups. |

## Tests

```console
python -m unittest discover -v
```

44 tests generate their own fonts. Coverage includes preview immutability, binary duplicates, collision metadata, changed inputs, variable fonts, multilingual and malformed name records, Windows naming rules, normalized repeat runs, TTC members, unrelated/existing backups, CFF markers and long bilingual filenames. Locally tested with Python 3.14.8 on Windows; GitHub Actions is configured for Windows and Linux with Python 3.12, 3.13 and 3.14. A configured matrix is not a claim that every remote job has already passed.

No user font collection or private test logs are included in the repository or release assets.

## License and attribution

MIT; see [LICENSE.txt](LICENSE.txt). Original project authorship belongs to Jay Soren / Futuremotion and the upstream contributors. Fork maintained by [jabrugger](https://github.com/jabrugger).
