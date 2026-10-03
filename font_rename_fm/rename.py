#!/usr/bin/env python3
"""Rename font files safely, preserving distinct contents and Unicode names."""
import argparse
import hashlib
import io
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from anyascii import anyascii
import cchardet as chardet
from fontTools.ttLib import TTCollection, TTFont

PREFERRED_IDS = ((3, 10, 0x0409), (3, 1, 0x0409), (3, 0, 0x0409),
                 (3, 1, 0x0C04), (3, 1, 0x0804), (3, 1, 0x0404),
                 (3, 1, 0x0411), (1, 0, 0))
PREFERRED_NAME_IDS = (4, 6, 16, 1)
FONT_SUFFIXES = {'.ttf', '.otf', '.ttc', '.otc'}
DISPLAY_NAME_IDS = {1, 2, 4, 16, 17, 18, 21, 22}


def strip_leading_manicule(name):
    stripped = name.lstrip()
    # CFF strings can expose UTF-8 bytes through a Latin-1 representation.
    markers = ('☞', '\u00e2\u0098\u009e', '\u00e2\u02dc\u017e')
    changed = False
    while True:
        marker = next((m for m in markers if stripped.startswith(m)), None)
        if marker is None:
            break
        changed = True
        stripped = stripped[len(marker):].lstrip()
    return stripped if changed and stripped else name


def normalize_internal_names(font):
    """Clean display names, preserving technical IDs and localized encodings."""
    changes = []
    usable_ids = set()
    for record in font['name'].names:
        if record.nameID in DISPLAY_NAME_IDS:
            try:
                clean_name(record.toUnicode())
                usable_ids.add(record.nameID)
            except (UnicodeError, LookupError, ValueError):
                pass
    for record in font['name'].names:
        if record.nameID not in DISPLAY_NAME_IDS:
            continue
        try:
            original = record.toUnicode()
        except (UnicodeError, LookupError):
            print(f'WARNING: undecodable internal name record {record.nameID} preserved', file=sys.stderr)
            continue
        if not original.strip():
            continue  # Optional fields may legitimately be empty.
        marker_only_style = record.nameID in {2, 17, 22} and original.strip() == '☞'
        cleaned = '' if marker_only_style else re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', strip_leading_manicule(original))
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(' .')
        if not cleaned and not marker_only_style:
            if record.nameID in usable_ids:
                # Preserve a broken localized record when another name is usable.
                continue
            raise ValueError('Normalization would leave an empty internal name')
        if cleaned != original:
            # Validate every record before modifying any of them.
            changes.append((record, original, cleaned, cleaned.encode(record.getEncoding())))
    for record, _, _, encoded in changes:
        record.string = encoded
    result = [(record.nameID, old, new) for record, old, new, _ in changes]
    if 'CFF ' in font:
        for top in font['CFF '].cff.topDictIndex:
            for field in ('FamilyName', 'FullName'):
                original = getattr(top, field, None)
                if isinstance(original, str):
                    cleaned = strip_leading_manicule(original)
                    if cleaned != original:
                        setattr(top, field, cleaned)
                        result.append((f'CFF.{field}', original, cleaned))
    return result


def normalize_retained_names(renamer):
    """Run after deduplication so only original byte identity removes files."""
    count = 0
    for ref in list(renamer.reserved.values()):
        if ref.path.suffix.lower() not in {'.ttf', '.otf'}:
            continue
        try:
            original = ref.read()
            with TTFont(io.BytesIO(original), recalcTimestamp=False) as font:
                changes = normalize_internal_names(font)
                if not changes:
                    continue
                print(f'INTERNAL:\n  FILE: {ref.path.name}\n  FOLDER: {ref.path.parent}')
                for name_id, old, new in changes:
                    print(f'  NAME ID {name_id}:\n    CURRENT: {old!r}\n    NEW: {new!r}')
                if renamer.apply:
                    # Editing metadata invalidates an existing digital signature.
                    if 'DSIG' in font:
                        del font['DSIG']
                    buffer = io.BytesIO()
                    font.save(buffer)
                    updated = buffer.getvalue()
                    # Ensure the saved result can be read before replacing a file.
                    with TTFont(io.BytesIO(updated)) as check:
                        if normalize_internal_names(check):
                            raise RuntimeError('Internal normalization did not round-trip')
                    backup = ref.path.with_name(ref.path.name + '.original.bak')
                    if backup.exists():
                        raise FileExistsError(f'Backup already exists; source kept: {backup}')
                    if ref.path.read_bytes() != original:
                        raise RuntimeError('Source changed; normalization skipped')
                    with backup.open('xb') as output:
                        output.write(original)
                    temp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(dir=ref.path.parent, delete=False) as output:
                            temp_path = Path(output.name)
                            output.write(updated)
                        if ref.path.read_bytes() != original:
                            raise RuntimeError('Source changed; normalization skipped')
                        os.replace(temp_path, ref.path)
                    finally:
                        if temp_path is not None:
                            temp_path.unlink(missing_ok=True)
            count += 1
        except Exception as exc:
            renamer.errors += 1
            print(f'ERROR (internal names kept): {ref.path}: {exc}')
    return count


