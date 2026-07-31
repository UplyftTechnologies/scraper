import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


class DashboardTests(unittest.TestCase):
    def test_dashboard_renders_saved_catalog_without_network(self):
        app_path = Path(__file__).resolve().parents[1] / "streamlit_app.py"
        app = AppTest.from_file(app_path, default_timeout=15).run()
        self.assertFalse(app.exception)
        self.assertTrue(
            any("Beauty pricing dashboard" in item.value for item in app.title)
        )
        self.assertGreaterEqual(len(app.metric), 4)
        self.assertGreaterEqual(len(app.dataframe), 1)
        self.assertTrue(
            {
                "Kits & Combos",
                "Cleansers",
                "Moisturizers",
                "Serums",
                "Sun Care",
                "Neck Creams",
                "Skin Supplements",
            }.issubset(set(app.sidebar.multiselect[0].options))
        )
        self.assertEqual(len(app.sidebar.multiselect[0].value), 17)

    def test_dashboard_switches_to_tira_collections(self):
        app_path = Path(__file__).resolve().parents[1] / "streamlit_app.py"
        app = AppTest.from_file(app_path, default_timeout=15).run()

        app.sidebar.selectbox[0].select("Tira").run()

        self.assertFalse(app.exception)
        self.assertIn("Moisturizers", app.sidebar.multiselect[0].options)
        self.assertIn(
            "Specialised Skincare",
            app.sidebar.multiselect[0].options,
        )
        self.assertEqual(len(app.sidebar.multiselect[0].value), 17)

    def test_dashboard_switches_to_amazon_categories(self):
        app_path = Path(__file__).resolve().parents[1] / "streamlit_app.py"
        app = AppTest.from_file(app_path, default_timeout=15).run()

        app.sidebar.selectbox[0].select("Amazon").run()

        self.assertFalse(app.exception)
        self.assertIn("Kits & Combos", app.sidebar.multiselect[0].options)
        self.assertIn("Skin Supplements", app.sidebar.multiselect[0].options)
        self.assertEqual(len(app.sidebar.multiselect[0].value), 17)
        self.assertTrue(app.sidebar.checkbox[1].disabled)


if __name__ == "__main__":
    unittest.main()
