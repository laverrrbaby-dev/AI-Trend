from __future__ import annotations

import re
import sys
import types
import unittest
from unittest.mock import patch


def _install_app_import_stubs() -> None:
    try:
        import plotly.graph_objects  # noqa: F401
        import streamlit  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    class _ColumnConfig:
        @staticmethod
        def NumberColumn(label: str, **kwargs: object) -> dict[str, object]:
            return {"label": label, "help": kwargs.get("help"), "type_config": {"type": "number", **kwargs}}

        @staticmethod
        def ProgressColumn(label: str, **kwargs: object) -> dict[str, object]:
            return {"label": label, "help": kwargs.get("help"), "type_config": {"type": "progress", **kwargs}}

        @staticmethod
        def TextColumn(label: str, **kwargs: object) -> dict[str, object]:
            return {"label": label, "help": kwargs.get("help"), "type_config": {"type": "text", **kwargs}}

    streamlit_stub = types.ModuleType("streamlit")
    streamlit_stub.__path__ = []
    streamlit_stub.column_config = _ColumnConfig()

    components_pkg = types.ModuleType("streamlit.components")
    components_pkg.__path__ = []
    components_v1 = types.ModuleType("streamlit.components.v1")
    components_v1.html = lambda *args, **kwargs: None

    plotly_pkg = types.ModuleType("plotly")
    plotly_pkg.__path__ = []
    graph_objects = types.ModuleType("plotly.graph_objects")
    graph_objects.Figure = object
    graph_objects.Scatter = object
    graph_objects.Candlestick = object
    graph_objects.Bar = object

    sys.modules.setdefault("streamlit", streamlit_stub)
    sys.modules.setdefault("streamlit.components", components_pkg)
    sys.modules.setdefault("streamlit.components.v1", components_v1)
    sys.modules.setdefault("plotly", plotly_pkg)
    sys.modules.setdefault("plotly.graph_objects", graph_objects)


_install_app_import_stubs()

import app


CODE_COPY_PATTERN = r"^(?:\d{6}|[A-Z][A-Z0-9]{0,5}(?:[.-][A-Z0-9]{1,2})?)$"


class CodeCopyTests(unittest.TestCase):
    def test_code_pattern_accepts_a_share_and_us_stock_symbols(self) -> None:
        accepted = ["159995", "MU", "NVDA", "BRK.B", "BF-B"]
        rejected = ["15999", "1234567", "BRK/B", "TOO-LONG"]

        for code in accepted:
            with self.subTest(code=code):
                self.assertIsNotNone(re.fullmatch(CODE_COPY_PATTERN, code))
        for code in rejected:
            with self.subTest(code=code):
                self.assertIsNone(re.fullmatch(CODE_COPY_PATTERN, code))

    def test_copy_handler_embeds_shared_code_pattern(self) -> None:
        with patch.object(app, "_embed_hidden_html") as embed:
            app._install_code_copy_handler()

        html = embed.call_args.args[0]
        self.assertIn(f"const codePattern = /{CODE_COPY_PATTERN}/;", html)
        self.assertIn('"dblclick"', html)
        self.assertIn("copyCode(code)", html)


if __name__ == "__main__":
    unittest.main()
