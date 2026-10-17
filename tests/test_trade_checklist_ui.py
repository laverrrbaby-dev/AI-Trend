from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))
sys.path.insert(0, str(PROJECT_ROOT))

from test_trade_checklist import valid_draft
from trend_radar.trade_checklist import load_checklist_draft, save_checklist_draft


class ChecklistPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def page(self) -> AppTest:
        return AppTest.from_string(
            "from pathlib import Path\n"
            "from trend_radar.trade_checklist_ui import render_trade_checklist\n"
            f"render_trade_checklist(Path({str(self.root)!r}))\n"
        ).run()

    def complete(self, page: AppTest) -> AppTest:
        values = valid_draft()
        for widget in page.text_input:
            widget.set_value(values[widget.key.removeprefix("trade-checklist-")])
        for widget in page.text_area:
            widget.set_value(values[widget.key.removeprefix("trade-checklist-")])
        for widget in page.selectbox:
            widget.select(values[widget.key.removeprefix("trade-checklist-")])
        page.run()
        for checkbox in page.checkbox:
            checkbox.check()
        return page.run()

    def test_valid_plan_requires_every_manual_check_before_passing(self) -> None:
        page = self.page()
        self.assertFalse(page.exception)
        self.assertFalse(page.success)
        self.assertTrue(all(widget.value == "" for widget in page.text_input))
        self.complete(page)
        self.assertFalse(page.exception)
        self.assertIn("检查通过", [message.value for message in page.success])
        self.assertIn("1.6667", [metric.value for metric in page.metric])
        self.assertTrue(any("原计划退出价" in metric.label for metric in page.metric))

    def test_editing_any_input_revokes_checks_even_after_restoring_value(self) -> None:
        page = self.complete(self.page())
        for field, value in [("account_balance", "700"), ("entry_price", "101"),
                             ("planned_exit_price", "89"), ("take_profit_price", "121"),
                             ("liquidation_price", "87"), ("leverage", "11"),
                             ("quantity", "0.02"), ("symbol", "ETH-USDT-SWAP")]:
            with self.subTest(field=field):
                page.text_input(key="trade-checklist-" + field).set_value(value).run()
                self.assertFalse(any(widget.value for widget in page.checkbox))
                self.assertFalse(page.success)
                page.text_input(key="trade-checklist-" + field).set_value(valid_draft()[field]).run()
                self.assertFalse(page.success)
                for checkbox in page.checkbox:
                    checkbox.check()
                page.run()
                self.assertIn("检查通过", [message.value for message in page.success])

    def test_refresh_restores_current_draft_and_new_check_clears_it(self) -> None:
        self.complete(self.page())
        restored = self.page()
        self.assertIn("检查通过", [message.value for message in restored.success])
        restored.button(key="trade-checklist-reset").click().run()
        self.assertFalse(restored.success)
        self.assertTrue(all(widget.value == "" for widget in restored.text_input))
        self.assertTrue(all(not widget.value for widget in restored.checkbox))
        self.assertFalse(any(load_checklist_draft(self.root).values()))
        again = self.page()
        self.assertFalse(again.success)
        self.assertTrue(all(widget.value == "" for widget in again.text_input))

    def test_wrong_balance_never_passes_with_all_checks(self) -> None:
        save_checklist_draft(self.root, valid_draft(account_balance="700"))
        page = self.page()
        self.assertFalse(page.success)
        self.assertTrue(any("700U" in item.value for item in page.markdown))

    def test_direction_type_and_reason_changes_also_revoke_confirmations(self) -> None:
        page = self.complete(self.page())
        page.selectbox(key="trade-checklist-direction").select("做空").run()
        self.assertFalse(any(widget.value for widget in page.checkbox))
        page = self.complete(page)
        page.selectbox(key="trade-checklist-trade_type").select("趋势交易").run()
        self.assertFalse(any(widget.value for widget in page.checkbox))
        page = self.complete(page)
        page.text_area(key="trade-checklist-exit_reason").set_value("结构破坏").run()
        self.assertFalse(any(widget.value for widget in page.checkbox))

    def test_save_failure_preserves_inputs_and_retry_recovers(self) -> None:
        page = self.page()
        path = self.root / "data/crypto/trade_checklist/draft.json"
        path.unlink()
        path.mkdir()
        self.complete(page)
        self.assertFalse(page.exception)
        self.assertTrue(any("保存失败" in message.value for message in page.error))
        self.assertFalse(any("已自动保存" in message.value for message in page.caption))
        self.assertEqual(page.text_input(key="trade-checklist-entry_price").value, "100")
        path.rmdir()
        page.button(key="trade-checklist-retry").click().run()
        self.assertFalse(page.error)
        self.assertEqual(load_checklist_draft(self.root)["entry_price"], "100")

    def test_corrupt_draft_is_reported_and_only_explicit_reset_replaces_it(self) -> None:
        path = self.root / "data/crypto/trade_checklist/draft.json"
        path.parent.mkdir(parents=True)
        path.write_text("bad-json", encoding="utf-8")
        page = self.page()
        self.assertFalse(page.exception)
        self.assertTrue(page.error)
        self.complete(page)
        self.assertFalse(page.success)
        self.assertEqual(path.read_text(encoding="utf-8"), "bad-json")
        page.button(key="trade-checklist-reset").click().run()
        self.assertFalse(page.error)
        self.assertFalse(page.success)

    def test_retry_after_failed_reset_saves_preserved_input(self) -> None:
        page = self.complete(self.page())
        path = self.root / "data/crypto/trade_checklist/draft.json"
        path.unlink()
        path.mkdir()
        page.button(key="trade-checklist-reset").click().run()
        self.assertTrue(page.error)
        self.assertEqual(page.text_input(key="trade-checklist-entry_price").value, "100")
        path.rmdir()
        page.button(key="trade-checklist-retry").click().run()
        self.assertFalse(page.error)
        self.assertEqual(load_checklist_draft(self.root)["entry_price"], "100")
        self.assertIn("检查通过", [message.value for message in page.success])

    def test_crypto_page_navigation_opens_checklist_and_keeps_draft(self) -> None:
        page = AppTest.from_string(
            "from pathlib import Path\nimport app\n"
            f"app.ROOT = Path({str(self.root)!r})\n"
            "app._render_crypto_turnover_page()\n"
        ).run()
        self.assertFalse(page.exception)
        self.assertIn("币圈现货关注提醒", [title.value for title in page.title])
        page.radio(key="crypto-page-section").set_value("交易前检查").run()
        self.assertFalse(page.exception)
        self.assertIn("交易前检查", [title.value for title in page.title])
        self.complete(page)
        page.radio(key="crypto-page-section").set_value("行情提醒").run()
        page.radio(key="crypto-page-section").set_value("交易前检查").run()
        self.assertIn("检查通过", [message.value for message in page.success])


if __name__ == "__main__":
    unittest.main()
