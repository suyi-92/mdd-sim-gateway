import unittest
from unittest.mock import patch

from control.app import carrier_id


class CarrierIdTests(unittest.TestCase):
    def test_visited_network_uses_exact_plmn_over_stale_reported_name(self):
        value = carrier_id.visited_network("46000", "CHN-UNICOM")
        self.assertEqual(value["name"], "China Mobile")
        self.assertEqual(value["name_zh"], "中国移动")
        self.assertEqual(value["reported_name"], "CHN-UNICOM")
        self.assertTrue(value["name_conflict"])

    def test_visited_bilingual_labels_match_plain_aosp_network_records(self):
        carriers, _, _ = carrier_id._database()
        for code, english, chinese, raw in [
                ("46000", "China Mobile", "中国移动", "CHINA MOBILE"),
                ("46001", "China Unicom", "中国联通", "CHN-UNICOM"),
                ("46011", "China Telecom", "中国电信", "CHN-CT")]:
            with self.subTest(code=code):
                self.assertTrue(any(c.get("carrier_name") == english and any(
                    set(a) == {"mccmnc_tuple"} and code in a["mccmnc_tuple"]
                    for a in c["attributes"]) for c in carriers))
                value = carrier_id.visited_network(code, raw)
                self.assertEqual((value["name"], value["name_zh"]), (english, chinese))
                self.assertFalse(value["name_conflict"])

    def test_visited_labels_do_not_guess_unknown_codes_or_sim_brands(self):
        for code in ["46099", "460000", "23433", "", "46000;bad"]:
            value = carrier_id.visited_network(code, "CMLink")
            self.assertEqual(value["name"], "CMLink")
            self.assertEqual(value["name_zh"], "")
        self.assertEqual(carrier_id.lookup({"mcc": "234", "mnc": "33",
                         "carrier_identity": {"spn": "CMLink"}})["name"], "CMLink UK")

    def test_exact_three_digit_mnc_wins_over_an_earlier_two_digit_record(self):
        value = carrier_id.lookup({"mcc": "405", "mnc": "045"})
        self.assertEqual(value["name"], "TATA DOCOMO")
        self.assertEqual(value["plmn"], "405-045")

    def test_exact_record_wins_even_when_shorter_plmn_has_more_matching_attributes(self):
        carriers = [
            {"carrier_name": "Short", "attributes": [
                {"mccmnc_tuple": ["40545"], "spn": ["Test"]}]},
            {"carrier_name": "Exact", "attributes": [{"mccmnc_tuple": ["405045"]}]},
        ]
        with patch.object(carrier_id, "_database", return_value=(carriers, {}, 1)):
            value = carrier_id.lookup({"mcc": "405", "mnc": "045",
                                       "carrier_identity": {"spn": "Test"}})
        self.assertEqual(value["name"], "Exact")

    def test_exact_parent_fallback_wins_over_an_unconditional_shorter_plmn(self):
        carriers = [
            {"carrier_name": "Short", "attributes": [{"mccmnc_tuple": ["40545"]}]},
            {"carrier_name": "Exact", "attributes": [
                {"mccmnc_tuple": ["405045"], "spn": ["Missing"]}]},
        ]
        with patch.object(carrier_id, "_database", return_value=(carriers, {}, 1)):
            value = carrier_id.lookup({"mcc": "405", "mnc": "045"})
        self.assertEqual(value["name"], "Exact")
        self.assertEqual(value["plmn"], "405-045")

    def test_plain_plmn_resolves_the_home_network(self):
        value = carrier_id.lookup({"mcc": "234", "mnc": "10"})
        self.assertEqual(value["name"], "O2")
        self.assertEqual(value["home_network"], "O2")
        self.assertEqual(value["plmn"], "234-10")
        self.assertFalse(value["specific"])

    def test_spn_selects_a_specific_mvno(self):
        value = carrier_id.lookup({
            "mcc": "234", "mnc": "10",
            "carrier_identity": {"spn": "GIFFGAFF"},
        })
        self.assertEqual(value["name"], "giffgaff")
        self.assertEqual(value["home_network"], "O2")
        self.assertEqual(value["match_source"], "mccmnc+spn")
        self.assertTrue(value["specific"])

    def test_local_spn_rule_identifies_cmlink_on_shared_ee_plmn(self):
        value = carrier_id.lookup({
            "mcc": "234", "mnc": "33",
            "carrier_identity": {"spn": "CMLink", "gid1": "0000", "gid2": "FFFF"},
        })
        self.assertEqual(value["name"], "CMLink UK")
        self.assertEqual(value["home_network"], "EE")
        self.assertEqual(value["match_source"], "mccmnc+spn")
        self.assertTrue(value["specific"])

    def test_shared_ee_plmn_without_cmlink_spn_stays_generic(self):
        value = carrier_id.lookup({"mcc": "234", "mnc": "33"})
        self.assertEqual(value["name"], "EE")
        self.assertFalse(value["specific"])

    def test_gid_prefix_is_case_insensitive_and_tolerates_sim_padding(self):
        value = carrier_id.lookup({
            "mcc": "310", "mnc": "240",
            "carrier_identity": {"gid1": "354dffffffff"},
        })
        self.assertEqual(value["name"], "Ultra/Univision")
        self.assertEqual(value["home_network"], "T-Mobile - US")

    def test_legacy_three_digit_padding_recovers_a_two_digit_mnc(self):
        value = carrier_id.lookup({"mcc": "234", "mnc": "015"})
        self.assertEqual(value["name"], "Vodafone")
        self.assertEqual(value["plmn"], "234-15")

    def test_unknown_plmn_stays_explicit(self):
        value = carrier_id.lookup({"mcc": "999", "mnc": "99"})
        self.assertEqual(value["name"], "")
        self.assertEqual(value["plmn"], "999-99")
        self.assertEqual(value["database"], "none")


if __name__ == "__main__":
    unittest.main()
