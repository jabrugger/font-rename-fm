# Font Rename Neo

A cross-platform Python CLI that renames fonts from their internal names, removes byte-identical duplicates and optionally normalizes internal display names. Preview is the default; applying changes is explicit.

This MIT-licensed fork extends [i-defranca/font-rename-fm](https://github.com/i-defranca/font-rename-fm), originally derived from [whtsky/font-rename](https://github.com/whtsky/font-rename). Original authorship and license are preserved. This fork's releases are distributed on GitHub; `pip install font-rename-fm` from PyPI installs the upstream package, not this fork.

The GitHub repository is named `font-rename-neo`. The distribution (`font-rename-fm`), Python module (`font_rename_fm`) and command (`font-rename`) retain their existing names for compatibility.

Prefer a Windows interface? [Font Renamer GUI](https://github.com/jabrugger/font-rename-gui) includes this engine and Python in one portable download. Neither a separate engine download nor a Python installation is needed for the GUI.

## Install

Requires Python 3.12 or later. Download the wheel from [GitHub Releases](https://github.com/jabrugger/font-rename-neo/releases), then install it:

```console
python -m pip install ./font_rename_fm-0.3.2-py3-none-any.whl
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

## Text log

```console
python -m font_rename_fm.rename "C:\Fonts" --normalize-internal --apply --log
```

Use `--log` without a path for `font_renamer[YYYY-MM-DD].log` in the current working folder (local date), or `--log "C:\Fonts\rename.log"` for a custom path. Both append the complete output to a UTF-8 file while keeping console output. Every log line includes the running PC's local date/time and milliseconds, without a timezone label, including FontTools diagnostics. Sessions also include start/end markers, arguments and exit status. Console output keeps its original format. Existing contents are preserved; the parent folder must exist. If the log cannot be opened, processing stops before font changes. Preview may write the requested log but leaves fonts unchanged.

## Behavior

- Recursively processes TTF, OTF, TTC and OTC files. Symlinks and hidden paths beginning with `.` are skipped.
- Uses internal full names, preferring valid native-language records where available. English Windows records take priority over legacy Macintosh records when selecting a Latin name. Detects some legacy records containing ASCII bytes incorrectly labeled as Unicode.
- Cleans forbidden Windows filename characters, repeated whitespace, trailing spaces/periods and reserved device names. Removes leading `☞` markers when followed by a name. Symbols within meaningful names are preserved.
- For non-Latin names, produces `Transliteration [Original]`. Transliteration is automatic, not translation; language-specific readings and missing Arabic vowels cannot be inferred reliably.
- SHA-256 narrows duplicate candidates, then exact byte comparison decides. Only byte-identical files are removed. Deduplication spans all supplied folders; the retained copy can therefore be in another input folder.
- Different contents sharing a name are preserved. Differing style, version, manufacturer, weight, width, PostScript name or unique identifier can be added in brackets. Up to two descriptive fields are combined; a 12-digit SHA-256 prefix is the fallback. Numbers are a final fallback for retained identical copies or occupied destinations. The first retained font keeps the plain name.
- Names are limited to 180 UTF-16 units and 235 UTF-8 bytes before the extension, reserving space for backups on filesystems with a 255-byte filename limit. This bounds the filename, not the full path length.
- Extracts TTC/OTC members and retains an original collection. Byte-identical collections can be consolidated. A member without a usable name does not block other members when the collection can be opened.
- FontTools warnings and errors include the full path of the font being processed, including internal-name normalization and collections.
- Keeps unreadable or unusable fonts and reports errors while continuing with other inputs. A nonzero exit status means at least one operation was skipped or failed; it does not roll back earlier successful operations.

The tool does not consult web catalogs, infer canonical commercial names, move families between alphabetical folders, or automatically correct extensions from the font's outline format. WOFF/WOFF2, FOT, FON/FNT and Type 1 PFB/PFM files are outside its current processing scope. A FOT file may refer to a TTF's old filename.

## Optional internal-name normalization

```console
python -m font_rename_fm.rename "C:\Fonts" --normalize-internal
python -m font_rename_fm.rename "C:\Fonts" --normalize-internal --apply
```

Compacts redundant zero padding in validated format-0 name tables; gaps containing nonzero data are preserved. Normalizes display family, style and full-name records: whitespace, forbidden filename characters and leading `☞` markers. Unusable nonempty style fields (including `?` or marker-only values) are recovered from matching style records or coherent OS/2/head metadata. Legacy ID 2 stays distinct from typographic IDs 17/22. Conflicting or insufficient evidence yields `Unknown`, a tool-defined placeholder, and each decision is logged. Legitimately empty optional fields remain empty. Also removes leading markers from CFF `FamilyName` and `FullName`, including the UTF-8 marker exposed as Latin-1 text. Technical PostScript and unique identifiers are preserved.

Each edited font receives a backup in the sibling `BAK` subfolder: `BAK/filename.ttf.original.bak` or `BAK/filename.otf.original.bak`. `BAK` folders are excluded from font processing. Legacy backups beside fonts remain recognized for provenance and overwrite protection. Existing backups are never overwritten. Unreadable localized records are kept. High-byte Mac Roman records alongside native Unicode names are conservatively preserved and reported as ambiguous, since forbidden-looking characters may be bytes of multibyte text; an operation that would leave a nonempty display name empty is rejected. The edits change font bytes and remove an invalidated DSIG signature. They do not harmonize contradictory commercial names. Extracted members can be normalized; the original TTC/OTC metadata stays unchanged.

Deduplication runs before normalization. A backup is trusted as provenance only when normalizing it reproduces the current font bytes exactly. Provenance caching is limited to 64 entries and 16 MiB of validated backup bytes; ordinary font contents are not retained in that cache. This keeps collision hashes stable and recognizes previously extracted, normalized collection members on repeat runs. It never authorizes deleting files with different bytes.

## Options

| Option | Effect |
| --- | --- |
| `--log [PATH]` | Append all output to a UTF-8 log and keep console output. Without PATH, use a dated filename in the current folder. |
| `--apply` | Apply file operations and optional normalization. |
| `--dry-run` | Explicit preview; incompatible with `--apply`. |
| `--keep-duplicates` | Keep exact copies too, with distinct filenames. |
| `--no-transliterate` | Use only the original internal name. |
| `--normalize-internal` | Also clean internal display names; applying creates backups in a `BAK` subfolder. |

## Tests

```console
python -m unittest discover -v
```

75 tests generate their own fonts. Coverage includes preview immutability, binary duplicates, collision metadata, changed inputs, variable fonts, multilingual and malformed name records, Windows naming rules, normalized repeat runs, TTC members, unrelated/existing backups, CFF markers and long bilingual filenames. Locally tested with Python 3.14.8 on Windows; GitHub Actions is configured for Windows and Linux with Python 3.12, 3.13 and 3.14. A configured matrix is not a claim that every remote job has already passed.

No user font collection or private test logs are included in the repository or release assets.

## License and attribution

MIT; see [LICENSE.txt](LICENSE.txt). Original project authorship belongs to Jay Soren / Futuremotion and the upstream contributors. Fork maintained by [jabrugger](https://github.com/jabrugger).


## Timestamp preservation

Internal-name corrections preserve original filesystem modification time and, on Windows, creation time. Backups preserve the same timestamps. Extracted collection members inherit the collection timestamps. FontTools internal head timestamps remain unchanged.

## Version comparison roadmap

Comparing glyphs and consolidating different font versions is being evaluated, but is not implemented. Different font bytes are retained even when internal names match. A higher version or glyph count alone does not establish equivalent character coverage or typographic behavior.

## Versioning

See [VERSIONING.md](VERSIONING.md): corrections increment PATCH, new features increment MINOR, and major changes or a final release increment MAJOR.
