"""The database must accept every site the scraper can produce.

Each retailer table pins `site` to a check constraint. A site added in Python
but not in SQL is rejected at write time with Postgres 23514, and because a
failed constraint rejects the whole request rather than the offending row, one
unknown site loses the entire batch it travelled in. That is how purplle,
kindlife and broadway rows were lost on their first run - and how `manual`
had never managed to store a row at all.
"""

import re
import unittest
from pathlib import Path

from pricing_scraper.dashboard_service import STOREFRONT_CLIENTS
from pricing_scraper.manual import MANUAL_SITE

SCHEMA = Path(__file__).resolve().parents[1] / "database" / "schema.sql"
TABLE = re.compile(
    r"create table if not exists public\.(\w+).*?"
    r"site text not null check \(site in \(([^)]*)\)\)",
    re.DOTALL,
)

# Sites that reach retailer_products, from every writer in the project.
SCRAPED_SITES = {"nykaa", "tira", "amazon", *STOREFRONT_CLIENTS}
# Where a scraped or imported product row lands.
PRODUCT_TABLES = {"retailer_products", "retailer_price_history"}


def allowed_by_table() -> dict[str, set[str]]:
    """Read each table's permitted site values out of the schema."""
    text = SCHEMA.read_text(encoding="utf-8")
    return {
        table: {value.strip().strip("'") for value in sites.split(",")}
        for table, sites in TABLE.findall(text)
    }


def allowed_site_sets() -> list[set[str]]:
    return list(allowed_by_table().values())


class SchemaSiteTests(unittest.TestCase):
    def test_the_schema_declares_a_site_constraint(self):
        self.assertTrue(allowed_site_sets(), "no site check constraint found")

    def test_every_scraped_site_is_accepted_by_every_table(self):
        for allowed in allowed_site_sets():
            missing = SCRAPED_SITES - allowed
            self.assertFalse(
                missing, f"schema rejects {sorted(missing)}; allowed: {sorted(allowed)}"
            )

    def test_the_manual_import_site_is_accepted_where_products_are_stored(self):
        """The spreadsheet import writes into retailer_products.

        It is not a scrape, so it is deliberately absent from the run table.
        """
        tables = allowed_by_table()
        for name in PRODUCT_TABLES:
            self.assertIn(name, tables, f"{name} has no site constraint to check")
            self.assertIn(MANUAL_SITE, tables[name])

    def test_every_table_with_a_site_column_was_checked(self):
        """A new table would otherwise be silently exempt from these tests."""
        self.assertGreaterEqual(len(allowed_by_table()), 3)

    def test_a_migration_exists_for_the_widened_constraint(self):
        """Editing schema.sql alone would not change a database already built."""
        migrations = sorted(SCHEMA.parent.glob("*.sql"))
        widened = [
            path
            for path in migrations
            if "drop constraint if exists" in path.read_text(encoding="utf-8").casefold()
            and "site_check" in path.read_text(encoding="utf-8").casefold()
        ]
        self.assertTrue(
            widened, "schema.sql changed but no migration widens an existing database"
        )


if __name__ == "__main__":
    unittest.main()
