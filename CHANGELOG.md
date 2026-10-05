# Changelog

## 0.3.0

- Add cooperative cancellation checkpoints for the separate desktop GUI. Completed operations remain; the current font operation finishes before stopping.

- Fix collection-sized memory growth: stop caching ordinary font bytes and bound validated provenance backups to 64 entries / 16 MiB, preserving exact comparisons and collision identities after eviction.

- Recover unusable styles from matching name records or coherent style metadata; use the tool-defined `Unknown` placeholder when evidence is missing or contradictory, without blocking other name cleanup. Log each recovery decision.

- Compact redundant zero padding in validated format-0 name tables during internal normalization, preserving all name records and backing up original bytes.

- Preserve ambiguous high-byte legacy Mac Roman name records alongside native Unicode names; avoid corrupting mislabeled multibyte text during internal normalization.

- Timestamp every log line using the running PC's local time and milliseconds, without a timezone label; preserve console formatting.

- Allow `--log` without a path, using `font_renamer[YYYY-MM-DD].log` in the current folder; repeated runs on the same local date append sessions.

- Store normalization backups in a sibling `BAK` subfolder, skip backup folders during traversal, and preserve support for legacy backup locations.

- Add `--log PATH` to append console output and diagnostics to a UTF-8 session log, with local timestamps and exit status.

- Include the full source font path in FontTools diagnostics during renaming, collection extraction and internal-name normalization. Preserve warning severity and avoid duplicate output.

## 0.2.10

- Fix Linux filename-length failures for long non-Latin names. Filenames and metadata suffixes now respect UTF-8 byte limits as well as Windows UTF-16 limits, with space reserved for backup filenames.

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
