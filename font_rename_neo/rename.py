#!/usr/bin/env python3
"""Rename font files safely, preserving distinct contents and Unicode names."""
import argparse
import contextlib
import logging
import json
from datetime import datetime
import hashlib
import io
import os
import re
import sys
import struct
import tempfile
import unicodedata
from collections import OrderedDict
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


@dataclass(frozen=True)
class FileDates:
    accessed_ns: int
    modified_ns: int
    created_ns: int | None

    @classmethod
    def capture(cls, path):
        info = path.stat()
        return cls(info.st_atime_ns, info.st_mtime_ns,
                   getattr(info, 'st_birthtime_ns', None))

    def restore(self, path):
        if os.name != 'nt':
            os.utime(path, ns=(self.accessed_ns, self.modified_ns))
            return
        # utime cannot restore Windows creation time. Set all three FILETIMEs
        # on a closed output file before replacing the original atomically.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
        kernel.SetFileTime.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        def filetime(ns):
            ticks = ns // 100 + 116444736000000000
            return wintypes.FILETIME(ticks & 0xffffffff, ticks >> 32)
        created = filetime(self.created_ns) if self.created_ns is not None else None
        accessed, modified = filetime(self.accessed_ns), filetime(self.modified_ns)
        handle = kernel.CreateFileW(str(path.resolve()), 0x100, 7, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.SetFileTime(handle, ctypes.byref(created) if created else None,
                                      ctypes.byref(accessed), ctypes.byref(modified)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(handle)


def local_timestamp():
    return datetime.now().isoformat(sep=' ', timespec='milliseconds')


class TimestampedLog:
    """Prefix physical log lines, even when print writes text and newline separately."""
    def __init__(self, logfile):
        self.logfile = logfile
        self.line_start = True

    def write(self, text):
        for part in text.splitlines(keepends=True):
            if self.line_start:
                self.logfile.write(f'[{local_timestamp()}] ')
            self.logfile.write(part)
            self.line_start = part.endswith('\n')
        return len(text)

    def flush(self):
        self.logfile.flush()


class TeeOutput:
    def __init__(self, console, logfile):
        self.console = console
        self.logfile = logfile

    def write(self, text):
        self.logfile.write(text)
        self.logfile.flush()
        self.console.write(text)
        self.console.flush()
        return len(text)

    def flush(self):
        self.logfile.flush()
        self.console.flush()


@contextlib.contextmanager
def text_log(logfile, arguments):
    timestamp = local_timestamp
    timestamped_log = TimestampedLog(logfile)
    with contextlib.redirect_stdout(TeeOutput(sys.stdout, timestamped_log)), contextlib.redirect_stderr(TeeOutput(sys.stderr, timestamped_log)):
        print(f'\n=== START {timestamp()} ===')
        print('ARGUMENTS: ' + json.dumps(arguments, ensure_ascii=False))
        try:
            yield
        except BaseException as exc:
            print(f'=== END {timestamp()} | FAILED: {type(exc).__name__}: {exc} ===', file=sys.stderr)
            raise
        else:
            print(f'=== END {timestamp()} ===')


class FontDiagnosticHandler(logging.Handler):
    def __init__(self, path):
        super().__init__(logging.WARNING)
        self.path = path

    def emit(self, record):
        print(f'{record.levelname} (fontTools): {record.getMessage()}\n  FILE: {self.path}',
              file=sys.stderr, flush=True)


@contextlib.contextmanager
def font_diagnostics(path):
    """Attach the current font to library diagnostics without duplicate output."""
    logger = logging.getLogger('fontTools')
    previous = (logger.handlers[:], logger.level, logger.propagate)
    handler = FontDiagnosticHandler(path)
    logger.handlers = [handler]
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        yield
    finally:
        logger.handlers, logger.level, logger.propagate = previous
        handler.close()


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


def ambiguous_legacy_name(record, records):
    """Do not edit mislabeled Mac Roman bytes alongside native Unicode names."""
    if record.platformID != 1 or record.platEncID != 0 or not any(byte >= 128 for byte in record.toBytes()):
        return False
    for other in records:
        if other.platformID == 1 or other.nameID != record.nameID:
            continue
        try:
            text = other.toUnicode()
        except (UnicodeError, LookupError):
            continue
        if any(ord(char) > 0x02FF for char in text):
            return True
    return False


def empty_name_padding(font):
    """Identify redundant zero padding in a fully valid format-0 name table."""
    if font.reader is None or 'name' not in font.reader:
        return None
    raw = font.reader['name']
    if len(raw) < 6:
        return None
    version, count, offset = struct.unpack('>HHH', raw[:6])
    expected = 6 + count * 12
    if version != 0 or not expected < offset <= len(raw) or any(raw[expected:offset]):
        return None
    for index in range(count):
        length, start = struct.unpack_from('>HH', raw, 6 + index * 12 + 8)
        if offset + start + length > len(raw):
            return None
    return offset, expected


def usable_style(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', strip_leading_manicule(value))
    value = re.sub(r'\s+', ' ', value).strip(' .')
    return value if any(char.isalnum() for char in value) and value.casefold() != 'unknown' else None


def recover_style(font, record):
    # Same ID preserves the distinction between legacy and typographic styles.
    for language in (record.langID, 0x0409, 0):
        candidates = {}
        for other in font['name'].names:
            if other is record or other.nameID != record.nameID or other.langID != language:
                continue
            if ambiguous_legacy_name(other, font['name'].names):
                continue
            try:
                value = usable_style(other.toUnicode())
            except (UnicodeError, LookupError):
                continue
            if value:
                candidates[value.casefold()] = value
        if len(candidates) == 1:
            return next(iter(candidates.values())), 'valid matching style record'
        if len(candidates) > 1:
            return 'Unknown', 'conflicting style records'
    if 'fvar' in font:
        return 'Unknown', 'variable font requires explicit style evidence'
    if 'OS/2' not in font or 'head' not in font:
        return 'Unknown', 'missing style metadata'
    os2, head = font['OS/2'], font['head']
    bold = bool(os2.fsSelection & 32)
    italic = bool(os2.fsSelection & (1 | 512))
    if (bold != bool(head.macStyle & 1) or italic != bool(head.macStyle & 2)
            or (os2.fsSelection & 64 and (bold or italic))):
        return 'Unknown', 'conflicting OS/2 and head style flags'
    if record.nameID == 2:
        value = ('Bold Italic' if italic else 'Bold') if bold else ('Italic' if italic else 'Regular')
        return value, 'consistent OS/2 and head legacy style flags'
    weights = {100: 'Thin', 200: 'ExtraLight', 300: 'Light', 400: 'Regular',
               500: 'Medium', 600: 'SemiBold', 700: 'Bold', 800: 'ExtraBold', 900: 'Black'}
    widths = {1: 'UltraCondensed', 2: 'ExtraCondensed', 3: 'Condensed', 4: 'SemiCondensed',
              5: '', 6: 'SemiExpanded', 7: 'Expanded', 8: 'ExtraExpanded', 9: 'UltraExpanded'}
    weight, width = os2.usWeightClass, os2.usWidthClass
    if weight not in weights or width not in widths or (bold and weight < 700):
        return 'Unknown', 'missing or conflicting weight/width metadata'
    # Recognizable full/PostScript suffixes must agree with the numeric weight.
    suffix_weights = {'Thin': 100, 'ExtraLight': 200, 'UltraLight': 200, 'Light': 300,
                      'Regular': 400, 'Normal': 400, 'Medium': 500, 'SemiBold': 600,
                      'DemiBold': 600, 'Bold': 700, 'ExtraBold': 800, 'UltraBold': 800,
                      'Black': 900, 'Heavy': 900}
    tokens = sorted(suffix_weights, key=len, reverse=True)
    pattern = r'(' + '|'.join(tokens) + r')(Italic|Oblique)?$'
    for other in font['name'].names:
        if other.nameID not in {4, 6} or ambiguous_legacy_name(other, font['name'].names):
            continue
        try:
            name = re.sub(r'[\s_-]', '', other.toUnicode())
        except (UnicodeError, LookupError):
            continue
        match = re.search(pattern, name, re.IGNORECASE)
        if match:
            token = next(token for token in tokens if token.casefold() == match.group(1).casefold())
            if suffix_weights[token] != weight or bool(match.group(2)) != italic:
                return 'Unknown', 'name suffix conflicts with numeric style metadata'
    parts = [widths[width], weights[weight]]
    if italic:
        if parts[-1] == 'Regular':
            parts.pop()
        parts.append('Oblique' if os2.fsSelection & 512 else 'Italic')
    return ' '.join(part for part in parts if part), 'consistent weight, width and style metadata'


def normalize_internal_names(font):
    """Clean display names, preserving technical IDs and localized encodings."""
    padding = empty_name_padding(font)
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
            logging.getLogger('fontTools').warning('Undecodable internal name record %s preserved', record.nameID)
            continue
        if ambiguous_legacy_name(record, font['name'].names):
            logging.getLogger('fontTools').warning(
                'Ambiguous legacy name encoding preserved: nameID=%s platform=%s encoding=%s language=0x%04X',
                record.nameID, record.platformID, record.platEncID, record.langID)
            continue
        if not original.strip():
            continue  # Optional fields may legitimately be empty.
        marker_only_style = record.nameID in {2, 17, 22} and original.strip() == '☞'
        cleaned = '' if marker_only_style else re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', strip_leading_manicule(original))
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(' .')
        if record.nameID in {2, 17, 22} and cleaned.casefold() != 'unknown' and not usable_style(cleaned):
            cleaned, reason = recover_style(font, record)
            try:
                cleaned.encode(record.getEncoding())
            except (UnicodeError, LookupError):
                cleaned, reason = 'Unknown', 'recovered style cannot be encoded in this record'
            print(f'STYLE RECOVERY: NAME ID {record.nameID}: {original!r} -> {cleaned!r}; {reason}')
        if not cleaned:
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
    if padding is not None:
        old, new = padding
        result.append(('name.layout', f'stringOffset={old}; {old-new} zero padding bytes',
                       f'stringOffset={new}; no padding'))
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


def backup_path(path):
    return path.parent / 'BAK' / (path.name + '.original.bak')


def normalize_retained_names(renamer):
    """Run after deduplication so only original byte identity removes files."""
    count = 0
    for ref in list(renamer.reserved.values()):
        if renamer.should_cancel():
            break
        if ref.path.suffix.lower() not in {'.ttf', '.otf'}:
            continue
        try:
            original_dates = FileDates.capture(ref.path) if renamer.apply else None
            original = ref.read()
            with font_diagnostics(ref.path), TTFont(io.BytesIO(original), recalcTimestamp=False) as font:
                changes = normalize_internal_names(font)
                if not changes:
                    continue
                print(f'INTERNAL:\n  FILE: {ref.path.name}\n  FOLDER: {ref.path.parent}')
                for name_id, old, new in changes:
                    label = f'NAME ID {name_id}' if isinstance(name_id, int) else f'TABLE {name_id}'
                    print(f'  {label}:\n    CURRENT: {old!r}\n    NEW: {new!r}')
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
                    backup = backup_path(ref.path)
                    legacy_backup = ref.path.with_name(ref.path.name + '.original.bak')
                    if backup.parent.is_symlink():
                        raise ValueError(f'Backup folder is a symlink: {backup.parent}')
                    if backup.exists() or backup.is_symlink() or legacy_backup.exists() or legacy_backup.is_symlink():
                        raise FileExistsError(f'Backup already exists; source kept: {backup}')
                    if ref.path.read_bytes() != original:
                        raise RuntimeError('Source changed; normalization skipped')
                    backup.parent.mkdir(exist_ok=True)
                    with backup.open('xb') as output:
                        output.write(original)
                    original_dates.restore(backup)
                    temp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(dir=ref.path.parent, delete=False) as output:
                            temp_path = Path(output.name)
                            output.write(updated)
                        original_dates.restore(temp_path)
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


def decoded_name_with_quality(record):
    """Declared-encoding names outrank guesses, including longer guesses."""
    try:
        value = record.toUnicode().strip()
        return value, '\x00' not in value
    except (UnicodeError, LookupError):
        raw = record.toBytes()
        encoding = chardet.detect(raw)['encoding']
        if not encoding:
            raise ValueError('Cannot decode font name')
        return raw.decode(encoding).strip(), False


def decode_name(record):
    return decoded_name_with_quality(record)[0]


def get_current_family_name(table):
    for name_id in PREFERRED_NAME_IDS:
        records = []
        for record in table.names:
            if record.nameID == name_id:
                try:
                    value, reliable = decoded_name_with_quality(record)
                    clean_name(value)  # Ignore placeholder records such as "????".
                    records.append((record, value, reliable))
                except (UnicodeError, LookupError, ValueError):
                    continue
        # An encoding guess can produce plausible-looking Latin symbols and
        # win the old longest-name heuristic over a valid localized record.
        reliable_records = [(record, value) for record, value, reliable in records if reliable]
        records = reliable_records or [(record, value) for record, value, _ in records]
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
    return portable_stem(stem.rstrip(' .'), 180, 235)


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


def portable_stem(value, utf16_limit, utf8_limit):
    """Respect Windows UTF-16 and POSIX byte limits, reserving backup space."""
    limit = utf16_limit
    result = fit_stem(value, limit)
    while len(result.encode('utf-8')) > utf8_limit:
        limit -= 1
        if limit < 4:
            raise ValueError('Not enough filename space for this collision suffix')
        result = fit_stem(value, limit)
    return result


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
                            result[key] = portable_stem(filename_stem(value, transliterate), 70, 70)
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
    def __init__(self, apply=False, keep_duplicates=False, transliterate=True, cancel_requested=None):
        self.cancel_requested = cancel_requested
        self.cancelled = False
        self.apply = apply
        self.keep_duplicates = keep_duplicates
        self.transliterate = transliterate
        self.seen = {}
        self.reserved = {}
        self.metadata = {}
        self.normalized_origins = OrderedDict()
        self.normalized_origin_bytes = 0
        self.changed = self.duplicates = self.errors = 0

    def should_cancel(self):
        if self.cancelled or (self.cancel_requested is not None and self.cancel_requested()):
            self.cancelled = True
            return True
        return False

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
        """Trust validated backups; bound caching instead of retaining the collection."""
        key = (self.key(path), hashlib.sha256(data).digest())
        if key in self.normalized_origins:
            original = self.normalized_origins[key]
            self.normalized_origins.move_to_end(key)
            return data if original is None else original
        original = None
        for backup in (backup_path(path), path.with_name(path.name + '.original.bak')):
            if backup.is_file() and not backup.is_symlink() and not backup.parent.is_symlink():
                try:
                    candidate = backup.read_bytes()
                    if normalized_bytes(candidate) == data:
                        original = candidate
                        break
                except Exception:
                    pass
        # No backup: retain only a sentinel, never the input font's bytes.
        size = len(original) if original is not None else 0
        limit = 16 * 1024 * 1024
        if size <= limit:
            while self.normalized_origins and (len(self.normalized_origins) >= 64
                                               or self.normalized_origin_bytes + size > limit):
                _, evicted = self.normalized_origins.popitem(last=False)
                self.normalized_origin_bytes -= len(evicted) if evicted is not None else 0
            self.normalized_origins[key] = original
            self.normalized_origin_bytes += size
        return data if original is None else original

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
            base = portable_stem(stem, 180 - len(suffix.encode('utf-16-le')) // 2,
                                 235 - len(suffix.encode('utf-8')))
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
        source_dates = FileDates.capture(source) if self.apply else None
        collection_data = source.read_bytes()
        ref = self.duplicate(collection_data)
        if ref is not None:
            self.remove_duplicate(source, collection_data, ref)
            return
        with TTCollection(io.BytesIO(collection_data), recalcTimestamp=False) as collection:
            for index, font in enumerate(collection.fonts):
                if self.should_cancel():
                    break
                try:
                    self.extract_member(source, font, source_dates)
                except Exception as exc:
                    self.errors += 1
                    print(f'ERROR (collection member {index} kept in original): {source}: {exc}')
        self.remember(collection_data, Retained(source, source))

    def extract_member(self, source, font, source_dates=None):
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
            try:
                (source_dates or FileDates.capture(source)).restore(target)
            except Exception:
                target.unlink()
                raise
        self.remember(data, Retained(target, target, None if self.apply else data))
        self.changed += 1

    def process(self, paths):
        for source in paths:
            if self.should_cancel():
                break
            try:
                with font_diagnostics(source):
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
                    and not any(part.startswith('.') or part.casefold() == 'bak' for part in candidate.parts)):
                files.add(candidate.resolve())
    # Snapshot before mutation; extraction and renames do not affect traversal.
    return sorted(files, key=lambda path: str(path).casefold())


def main(argv=None):
    # Windows redirected stdout otherwise defaults to a legacy code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(prog='font-rename-neo', description='Rename fonts using internal names. Preview by default; distinct files are never overwritten.')
    from importlib.metadata import version, PackageNotFoundError
    try:
        installed_version = version('font-rename-neo')
    except PackageNotFoundError:
        installed_version = '0.4.0'
    parser.add_argument('--version', action='version', version=f'Font Rename Neo {installed_version} (font_rename_neo)')
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--apply', action='store_true', help='Apply renames, extraction and removal of byte-identical duplicates')
    parser.add_argument('--keep-duplicates', action='store_true', help='Keep byte-identical duplicates with numbered filenames')
    parser.add_argument('--no-transliterate', action='store_true', help='Use only the original internal name')
    parser.add_argument('--dry-run', action='store_true', help='Explicit preview; cannot be combined with --apply')
    parser.add_argument('--normalize-internal', action='store_true', help='Also clean internal display names; --apply creates backups in a BAK subfolder')
    parser.add_argument('--log', nargs='?', type=Path,
                        const=Path(f'font_rename_neo[{datetime.now().astimezone().date().isoformat()}].log'),
                        metavar='PATH',
                        help='Append all output to a UTF-8 log; without PATH use font_rename_neo[YYYY-MM-DD].log in the current folder')
    parser.add_argument('--cancel-file', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error('--apply and --dry-run cannot be combined')
    if args.log is not None:
        if args.log.suffix.lower() in FONT_SUFFIXES or args.log.name.lower().endswith('.original.bak') or args.log.is_symlink():
            parser.error('--log must not point to a font, normalization backup or symlink')
        if any(same_path(args.log, path) for path in args.files):
            parser.error('--log must differ from the input paths')
        try:
            logfile = args.log.open('a', encoding='utf-8')
        except OSError as exc:
            parser.error(f'Cannot open log; no fonts changed: {exc}')
        with logfile, text_log(logfile, list(argv) if argv is not None else sys.argv[1:]):
            result = run_command(args, parser)
            print(f'EXIT STATUS: {result}')
            return result
    return run_command(args, parser)


def run_command(args, parser):
    try:
        files = collect_files(args.files)
    except OSError as exc:
        parser.error(str(exc))
    cancel_requested = args.cancel_file.exists if args.cancel_file is not None else None
    renamer = Renamer(args.apply, args.keep_duplicates, not args.no_transliterate, cancel_requested)
    print('APPLY' if args.apply else 'PREVIEW: no files will be changed. Use --apply to apply.')
    renamer.process(files)
    if args.normalize_internal and not renamer.should_cancel():
        normalized = normalize_retained_names(renamer)
        print(f'{normalized} fonts with internal name changes.')
    print(f'{renamer.changed} changes, {renamer.duplicates} byte-identical duplicates, {renamer.errors} errors.')
    if renamer.cancelled:
        print('CANCELLED: stopped between fonts; completed changes remain.')
        return 130
    return 1 if renamer.errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
