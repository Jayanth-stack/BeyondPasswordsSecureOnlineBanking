import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

DATA_FILES = (
    os.path.join(ROOT, 'customer.py'),
    os.path.join(ROOT, 'employee.py'),
)

# Classic "% (user_input)" interpolation of SQL strings.
INTERPOLATED_QUERY = re.compile(
    r"""(?:SELECT|INSERT|UPDATE|DELETE|VALUES)[\s\S]{0,400}?['\"]\s*%\s*\(""",
    re.IGNORECASE,
)
PERCENT_FORMAT_TYPES = re.compile(r"WHERE\s+\w+\s*=\s*%[df]\b", re.IGNORECASE)
FSTRING_SQL = re.compile(
    r"""f['\"]{3}[\s\S]*?(?:SELECT|INSERT|UPDATE|DELETE)""",
    re.IGNORECASE,
)


class SqlSafetyScanTests(unittest.TestCase):
    def test_no_string_interpolated_queries(self):
        for path in DATA_FILES:
            with self.subTest(file=os.path.basename(path)):
                with open(path) as handle:
                    source = handle.read()
                self.assertIsNone(
                    INTERPOLATED_QUERY.search(source),
                    '%-formatting still used to build SQL in {}'.format(path),
                )
                self.assertIsNone(
                    PERCENT_FORMAT_TYPES.search(source),
                    'numeric %%d/%%f interpolation still used in {}'.format(path),
                )
                self.assertIsNone(
                    FSTRING_SQL.search(source),
                    'f-string SQL still used in {}'.format(path),
                )
                self.assertNotIn('cursor.execute', source)


if __name__ == '__main__':
    unittest.main()
