"""地下管网施工冲突协同基础契约测试。"""

import unittest

from utility_coordination import WorkPermit, unique_by_identity


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = {'permit_code': 'permit-code-001', 'corridor_code': 'corridor-code-001', 'revision': 1, 'state': 'state-001'}

    def test_fingerprint_is_stable(self) -> None:
        left = WorkPermit(**self.values)
        right = WorkPermit(**dict(reversed(list(self.values.items()))))
        self.assertEqual(left.fingerprint(), right.fingerprint())

    def test_evolve_keeps_original(self) -> None:
        original = WorkPermit(**self.values)
        change_key = next(key for key, value in self.values.items() if isinstance(value, str))
        changed = original.evolve(**{change_key: "revised-value"})
        self.assertNotEqual(original.fingerprint(), changed.fingerprint())
        self.assertEqual(getattr(original, change_key), self.values[change_key])

    def test_conflicting_identity_is_rejected(self) -> None:
        first = WorkPermit(**self.values)
        changed_values = dict(self.values)
        change_key = next(key for key in self.values if key != "permit_code")
        changed_values[change_key] = 2 if isinstance(changed_values[change_key], int) else "conflict"
        second = WorkPermit(**changed_values)
        with self.assertRaises(ValueError):
            unique_by_identity([first, second])


if __name__ == "__main__":
    unittest.main()
