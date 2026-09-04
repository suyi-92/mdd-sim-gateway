import unittest

from control.app.lpa import LpaError


class LpaErrorMessageTests(unittest.TestCase):
    def test_duplicate_profile_is_reported_as_already_installed(self):
        error = LpaError(
            "es10b_load_bound_profile_package",
            detail="store_metadata,install_failed_due_to_iccid_already_exists_on_euicc",
        )

        message = error.user_message()

        self.assertIn("already installed", message)
        self.assertIn("Refresh the profile list", message)
        self.assertNotIn("store_metadata", message)


if __name__ == "__main__":
    unittest.main()
