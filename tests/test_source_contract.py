import hashlib
import json
import unittest

from food_recall.client import Client
from food_recall.decoding import parse_page, records as decode_records
from food_recall.origin import load_scenario, serve


class SourceContractTests(unittest.TestCase):
    def test_domain_observations_preserve_source_distinctions(self):
        scenario = load_scenario()
        records = scenario["source_records"]
        with serve(scenario) as origin:
            client = Client(origin.base_url)
            self.assertIsNone(records[0]["lots"])
            self.assertIn("L11", records[1]["lots"])
            self.assertIn("L11", records[2]["withdrawn_lots"])
            self.assertNotEqual(records[0]["authority"], records[3]["authority"])

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
