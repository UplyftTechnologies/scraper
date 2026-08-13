import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pricing_scraper.automation import source_fingerprint
from pricing_scraper.models import Product
from pricing_scraper.refresh import (
    CHANGED,
    FRESH,
    INCOMPLETE,
    NEW,
    STALE,
    RefreshPolicy,
    build_plan,
    decide,
    load_known_products,
    plan_for_site,
)

NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


def product(product_id="sku-1", **overrides):
    values = {
        "site": "nykaa",
        "product_id": product_id,
        "brand": "Brand",
        "product_name": "Face wash",
        "selling_price": 400.0,
    }
    values.update(overrides)
    return Product(**values)


def stored_row(item=None, *, scraped_at=None, **overrides):
    item = item or product()
    row = item.to_dict()
    row["description"] = "A gentle cleanser"
    row["image_urls"] = ["https://example.test/a.jpg"]
    row["scraped_at"] = scraped_at or (NOW - timedelta(days=1)).isoformat()
    row["source_fingerprint"] = source_fingerprint(item)
    row.update(overrides)
    return row


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.policy = RefreshPolicy(refresh_days=30)

    def test_an_unknown_product_is_new(self):
        decision = decide(product(), None, policy=self.policy, now=NOW)
        self.assertTrue(decision.needed)
        self.assertEqual(decision.reason, NEW)

    def test_a_complete_recent_product_is_left_alone(self):
        decision = decide(product(), stored_row(), policy=self.policy, now=NOW)
        self.assertFalse(decision.needed)
        self.assertEqual(decision.reason, FRESH)

    def test_a_price_change_is_picked_up_the_same_day(self):
        """The stored row is recent and complete, but the listing disagrees."""
        stored = stored_row()
        decision = decide(
            product(selling_price=349.0), stored, policy=self.policy, now=NOW
        )
        self.assertTrue(decision.needed)
        self.assertEqual(decision.reason, CHANGED)

    def test_a_row_missing_detail_content_is_requested(self):
        decision = decide(
            product(), stored_row(description=""), policy=self.policy, now=NOW
        )
        self.assertTrue(decision.needed)
        self.assertEqual(decision.reason, INCOMPLETE)

    def test_an_empty_gallery_counts_as_missing(self):
        decision = decide(
            product(), stored_row(image_urls=[]), policy=self.policy, now=NOW
        )
        self.assertTrue(decision.needed)
        self.assertEqual(decision.reason, INCOMPLETE)

    def test_a_row_older_than_the_window_is_refreshed(self):
        stale = stored_row(scraped_at=(NOW - timedelta(days=45)).isoformat())
        decision = decide(product(), stale, policy=self.policy, now=NOW)
        self.assertTrue(decision.needed)
        self.assertEqual(decision.reason, STALE)

    def test_a_row_inside_the_window_is_not(self):
        recent = stored_row(scraped_at=(NOW - timedelta(days=29)).isoformat())
        decision = decide(product(), recent, policy=self.policy, now=NOW)
        self.assertFalse(decision.needed)

    def test_an_unreadable_timestamp_is_treated_as_stale(self):
        decision = decide(
            product(), stored_row(scraped_at="not a date"), policy=self.policy, now=NOW
        )
        self.assertTrue(decision.needed)
        self.assertEqual(decision.reason, STALE)

    def test_a_disabled_policy_always_requests(self):
        policy = RefreshPolicy(enabled=False)
        decision = decide(product(), stored_row(), policy=policy, now=NOW)
        self.assertTrue(decision.needed)

    def test_zero_days_disables_only_the_age_rule(self):
        """A window of zero keeps change and completeness checks working."""
        policy = RefreshPolicy(refresh_days=0)
        ancient = stored_row(scraped_at="2001-01-01T00:00:00+00:00")
        self.assertFalse(decide(product(), ancient, policy=policy, now=NOW).needed)
        self.assertTrue(
            decide(
                product(selling_price=1.0), ancient, policy=policy, now=NOW
            ).needed
        )


class PolicyConfigTests(unittest.TestCase):
    def test_it_falls_back_to_the_nightly_refresh_window(self):
        policy = RefreshPolicy.from_config(
            {"automation": {"detail_refresh_days": 14}}
        )
        self.assertEqual(policy.refresh_days, 14)
        self.assertTrue(policy.enabled)

    def test_its_own_section_wins(self):
        policy = RefreshPolicy.from_config(
            {
                "automation": {"detail_refresh_days": 14},
                "refresh": {"refresh_days": 7, "required_fields": ["description"]},
            }
        )
        self.assertEqual(policy.refresh_days, 7)
        self.assertEqual(policy.required_fields, ("description",))

    def test_an_explicit_override_beats_the_config(self):
        policy = RefreshPolicy.from_config(
            {"refresh": {"enabled": True}}, enabled=False
        )
        self.assertFalse(policy.enabled)


class PlanTests(unittest.TestCase):
    def test_it_counts_each_outcome(self):
        known = {
            "fresh-1": stored_row(product("fresh-1")),
            "stale-1": stored_row(
                product("stale-1"), scraped_at="2020-01-01T00:00:00+00:00"
            ),
        }
        plan = build_plan(
            [product("fresh-1"), product("stale-1"), product("new-1")],
            known,
            policy=RefreshPolicy(refresh_days=30),
            now=NOW,
        )
        self.assertEqual(plan.to_request, 2)
        self.assertEqual(plan.skipped, 1)
        self.assertEqual(plan.counts, {FRESH: 1, STALE: 1, NEW: 1})
        self.assertFalse(plan.needs("fresh-1"))
        self.assertTrue(plan.needs("stale-1"))
        # A product the plan never saw must never be skipped.
        self.assertTrue(plan.needs("unseen"))

    def test_stored_rows_can_be_rebuilt_for_reuse(self):
        """A skipped product still has to reach the export with its content."""
        known = {"fresh-1": stored_row(product("fresh-1"))}
        plan = build_plan(
            [product("fresh-1")],
            known,
            policy=RefreshPolicy(refresh_days=30),
            now=NOW,
        )
        rebuilt = plan.stored_products()
        self.assertEqual(rebuilt["fresh-1"].description, "A gentle cleanser")
        self.assertEqual(
            rebuilt["fresh-1"].image_urls, ["https://example.test/a.jpg"]
        )


class KnownProductTests(unittest.TestCase):
    def test_it_reads_the_csv_export_for_one_site(self):
        from pricing_scraper.exporter import export_products

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "combined.csv"
            export_products(
                [
                    product("nykaa-1", site="nykaa", description="x"),
                    product("tira-1", site="tira", description="y"),
                ],
                root / "out.xlsx",
                csv_path,
            )

            known, source = load_known_products(
                "nykaa", csv_path=csv_path, use_database=False
            )

        self.assertEqual(source, "csv")
        self.assertEqual(set(known), {"nykaa-1"})

    def test_a_missing_export_is_not_an_error(self):
        """A freshness check that cannot read anything must not stop a run."""
        known, source = load_known_products(
            "nykaa", csv_path=Path("nope/missing.csv"), use_database=False
        )
        self.assertEqual(known, {})
        self.assertEqual(source, "none")

    def test_a_disabled_policy_reads_nothing_at_all(self):
        plan = plan_for_site(
            "nykaa",
            [product()],
            policy=RefreshPolicy(enabled=False),
            csv_path=Path("nope/missing.csv"),
            use_database=False,
        )
        self.assertEqual(plan.source, "disabled")
        self.assertTrue(plan.needs("sku-1"))


if __name__ == "__main__":
    unittest.main()
