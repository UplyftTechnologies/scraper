import unittest

from pricing_scraper.models import Product
from pricing_scraper.supervisor import apply_propagation, plan_propagation


def product(site, product_id, name, *, gtin="", variant="30 ml", brand="Minimalist"):
    return Product(
        site=site,
        product_id=product_id,
        brand=brand,
        product_name=name,
        variant=variant,
        gtin=gtin,
        selling_price=499.0,
    )


SERUM = "Niacinamide 10% Face Serum"


class PlanTests(unittest.TestCase):
    def test_a_barcode_spreads_to_the_platforms_missing_it(self):
        plan = plan_propagation(
            [
                product("nykaa", "N1", SERUM, gtin="8904417306224"),
                product("tira", "T1", SERUM),
                product("amazon", "A1", SERUM),
            ]
        )

        self.assertEqual(len(plan.filled), 2)
        self.assertEqual({item.site for item in plan.filled}, {"tira", "amazon"})
        self.assertEqual({item.gtin for item in plan.filled}, {"8904417306224"})
        self.assertEqual({item.donor_site for item in plan.filled}, {"nykaa"})

    def test_a_product_that_has_its_own_barcode_is_left_alone(self):
        plan = plan_propagation(
            [
                product("nykaa", "N1", SERUM, gtin="8904417306224"),
                product("tira", "T1", SERUM, gtin="8904417306224"),
            ]
        )
        self.assertEqual(plan.filled, [])

    def test_a_different_volume_is_a_different_product(self):
        """The whole point of the rule: 30 ml and 60 ml are not the same SKU."""
        plan = plan_propagation(
            [
                product("nykaa", "N1", SERUM, gtin="8904417306224", variant="30 ml"),
                product("tira", "T1", SERUM, variant="60 ml"),
            ]
        )
        self.assertEqual(plan.filled, [])

    def test_a_different_brand_never_matches(self):
        plan = plan_propagation(
            [
                product("nykaa", "N1", SERUM, gtin="8904417306224"),
                product("tira", "T1", SERUM, brand="The Ordinary"),
            ]
        )
        self.assertEqual(plan.filled, [])

    def test_a_different_strength_never_matches(self):
        plan = plan_propagation(
            [
                product("nykaa", "N1", "Niacinamide 10% Serum", gtin="8904417306224"),
                product("tira", "T1", "Niacinamide 5% Serum"),
            ]
        )
        self.assertEqual(plan.filled, [])

    def test_two_platforms_disagreeing_never_match_each_other(self):
        """A different published barcode is itself proof of a different product.

        The matcher rejects the pair outright, so the disagreement is settled
        before the supervisor ever sees it.
        """
        plan = plan_propagation(
            [
                product("nykaa", "N1", SERUM, gtin="8904417306224"),
                product("tira", "T1", SERUM, gtin="8906087778462"),
            ]
        )
        self.assertEqual(plan.filled, [])
        self.assertEqual(plan.conflicts, [])

    def test_a_conflict_through_a_barcodeless_middle_is_reported(self):
        """Nykaa has no barcode here, so Tira and Amazon both match it.

        Each pairing is scored against the anchor, which carries no barcode to
        contradict, so two platforms with different barcodes end up in one
        group. Copying either onto Nykaa would state something false, so the
        group is reported and skipped.
        """
        plan = plan_propagation(
            [
                product("nykaa", "N1", SERUM),
                product("tira", "T1", SERUM, gtin="8904417306224"),
                product("amazon", "A1", SERUM, gtin="8906087778462"),
            ]
        )

        self.assertEqual(plan.filled, [])
        self.assertEqual(len(plan.conflicts), 1)
        clash = plan.conflicts[0]
        self.assertEqual(
            {clash.left_gtin, clash.right_gtin},
            {"8904417306224", "8906087778462"},
        )

    def test_nykaa_is_preferred_over_amazon_as_the_source(self):
        """Amazon's barcode is inferred from a model number, so it ranks last."""
        plan = plan_propagation(
            [
                product("amazon", "A1", SERUM, gtin="8904417306224"),
                product("nykaa", "N1", SERUM, gtin="8904417306224"),
                product("tira", "T1", SERUM),
            ]
        )
        self.assertEqual([item.donor_site for item in plan.filled], ["nykaa"])

    def test_products_with_no_barcode_anywhere_are_untouched(self):
        plan = plan_propagation(
            [product("nykaa", "N1", SERUM), product("tira", "T1", SERUM)]
        )
        self.assertEqual(plan.filled, [])
        self.assertEqual(plan.conflicts, [])

    def test_counts_are_reported(self):
        plan = plan_propagation(
            [
                product("nykaa", "N1", SERUM, gtin="8904417306224"),
                product("tira", "T1", SERUM),
            ]
        )
        self.assertEqual(plan.considered, 2)
        self.assertEqual(plan.had_gtin, 1)
        self.assertEqual(plan.by_site, {"tira": 1})


class WriteTests(unittest.TestCase):
    class FakeStore:
        products_table = "retailer_products"

        def __init__(self, fail_for=()):
            self.patches = []
            self.fail_for = set(fail_for)

        def _patch(self, table, *, params, values):
            del table
            product_id = params["product_id"].removeprefix("eq.")
            if product_id in self.fail_for:
                raise RuntimeError("nope")
            self.patches.append((params["site"], product_id, values))

    def test_only_the_gtin_column_is_written(self):
        store = self.FakeStore()
        plan = plan_propagation(
            [
                product("nykaa", "N1", SERUM, gtin="8904417306224"),
                product("tira", "T1", SERUM),
            ]
        )

        written, failures = apply_propagation(store, plan.filled)

        self.assertEqual(written, 1)
        self.assertEqual(failures, 0)
        site, product_id, values = store.patches[0]
        self.assertEqual((site, product_id), ("eq.tira", "T1"))
        # Nothing else on the row may be disturbed.
        self.assertEqual(values, {"gtin": "8904417306224"})

    def test_one_failed_write_does_not_stop_the_rest(self):
        store = self.FakeStore(fail_for={"T1"})
        plan = plan_propagation(
            [
                product("nykaa", "N1", SERUM, gtin="8904417306224"),
                product("tira", "T1", SERUM),
                product("amazon", "A1", SERUM),
            ]
        )

        written, failures = apply_propagation(store, plan.filled)

        self.assertEqual(written, 1)
        self.assertEqual(failures, 1)


if __name__ == "__main__":
    unittest.main()
