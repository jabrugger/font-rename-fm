# Changelog

## 0.2.9

First public release of this fork, incorporating the locally tested 0.2.x improvements:

- Preview by default and explicit `--apply` for changes.
- Windows filename cleanup and guarded collision handling.
- Exact binary deduplication across input folders; distinct fonts are retained.
- Descriptive metadata suffixes, with a short content hash as fallback.
- Automatic transliteration alongside original non-Latin names.
- Improved selection of Windows/native records over misleading legacy names.
- Optional internal display-name cleanup with original backups.
- Leading manicule cleanup in filenames, name records and CFF display names.
- Stable repeat runs after normalization and TTC/OTC extraction.
- Collection retention and continued extraction after an unusable member.
- Readable current/new preview output and Unicode-safe Windows output.
- 44 generated-font tests and a Windows/Linux Python 3.12–3.14 CI matrix.

## Upstream 0.1.1

Original `font-rename-fm` release by the upstream project. This fork preserves the MIT license and attribution.
