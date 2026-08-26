from customer import Customers

from utility.encrypt import encrypt, encrypt_ssn
from datetime import datetime
from utility.db import Database


def getdate():
    now = datetime.now()
    return now.strftime('%Y-%m-%d')


class Employee:
    def __init__(self, db=None):
        self.db = db or Database()

    #################        FUNCTION TO CREATE EMPLOYEE          #################
    def create_employee(self, emp_id, last_name, middle_name, first_name, contact_no, email_id, password, ssn, dob,
                        tier, active=1, address=""):
        if self.check_user_id(emp_id) == 1:
            return 'EmpID already Exists'

        if self.check_existing_contact(contact_no) == 1:
            return 'Contact already Exists'

        if self.check_existing_ssn(ssn) == 1:
            return 'SSN already Exists'

        if self.check_existing_email(email_id) == 1:
            return 'Email already Exists'

        try:
            self.db.execute(
                """
                INSERT INTO Employees(
                    emp_id, last_name, middle_name, first_name, dob, contact_no, email_id,
                    address, password, ssn, active, tier
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (emp_id, last_name, middle_name, first_name, dob, contact_no, email_id,
                 address, encrypt(password), encrypt_ssn(ssn), int(active), int(tier)),
            )
            print('Employee added $$ ' + emp_id)
            return 1
        except Exception as e:
            print('Cannot add this Employee:', e)
            return -1

    def update_account_info(self, emp_id, last_name, middle_name, first_name, contact_no, email_id, ssn, dob, address,
                            tier):
        try:
            self.db.execute(
                """
                UPDATE Employees
                SET last_name=%s, middle_name=%s, first_name=%s, dob=%s, contact_no=%s,
                    email_id=%s, address=%s, ssn=%s, tier=%s
                WHERE emp_id=%s
                """,
                (last_name, middle_name, first_name, dob, contact_no, email_id, address, ssn, int(tier), emp_id),
            )
            print('Customer Updated')
            return 'updated'
        except Exception as e:
            print('Cannot update customer:', e)
            return 'Try Again Later'

    #################        FUNCTION TO CHECK EXISTANCE OF EMPLOYEE ID      #################
    def check_user_id(self, emp_id):
        result = self.db.fetch_all(
            "SELECT emp_id FROM Employees WHERE emp_id=%s",
            (emp_id,),
        )
        if len(result) == 0:
            return 0
        return 1

    #################        FUNCTION TO VERIFY EMPLOYEE                 #################
    def verify_employee(self, emp_id, password):
        result = self.db.fetch_all(
            "SELECT emp_id FROM Employees WHERE emp_id=%s AND active=1 AND password=%s",
            (emp_id, encrypt(password)),
        )
        if len(result) == 0:
            return 0
        return 1

    def retrieve_hashed_password(self, userid):
        result = self.db.fetch_one(
            "SELECT password FROM Employees WHERE emp_id = %s",
            (userid,),
        )
        if result:
            return result[0]
        return None

    #################        FUNCTION TO CHECK CONTACT NO EXISTANCE                  #################
    def check_existing_contact(self, contact_no):
        result = self.db.fetch_all(
            "SELECT emp_id FROM Employees WHERE contact_no=%s",
            (contact_no,),
        )
        if len(result) == 0:
            print('Contact number not registered')
            return 0
        return 1

    #################        FUNCTION TO CHECK EMAIL EXISTANCE                  #################
    def check_existing_email(self, email):
        result = self.db.fetch_all(
            "SELECT emp_id FROM Employees WHERE email_id=%s",
            (email,),
        )
        if len(result) == 0:
            print('Email not registered')
            return 0
        return 1

    #################        FUNCTION TO CHECK SSN EXISTANCE                  #################
    def check_existing_ssn(self, ssn):
        result = self.db.fetch_all(
            "SELECT emp_id FROM Employees WHERE ssn=%s",
            (ssn,),
        )
        if len(result) == 0:
            print('SSN not registered')
            return 0
        return 1

    #################        FUNCTION TO GET ANY TIER1 EMPLOYEE                   #################
    def getTier1_emp(self):
        result = self.db.fetch_all(
            "SELECT emp_id FROM Employees WHERE active = 1 AND tier = 1",
        )
        if len(result) == 0:
            print('No tier1 employee found')
            return 'None'
        print(result[0][0])
        return result[0][0]

    #################        FUNCTION TO GET ANY TIER2 EMPLOYEE                    #################
    def getTier2_emp(self):
        result = self.db.fetch_all(
            "SELECT emp_id FROM Employees WHERE active = 1 AND tier = 2",
        )
        if len(result) == 0:
            print('No tier2 employee found')
            return 'None'
        print(result[0][0])
        return result[0][0]

    #################        FUNCTION TO ADD TRANSACTIONS (APPROVER: TIER2)                    #################
    def add_transaction(self, account1, account2, amount):
        c = Customers(db=self.db)
        if c.verify_account(int(account1)) == 0:
            return 'account ' + str(account1) + ' doesn\'t exists'

        if c.verify_account(int(account2)) == 0:
            return 'account ' + str(account2) + ' doesn\'t exists'

        approver = 1
        print('at add_transaction')
        if float(amount) > 1000:
            approver = 2
        try:
            self.db.execute(
                """
                INSERT INTO Transactions(from_account, to_account, approver1_id, approver2, amount, status, deposit)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (int(account1), int(account2), '-1', approver, float(amount), 1, 0),
            )
            msg = 'Request to be approved by tier' + str(approver) + ' employee'
            print(msg)
            return msg
        except Exception as e:
            print('Cannot transfer funds:', e)
            return 'Try Again later'

    #################        FUNCTION TO ADD TRANSACTION DEPOSIT (APPROVER: TIER2)                    #################
    def add_transaction_deposit(self, account, amount):
        approver = 1
        print('at add_transaction_deposit')
        if float(amount) > 1000:
            approver = 2
        try:
            self.db.execute(
                """
                INSERT INTO Transactions(from_account, to_account, approver1_id, approver2, amount, status, deposit)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (int(account), int(account), '-1', approver, float(amount), 1, 1),
            )
            msg = 'Request to be approved by tier' + str(approver) + ' employee'
            print(msg)
            return msg
        except Exception as e:
            print('Cannot deposit funds:', e)
            return 'Try Again later'

    def deny_funds_requested(self, employee_id, transaction_no):
        tier = self.get_employee_tier(employee_id)
        if tier < 2:
            return 'Not authorized to GET/Approve transactions'

        try:
            self.db.execute(
                "UPDATE Transactions SET remark=%s, status=0 WHERE transaction_no = %s",
                ('Request Denied by Bank', int(transaction_no)),
            )
            return 'Request Cancelled'
        except Exception as e:
            print(e, ' : Error in  Denying Request')
            return 'Please try again later'

    #################                 FUNCTION TO APPROVE FUND TRANFER REQESTS           #################
    def approve_fund_request(self, transaction_no):
        from_account = emp.get_fromAccount_of_transaction(transaction_no)
        to_account = emp.get_toAccount_of_transaction(transaction_no)
        amount = emp.get_amount_of_transaction(transaction_no)
        status = emp.get_transaction_status(transaction_no)
        result = 'Invalid transaction_no'
        print(from_account, to_account, amount)
        if from_account != -1 and to_account != -1 and amount != -1 and status != 0:
            c = Customers(db=self.db)
            result = c.fund_transfers(from_account, to_account, amount, int(transaction_no))
        return result

    #################        FUNCTION TO GET FUND TRANSFER REQUESTs LIST                    #################
    def fund_transfer_requests(self, employee_id):
        tier = self.get_employee_tier(employee_id)
        if tier != 2:
            return 'None'

        try:
            result = self.db.fetch_all(
                "SELECT * FROM Transactions WHERE approver2 = %s AND status = 1",
                (tier,),
            )
            if len(result) == 0:
                return 'None'
            return result
        except Exception as e:
            print(e, ' : Error in  getting  fund_transfer_requests List')

    #################        FUNCTION TO GET ACCOUNT UPDATE REQUEST's LIST                    #################
    def update_info_request_list(self, employee_id):
        tier = self.get_employee_tier(employee_id)
        approver = 1
        if tier == 3:
            approver = 3

        result = self.db.fetch_all(
            "SELECT * FROM Updateinfo WHERE status = 1 AND approver = %s",
            (approver,),
        )
        print(result)
        if len(result) == 0:
            print('Customer with this ID not exists')
            return 0
        return result

    def handle_appointment(self):
        try:
            self.db.execute(
                "DELETE FROM Appointments WHERE emp_id=%s",
                (self.__emp_id,),
            )
            print('Appointment attended.')
        except Exception as e:
            print('Cannot Delete Appointment:', e)

    def get_employee_tier(self, emp_id):
        result = self.db.fetch_one(
            "SELECT tier FROM Employees WHERE emp_id = %s",
            (emp_id,),
        )
        if result is None:
            print('Account doesn\'t exists')
            return "None"
        return result[0]

    def get_amount_of_transaction(self, transaction_no):
        result = self.db.fetch_one(
            "SELECT amount FROM Transactions WHERE transaction_no=%s",
            (int(transaction_no),),
        )
        if result is None:
            return -1
        return result[0]

    def get_fromAccount_of_transaction(self, transaction_no):
        result = self.db.fetch_one(
            "SELECT from_account FROM Transactions WHERE transaction_no=%s",
            (int(transaction_no),),
        )
        if result is None:
            return -1
        return result[0]

    def get_toAccount_of_transaction(self, transaction_no):
        result = self.db.fetch_one(
            "SELECT to_account FROM Transactions WHERE transaction_no=%s",
            (int(transaction_no),),
        )
        if result is None:
            return -1
        return result[0]

    def get_transaction_status(self, transaction_no):
        result = self.db.fetch_one(
            "SELECT status FROM Transactions WHERE transaction_no=%s",
            (int(transaction_no),),
        )
        if result is None:
            return -1
        return result[0]

    def transfer_transaction_to_tier2(self, transaction_no):
        try:
            self.db.execute(
                "UPDATE Transactions SET approver1_id=%s, approver2 = %s WHERE transaction_no = %s",
                ('-1', 2, int(transaction_no)),
            )
            return 'Request Sent to Tier2 employee'
        except Exception as e:
            print(e, ' : Error in  approving transaction')
            return 0

    def system_logs(self):
        if self.__tier == '3':
            try:
                result = self.db.fetch_all("SELECT * FROM System_Log")
                for x in result:
                    print(x)
            except Exception as e:
                print('Cannot view System Logs:', e)
        else:
            print("You are not authorized to view System logs.")

    #################        FUNCTION TO UPDATE EMPLOYEE ACCOUNT INFO                     #################
    def update_employee(self, userid, emp_id, email, firstname, midname, lastname, contact_no, dob, address):
        tier = self.get_employee_tier(userid)
        if tier != 3:
            return 'Not authorized to update customer'

        try:
            self.db.execute(
                """
                UPDATE Employees
                SET last_name=%s, middle_name=%s, first_name=%s, dob=%s, contact_no=%s,
                    email_id=%s, address=%s
                WHERE emp_id=%s
                """,
                (lastname, midname, firstname, dob, contact_no, email, address, emp_id),
            )
            print('Employee Updated')
            return 'Employee Updated'
        except Exception as e:
            print('Cannot update Employee:', e)
            return 'Cannot update Employee'

    #################        FUNCTION TO GET EMPLOYEE'S ALL INFO              #################
    def get_employee_details(self, emp_id):
        result = self.db.fetch_one(
            """
            SELECT first_name, middle_name, last_name, dob, contact_no, email_id, address, ssn, active, tier
            FROM Employees WHERE emp_id = %s
            """,
            (emp_id,),
        )
        if result is None:
            print('Employee doesn\'t exists')
            return 'None'
        return {
            'first_name': result[0],
            'middle_name': result[1],
            'last_name': result[2],
            'dob': result[3],
            'contact_no': result[4],
            'email_id': result[5],
            'address': result[6],
            'ssn': result[7],
            'active': result[8],
            'tier': result[9]
        }

    def deactivate_account(self, userid, account):
        tier = self.get_employee_tier(userid)
        if tier != 2:
            return 'Not authorized to dectivate accounts'
        c = Customers(db=self.db)
        if c.verify_account(int(account)) == 0:
            return 'account doesn\'t exists'
        try:
            self.db.execute(
                "DELETE FROM Accounts WHERE account_no = %s",
                (int(account),),
            )
            return 'Account Closed'
        except Exception as e:
            print(e, ' : Error in  Account cannot be Closed')
            return 'Account cannot be Closed'

    def deactivate_customer(self, userid, customer_id):
        tier = self.get_employee_tier(userid)
        if tier != 2:
            return 'Not authorized to dectivate customer'
        c = Customers(db=self.db)
        if c.check_user_id(customer_id) == 0:
            return 'customer doesn\'t exists'

        try:
            with self.db.transaction():
                self.db.execute(
                    "DELETE FROM Accounts WHERE customer_id = %s",
                    (customer_id,),
                )
                self.db.execute(
                    "DELETE FROM Customers WHERE customer_id = %s",
                    (customer_id,),
                )
            return 'Customer deactivated'
        except Exception as e:
            print(e, ' : Error in  deactivate Customer')
            return 'Cannot deactivate Customer'

    def deactivate_employee(self, userid, emp_id):
        if self.check_user_id(emp_id) == 0:
            return 'Employee doesn\'t exists'

        tier = self.get_employee_tier(userid)
        if tier != 3:
            return 'Not authorized to dectivate employee'
        try:
            self.db.execute(
                "UPDATE Employees SET active = 0 WHERE emp_id = %s",
                (emp_id,),
            )
            return 'Employee deactivated'
        except Exception as e:
            print(e, ' : Error in  deactivate Employee')
            return 'Cannot deactivate Employee'

    def approve_update_info(self, userid, update_req_no):
        tier = self.get_employee_tier(userid)
        if tier != 2:
            return 'Not authorized to update customer'
        values = self.db.fetch_all(
            "SELECT requester, userid, contact_no, email_id, address FROM Updateinfo WHERE update_req_no = %s",
            (int(update_req_no),),
        )
        if len(values) == 0:
            return 'Invalid Update reqest ID'
        requester = values[0][0]
        try:
            if requester == 'Customer':
                self.db.execute(
                    "UPDATE Customers SET contact_no = %s, email_id = %s, address = %s WHERE customer_id = %s",
                    (values[0][2], values[0][3], values[0][4], values[0][1]),
                )
            else:
                self.db.execute(
                    "UPDATE Employees SET contact_no = %s, email_id = %s, address = %s WHERE emp_id = %s",
                    (values[0][2], values[0][3], values[0][4], values[0][1]),
                )
        except Exception as e:
            print(e, ' : Error in  Update ')
            return 'Cannot Update ' + requester

        try:
            self.db.execute(
                "UPDATE Updateinfo SET status = 0 WHERE update_req_no=%s",
                (int(update_req_no),),
            )
            return requester + ' Updated'
        except Exception as e:
            print(e, ' : Error in  Update ')
            return 'Cannot Update ' + requester

    def deny_update_info(self, userid, update_req_no):
        try:
            self.db.execute(
                "UPDATE Updateinfo SET status = 0 WHERE update_req_no = %s",
                (int(update_req_no),),
            )
            return 'Done'
        except Exception as e:
            print(e, ' : Error in  deny update info')
            return 'Try again later'

    #################        FUNCTION TO RESET PASSORD                    #################
    def reset_password(self, userid, oldPassword, newPassword):
        if self.verify_employee(userid, oldPassword):
            try:
                self.db.execute(
                    "UPDATE Employees SET password = %s WHERE emp_id = %s",
                    (encrypt(newPassword), userid),
                )
                return 'Password Updated'
            except Exception as e:
                print('Cannot make request:', e)
                return 'Try Again Later'
        else:
            return 'Invalid UserID/Password'

    #################        FUNCTION TO FORCE RESET PASSORD                    #################
    def reset_fpassword(self, userid, newPassword):
        if self.check_user_id(userid) == 0:
            return 'UserID doesn\'t exists'

        try:
            self.db.execute(
                "UPDATE Employees SET password = %s WHERE emp_id = %s",
                (encrypt(newPassword), userid),
            )
            return 'Password Updated'
        except Exception as e:
            print('Cannot make request:', e)
            return 'Try Again Later'

    def retrieve_phone_number(self, userid):
        result = self.db.fetch_one(
            "SELECT contact_no FROM Employees WHERE emp_id = %s",
            (userid,),
        )
        if result:
            return result[0]
        print("No contact number found for the given user ID.")
        return None


emp = Employee()
