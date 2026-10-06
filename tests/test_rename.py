import contextlib
import io
import logging
import os
import struct
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTCollection, TTFont
from font_rename_fm.rename import (Renamer, clean_name, collect_files,
                                  filename_stem, get_font_name, main)
from font_rename_fm.rename import normalize_internal_names, normalize_retained_names, FileDates


def make_font(path, name='Test Regular', width=500):
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(['.notdef'])
    fb.setupCharacterMap({})
    fb.setupGlyf({'.notdef': TTGlyphPen(None).glyph()})
    fb.setupHorizontalMetrics({'.notdef': (width, 0)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({'familyName': name, 'styleName': 'Regular',
                      'fullName': name, 'psName': 'Test-Regular'})
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200,
               usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    fb.save(path)


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.log = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.log)
        self.redirect.__enter__()

    def tearDown(self):
        self.redirect.__exit__(None, None, None)
        self.temp.cleanup()

    def run_renamer(self, apply=True, **kwargs):
        engine = Renamer(apply=apply, **kwargs)
        engine.process(collect_files([self.folder]))
        self.assertEqual(engine.errors, 0, self.log.getvalue())
        return engine

    def test_malformed_name_warning_identifies_source(self):
        path = self.folder / 'malformed.ttf'
        make_font(path)
        data = bytearray(path.read_bytes())
        with TTFont(path, lazy=True) as font:
            offset = font.reader.tables['name'].offset
        # Corrupt the first record's string length; other names remain usable.
        struct.pack_into('>H', data, offset + 6 + 8, 65535)
        path.write_bytes(data)
        errors = io.StringIO()
        logger = logging.getLogger('fontTools')
        previous = (logger.handlers[:], logger.level, logger.propagate)
        with contextlib.redirect_stderr(errors):
            engine = self.run_renamer(apply=False)
        self.assertEqual(engine.errors, 0)
        self.assertIn('skipping malformed name record #0', errors.getvalue())
        self.assertIn(f'FILE: {path.resolve()}', errors.getvalue())
        self.assertEqual(errors.getvalue().count('skipping malformed name record #0'), 1)
        self.assertEqual((logger.handlers, logger.level, logger.propagate), previous)
        self.assertEqual(path.read_bytes(), bytes(data))

    def historic_dates(self, path):
        dates = FileDates(946684800123456700, 978307200765432100,
                          915148800345678900 if os.name == 'nt' else None)
        dates.restore(path)
        return FileDates.capture(path)

    def assert_original_dates(self, path, dates):
        current = FileDates.capture(path)
        self.assertEqual(current.modified_ns, dates.modified_ns)
        self.assertEqual(current.created_ns, dates.created_ns)

    def test_rename_preserves_original_file_dates(self):
        path = self.folder/'old.ttf'
        make_font(path)
        dates = self.historic_dates(path)
        self.run_renamer()
        self.assert_original_dates(self.folder/'Test Regular.ttf',dates)

    def test_internal_normalization_preserves_dates_and_backup_dates(self):
        path = self.folder/'old.ttf'
        make_font(path, ' Test Regular ')
        with TTFont(path, recalcTimestamp=False) as font:
            head_dates = font['head'].created, font['head'].modified
        dates = self.historic_dates(path)
        engine = self.run_renamer()
        self.assertEqual(normalize_retained_names(engine),1)
        target = self.folder/'Test Regular.ttf'
        self.assert_original_dates(target,dates)
        self.assert_original_dates(self.folder/'BAK'/'Test Regular.ttf.original.bak',dates)
        with TTFont(target) as font:
            self.assertEqual((font['head'].created,font['head'].modified),head_dates)

    def test_extracted_fonts_inherit_collection_dates(self):
        member = self.folder/'member.ttf'
        make_font(member)
        source = self.folder/'collection.ttc'
        collection=TTCollection()
        collection.fonts=[TTFont(member)]
        collection.save(source)
        collection.close()
        member.unlink()
        dates=self.historic_dates(source)
        self.run_renamer()
        self.assert_original_dates(source,dates)
        self.assert_original_dates(self.folder/'Test Regular.ttf',dates)

    def test_date_restore_failure_keeps_original_font(self):
        path=self.folder/'old.ttf'
        make_font(path,' Test Regular ')
        engine=self.run_renamer()
        target=self.folder/'Test Regular.ttf'
        before=target.read_bytes()
        with patch.object(FileDates,'restore',side_effect=OSError('date restoration failed')):
            self.assertEqual(normalize_retained_names(engine),0)
        self.assertEqual(target.read_bytes(),before)
        self.assertEqual(engine.errors,1)

    def test_normalization_warning_identifies_retained_font(self):
        path = self.folder / 'Test Regular.ttf'
        make_font(path)
        engine = self.run_renamer(apply=False)
        errors = io.StringIO()
        def diagnostic(font):
            logging.getLogger('fontTools.ttLib.tables._n_a_m_e').warning('Name diagnostic')
            return []
        with contextlib.redirect_stderr(errors), patch('font_rename_fm.rename.normalize_internal_names', side_effect=diagnostic):
            normalize_retained_names(engine)
        self.assertIn('Name diagnostic', errors.getvalue())
        self.assertIn(f'FILE: {path.resolve()}', errors.getvalue())

    def test_log_captures_unicode_console_and_diagnostics_and_appends(self):
        path = self.folder / 'source.ttf'
        make_font(path, '黑体')
        logfile = self.folder / 'session.log'
        logfile.write_text('PREVIOUS SESSION\n', encoding='utf-8')
        errors = io.StringIO()
        from font_rename_fm.rename import get_font_name as original_name
        def diagnostic(font):
            logging.getLogger('fontTools.ttLib').warning('Diagnostic 中文')
            return original_name(font)
        before = path.read_bytes()
        with contextlib.redirect_stderr(errors), patch('font_rename_fm.rename.get_font_name', side_effect=diagnostic):
            self.assertEqual(main([str(path), '--log', str(logfile)]), 0)
        log = logfile.read_text(encoding='utf-8')
        self.assertTrue(log.startswith('PREVIOUS SESSION\n'))
        for expected in ('=== START ', '=== END ', 'RENAME:', '黑体', 'Diagnostic 中文', f'FILE: {path.resolve()}', 'EXIT STATUS: 0'):
            self.assertIn(expected, log)
        self.assertIn('Diagnostic 中文', errors.getvalue())
        self.assertIn('RENAME:', self.log.getvalue())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(main([str(path), '--log', str(logfile)]), 0)
        self.assertEqual(logfile.read_text(encoding='utf-8').count('=== START '), 2)

    def test_unwritable_log_stops_before_font_changes(self):
        path = self.folder / 'source.ttf'
        make_font(path)
        before = path.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main([str(path), '--apply', '--log', str(self.folder/'absent'/'session.log')])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.folder/'Test Regular.ttf').exists())

    def test_log_rejects_font_path_without_modifying_it(self):
        path = self.folder / 'source.ttf'
        make_font(path)
        before = path.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main([str(path), '--apply', '--log', str(path)])
        self.assertEqual(path.read_bytes(), before)

    def test_log_records_input_errors_and_closes_session(self):
        logfile = self.folder / 'session.log'
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main([str(self.folder/'absent'), '--log', str(logfile)])
        log = logfile.read_text(encoding='utf-8')
        self.assertIn('error:', log)
        self.assertIn('=== END ', log)
        self.assertIn('FAILED: SystemExit', log)

    def test_backup_folder_is_skipped_even_with_font_extensions(self):
        make_font(self.folder / 'source.ttf')
        folder = self.folder / 'bak'
        folder.mkdir()
        make_font(folder / 'original.ttf')
        self.assertEqual(collect_files([self.folder]), [(self.folder / 'source.ttf').resolve()])
        self.assertEqual(collect_files([folder]), [])

    def test_existing_backup_in_bak_blocks_normalization(self):
        path = self.folder / 'Test.ttf'
        make_font(path, ' *Test ')
        original = path.read_bytes()
        folder = self.folder / 'BAK'
        folder.mkdir()
        backup = folder / 'Test.ttf.original.bak'
        backup.write_bytes(b'previous backup')
        engine = self.run_renamer()
        self.assertEqual(normalize_retained_names(engine), 0)
        self.assertEqual(engine.errors, 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), b'previous backup')

    def test_log_without_path_uses_dated_file_in_current_folder(self):
        from datetime import datetime
        path = self.folder / 'source.ttf'
        make_font(path)
        old_directory = Path.cwd()
        try:
            os.chdir(self.folder)
            expected = self.folder / f'font_renamer[{datetime.now().astimezone().date().isoformat()}].log'
            self.assertEqual(main([str(path), '--log']), 0)
            self.assertTrue(expected.is_file())
            self.assertIn('RENAME:', expected.read_text(encoding='utf-8'))
            self.assertEqual(main([str(path), '--log']), 0)
            self.assertEqual(expected.read_text(encoding='utf-8').count('=== START '), 2)
        finally:
            os.chdir(old_directory)

    def test_omitting_log_does_not_create_logfile(self):
        path = self.folder / 'source.ttf'
        make_font(path)
        old_directory = Path.cwd()
        try:
            os.chdir(self.folder)
            self.assertEqual(main([str(path)]), 0)
            self.assertEqual(list(self.folder.glob('*.log')), [])
        finally:
            os.chdir(old_directory)

    def test_log_timestamps_every_line_with_fragmented_writes(self):
        from font_rename_fm.rename import text_log
        logfile, console, errors = io.StringIO(), io.StringIO(), io.StringIO()
        stamp = '2026-10-05 15:23:04.123'
        with contextlib.redirect_stdout(console), contextlib.redirect_stderr(errors), patch('font_rename_fm.rename.local_timestamp', return_value=stamp):
            with text_log(logfile, ['--log']):
                print('RENAME:\n  CURRENT: old.ttf\n  NEW: new.ttf')
                print('WARNING: malformed name', file=__import__('sys').stderr)
                print('fragment', end='')
                print('ed line')
        lines = logfile.getvalue().splitlines()
        self.assertTrue(all(line.startswith(f'[{stamp}] ') for line in lines))
        self.assertIn(f'[{stamp}] fragmented line', lines)
        self.assertIn(f'[{stamp}] WARNING: malformed name', lines)
        self.assertIn(f'[{stamp}]   NEW: new.ttf', lines)
        self.assertNotIn(f'[{stamp}]', console.getvalue())
        self.assertEqual(errors.getvalue(), 'WARNING: malformed name\n')

    def test_timestamp_uses_machine_local_time_without_timezone_label(self):
        from datetime import datetime
        from font_rename_fm.rename import local_timestamp
        local = datetime(2026, 10, 5, 9, 12, 34, 567000)
        with patch('font_rename_fm.rename.datetime') as clock:
            clock.now.return_value = local
            self.assertEqual(local_timestamp(), '2026-10-05 09:12:34.567')
            clock.now.assert_called_once_with()

    def test_mislabeled_japanese_mac_bytes_preserved(self):
        from fontTools.ttLib.tables._n_a_m_e import NameRecord
        path = self.folder / 'japanese.ttf'
        make_font(path, 'Japanese')
        native = 'ＦＡ ポップＢ'
        raw = native.encode('shift_jis')
        with TTFont(path, recalcTimestamp=False) as font:
            font['name'].names = []
            for name_id in (1, 4):
                record = NameRecord()
                record.nameID, record.platformID, record.platEncID, record.langID = name_id, 1, 0, 11
                record.string = raw
                font['name'].names.append(record)
                font['name'].setName(native, name_id, 3, 1, 0x0411)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(normalize_internal_names(font), [])
            stream = io.BytesIO()
            font.save(stream)
        with TTFont(io.BytesIO(stream.getvalue())) as font:
            for record in font['name'].names:
                if record.platformID == 1:
                    self.assertEqual(record.toBytes(), raw)
                    self.assertEqual(record.toBytes().decode('shift_jis'), native)

    def padded_name_font(self, padding=b'\x00' * 168):
        from fontTools.ttLib.tables.DefaultTable import DefaultTable
        path = self.folder / 'Test.ttf'
        make_font(path, 'Test')
        with TTFont(path, recalcTimestamp=False) as font:
            raw = font.getTableData('name')
            version, count, offset = struct.unpack('>HHH', raw[:6])
            table = DefaultTable('name')
            table.data = struct.pack('>HHH', version, count, offset + len(padding)) + raw[6:offset] + padding + raw[offset:]
            font['name'] = table
            font.save(path)
        return path

    def test_zero_name_padding_compacted_with_backup_and_unchanged_font_data(self):
        path = self.padded_name_font()
        original = path.read_bytes()
        with TTFont(path) as font:
            names = [(n.nameID, n.platformID, n.platEncID, n.langID, n.toBytes()) for n in font['name'].names]
            tables = {tag: font.getTableData(tag) for tag in font.keys() if tag not in {'GlyphOrder', 'head', 'name', 'DSIG'}}
        engine = self.run_renamer()
        self.assertEqual(normalize_retained_names(engine), 1)
        self.assertEqual((self.folder/'BAK'/'Test.ttf.original.bak').read_bytes(), original)
        with TTFont(path) as font:
            version, count, offset = struct.unpack('>HHH', font.reader['name'][:6])
            self.assertEqual(offset, 6 + 12 * count)
            self.assertEqual([(n.nameID, n.platformID, n.platEncID, n.langID, n.toBytes()) for n in font['name'].names], names)
            for tag, data in tables.items():
                self.assertEqual(font.getTableData(tag), data)
        self.assertEqual(normalize_retained_names(self.run_renamer()), 0)

    def test_nonzero_name_gap_preserved(self):
        path = self.padded_name_font(b'unknown extra data')
        original = path.read_bytes()
        engine = self.run_renamer()
        self.assertEqual(normalize_retained_names(engine), 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.folder/'BAK').exists())

    def test_padding_compaction_preview_does_not_modify_font(self):
        path = self.padded_name_font()
        original = path.read_bytes()
        engine = self.run_renamer(apply=False)
        self.assertEqual(normalize_retained_names(engine), 1)
        self.assertIn('TABLE name.layout', self.log.getvalue())
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.folder/'BAK').exists())

    def broken_styles(self, path, ids=(2, 17)):
        font = TTFont(path, recalcTimestamp=False)
        font['name'].names = [r for r in font['name'].names if r.nameID not in ids]
        for name_id in ids:
            font['name'].setName('?', name_id, 3, 1, 0x0409)
        return font

    def test_unknown_style_recovers_valid_matching_record(self):
        path = self.folder/'Test.ttf'
        make_font(path)
        with self.broken_styles(path) as font:
            font['name'].setName('Bold', 2, 1, 0, 0)
            normalize_internal_names(font)
            self.assertEqual(font['name'].getName(2, 3, 1, 0x0409).toUnicode(), 'Bold')

    def test_unknown_styles_recover_consistent_bold_italic_metadata(self):
        path = self.folder/'Test.ttf'
        make_font(path, 'Test Bold Italic')
        with self.broken_styles(path) as font:
            font['OS/2'].fsSelection = 33
            font['OS/2'].usWeightClass = 700
            font['head'].macStyle = 3
            font['name'].removeNames(nameID=6)
            font['name'].setName('Test-BoldItalic', 6, 3, 1, 0x0409)
            normalize_internal_names(font)
            for name_id in (2, 17):
                self.assertEqual(font['name'].getName(name_id, 3, 1, 0x0409).toUnicode(), 'Bold Italic')
            self.assertIn('consistent', self.log.getvalue())
            self.assertEqual(normalize_internal_names(font), [])

    def test_unknown_style_with_conflicting_flags_uses_unknown(self):
        path = self.folder/'Test.ttf'
        make_font(path)
        with self.broken_styles(path) as font:
            font['OS/2'].fsSelection = 32
            font['head'].macStyle = 0
            normalize_internal_names(font)
            for name_id in (2, 17):
                self.assertEqual(font['name'].getName(name_id, 3, 1, 0x0409).toUnicode(), 'Unknown')
            self.assertEqual(normalize_internal_names(font), [])

    def test_unknown_typographic_style_rejects_conflicting_name_suffix(self):
        path = self.folder/'Test.ttf'
        make_font(path, 'Test Light')
        with self.broken_styles(path) as font:
            font['OS/2'].fsSelection = 64
            font['head'].macStyle = 0
            font['OS/2'].usWeightClass = 400
            normalize_internal_names(font)
            self.assertEqual(font['name'].getName(17, 3, 1, 0x0409).toUnicode(), 'Unknown')

    def test_unknown_styles_do_not_block_other_normalization(self):
        path = self.folder/'Test.ttf'
        make_font(path, ' *Test ')
        with self.broken_styles(path) as font:
            font['OS/2'].fsSelection = 32
            font['head'].macStyle = 0
            font.save(path)
        engine = self.run_renamer()
        self.assertEqual(normalize_retained_names(engine), 1)
        self.assertEqual(engine.errors, 0)
        with TTFont(self.folder/'Test.ttf') as font:
            self.assertEqual(font['name'].getName(4, 3, 1, 0x0409).toUnicode(), 'Test')
            self.assertEqual(font['name'].getName(2, 3, 1, 0x0409).toUnicode(), 'Unknown')

    def test_provenance_memory_does_not_grow_with_input_font_bytes(self):
        import tracemalloc
        engine = Renamer()
        was_tracing = tracemalloc.is_tracing()
        if not was_tracing:
            tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]
        try:
            # 32 MiB of distinct inputs: retaining each input reproduces the leak.
            for index in range(128):
                data = index.to_bytes(4, 'big') * 65536
                self.assertIs(engine.original_before_normalization(self.folder/f'{index}.ttf', data), data)
            retained = tracemalloc.get_traced_memory()[0] - baseline
            self.assertLess(retained, 2 * 1024 * 1024)
        finally:
            if not was_tracing:
                tracemalloc.stop()

    def test_provenance_after_cache_eviction_preserves_normalized_identity(self):
        path = self.folder/'Test.ttf'
        make_font(path, ' *Test ')
        original = path.read_bytes()
        engine = self.run_renamer()
        self.assertEqual(normalize_retained_names(engine), 1)
        target = self.folder/'Test.ttf'
        normalized = target.read_bytes()
        self.assertEqual(engine.original_before_normalization(target, normalized), original)
        for index in range(100):
            engine.original_before_normalization(self.folder/f'other-{index}.ttf', str(index).encode())
        self.assertEqual(engine.original_before_normalization(target, normalized), original)
        self.assertNotEqual(original, normalized)

    def test_cancellation_between_fonts_preserves_remaining_files(self):
        first, second = self.folder/'a.ttf', self.folder/'b.ttf'
        make_font(first, 'First')
        make_font(second, 'Second')
        original = second.read_bytes()
        engine = Renamer(apply=True, cancel_requested=lambda: (self.folder/'First.ttf').exists())
        engine.process([first, second])
        self.assertTrue(engine.cancelled)
        self.assertEqual(engine.changed, 1)
        self.assertEqual(second.read_bytes(), original)

    def test_cli_cancel_file_stops_before_changes_and_records_status(self):
        source = self.folder/'a.ttf'
        make_font(source)
        original = source.read_bytes()
        cancel = self.folder/'cancel.flag'
        cancel.touch()
        log = self.folder/'session.log'
        self.assertEqual(main([str(source), '--apply', '--normalize-internal', '--cancel-file', str(cancel), '--log', str(log)]), 130)
        self.assertEqual(source.read_bytes(), original)
        self.assertIn('CANCELLED', log.read_text(encoding='utf-8'))
        self.assertIn('EXIT STATUS: 130', log.read_text(encoding='utf-8'))

    def test_invalid_windows_chars(self):
        source = self.folder / 'original.ttf'
        make_font(source, '*Antique Olive Bd.C.')
        before = source.read_bytes()
        self.run_renamer()
        self.assertEqual((self.folder / 'Antique Olive Bd.C.ttf').read_bytes(), before)

    def test_manicule_prefix_removed_from_filename_and_display_names(self):
        source = self.folder / '☞Test.ttf'
        make_font(source, ' ☞☞ Test ')
        engine = self.run_renamer()
        self.assertEqual(normalize_retained_names(engine), 1)
        target = self.folder / 'Test.ttf'
        self.assertTrue(target.exists())
        with TTFont(target) as font:
            self.assertEqual(get_font_name(font), 'Test')
        self.assertTrue((target.parent / 'BAK' / (target.name + '.original.bak')).exists())

    def test_manicule_inside_name_and_symbol_only_name_preserved(self):
        self.assertEqual(clean_name('Symbol ☞ Font'), 'Symbol ☞ Font')
        self.assertEqual(clean_name('☞'), '☞')

    def test_cff_manicule_and_marker_only_style_cleaned(self):
        self.test_otf_extension_is_preserved()
        source = self.folder / 'OpenType Test.otf'
        with TTFont(source, recalcTimestamp=False) as font:
            font['name'].setName('☞', 2, 3, 1, 0x0409)
            font['name'].setName('☞OpenType Test', 4, 3, 1, 0x0409)
            font['CFF '].cff.topDictIndex[0].FullName = '\u00e2\u0098\u009eOpenType Test'
            font.save(source)
        before = source.read_bytes()
        engine = self.run_renamer()
        self.assertEqual(normalize_retained_names(engine), 1)
        with TTFont(source) as font:
            self.assertEqual(font['name'].getName(2, 3, 1, 0x0409).toUnicode(), 'Regular')
            self.assertEqual(get_font_name(font), 'OpenType Test')
            self.assertEqual(font['CFF '].cff.topDictIndex[0].FullName, 'OpenType Test')
        self.assertEqual((source.parent / 'BAK' / (source.name + '.original.bak')).read_bytes(), before)
        self.assertEqual(normalize_retained_names(self.run_renamer()), 0)

    def test_windows_symbol_name_preferred_over_mac_placeholder(self):
        source = self.folder / 'Another Dingbat Font JL.ttf'
        make_font(source, 'New')
        with TTFont(source, recalcTimestamp=False) as font:
            for name_id in (1, 4, 6):
                font['name'].removeNames(nameID=name_id, platformID=3)
                font['name'].setName('Another Dingbat Font JL', name_id, 3, 0, 0x0409)
            font.save(source)
        engine = self.run_renamer()
        self.assertEqual(engine.changed, 0)
        self.assertTrue(source.exists())

    def test_placeholder_record_falls_back_to_valid_name(self):
        source = self.folder / 'a.ttf'
        make_font(source, 'Babylon5')
        with TTFont(source, recalcTimestamp=False) as font:
            for name_id in (1, 2, 4, 6):
                font['name'].setName('????', name_id, 1, 0, 0)
            font.save(source)
        engine = self.run_renamer()
        self.assertTrue((self.folder / 'Babylon5.ttf').exists())
        self.assertEqual(normalize_retained_names(engine), 1)
        self.assertEqual(engine.errors, 0)

    def test_empty_optional_style_is_preserved(self):
        source = self.folder / 'a.ttf'
        make_font(source, 'Test')
        with TTFont(source) as font:
            for record in font['name'].names:
                if record.nameID == 2:
                    record.string = b''
            self.assertEqual(normalize_internal_names(font), [])
            self.assertTrue(all(r.toBytes() == b'' for r in font['name'].names if r.nameID == 2))

    def test_undecodable_record_preserved_while_valid_names_cleaned(self):
        source = self.folder / 'a.ttf'
        make_font(source, ' *Test ')
        with TTFont(source) as font:
            font['name'].setName(b'odd', 4, 0, 0, 0)
            with contextlib.redirect_stderr(io.StringIO()):
                changes = normalize_internal_names(font)
            self.assertTrue(changes)
            self.assertEqual(font['name'].getName(4, 0, 0, 0).toBytes(), b'odd')

    def test_internal_preview_apply_backup_and_idempotence(self):
        source = self.folder / 'original.ttf'
        make_font(source, '  *Test   Font? ')
        original = source.read_bytes()
        preview = self.run_renamer(apply=False)
        self.assertEqual(normalize_retained_names(preview), 1)
        self.assertEqual(source.read_bytes(), original)
        engine = self.run_renamer()
        self.assertEqual(normalize_retained_names(engine), 1)
        target = self.folder / 'Test Font.ttf'
        self.assertEqual((self.folder / 'BAK' / 'Test Font.ttf.original.bak').read_bytes(), original)
        with TTFont(target) as font:
            self.assertEqual(get_font_name(font), 'Test Font')
            self.assertEqual(font['name'].getDebugName(6), 'Test-Regular')
            self.assertEqual(normalize_internal_names(font), [])
        self.assertEqual(normalize_retained_names(self.run_renamer()), 0)

    def test_internal_cleanup_does_not_merge_distinct_originals(self):
        make_font(self.folder / 'a.ttf', ' *Test')
        make_font(self.folder / 'b.ttf', 'Test')
        engine = self.run_renamer()
        normalize_retained_names(engine)
        self.assertEqual(len(list(self.folder.glob('*.ttf'))), 2)
        self.assertEqual(engine.duplicates, 0)

    def test_internal_empty_rejected_without_modification(self):
        source = self.folder / 'a.ttf'
        make_font(source, '*?')
        with TTFont(source) as font:
            before = [r.toBytes() for r in font['name'].names]
            with self.assertRaises(ValueError):
                normalize_internal_names(font)
            self.assertEqual(before, [r.toBytes() for r in font['name'].names])

    def test_reserved_and_empty_names(self):
        for value in ('CON', 'con.txt', 'NUL', 'LPT1', 'COM¹'):
            self.assertTrue(clean_name(value).startswith('_'))
        self.assertEqual(clean_name('Font: A/B?'), 'Font AB')
        with self.assertRaises(ValueError):
            clean_name('*?<>')

    def test_preview_does_not_modify_files(self):
        a = self.folder / 'a.ttf'
        make_font(a, '*Test')
        shutil.copyfile(a, self.folder / 'b.ttf')
        before = {str(p.relative_to(self.folder)): p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}
        engine = self.run_renamer(apply=False)
        self.assertEqual(engine.duplicates, 1)
        self.assertEqual(before, {str(p.relative_to(self.folder)): p.read_bytes() for p in self.folder.rglob('*') if p.is_file()})

    def test_identical_files_leave_one(self):
        a = self.folder / 'a.ttf'
        make_font(a)
        before = a.read_bytes()
        shutil.copyfile(a, self.folder / 'b.ttf')
        engine = self.run_renamer()
        self.assertEqual(engine.duplicates, 1)
        remaining = list(self.folder.iterdir())
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].read_bytes(), before)

    def test_different_contents_same_name_are_both_kept(self):
        make_font(self.folder / 'a.ttf', width=500)
        make_font(self.folder / 'b.ttf', width=600)
        before = {p.read_bytes() for p in self.folder.iterdir()}
        self.run_renamer()
        self.assertEqual({p.read_bytes() for p in self.folder.iterdir()}, before)
        names = {p.name for p in self.folder.iterdir()}
        self.assertIn('Test Regular.ttf', names)
        self.assertRegex(next(n for n in names if n != 'Test Regular.ttf'),
                         r'^Test Regular \[sha256 [0-9a-f]{12}\]\.ttf$')
        engine = self.run_renamer()
        self.assertEqual(engine.changed, 0)

    def set_metadata(self, path, values):
        with TTFont(path, recalcTimestamp=False) as font:
            for name_id, value in values.items():
                font['name'].removeNames(nameID=name_id)
                font['name'].setName(value, name_id, 3, 1, 0x0409)
            font.save(path)

    def test_collision_version_and_manufacturer(self):
        for name, version, maker in (('a', 'Version 1.00', 'Maker A'),
                                      ('b', 'Version 2.10', 'Maker A'),
                                      ('c', 'Version 2.10', 'Maker B')):
            path = self.folder / (name + '.ttf')
            make_font(path)
            self.set_metadata(path, {5: version, 8: maker})
        before = {p.read_bytes() for p in self.folder.glob('*.ttf')}
        self.run_renamer()
        self.assertEqual({p.name for p in self.folder.glob('*.ttf')},
                         {'Test Regular.ttf', 'Test Regular [v2.10].ttf',
                          'Test Regular [v2.10; Maker B].ttf'})
        self.assertEqual({p.read_bytes() for p in self.folder.glob('*.ttf')}, before)
        self.assertEqual(self.run_renamer().changed, 0)

    def test_collision_style_precedes_version(self):
        for name, style in (('a', 'Regular'), ('b', 'Condensed')):
            path = self.folder / (name + '.ttf')
            make_font(path)
            self.set_metadata(path, {17: style})
        self.run_renamer()
        self.assertTrue((self.folder / 'Test Regular [Condensed].ttf').exists())

    def test_metadata_only_added_for_collisions_and_preview_matches_apply(self):
        make_font(self.folder / 'a.ttf')
        make_font(self.folder / 'b.ttf', width=600)
        self.set_metadata(self.folder / 'b.ttf', {8: 'Vendor: / ?'})
        make_font(self.folder / 'c.ttf', 'Other')
        preview = self.run_renamer(apply=False)
        proposed = {ref.path.name for ref in preview.reserved.values()}
        self.run_renamer()
        self.assertEqual(proposed, {p.name for p in self.folder.glob('*.ttf')})
        self.assertIn('Other.ttf', proposed)

    def test_identical_copies_kept_with_hash_then_number(self):
        make_font(self.folder / 'a.ttf')
        shutil.copyfile(self.folder / 'a.ttf', self.folder / 'b.ttf')
        shutil.copyfile(self.folder / 'a.ttf', self.folder / 'c.ttf')
        self.run_renamer(keep_duplicates=True)
        self.assertEqual(len(list(self.folder.glob('*.ttf'))), 3)
        self.assertEqual(self.run_renamer(keep_duplicates=True).changed, 0)

    def test_existing_target_identical(self):
        make_font(self.folder / 'Test Regular.ttf')
        shutil.copyfile(self.folder / 'Test Regular.ttf', self.folder / 'a.ttf')
        self.run_renamer()
        self.assertEqual([p.name for p in self.folder.iterdir()], ['Test Regular.ttf'])

    def test_existing_target_different(self):
        make_font(self.folder / 'Test Regular.ttf', width=500)
        make_font(self.folder / 'a.ttf', width=600)
        self.run_renamer()
        self.assertEqual(len(list(self.folder.iterdir())), 2)

    def test_keep_duplicates_option(self):
        make_font(self.folder / 'a.ttf')
        shutil.copyfile(self.folder / 'a.ttf', self.folder / 'b.ttf')
        self.run_renamer(keep_duplicates=True)
        self.assertEqual(len(list(self.folder.iterdir())), 2)

    def test_deduplication_across_subdirectories(self):
        make_font(self.folder / 'a.ttf')
        sub = self.folder / 'sub'
        sub.mkdir()
        shutil.copyfile(self.folder / 'a.ttf', sub / 'b.ttf')
        self.run_renamer()
        self.assertEqual(len(list(self.folder.rglob('*.ttf'))), 1)

    def test_chinese_arabic_cyrillic_original_preserved(self):
        for name in ('黑体', 'العربية', 'Пример'):
            with self.subTest(name=name):
                stem = filename_stem(name)
                self.assertTrue(stem.endswith(f'[{name}]'))
                self.assertTrue(stem.split(' [')[0].isascii())
                self.assertEqual(filename_stem(name, False), name)

    def test_native_name_selected_even_with_english_name(self):
        path = self.folder / 'a.ttf'
        make_font(path)
        with TTFont(path) as font:
            font['name'].setName('العربية', 4, 3, 1, 0x0401)
            self.assertEqual(get_font_name(font), 'العربية')

    def test_malformed_unicode_ascii_not_treated_as_chinese(self):
        path = self.folder / 'a.ttf'
        make_font(path, 'A Charming Font Expanded')
        with TTFont(path) as font:
            font['name'].setName(b'A Charming Font Expanded', 4, 0, 0, 0)
            self.assertEqual(get_font_name(font), 'A Charming Font Expanded')

    def test_transliterated_actual_font(self):
        path = self.folder / 'a.ttf'
        make_font(path, '黑体')
        data = path.read_bytes()
        self.run_renamer()
        target = self.folder / (filename_stem('黑体') + '.ttf')
        self.assertEqual(target.read_bytes(), data)

    def test_malformed_font_kept_and_continues(self):
        bad = self.folder / 'a.ttf'
        bad.write_bytes(b'not a font')
        make_font(self.folder / 'b.ttf')
        engine = Renamer(apply=True)
        engine.process(collect_files([self.folder]))
        self.assertEqual(engine.errors, 1)
        self.assertEqual(bad.read_bytes(), b'not a font')
        self.assertTrue((self.folder / 'Test Regular.ttf').exists())

    def test_non_font_files_and_repeated_roots(self):
        make_font(self.folder / 'a.ttf')
        (self.folder / 'readme.txt').write_text('keep me')
        self.assertEqual(len(collect_files([self.folder, self.folder])), 1)
        self.run_renamer()
        self.assertEqual((self.folder / 'readme.txt').read_text(), 'keep me')

    def test_duplicate_changed_after_comparison_is_kept(self):
        make_font(self.folder / 'a.ttf')
        b = self.folder / 'b.ttf'
        shutil.copyfile(self.folder / 'a.ttf', b)
        engine = Renamer(apply=True)
        engine.process([self.folder / 'a.ttf'])
        old_data = b.read_bytes()
        ref = engine.duplicate(old_data)
        b.write_bytes(old_data + b'changed')
        with self.assertRaises(RuntimeError):
            engine.remove_duplicate(b, old_data, ref)
        self.assertTrue(b.exists())

    def test_destination_appearing_during_rename_not_overwritten(self):
        source = self.folder / 'a.ttf'
        make_font(source)
        target = self.folder / 'Test Regular.ttf'
        engine = Renamer(apply=True)
        original = engine.destination
        def race(*args):
            result = original(*args)
            target.write_bytes(b'new occupant')
            return result
        with patch.object(engine, 'destination', side_effect=race):
            engine.process([source])
        self.assertEqual(engine.errors, 1)
        self.assertTrue(source.exists())
        self.assertEqual(target.read_bytes(), b'new occupant')

    def test_collection_preview_and_apply_preserve_original(self):
        make_font(self.folder / 'one.ttf', 'One')
        make_font(self.folder / 'two.ttf', 'Two')
        path = self.folder / 'collection.ttc'
        collection = TTCollection()
        collection.fonts = [TTFont(self.folder / 'one.ttf'), TTFont(self.folder / 'two.ttf')]
        collection.save(path)
        collection.close()
        (self.folder / 'one.ttf').unlink()
        (self.folder / 'two.ttf').unlink()
        original = path.read_bytes()
        engine = self.run_renamer(apply=False)
        self.assertEqual(engine.changed, 2)
        self.assertEqual(len(list(self.folder.iterdir())), 1)
        self.run_renamer()
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue((self.folder / 'One.ttf').exists())
        self.assertTrue((self.folder / 'Two.ttf').exists())
        engine = self.run_renamer()
        self.assertEqual(engine.changed, 0)
        self.assertEqual(len(list(self.folder.iterdir())), 3)

    def test_filename_length_for_windows(self):
        stem = filename_stem('黑体' * 200)
        self.assertLessEqual(len(stem.encode('utf-16-le')) // 2, 180)
        self.assertTrue(stem.endswith(']'))

    def test_otf_extension_is_preserved(self):
        path = self.folder / 'a.otf'
        fb = FontBuilder(1000, isTTF=False)
        fb.setupGlyphOrder(['.notdef'])
        fb.setupCharacterMap({})
        fb.setupHorizontalMetrics({'.notdef': (500, 0)})
        fb.setupHorizontalHeader(ascent=800, descent=-200)
        fb.setupNameTable({'familyName': 'OpenType Test', 'styleName': 'Regular',
                          'fullName': 'OpenType Test', 'psName': 'OpenType-Test'})
        fb.setupOS2(sTypoAscender=800, sTypoDescender=-200,
                   usWinAscent=800, usWinDescent=200)
        from fontTools.pens.t2CharStringPen import T2CharStringPen
        pen = T2CharStringPen(500, None)
        fb.setupCFF('OpenType-Test', {}, {'.notdef': pen.getCharString()}, {})
        fb.setupPost()
        fb.save(path)
        data = path.read_bytes()
        self.run_renamer()
        self.assertEqual((self.folder / 'OpenType Test.otf').read_bytes(), data)

    def test_cli_defaults_to_preview(self):
        make_font(self.folder / 'a.ttf')
        self.assertEqual(main([str(self.folder)]), 0)
        self.assertTrue((self.folder / 'a.ttf').exists())

    def test_identical_collections_leave_one_original(self):
        font_path = self.folder / 'font.ttf'
        make_font(font_path)
        collection = TTCollection()
        collection.fonts = [TTFont(font_path)]
        collection.save(self.folder / 'a.ttc')
        collection.close()
        font_path.unlink()
        shutil.copyfile(self.folder / 'a.ttc', self.folder / 'b.ttc')
        original = (self.folder / 'a.ttc').read_bytes()
        engine = self.run_renamer()
        self.assertEqual(engine.duplicates, 1)
        remaining = list(self.folder.glob('*.ttc'))
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].read_bytes(), original)

    def test_normalized_hash_collision_is_stable_on_second_run(self):
        make_font(self.folder / 'a.ttf', ' *Test ', width=500)
        make_font(self.folder / 'b.ttf', ' *Test ', width=600)
        engine = self.run_renamer()
        normalize_retained_names(engine)
        names = {p.name for p in self.folder.glob('*.ttf')}
        repeated = self.run_renamer()
        normalize_retained_names(repeated)
        self.assertEqual(repeated.changed, 0)
        self.assertEqual(names, {p.name for p in self.folder.glob('*.ttf')})

    def test_normalized_collection_is_not_extracted_again(self):
        source = self.folder / 'source.ttf'
        make_font(source, ' *Test ')
        collection = TTCollection()
        collection.fonts = [TTFont(source)]
        collection.save(self.folder / 'collection.ttc')
        collection.close()
        source.unlink()
        engine = self.run_renamer()
        normalize_retained_names(engine)
        before = {str(p.relative_to(self.folder)): p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}
        repeated = self.run_renamer()
        normalize_retained_names(repeated)
        self.assertEqual(repeated.changed, 0)
        self.assertEqual(before, {str(p.relative_to(self.folder)): p.read_bytes() for p in self.folder.rglob('*') if p.is_file()})

    def test_collection_bad_member_does_not_block_good_member(self):
        bad = self.folder / 'bad.ttf'
        good = self.folder / 'good.ttf'
        make_font(bad, '*?')
        with TTFont(bad, recalcTimestamp=False) as font:
            for record in font['name'].names:
                if record.nameID == 6:
                    record.string = '*?'.encode(record.getEncoding())
            font.save(bad)
        make_font(good, 'Good')
        source = self.folder / 'collection.ttc'
        collection = TTCollection()
        collection.fonts = [TTFont(bad), TTFont(good)]
        collection.save(source)
        collection.close()
        bad.unlink()
        good.unlink()
        original = source.read_bytes()
        engine = Renamer(apply=True)
        engine.process([source])
        self.assertEqual(engine.errors, 1)
        self.assertTrue((self.folder / 'Good.ttf').exists())
        self.assertEqual(source.read_bytes(), original)

    def test_long_bilingual_collision_preserves_brackets(self):
        name = '黑体' * 100
        make_font(self.folder / 'a.ttf', name, width=500)
        make_font(self.folder / 'b.ttf', name, width=600)
        self.run_renamer()
        for path in self.folder.glob('*.ttf'):
            self.assertEqual(path.stem.count('['), path.stem.count(']'))
            self.assertLessEqual(len(path.stem.encode('utf-16-le')) // 2, 180)
            self.assertLessEqual(len((path.name + '.original.bak').encode('utf-8')), 255)

    def test_long_native_manufacturer_collision_preserves_brackets(self):
        for filename, maker in (('a.ttf', '黑体' * 100), ('b.ttf', 'العربية' * 100)):
            path = self.folder / filename
            make_font(path, 'Test')
            self.set_metadata(path, {8: maker})
        self.run_renamer()
        for path in self.folder.glob('*.ttf'):
            self.assertEqual(path.stem.count('['), path.stem.count(']'))
            self.assertLessEqual(len(path.stem.encode('utf-16-le')) // 2, 180)
            self.assertLessEqual(len((path.name + '.original.bak').encode('utf-8')), 255)

    def test_false_backup_does_not_authorize_duplicate_removal(self):
        make_font(self.folder / 'a.ttf', 'Test', width=500)
        make_font(self.folder / 'b.ttf', 'Test', width=600)
        fake = self.folder / 'b.ttf.original.bak'
        shutil.copyfile(self.folder / 'a.ttf', fake)
        before = {p.read_bytes() for p in self.folder.glob('*.ttf')}
        engine = self.run_renamer()
        self.assertEqual(engine.duplicates, 0)
        self.assertEqual(before, {p.read_bytes() for p in self.folder.glob('*.ttf')})

    def test_existing_backup_blocks_replacement_without_overwrite(self):
        source = self.folder / 'Test.ttf'
        make_font(source, ' *Test ')
        original = source.read_bytes()
        backup = source.with_name(source.name + '.original.bak')
        backup.write_bytes(b'previous backup')
        engine = self.run_renamer()
        self.assertEqual(normalize_retained_names(engine), 0)
        self.assertEqual(engine.errors, 1)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), b'previous backup')

    def test_variable_fonts_with_same_name_remain_distinct(self):
        from fontTools.ttLib import newTable
        from fontTools.ttLib.tables._f_v_a_r import Axis
        for filename, weight in (('a.ttf', 400), ('b.ttf', 500)):
            path = self.folder / filename
            make_font(path, 'Variable Test')
            with TTFont(path, recalcTimestamp=False) as font:
                font['fvar'] = newTable('fvar')
                axis = Axis()
                axis.axisTag = 'wght'
                axis.minValue, axis.defaultValue, axis.maxValue = 100, weight, 900
                axis.flags, axis.axisNameID = 0, 256
                font['fvar'].axes, font['fvar'].instances = [axis], []
                font['name'].setName('Weight', 256, 3, 1, 0x0409)
                font.save(path)
        before = {p.read_bytes() for p in self.folder.glob('*.ttf')}
        self.run_renamer()
        self.assertEqual(before, {p.read_bytes() for p in self.folder.glob('*.ttf')})
        self.assertEqual(self.run_renamer().changed, 0)


if __name__ == '__main__':
    unittest.main()
