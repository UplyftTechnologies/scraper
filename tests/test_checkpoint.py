import tempfile
import unittest
from pathlib import Path

from pricing_scraper.checkpoint import CheckpointStore, DetailCheckpointStore
from pricing_scraper.models import Product


class CheckpointTests(unittest.TestCase):
    def test_skips_interrupted_null_tail_without_losing_valid_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DetailCheckpointStore(
                Path(directory),
                site="tira",
                category_id="all-selected",
            )
            store.append_parent(
                "parent-1",
                [
                    Product(
                        site="tira",
                        product_id="sku-1",
                        brand="Test",
                        product_name="Test",
                    )
                ],
            )
            with store.products_path.open("ab") as handle:
                handle.write(b"\x00" * 128 + b"\n")
            with store.processed_path.open("ab") as handle:
                handle.write(b"\x00" * 32 + b"\n")

            self.assertEqual(
                [product.product_id for product in store.load_products()],
                ["sku-1"],
            )
            self.assertEqual(
                store.load_processed_ids(),
                {"parent-1"},
            )

    def test_long_scope_uses_stable_windows_safe_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            scope = "_".join(f"category_{index}" for index in range(100))
            first = DetailCheckpointStore(
                Path(directory),
                site="tira",
                category_id=scope,
            )
            second = DetailCheckpointStore(
                Path(directory),
                site="tira",
                category_id=scope,
            )

            self.assertEqual(first.products_path, second.products_path)
            self.assertLess(len(first.products_path.name), 130)
            first.append_parent(
                "parent-1",
                [
                    Product(
                        site="tira",
                        product_id="sku-1",
                        brand="Test",
                        product_name="Test",
                    )
                ],
            )
            self.assertTrue(first.products_path.exists())

    def test_detail_checkpoint_tracks_processed_parents_and_sku_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DetailCheckpointStore(
                Path(directory),
                site="nykaa",
                category_id="8377",
            )
            products = [
                Product(
                    site="nykaa",
                    product_id="sku-88",
                    parent_product_id="parent-1",
                    sku="SKU88",
                    brand="Brand",
                    product_name="Cleanser",
                    variant="88ml",
                ),
                Product(
                    site="nykaa",
                    product_id="sku-236",
                    parent_product_id="parent-1",
                    sku="SKU236",
                    brand="Brand",
                    product_name="Cleanser",
                    variant="236ml",
                ),
            ]
            state = store.append_parent("parent-1", products)
            self.assertEqual(state.parents_processed, 1)
            self.assertEqual(state.products_saved, 2)
            self.assertEqual(store.load_processed_ids(), {"parent-1"})
            self.assertEqual(len(store.load_products()), 2)
            self.assertTrue(store.mark_complete().completed)

    def test_appends_pages_resumes_and_marks_empty_page_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(
                Path(directory),
                site="nykaa",
                category_id="8377",
                start_page=1,
            )
            product = Product(
                site="nykaa",
                product_id="sku-1",
                brand="Brand",
                product_name="Face wash",
            )

            state = store.append_page(1, [product])
            self.assertEqual(state.next_page, 2)
            self.assertFalse(state.completed)

            restored = CheckpointStore(
                Path(directory),
                site="nykaa",
                category_id="8377",
                start_page=1,
            )
            self.assertEqual(restored.load_state().next_page, 2)
            self.assertEqual(
                [item.product_id for item in restored.load_products()],
                ["sku-1"],
            )

            completed = restored.mark_complete(
                empty_page=2,
                products=restored.load_products(),
            )
            self.assertTrue(completed.completed)
            self.assertEqual(completed.products_saved, 1)

    def test_null_filled_detail_state_rebuilds_from_append_only_files(self):
        """A crash-damaged state file must not end the run before it starts.

        An interrupted rename leaves the state file the right length and full
        of NUL bytes. Everything it recorded is recoverable from the processed
        and products files beside it, so the run has to resume, not fail.
        """
        with tempfile.TemporaryDirectory() as directory:
            store = DetailCheckpointStore(
                Path(directory),
                site="nykaa",
                category_id="scope",
            )
            store.append_parent(
                "parent-1",
                [
                    Product(
                        site="nykaa",
                        product_id="sku-1",
                        brand="Brand",
                        product_name="Serum",
                    )
                ],
            )
            damaged_length = store.state_path.stat().st_size
            store.state_path.write_bytes(b"\x00" * damaged_length)

            state = store.load_state()

            self.assertFalse(state.completed)
            self.assertEqual(state.parents_processed, 1)
            self.assertEqual(state.products_saved, 1)
            self.assertEqual(store.load_processed_ids(), {"parent-1"})
            # The damaged file is kept for diagnosis and replaced with a
            # readable one, so the next run does not repeat the recovery.
            self.assertTrue(
                any(
                    path.name.startswith(f"{store.state_path.name}.corrupt-")
                    for path in Path(directory).iterdir()
                )
            )
            self.assertEqual(store.load_state().parents_processed, 1)

    def test_null_filled_listing_state_restarts_category_from_first_page(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(
                Path(directory),
                site="nykaa",
                category_id="8377",
                start_page=1,
            )
            store.append_page(
                1,
                [
                    Product(
                        site="nykaa",
                        product_id="sku-1",
                        brand="Brand",
                        product_name="Face wash",
                    )
                ],
            )
            store.state_path.write_bytes(
                b"\x00" * store.state_path.stat().st_size
            )

            state = store.load_state()

            self.assertEqual(state.next_page, 1)
            self.assertFalse(state.completed)
            # The rows already collected survive; the client is given their IDs
            # so the repeated pages cost requests, not data.
            self.assertEqual(
                [item.product_id for item in store.load_products()],
                ["sku-1"],
            )

    def test_state_file_is_flushed_before_the_rename(self):
        """The write must leave no temporary file and must be readable at once."""
        with tempfile.TemporaryDirectory() as directory:
            store = DetailCheckpointStore(
                Path(directory),
                site="tira",
                category_id="scope",
            )
            store.append_parent("parent-1", [])

            self.assertTrue(store.state_path.exists())
            self.assertEqual(
                [path.name for path in Path(directory).glob("*.tmp")],
                [],
            )
            self.assertNotIn(b"\x00", store.state_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
