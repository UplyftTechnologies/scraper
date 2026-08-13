import unittest

from pricing_scraper.db_sink import DatabaseSink
from pricing_scraper.models import Product


class FakeStore:
    products_table = "retailer_products"
    price_history_table = "retailer_price_history"

    def __init__(self, fail_on: set[int] | None = None):
        self.calls: list[tuple[str, int]] = []
        self.rows: list[dict] = []
        self.history: list[dict] = []
        self.finalized: list[dict] = []
        self.fail_on = fail_on or set()
        self._batch = 0

    def _upsert(self, table, rows, conflict):
        del conflict
        if table == self.products_table:
            self._batch += 1
            if self._batch in self.fail_on:
                raise RuntimeError("supabase said no")
            self.rows.extend(rows)
        else:
            self.history.extend(rows)
        self.calls.append((table, len(rows)))
        return len(rows)

    def finalize_missing(self, *, site, run_id, inactive_threshold):
        self.finalized.append(
            {"site": site, "run_id": run_id, "threshold": inactive_threshold}
        )


def products(count, *, site="tira", start=0):
    return [
        Product(
            site=site,
            product_id=f"P{index}",
            brand="Brand",
            product_name=f"Product {index}",
            selling_price=100.0 + index,
        )
        for index in range(start, start + count)
    ]


class DatabaseSinkTests(unittest.TestCase):
    def test_it_writes_in_batches_while_the_run_works(self):
        store = FakeStore()
        sink = DatabaseSink(store=store, site="tira", batch_size=10)

        sink.add(products(25))

        # 25 products at 10 per batch: two full batches sent, five still queued.
        self.assertEqual(len(store.rows), 20)
        result = sink.close()
        self.assertEqual(len(store.rows), 25)
        self.assertEqual(result.products_written, 25)
        self.assertEqual(result.price_points_written, 25)
        self.assertEqual(result.batches, 3)

    def test_every_product_gets_a_price_point(self):
        store = FakeStore()
        sink = DatabaseSink(store=store, site="tira", batch_size=100)
        sink.add(products(4))
        sink.close()

        self.assertEqual(len(store.history), 4)
        self.assertEqual(
            {row["product_id"] for row in store.history},
            {"P0", "P1", "P2", "P3"},
        )

    def test_rows_carry_the_run_id_so_missing_products_can_be_aged(self):
        store = FakeStore()
        sink = DatabaseSink(store=store, site="tira", run_id="run-1", batch_size=2)
        sink.add(products(2))
        sink.close()

        self.assertEqual(store.rows[0]["last_seen_run_id"], "run-1")
        self.assertTrue(store.rows[0]["is_active"])
        self.assertEqual(store.rows[0]["missing_run_count"], 0)
        self.assertTrue(store.rows[0]["first_seen_at"])

    def test_a_failed_batch_never_ends_the_run(self):
        """The checkpoint still holds the data, so a lost batch is recoverable."""
        store = FakeStore(fail_on={1})
        sink = DatabaseSink(store=store, site="tira", batch_size=5)

        sink.add(products(10))
        result = sink.close()

        self.assertEqual(result.failures, 1)
        self.assertIn("supabase said no", result.error)
        # The second batch still went through.
        self.assertEqual(result.products_written, 5)

    def test_a_complete_sweep_ages_products_it_did_not_see(self):
        store = FakeStore()
        sink = DatabaseSink(store=store, site="nykaa", run_id="run-9", batch_size=5)
        sink.add(products(1))

        sink.close(complete_sweep=True, inactive_threshold=3)

        self.assertEqual(len(store.finalized), 1)
        self.assertEqual(store.finalized[0]["site"], "nykaa")
        self.assertEqual(store.finalized[0]["run_id"], "run-9")

    def test_a_partial_sweep_never_ages_anything(self):
        """A run that stopped early has no opinion about what is missing."""
        store = FakeStore()
        sink = DatabaseSink(store=store, site="nykaa", run_id="run-9", batch_size=5)
        sink.add(products(1))

        sink.close(complete_sweep=False)

        self.assertEqual(store.finalized, [])

    def test_a_sweep_with_a_failed_batch_never_ages_anything(self):
        """A gap in the stream would make live products look missing."""
        store = FakeStore(fail_on={1})
        sink = DatabaseSink(store=store, site="nykaa", run_id="run-9", batch_size=1)
        sink.add(products(2))

        sink.close(complete_sweep=True)

        self.assertEqual(store.finalized, [])

    def test_products_without_an_id_are_ignored(self):
        store = FakeStore()
        sink = DatabaseSink(store=store, site="tira", batch_size=10)
        sink.add([Product(site="tira", product_id="", brand="B", product_name="N")])
        sink.close()
        self.assertEqual(store.rows, [])

    def test_a_product_seen_twice_is_sent_twice_so_detail_wins(self):
        """The listing row goes up first; the richer detail row replaces it."""
        store = FakeStore()
        sink = DatabaseSink(store=store, site="tira", batch_size=100)
        listing = Product(site="tira", product_id="P1", brand="B", product_name="N")
        detail = Product(
            site="tira",
            product_id="P1",
            brand="B",
            product_name="N",
            description="the full text",
        )
        sink.add([listing])
        sink.add([detail])
        sink.close()

        self.assertEqual(len(store.rows), 2)
        self.assertEqual(store.rows[-1]["description"], "the full text")


if __name__ == "__main__":
    unittest.main()