def decode_name(record):
    try:
        return record.toUnicode().strip()
    except (UnicodeError, LookupError):
        raw = record.toBytes()
        encoding = chardet.detect(raw)['encoding']
        if not encoding:
            raise ValueError('Cannot decode font name')
        return raw.decode(encoding).strip()


def get_current_family_name(table):
    for name_id in PREFERRED_NAME_IDS:
        records = []
        for record in table.names:
            if record.nameID == name_id:
                try:
                    value = decode_name(record)
                    clean_name(value)  # Ignore placeholder records such as "????".
                    records.append((record, value))
                except (UnicodeError, LookupError, ValueError):
                    continue
        names = [name for _, name in records]
        latin_names = {name for name in names if not has_non_latin_letters(name)}
        native_names = []
        for record, name in records:
            if not has_non_latin_letters(name):
                continue
            # Some old fonts put ASCII bytes in a Unicode record. They decode
            # into spurious CJK characters. Trust the matching valid Latin record.
            raw_latin = record.toBytes().decode('ascii', errors='replace').strip()
            if record.isUnicode() and raw_latin in latin_names:
                continue
            native_names.append(name)
        if native_names:
            return sorted(native_names, key=lambda value: (len(value), value))[-1]
        for platform, encoding, language in PREFERRED_IDS:
            for record, value in records:
                if (record.platformID, record.platEncID, record.langID) == (platform, encoding, language) and value:
                    return value
        names = [name for name in names if name]
        if names:
            return sorted(names, key=len)[-1]
    raise ValueError('No usable internal font name')


def get_font_name(font):
    return get_current_family_name(font['name'])


