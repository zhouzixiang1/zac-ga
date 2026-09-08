import unittest
from decimal import Decimal
from pathlib import Path
import generate_argument_values as generator


class ArgumentValuesTests(unittest.TestCase):
    def test_transfer_and_excitation_crossover_is_not_a_full_objective(self):
        values = generator.derive_values()
        one, two, trip = [Decimal(values[key]) for key in
                         ("ArgumentStayOneLoss", "ArgumentStayTwoLoss", "ArgumentRoundtripLoss")]
        self.assertLess(one, trip)
        self.assertGreater(two, trip)
        self.assertAlmostEqual(two, 2*one, places=5)

    def test_qft_decomposition_comes_from_accepted_pair(self):
        values = generator.derive_values()
        self.assertEqual(values["MechanismQFTBaseTransfers"], "648")
        self.assertEqual(values["MechanismQFTGATransfers"], "402")
        self.assertGreater(Decimal(values["MechanismQFTTransferGain"]), 0)
        self.assertLess(Decimal(values["MechanismQFTExcitationGain"]), 0)

    def test_generated_files_are_current(self):
        for path, expected in generator.outputs().items():
            self.assertEqual(Path(path).read_text(encoding="utf-8"), expected, str(path))


if __name__ == "__main__":
    unittest.main()
