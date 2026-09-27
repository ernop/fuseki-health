import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor

CONFIG = {'smtp_login': 'sender@example.com', 'smtp_password': 'app-password',
          'from_email': 'sender@example.com', 'to_email': 'owner@example.com',
          'editor_prefix': '/private123', 'disk_reader_key': 'KEY'}
HOUR = 60 * 60


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Path(self.directory.name) / 'config.json'
        self.config.write_text(json.dumps(CONFIG))
        self.status = Path(self.directory.name) / 'status.json'

    def tearDown(self):
        self.directory.cleanup()

    def run_checks(self, availability, disk, times):
        with (patch.object(monitor, 'availability_errors', side_effect=availability),
              patch.object(monitor, 'disk_errors', side_effect=disk),
              patch.object(monitor, 'send_email') as send_email,
              patch.object(monitor.time, 'time', side_effect=times)):
            for _ in times:
                monitor.run(self.config, self.status)
        return [call.args[1] for call in send_email.call_args_list]

    def test_mails_on_down_and_recovery_only(self):
        subjects = self.run_checks([[], ['homepage failed'], ['homepage failed'], []],
                                   [[], [], [], []], [100, 200, 300, 400])
        self.assertEqual(subjects, ['fuseki.net is DOWN', 'fuseki.net has RECOVERED'])

    def test_first_check_mails_only_a_problem(self):
        self.assertEqual(self.run_checks([[]], [[]], [100]), [])
        self.status.unlink()
        self.assertEqual(self.run_checks([[]], [['/ has 3.0 GiB free']], [100]),
                         ['tpbeta disk space is LOW'])

    def test_reminder_every_six_hours_while_down(self):
        subjects = self.run_checks([[], [], []], [['low'], ['low'], ['low']],
                                   [0, 5 * HOUR, 6 * HOUR])
        self.assertEqual(subjects, ['tpbeta disk space is LOW'] * 2)

    def test_status_file_changes_only_with_the_state(self):
        self.run_checks([[], []], [['/ has 3.0 GiB free'], ['/ has 2.9 GiB free']], [100, 400])
        record = json.loads(self.status.read_text())
        self.assertEqual(record['groups']['disk'],
                         {'status': 'down', 'changed_at': 100, 'notified_at': 100,
                          'problem': 'tpbeta disk space is LOW'})
        self.assertEqual(record['groups']['availability'],
                         {'status': 'up', 'changed_at': 100, 'notified_at': 0})
        self.assertRegex(record['checked_month'], r'^\d{4}-\d{2}$')

    def test_failed_mail_leaves_the_state_for_the_next_run(self):
        self.run_checks([[]], [[]], [100])
        before = self.status.read_text()
        with (patch.object(monitor, 'availability_errors', return_value=['feed failed']),
              patch.object(monitor, 'disk_errors', return_value=[]),
              patch.object(monitor, 'send_email', side_effect=OSError('smtp refused')),
              patch.object(monitor.time, 'time', return_value=200)):
            with self.assertRaises(OSError):
                monitor.run(self.config, self.status)
        self.assertEqual(self.status.read_text(), before)


class CheckTests(unittest.TestCase):
    def test_editor_prefix_never_appears_in_messages(self):
        def answer(url, statuses, marker=None, follow_redirects=True):
            return f'{url}: request failed: timed out'
        with patch.object(monitor, 'fetch', side_effect=answer):
            errors = monitor.availability_errors(CONFIG)
        self.assertTrue(errors)
        self.assertFalse(any('private123' in error for error in errors))
        self.assertIn('https://edit.fuseki.net/<editor prefix>/articles/: request failed: timed out', errors)

    def test_empty_editor_prefix_is_refused(self):
        with self.assertRaises(ValueError):
            monitor.availability_errors({**CONFIG, 'editor_prefix': '/'})

    def test_parse_df(self):
        self.assertEqual(monitor.parse_df('     Avail   1B-blocks\n3027750912 82086711296\n'),
                         (3027750912, 82086711296))
        for output in ('', 'Avail 1B-blocks\n', 'Used Size\n1 2\n', 'Avail 1B-blocks\n1 2\n3 4\n'):
            with self.subTest(output=output), self.assertRaises(ValueError):
                monitor.parse_df(output)

    def test_disk_threshold_and_unreadable_disk(self):
        gib = 1024 ** 3
        with patch.object(monitor, 'read_disk', return_value=(25 * gib, 76 * gib)):
            self.assertEqual(monitor.disk_errors(CONFIG), [])
        with patch.object(monitor, 'read_disk', return_value=(3 * gib, 76 * gib)):
            low = monitor.disk_errors(CONFIG)
        self.assertIn('3.0 GiB free of 76.0 GiB', low[0])
        self.assertEqual(monitor.disk_subject(low), 'tpbeta disk space is LOW')
        with patch.object(monitor, 'read_disk', side_effect=OSError('Connection timed out')):
            unreadable = monitor.disk_errors(CONFIG)
        self.assertEqual(unreadable, ['could not read free space: Connection timed out'])
        self.assertEqual(monitor.disk_subject(unreadable), 'tpbeta disk space could not be checked')


if __name__ == '__main__':
    unittest.main()