def clean_name(name):
    name = strip_leading_manicule(name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    name = re.sub(r'\s+', ' ', name).strip(' .')
    if not name:
        raise ValueError('Internal name is empty after filename cleanup')
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', name, re.I):
        name = '_' + name
    return name


def has_non_latin_letters(name):
    return any(c.isalpha() and 'LATIN' not in unicodedata.name(c, '') for c in name)


def filename_stem(name, transliterate=True):
    original = clean_name(name)
    if transliterate and has_non_latin_letters(original):
        latin = clean_name(anyascii(original))
        if latin != original:
            native = truncate_name(original, 86)
            latin = truncate_name(latin, 180 - len(native.encode('utf-16-le')) // 2 - 3)
            stem = f'{latin} [{native}]'
        else:
            stem = original
    else:
        stem = original
    while len(stem.encode('utf-16-le')) // 2 > 180:
        stem = stem[:-1]
    return stem.rstrip(' .')


def truncate_name(value, limit):
    while len(value.encode('utf-16-le')) // 2 > limit:
        value = value[:-1]
    return value.rstrip(' .')


def fit_stem(value, limit):
    """Shorten a bilingual stem without cutting its native-name brackets."""
    if value.endswith(']') and ' [' in value:
        latin, native = value.rsplit(' [', 1)
        native = truncate_name(native[:-1], max(1, (limit - 3) // 2))
        latin = truncate_name(latin, limit - len(native.encode('utf-16-le')) // 2 - 3)
        return f'{latin} [{native}]'
    return truncate_name(value, limit)


def normalized_bytes(original):
    with TTFont(io.BytesIO(original), recalcTimestamp=False) as font:
        if not normalize_internal_names(font):
            return original
        if 'DSIG' in font:
            del font['DSIG']
        output = io.BytesIO()
        font.save(output)
        return output.getvalue()


def same_path(left, right):
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def identification_metadata(data, transliterate=True):
    """Optional descriptive fields; missing or damaged metadata is harmless."""
    result = {}
    try:
        with TTFont(io.BytesIO(data), lazy=True) as font:
            for key, ids in (('style', (17, 2)), ('version', (5,)),
                             ('maker', (8,)), ('postscript', (6,)), ('id', (3,))):
                for name_id in ids:
                    records = [r for r in font['name'].names if r.nameID == name_id]
                    records.sort(key=lambda r: (r.langID != 0x0409, r.platformID != 3,
                                               r.platformID, r.platEncID, r.langID))
                    for record in records:
                        try:
                            value = decode_name(record)
                            if key == 'version':
                                match = re.search(r'(?i)\b(?:version\s*)?(\d+(?:\.\d+)+)', value)
                                value = 'v' + match.group(1) if match else value
                            result[key] = fit_stem(filename_stem(value, transliterate), 70)
                            break
                        except (UnicodeError, LookupError, ValueError):
                            continue
                    if key in result:
                        break
            if 'OS/2' in font:
                result['weight'] = f'weight {font["OS/2"].usWeightClass}'
                result['width'] = f'width {font["OS/2"].usWidthClass}'
    except Exception:
        pass
    return result


@dataclass
class Retained:
    path: Path
    original_path: Path
    data: bytes | None = None

    def read(self):
        return self.data if self.data is not None else self.original_path.read_bytes()


class Renamer:
    def __init__(self, apply=False, keep_duplicates=False, transliterate=True):
        self.apply = apply
        self.keep_duplicates = keep_duplicates
        self.transliterate = transliterate
        self.seen = {}
        self.reserved = {}
        self.metadata = {}
        self.normalized_origins = {}
        self.changed = self.duplicates = self.errors = 0

    def key(self, path):
        return os.path.normcase(str(path))

    def remember(self, data, reference):
        self.seen.setdefault(hashlib.sha256(data).digest(), []).append(reference)
        self.reserved[self.key(reference.path)] = reference

    def duplicate(self, data):
        if not self.keep_duplicates:
            for ref in self.seen.get(hashlib.sha256(data).digest(), []):
                # Hash narrows candidates; byte-for-byte comparison decides.
                if ref.read() == data:
                    return ref
        return None

    def original_before_normalization(self, path, data):
        """Trust a backup only if normalizing it reproduces the current bytes."""
        key = (self.key(path), hashlib.sha256(data).digest())
        if key not in self.normalized_origins:
            original = data
            backup = path.with_name(path.name + '.original.bak')
            if backup.is_file() and not backup.is_symlink():
                try:
                    candidate = backup.read_bytes()
                    if normalized_bytes(candidate) == data:
                        original = candidate
                except Exception:
                    pass
            self.normalized_origins[key] = original
        return self.normalized_origins[key]

    def destination(self, source, stem, extension, data, identity_data=None, extraction=False):
        number = 1
        labels = []
        digest = hashlib.sha256(data if identity_data is None else identity_data).hexdigest()
        def metadata(blob):
            key = hashlib.sha256(blob).digest()
            if key not in self.metadata:
                self.metadata[key] = identification_metadata(blob, self.transliterate)
            return self.metadata[key]
        while True:
            suffix = (' [' + '; '.join(labels) + ']') if labels else ''
            if number > 1:
                suffix += f' ({number})'
            base = fit_stem(stem, 180 - len(suffix.encode('utf-16-le')) // 2)
            target = source.parent / f'{base}{suffix}{extension}'
            ref = self.reserved.get(self.key(target))
            occupant = None
            if ref is not None:
                occupant = ref.read()
                if not self.keep_duplicates and occupant == data:
                    return target, ref
                if extraction and self.original_before_normalization(ref.original_path, occupant) == data:
                    return target, ref
            elif same_path(target, source):
                return target, None
            elif not target.exists():
                return target, None
            elif not target.is_symlink() and target.is_file():
                occupant = target.read_bytes()
                if not self.keep_duplicates and occupant == data:
                    return target, Retained(target, target)
                if extraction and self.original_before_normalization(target, occupant) == data:
                    return target, Retained(target, target)
            own = metadata(data)
            other = metadata(occupant) if occupant is not None else {}
            extra = None
            if occupant is not None and occupant != data:
                for key in ('style', 'version', 'maker', 'weight', 'width', 'postscript', 'id'):
                    value = own.get(key)
                    if (value and other.get(key) and value.casefold() != other[key].casefold()
                            and value not in labels):
                        extra = value
                        break
            if extra and len(labels) < 2:
                labels.append(extra)
            elif not any(label.startswith('sha256 ') for label in labels):
                labels.append('sha256 ' + digest[:12])
            else:
                # Exact copies deliberately retained, or a hash/name collision.
                number += 1

    def remove_duplicate(self, source, data, ref):
        if same_path(source, ref.path):
            return
        print(f'DUPLICATE:\n  CURRENT: {source.name}\n  FOLDER: {source.parent}\n  KEEP: {ref.path}')
        if self.apply:
            if source.read_bytes() != data or ref.read() != data:
                raise RuntimeError('File changed since comparison; duplicate kept')
            source.unlink()
        self.duplicates += 1

    def rename_font(self, source):
        data = source.read_bytes()
        with TTFont(io.BytesIO(data), lazy=True) as font:
            stem = filename_stem(get_font_name(font), self.transliterate)
        ref = self.duplicate(data)
        if ref is not None:
            self.remove_duplicate(source, data, ref)
            return
        identity = self.original_before_normalization(source, data)
        target, ref = self.destination(source, stem, source.suffix.lower(), data, identity)
        if ref is not None:
            self.remember(data, ref)
            self.remove_duplicate(source, data, ref)
            return
        if not same_path(source, target):
            print(f'RENAME:\n  CURRENT: {source.name!r}\n  NEW: {target.name!r}\n  FOLDER: {source.parent}')
            if self.apply:
                if source.read_bytes() != data:
                    raise RuntimeError('Source changed since inspection; rename skipped')
                if os.name == 'nt':
                    # Windows rename refuses to overwrite an existing target.
                    source.rename(target)
                else:
                    # POSIX rename can overwrite: create the destination exclusively.
                    os.link(source, target)
                    source.unlink()
            self.changed += 1
        self.remember(data, Retained(target, target if self.apply else source))

    def unpack_collection(self, source):
        collection_data = source.read_bytes()
        ref = self.duplicate(collection_data)
        if ref is not None:
            self.remove_duplicate(source, collection_data, ref)
            return
        with TTCollection(io.BytesIO(collection_data), recalcTimestamp=False) as collection:
            for index, font in enumerate(collection.fonts):
                try:
                    self.extract_member(source, font)
                except Exception as exc:
                    self.errors += 1
                    print(f'ERROR (collection member {index} kept in original): {source}: {exc}')
        self.remember(collection_data, Retained(source, source))

    def extract_member(self, source, font):
        stem = filename_stem(get_font_name(font), self.transliterate)
        extension = '.otf' if 'CFF ' in font or 'CFF2' in font else '.ttf'
        buffer = io.BytesIO()
        font.save(buffer)
        data = buffer.getvalue()
        ref = self.duplicate(data)
        if ref is not None:
            print(f'EXTRACT SKIPPED (identical): {source} -> {ref.path}')
            return
        target, ref = self.destination(source, stem, extension, data, extraction=True)
        if ref is not None:
            self.remember(data, ref)
            print(f'EXTRACT SKIPPED (already present): {source} -> {target}')
            return
        print(f'EXTRACT:\n  COLLECTION: {source.name}\n  NEW: {target.name}\n  FOLDER: {source.parent}\n  Collection retained.')
        if self.apply:
            with target.open('xb') as output:
                output.write(data)
        self.remember(data, Retained(target, target, None if self.apply else data))
        self.changed += 1

    def process(self, paths):
        for source in paths:
            try:
                if source.suffix.lower() in {'.ttc', '.otc'}:
                    self.unpack_collection(source)
                else:
                    self.rename_font(source)
            except Exception as exc:
                self.errors += 1
                print(f'ERROR (kept): {source}: {exc}')


def collect_files(paths):
    files = set()
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
        candidates = path.rglob('*') if path.is_dir() else [path]
        for candidate in candidates:
            if (candidate.is_file() and not candidate.is_symlink()
                    and candidate.suffix.lower() in FONT_SUFFIXES
                    and not any(part.startswith('.') for part in candidate.parts)):
                files.add(candidate.resolve())
    # Snapshot before mutation; extraction and renames do not affect traversal.
    return sorted(files, key=lambda path: str(path).casefold())


def main(argv=None):
    # Windows redirected stdout otherwise defaults to a legacy code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description='Rename fonts using internal names. Preview by default; distinct files are never overwritten.')
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--apply', action='store_true', help='Apply renames, extraction and removal of byte-identical duplicates')
    parser.add_argument('--keep-duplicates', action='store_true', help='Keep byte-identical duplicates with numbered filenames')
    parser.add_argument('--no-transliterate', action='store_true', help='Use only the original internal name')
    parser.add_argument('--dry-run', action='store_true', help='Explicit preview; cannot be combined with --apply')
    parser.add_argument('--normalize-internal', action='store_true', help='Also clean internal display names; --apply creates .original.bak backups')
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error('--apply and --dry-run cannot be combined')
    try:
        files = collect_files(args.files)
    except OSError as exc:
        parser.error(str(exc))
    renamer = Renamer(args.apply, args.keep_duplicates, not args.no_transliterate)
    print('APPLY' if args.apply else 'PREVIEW: no files will be changed. Use --apply to apply.')
    renamer.process(files)
    if args.normalize_internal:
        normalized = normalize_retained_names(renamer)
        print(f'{normalized} fonts with internal name changes.')
    print(f'{renamer.changed} changes, {renamer.duplicates} byte-identical duplicates, {renamer.errors} errors.')
    return 1 if renamer.errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
