import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import Error as PlaywrightError

from fanbox_lab import Experiment, LabError, arguments, close_browser_resource
from manual_browser import launch_arguments
from x_lab import XExperiment


class ManualLoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lab = XExperiment(arguments(["login", "--platform", "x", "--data-dir", self.temp.name]))

    def test_manual_browser_has_no_automation_connection_or_daily_profile(self):
        profile = self.lab.data / "manual-profiles" / "chrome"
        command = launch_arguments(Path("chrome.exe"), profile, self.lab.login_url)
        self.assertIn("--user-data-dir=" + str(profile), command)
        for argument in command:
            self.assertNotIn("remote-debugging", argument)
            self.assertNotIn("enable-automation", argument)
            self.assertNotIn("AutomationControlled", argument)
            self.assertNotIn("no-sandbox", argument)

    async def test_only_attaches_after_manual_browser_closes_and_verifies_before_saving(self):
        events = []
        context = SimpleNamespace(pages=[object()], close=AsyncMock(side_effect=lambda: events.append("close")))

        async def manual(*_):
            events.append("manual_closed")

        async def attach(*_, **__):
            self.assertEqual(events, ["manual_closed"])
            events.append("attach")
            return context

        playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=AsyncMock(side_effect=attach)))
        with patch("manual_browser.executable_for", return_value=Path("chrome.exe")), \
                patch("manual_browser.wait_for_manual_browser", side_effect=manual), \
                patch.object(self.lab, "prepare_context", AsyncMock()), \
                patch.object(self.lab, "verify", AsyncMock(side_effect=lambda _: events.append("verify"))), \
                patch.object(self.lab, "save_state", AsyncMock(side_effect=lambda _: events.append("save"))):
            await self.lab.login(playwright)
        self.assertEqual(events, ["manual_closed", "attach", "verify", "save", "close"])
        self.assertTrue(self.lab.report["login_captured"])
        attached_profile = playwright.chromium.launch_persistent_context.call_args.args[0]
        self.assertEqual(attached_profile, self.lab.data / "manual-profiles" / "chrome")
        self.assertTrue(playwright.chromium.launch_persistent_context.call_args.kwargs["headless"])
        self.assertTrue(self.lab.report["credential_capture_headless"])

    async def test_closed_without_successful_login_does_not_save_credentials(self):
        context = SimpleNamespace(pages=[object()], close=AsyncMock())
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=AsyncMock(return_value=context)))
        with patch("manual_browser.executable_for", return_value=Path("chrome.exe")), \
                patch("manual_browser.wait_for_manual_browser", AsyncMock()), \
                patch.object(self.lab, "prepare_context", AsyncMock()), \
                patch.object(self.lab, "verify", AsyncMock(side_effect=LabError("not logged in", 3))), \
                patch.object(self.lab, "save_state", AsyncMock()) as save:
            with self.assertRaises(LabError) as result:
                await self.lab.login(playwright)
        self.assertEqual(result.exception.code, 3)
        save.assert_not_awaited()
        context.close.assert_awaited_once()

    async def test_close_error_does_not_replace_login_error(self):
        resource = SimpleNamespace(close=AsyncMock(side_effect=PlaywrightError("Target page, context or browser has been closed")))
        with self.assertRaises(LabError) as result:
            try:
                raise LabError("login was restricted", 5)
            finally:
                await close_browser_resource(resource)
        self.assertEqual(result.exception.code, 5)
        await close_browser_resource(resource)  # Already-closed cleanup is also harmless on its own.


class FanboxManualLoginTests(ManualLoginTests):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lab = Experiment(arguments(["login", "--data-dir", self.temp.name]))
