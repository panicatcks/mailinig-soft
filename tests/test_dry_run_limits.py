import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import send_email
from email_gui import completion_message


class DryRunLimitTests(unittest.TestCase):
    def test_exhausted_limit_only_blocks_real_sending(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            template = base / "template.html"
            template.write_text("<html><title>Test</title><body>Hello</body></html>")
            args = argparse.Namespace(
                subject=None, smtp_account=[], smtp_host="smtp.example.com",
                smtp_port=465, smtp_user="sender@example.com", smtp_password="test",
                from_email="sender@example.com", limit_per_day=10,
                limit_per_minute=50, state_file=str(base / "state.json"),
                progress_file=str(base / "progress.json"), campaign_key="limit-test", campaign_id="",
                dry_run=True, hub_url="", hub_connection_id=0, hub_module_secret="",
            )
            state = send_email.SendingState(Path(args.state_file), args.campaign_key)
            account = send_email.parse_smtp_accounts(args)[0]
            state.sent_today = 10
            state.account_sent_today[account.key] = 10
            state.cursor_index = 1
            state.last_row = 2
            state.save()
            original_state = state.state_path.read_bytes()
            recipients = [
                send_email.RecipientRow("previous@example.com", {}, source_row=2),
                send_email.RecipientRow("next@example.com", {}, source_row=3),
                send_email.RecipientRow("later@example.com", {}, source_row=4),
            ]
            output = io.StringIO()
            with patch.object(send_email.smtplib, "SMTP_SSL") as smtp, redirect_stdout(output):
                send_email.send_all(args, recipients, template)
                self.assertIn("DRY RUN OK", output.getvalue())
                self.assertIn("next@example.com", output.getvalue())
                self.assertNotIn("previous@example.com", output.getvalue())
                self.assertEqual(state.state_path.read_bytes(), original_state)
                progress = json.loads(Path(args.progress_file).read_text())
                self.assertEqual(progress["status"], "completed")
                self.assertIn("Письма не отправлялись", progress["message"])
                args.dry_run = False
                send_email.send_all(args, recipients, template)
                progress = json.loads(Path(args.progress_file).read_text())
                self.assertEqual(progress["status"], "daily_limit")
                smtp.assert_not_called()
                self.assertEqual(state.state_path.read_bytes(), original_state)

                send_email.send_all(args, recipients[:1], template)
                progress = json.loads(Path(args.progress_file).read_text())
                self.assertEqual(progress["status"], "completed")

                state.account_sent_today[account.key] = 9
                state.sent_today = 9
                state.save()
                original_state = state.state_path.read_bytes()
                with patch.object(send_email, "STOP_REQUESTED", True):
                    send_email.send_all(args, recipients, template)
                progress = json.loads(Path(args.progress_file).read_text())
                self.assertEqual(progress["status"], "stopped")
                self.assertIn("пользователем", progress["message"])
                self.assertEqual(state.state_path.read_bytes(), original_state)
                smtp.assert_not_called()

                args.xlsx_kind_filter = "ALL"
                send_email.send_all(args, recipients, template)
                progress = json.loads(Path(args.progress_file).read_text())
                self.assertEqual(progress["status"], "daily_limit")
                self.assertEqual(progress["sent"], 1)
                smtp.return_value.send_message.assert_called_once()

                args.limit_per_day = 0
                with self.assertRaisesRegex(RuntimeError, "limit-per-day"):
                    send_email.send_all(args, recipients, template)

    def test_completion_messages_do_not_hide_errors(self):
        cases = [
            (0, {"status": "completed"}, "✅"),
            (0, {"status": "daily_limit"}, "ℹ"),
            (0, {"status": "stopped"}, "⏹"),
            (0, {"failed": 2}, "⚠"),
            (1, {"status": "completed"}, "❌"),
            (0, {"status": "failed"}, "❌"),
            (None, {"status": "completed"}, "⚠"),
        ]
        for code, payload, icon in cases:
            with self.subTest(code=code, payload=payload):
                self.assertTrue(completion_message(code, payload).startswith(icon))


if __name__ == "__main__":
    unittest.main()
