"""Employee identity, PII approval, and force-reset — large blast radius if wrong."""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401


class CreateEmployeeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import employee as employee_module

        importlib.reload(employee_module)
        cls.Employee = employee_module.Employee
        cls.cursor = employee_module.cursor
        cls.db = employee_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()

    def _create(self, emp, **overrides):
        kwargs = dict(
            emp_id="emp1",
            last_name="L",
            middle_name="",
            first_name="F",
            contact_no="4155552671",
            email_id="a@b.com",
            password="pw-secret",
            ssn="123456789",
            dob="2000-01-01",
            tier=1,
        )
        kwargs.update(overrides)
        return emp.create_employee(**kwargs)

    def test_duplicate_userid(self):
        emp = self.Employee()
        with patch.object(emp, "check_user_id", return_value=1):
            self.assertEqual(self._create(emp), "EmpID already Exists")
        self.cursor.execute.assert_not_called()

    def test_duplicate_contact(self):
        emp = self.Employee()
        with patch.object(emp, "check_user_id", return_value=0), patch.object(
            emp, "check_existing_contact", return_value=1
        ):
            self.assertEqual(self._create(emp), "Contact already Exists")
        self.cursor.execute.assert_not_called()

    def test_duplicate_ssn(self):
        emp = self.Employee()
        with patch.object(emp, "check_user_id", return_value=0), patch.object(
            emp, "check_existing_contact", return_value=0
        ), patch.object(emp, "check_existing_ssn", return_value=1):
            self.assertEqual(self._create(emp), "SSN already Exists")
        self.cursor.execute.assert_not_called()

    def test_duplicate_email(self):
        emp = self.Employee()
        with patch.object(emp, "check_user_id", return_value=0), patch.object(
            emp, "check_existing_contact", return_value=0
        ), patch.object(emp, "check_existing_ssn", return_value=0), patch.object(
            emp, "check_existing_email", return_value=1
        ):
            self.assertEqual(self._create(emp), "Email already Exists")
        self.cursor.execute.assert_not_called()

    def test_insert_hashes_password_and_ssn(self):
        from utility.encrypt import encrypt_ssn

        emp = self.Employee()
        with patch.object(emp, "check_user_id", return_value=0), patch.object(
            emp, "check_existing_contact", return_value=0
        ), patch.object(emp, "check_existing_ssn", return_value=0), patch.object(
            emp, "check_existing_email", return_value=0
        ):
            self.assertEqual(self._create(emp), 1)
        sql = self.cursor.execute.call_args.args[0]
        self.assertNotIn("pw-secret", sql)
        self.assertIn("$2", sql)
        self.assertIn(encrypt_ssn("123456789"), sql)
        self.db.commit.assert_called()


class EmployeePiiAndResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import employee as employee_module

        cls.Employee = employee_module.Employee
        cls.cursor = employee_module.cursor
        cls.db = employee_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()
        self.cursor.fetchall.side_effect = None
        self.cursor.fetchone.side_effect = None
        self.db.commit.side_effect = None

    def test_approve_update_info_employee_requester_updates_employees(self):
        emp = self.Employee()
        self.cursor.fetchall.return_value = [
            ("Employee", "emp2", "555", "e@x.com", "addr")
        ]
        with patch.object(emp, "get_employee_tier", return_value=2), patch.object(
            emp, "getTier2_emp", return_value="t2"
        ):
            self.assertEqual(emp.approve_update_info("emp1", 9), "Employee Updated")
        executed = " ".join(str(call.args[0]) for call in self.cursor.execute.call_args_list)
        self.assertIn("UPDATE Employees", executed)
        self.assertIn("e@x.com", executed)
        self.assertIn("status = 0", executed)
        self.assertNotIn("UPDATE Customers", executed)

    def test_deny_update_info_closes_request(self):
        self.assertEqual(self.Employee().deny_update_info("emp1", 12), "Done")
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("status = 0", sql)
        self.assertIn("12", sql)
        self.db.commit.assert_called()

    def test_force_reset_missing_user(self):
        emp = self.Employee()
        with patch.object(emp, "check_user_id", return_value=0):
            self.assertEqual(emp.reset_fpassword("ghost", "new"), "UserID doesn't exists")
        self.cursor.execute.assert_not_called()

    def test_force_reset_hashes_password(self):
        emp = self.Employee()
        with patch.object(emp, "check_user_id", return_value=1):
            self.assertEqual(emp.reset_fpassword("emp1", "new-secret"), "Password Updated")
        sql = self.cursor.execute.call_args.args[0]
        self.assertNotIn("new-secret", sql)
        self.assertIn("$2", sql)
        self.db.commit.assert_called()

    def test_get_employee_details_maps_fields(self):
        self.cursor.fetchall.return_value = [
            ("F", "M", "L", "dob", "phone", "e@x.com", "addr", "ssn", 1, 2)
        ]
        result = self.Employee().get_employee_details("emp1")
        self.assertEqual(result["first_name"], "F")
        self.assertEqual(result["tier"], 2)
        self.assertEqual(result["email_id"], "e@x.com")

    def test_get_employee_details_missing(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Employee().get_employee_details("ghost"), "None")

    def test_transfer_to_tier2_rewrites_approvers(self):
        emp = self.Employee()
        with patch.object(emp, "getTier2_emp", return_value="t2"):
            self.assertEqual(
                emp.transfer_transaction_to_tier2(77),
                "Request Sent to Tier2 employee",
            )
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("approver1_id='-1'", sql)
        self.assertIn("approver2 = 2", sql)
        self.assertIn("77", sql)

    def test_retrieve_hashed_password_is_parameterized(self):
        self.cursor.fetchone.return_value = ("$2b$hash",)
        hashed = self.Employee().retrieve_hashed_password("emp1")
        self.assertEqual(hashed, "$2b$hash")
        args, kwargs = self.cursor.execute.call_args
        self.assertEqual(args[1], ("emp1",))
        self.assertNotIn("'emp1'", args[0])

    def test_check_existing_ssn_queries_employees(self):
        self.cursor.fetchall.return_value = [("emp1",)]
        self.assertEqual(self.Employee().check_existing_ssn("123456789"), 1)
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("Employees", sql)
        self.assertIn("123456789", sql)

    def test_update_info_list_tier3_uses_admin_approver(self):
        emp = self.Employee()
        self.cursor.fetchall.return_value = [(1,)]
        with patch.object(emp, "get_employee_tier", return_value=3):
            result = emp.update_info_request_list("admin1")
        self.assertEqual(result, [(1,)])
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("approver = 3", sql)
        self.assertIn("status = 1", sql)

    def test_update_info_list_non_admin_uses_tier1_queue(self):
        emp = self.Employee()
        self.cursor.fetchall.return_value = []
        with patch.object(emp, "get_employee_tier", return_value=1):
            self.assertEqual(emp.update_info_request_list("emp1"), 0)
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("approver = 1", sql)

    def test_tier3_updates_employee_record(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=3):
            self.assertEqual(
                emp.update_employee(
                    "admin1", "emp2", "a@b.com", "A", "", "B", "1", "d", "x"
                ),
                "Employee Updated",
            )
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("UPDATE Employees", sql)
        self.assertIn("emp2", sql)
        self.assertNotIn("ssn", sql.lower())

    def test_update_account_info_writes_ssn_and_tier(self):
        self.assertEqual(
            self.Employee().update_account_info(
                "emp1", "L", "", "F", "1", "a@b.com", "123456789", "dob", "addr", 2
            ),
            "updated",
        )
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("UPDATE Employees", sql)
        self.assertIn("123456789", sql)
        self.assertIn("tier=2", sql.replace(" ", ""))
        self.db.commit.assert_called()

    def test_verify_employee_queries_hashed_password(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Employee().verify_employee("emp1", "pw-secret"), 0)
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("Employees", sql)
        self.assertIn("active=1", sql)
        self.assertNotIn("pw-secret", sql)
        self.assertIn("$2", sql)


if __name__ == "__main__":
    unittest.main()
