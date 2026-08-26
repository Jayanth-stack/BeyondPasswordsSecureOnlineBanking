import phonenumbers

from utility.encrypt import encrypt, encrypt_ssn, check_encrypted_password
from datetime import datetime
from dotenv import load_dotenv
from utility.crypto_receipt import generate_receipt, generate_nonce
from utility.db import Database
from utility.ledger import record_entry, list_entries, entries_as_legacy_html, ensure_ledger_table

load_dotenv()


def getdate():
    now = datetime.now()
    return now.strftime("%d/%m/%Y %H:%M:%S")


def format_phone_number(phone):
    try:
        parsed_phone = phonenumbers.parse(phone, None)
        return phonenumbers.format_number(parsed_phone, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.phonenumberutil.NumberParseException:
        return None


class Customers:
    def __init__(self, db=None):
        self.db = db or Database()

    # #################        FUNCTION TO CREATE NEW CUSTOMER                     #################
    def create_customer_id(self, customer_id, last_name, middle_name, first_name, contact_no, email_id, password, ssn,
                           dob, active=1, address="", login_history=""):
        if self.check_user_id(customer_id) == 1:
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
                INSERT INTO Customers(
                    customer_id, last_name, middle_name, first_name, dob, contact_no,
                    email_id, address, password, ssn, active, login_history
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (customer_id, last_name, middle_name, first_name, dob, contact_no, email_id,
                 address, encrypt(password), encrypt_ssn(ssn), active, login_history),
            )
            print('Customer added $$$' + customer_id)
            return 1
        except Exception as e:
            print('Cannot add this customer:', e)
            return -1

    #################        FUNCTION TO OPEN NEW ACCOUNT                    #################
    def open_account(self, customer_id, account_type):
        if self.check_account(customer_id, account_type) == 1:
            return 'Customer already have ', account_type, 'account'

        ensure_ledger_table(self.db)
        try:
            with self.db.transaction():
                _, account_no = self.db.execute(
                    """
                    INSERT INTO Accounts(customer_id, account_type, balance, active, transaction_history)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (customer_id, account_type, 250.0, 1, 'Bonus amount credited'),
                )
                record_entry(
                    self.db,
                    account_no=account_no,
                    amount=250.0,
                    direction='credit',
                    kind='bonus',
                    description='Bonus amount credited',
                    balance_after=250.0,
                )
            print('New account opened')
            return "Done"
        except Exception as e:
            print('Cannot open account:', e)
            return 'Try Again'

    ##################        FUNCTION TO UPDATE CUSTOMER ACCOUNT INFO                     #################
    def update_account_info(self, customer_id, last_name, middle_name, first_name, contact_no, email_id, ssn, dob,
                            address):
        try:
            self.db.execute(
                """
                UPDATE Customers
                SET last_name=%s, middle_name=%s, first_name=%s, dob=%s, contact_no=%s,
                    email_id=%s, address=%s, ssn=%s
                WHERE customer_id=%s
                """,
                (last_name, middle_name, first_name, dob, contact_no, email_id, address, ssn, customer_id),
            )
            print('Customer Updated')
            return 'updated'
        except Exception as e:
            print('Cannot update customer:', e)
            return 'Try Again Later'

    #################        FUNCTION TO MAKE ACCOUNT UPDATE REQUEST                    #################
    def update_info_reqest(self, requester, userid, email, contact_no, address):
        approver = 1
        if requester == 'Employee':
            approver = 3

        try:
            self.db.execute(
                """
                INSERT INTO Updateinfo(requester, userid, email_id, contact_no, address, status, approver)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (requester, userid, email, contact_no, address, 1, approver),
            )
            return 'Update Info Request Placed'
        except Exception as e:
            print('Cannot make request:', e)
            return 'Try Again Later'

    #################        FUNCTION TO GET ACCOUNT UPDATE REQUEST's LIST                    #################
    def update_info_reqest_list(self, userid):
        result = self.db.fetch_all(
            """
            SELECT userid, contact_no, email_id, address, status, approver
            FROM Updateinfo
            WHERE userid = %s AND status = 1 AND requester = 'Customer'
            """,
            (userid,),
        )
        if len(result) == 0:
            return 'None'
        return result

    def _account_row(self, account_no):
        return self.db.fetch_one(
            "SELECT account_no, balance, active, account_type FROM Accounts WHERE account_no = %s",
            (int(account_no),),
        )

    def _balance(self, account_no):
        row = self.db.fetch_one(
            "SELECT balance FROM Accounts WHERE account_no = %s",
            (int(account_no),),
        )
        return None if row is None else row[0]

    #################        FUNCTION TRANSFER FUNDS                    #################
    def fund_transfers(self, account1, account2, amount, transaction_no=-1):
        amount = float(amount)
        account1 = int(account1)
        account2 = int(account2)
        transaction_no = int(transaction_no)

        receiver = self._account_row(account2)
        if receiver is None:
            print('Receiver\'s Account doesn\'t exists')
            return 'Receiver\'s Account doesn\'t exists'
        if receiver[2] != 1:
            print('Receiver\'s Account not active')
            return 'Receiver\'s Account not active'

        print(amount, account1, account2)

        deposit_row = None
        if transaction_no != -1:
            deposit_row = self.db.fetch_one(
                "SELECT deposit FROM Transactions WHERE transaction_no = %s",
                (transaction_no,),
            )
        is_deposit = bool(deposit_row and deposit_row[0] == 1)

        if not is_deposit:
            sender = self._account_row(account1)
            if sender is None:
                print('Sender\'s Account doesn\'t exists')
                return 'Sender\'s Account doesn\'t exists'
            if sender[2] != 1:
                print('Sender\'s Account not active')
                return 'Sender\'s Account not active'
            if sender[3] == 'credit' and float(sender[1]) - amount < -5000.0:
                print('Insufficient Balance')
                print(transaction_no)
                if transaction_no != -1:
                    self.deny_funds_requested(transaction_no)
                return 'Insufficient Balance in Credit Card'
            if sender[1] < amount and sender[3] != 'credit':
                print('Insufficient Balance')
                return 'Insufficient Balance'

        ensure_ledger_table(self.db)
        try:
            with self.db.transaction():
                if is_deposit:
                    self.db.execute(
                        "UPDATE Accounts SET balance = balance + %s WHERE account_no = %s",
                        (amount, account2),
                    )
                    stamp = '$' + str(amount) + ' deposited to ' + str(account2) + ' on ' + getdate()
                    self.db.execute(
                        "UPDATE Accounts SET transaction_history = CONCAT(%s, transaction_history) WHERE account_no = %s",
                        (stamp + ',<br>', account2),
                    )
                    record_entry(
                        self.db,
                        account_no=account2,
                        amount=amount,
                        direction='credit',
                        kind='deposit',
                        description=stamp,
                        counterpart_account=account2,
                        transaction_no=transaction_no,
                        balance_after=self._balance(account2),
                    )
                else:
                    self.db.execute(
                        "UPDATE Accounts SET balance = balance - %s WHERE account_no = %s",
                        (amount, account1),
                    )
                    self.db.execute(
                        "UPDATE Accounts SET balance = balance + %s WHERE account_no = %s",
                        (amount, account2),
                    )
                    credit_stamp = '$' + str(amount) + ' credited from ' + str(account1) + ' on ' + getdate()
                    debit_stamp = '$' + str(amount) + ' transfered to ' + str(account2) + ' on ' + getdate()
                    self.db.execute(
                        "UPDATE Accounts SET transaction_history = CONCAT(%s, transaction_history) WHERE account_no = %s",
                        (credit_stamp + ',<br>', account2),
                    )
                    self.db.execute(
                        "UPDATE Accounts SET transaction_history = CONCAT(%s, transaction_history) WHERE account_no = %s",
                        (debit_stamp + ',<br>', account1),
                    )
                    record_entry(
                        self.db,
                        account_no=account1,
                        amount=amount,
                        direction='debit',
                        kind='transfer',
                        description=debit_stamp,
                        counterpart_account=account2,
                        transaction_no=transaction_no,
                        balance_after=self._balance(account1),
                    )
                    record_entry(
                        self.db,
                        account_no=account2,
                        amount=amount,
                        direction='credit',
                        kind='transfer',
                        description=credit_stamp,
                        counterpart_account=account1,
                        transaction_no=transaction_no,
                        balance_after=self._balance(account2),
                    )

                if transaction_no != -1:
                    self.db.execute(
                        "UPDATE Transactions SET status=0, remark=%s WHERE transaction_no = %s",
                        ('Request Approved', transaction_no),
                    )

            print('Fund transfered')
            receipt_data = {
                "transaction_no": transaction_no,
                "from_account": account1,
                "to_account": account2,
                "amount": amount,
                "timestamp": getdate(),
                "nonce": generate_nonce()
            }
            return generate_receipt(receipt_data)
        except Exception as e:
            print('Cannot transfer funds:', e)
            return 'Try Again later'

    #################         FUNCTION TO MAKE FUND REQUEST (STATUS/LIVE_REQ = 1 )    #################
    def fund_request(self, fromAccount, toAccount, amount):
        customer_id = self.get_customerID_from_account(int(fromAccount))
        try:
            self.db.execute(
                """
                INSERT INTO Transactions(from_account, to_account, approver1_id, amount, status)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (int(fromAccount), int(toAccount), customer_id, float(amount), 1),
            )
            print('Request Sent')
            return 'Request Sent'
        except Exception as e:
            print('Cannot Request:', e)
            return 'Try Again later'

    #################        FUNCTION TO DEBIT FUNDS                     #################
    def debit_request(self, account, amount):
        account = int(account)
        amount = float(amount)
        result = self._account_row(account)
        if result is None:
            print('Account doesn\'t exists')
            return 'Account doesn\'t exists'
        if result[2] != 1:
            print('Account not active')
            return 'Account not active'
        if result[3] == 'credit' and float(result[1]) - amount < -5000.0:
            print('Insufficient Balance')
            return 'Insufficient Balance in Credit Card'
        if result[1] < amount and result[3] != 'credit':
            print('Insufficient Balance')
            return 'Insufficient Balance'

        ensure_ledger_table(self.db)
        try:
            with self.db.transaction():
                self.db.execute(
                    "UPDATE Accounts SET balance = balance - %s WHERE account_no = %s",
                    (amount, account),
                )
                stamp = '$' + str(amount) + ' debited  ' + ' on ' + getdate()
                self.db.execute(
                    "UPDATE Accounts SET transaction_history = CONCAT(%s, transaction_history) WHERE account_no = %s",
                    (stamp + ',<br>', account),
                )
                record_entry(
                    self.db,
                    account_no=account,
                    amount=amount,
                    direction='debit',
                    kind='withdrawal',
                    description=stamp,
                    balance_after=self._balance(account),
                )
            print('Amount Debited')
            return 'Amount Debited'
        except Exception as e:
            print('Cannot Debit funds:', e)
            return 'Cannot Debit funds:'

    #################        FUNCTION TO CREDIT FUNDS                     #################
    def credit_request(self, account, amount):
        account = int(account)
        amount = float(amount)
        result = self._account_row(account)
        if result is None:
            print('Account(to credit) doesn\'t exists')
            return 'Account(to credit) doesn\'t exists'
        if result[2] != 1:
            print('Account(to credit) not active')
            return 'Account(to credit) not active'

        ensure_ledger_table(self.db)
        try:
            with self.db.transaction():
                self.db.execute(
                    "UPDATE Accounts SET balance = balance + %s WHERE account_no = %s",
                    (amount, account),
                )
                stamp = '$' + str(amount) + ' direct deposited  ' + ' on ' + getdate()
                print(stamp)
                self.db.execute(
                    "UPDATE Accounts SET transaction_history = CONCAT(%s, transaction_history) WHERE account_no = %s",
                    (stamp + ',<br>', account),
                )
                record_entry(
                    self.db,
                    account_no=account,
                    amount=amount,
                    direction='credit',
                    kind='credit',
                    description=stamp,
                    balance_after=self._balance(account),
                )
            print('Amount Credited')
            return 'Success'
        except Exception as e:
            print('Cannot credit funds:', e)
            return 'Try again later'

    #################        FUNCTION TO GET TRANSACTION HISTOY               #################
    def account_belongs_to(self, account_no, customer_id):
        row = self.db.fetch_one(
            "SELECT 1 FROM Accounts WHERE account_no = %s AND customer_id = %s",
            (int(account_no), customer_id),
        )
        return row is not None

    def get_transaction_history(self, account_no, customer_id=None):
        account_no = int(account_no)
        if customer_id is not None and not self.account_belongs_to(account_no, customer_id):
            return None
        entries = list_entries(self.db, account_no)
        if entries:
            return {
                'entries': entries,
                'html': entries_as_legacy_html(entries),
                'source': 'ledger',
            }
        row = self.db.fetch_one(
            "SELECT transaction_history FROM Accounts WHERE account_no = %s",
            (account_no,),
        )
        blob = ''
        if row and row[0]:
            blob = row[0]
        return {
            'entries': [],
            'html': blob,
            'source': 'legacy',
        }

    #################        FUNCTION TO CHECK EXISTING CUSTOMER ID          #################
    def check_user_id(self, customer_id):
        result = self.db.fetch_all(
            "SELECT customer_id FROM Customers WHERE customer_id=%s AND active=1",
            (customer_id,),
        )
        print(len(result))
        if len(result) == 0:
            print('Customer with this ID not exists')
            return 0
        return 1

    #################        FUNCTION TO VERIFY CUSTOMER                    #################
    def verify_customer(self, customer_id, password):
        hashed_password_in_db = self.retrieve_hashed_password(customer_id)
        if hashed_password_in_db and check_encrypted_password(password, hashed_password_in_db):
            return 1
        else:
            return 0

    def retrieve_hashed_password(self, userid):
        result = self.db.fetch_one(
            "SELECT password FROM Customers WHERE customer_id = %s",
            (userid,),
        )
        if result:
            return result[0]
        return None

    #################        FUNCTION TO GET CUSTOMER'S CONTACT_NO                    #################
    def get_customer_contactNo(self, customer_id):
        result = self.db.fetch_one(
            "SELECT contact_no FROM Customers WHERE customer_id=%s",
            (customer_id,),
        )
        return result[0] if result else None

    #################        FUNCTION TO CHECK CUSTOMER'S EXISTING ACCOUNT               #################
    def check_account(self, customer_id, account_type):
        result = self.db.fetch_all(
            "SELECT account_type FROM Accounts WHERE customer_id=%s AND account_type=%s",
            (customer_id, account_type),
        )
        if len(result) == 0:
            return 0
        return 1

    #################        FUNCTION TO VERIFY EXISTING ACCOUNT               #################
    def verify_account(self, account_no):
        result = self.db.fetch_all(
            "SELECT account_no FROM Accounts WHERE account_no=%s",
            (int(account_no),),
        )
        if len(result) == 0:
            return 0
        return 1

    #################        FUNCTION TO GET CUSTOMER'S ALL ACCOUNTS                     #################
    def get_all_account(self, customer_id):
        rows = self.db.fetch_all(
            "SELECT account_no, account_type, balance FROM Accounts WHERE customer_id=%s",
            (customer_id,),
        )
        response = {'checkin': 'None', 'savings': 'None', 'credit': 'None'}
        for val in rows:
            response[val[1]] = {
                'Account': val[0],
                'Balance': val[2]
            }
        return response

    #################        FUNCTION TO GET CUSTOMER'S ALL INFO              #################
    def get_customer_details(self, customer_id):
        result = self.db.fetch_one(
            """
            SELECT first_name, middle_name, last_name, dob, contact_no, email_id, address, ssn, active, login_history
            FROM Customers WHERE customer_id = %s
            """,
            (customer_id,),
        )
        if result is None:
            print('Customer doesn\'t exists')
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
            'login_history': result[9]
        }

    #################        FUNCTION TO UPDATE LOGIN HISTORY                 #################
    def update_login_history(self, customer_id):
        try:
            self.db.execute(
                "UPDATE Customers SET login_history = CONCAT(%s, login_history) WHERE customer_id = %s",
                (getdate(), customer_id),
            )
            print('Loging History of customer : ', customer_id, 'updated')
        except Exception as e:
            print(e, ' : Error in  updating Loging History of customer : ', customer_id)

    #################        FUNCTION TO CHECK EXISTING CONTACT                     #################
    def check_existing_contact(self, contact_no):
        result = self.db.fetch_all(
            "SELECT contact_no FROM Customers WHERE contact_no=%s",
            (contact_no,),
        )
        if len(result) == 0:
            print('Contact number not registered')
            return 0
        return 1

    #################        FUNCTION TO CHECK EXISTING EMAIL                     #################
    def check_existing_email(self, email):
        result = self.db.fetch_all(
            "SELECT email_id FROM Customers WHERE email_id=%s",
            (email,),
        )
        if len(result) == 0:
            print('Email not registered')
            return 0
        return 1

    #################        FUNCTION TO CHECK EXISTING SSN                     #################
    def check_existing_ssn(self, ssn):
        result = self.db.fetch_all(
            "SELECT ssn FROM Customers WHERE ssn=%s",
            (ssn,),
        )
        if len(result) == 0:
            print('SSN not registered')
            return 0
        return 1

    #################        FUNCTION TO GET CUSTOMER ID FROM ACCOUNT NO          #################
    def get_customerID_from_account(self, account):
        result = self.db.fetch_one(
            "SELECT customer_id FROM Accounts WHERE account_no = %s",
            (int(account),),
        )
        if result is None:
            print('SSN not registered')
            return -1
        return result[0]

    #################                 FUNCTION TO MAKE CASHIER CHEQUE            #################
    def make_cashier_check(self, issuer_id, to_account, from_account, amount):
        if self.verify_account(int(from_account)) == 0:
            return 'Sender\'s Account doesn\'t Exists'
        if self.verify_account(int(to_account)) == 0:
            return 'Receiver\'s Account doesn\'t Exists'
        try:
            self.db.execute(
                """
                INSERT INTO Cheque(issuer_id, to_account, from_account, amount, active)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (issuer_id, int(to_account), int(from_account), float(amount), 1),
            )
            return "Success"
        except Exception as e:
            print(e, ' : Error in  creating check')
            return "Fail"

    #################                 FUNCTION TO GET CHEQUE DETAILS            #################
    def get_cashier_check(self, cheque_no):
        result = self.db.fetch_all(
            "SELECT * FROM Cheque WHERE cheque_no=%s",
            (int(cheque_no),),
        )
        if len(result) == 0:
            return 'None'
        return result

    #################                 FUNCTION TO DEPOSIT CHECK                 #################
    def deposit_check(self, userid, cheque_no):
        result = self.db.fetch_all(
            """
            SELECT c.to_account, c.from_account, c.amount, c.active
            FROM Cheque AS c
            INNER JOIN Accounts AS ac ON c.to_account = ac.account_no
            WHERE ac.customer_id = %s AND c.cheque_no = %s
            """,
            (userid, int(cheque_no)),
        )
        if len(result) == 0:
            return 'Invalid Cheque'

        if result[0][3] == 0:
            return 'Check already used'

        to_account = result[0][0]
        from_account = result[0][1]
        amount = result[0][2]

        transfer_status = self.fund_transfers(int(from_account), int(to_account), float(amount))
        if transfer_status != 'done':
            return transfer_status

        try:
            self.db.execute(
                "UPDATE Cheque SET active=0 WHERE cheque_no=%s",
                (int(cheque_no),),
            )
            return "Success"
        except Exception as e:
            print(e, ' : Error in  approving check')
            return "fail"

    ################                    FUNCTION TO GET CHEQUE LIST                 #################
    def get_cheque_list(self, userid):
        result = self.db.fetch_all(
            """
            SELECT c.cheque_no, c.to_account, c.from_account, c.amount, c.active
            FROM Cheque AS c
            INNER JOIN Accounts AS ac ON c.from_account = ac.account_no OR c.to_account = ac.account_no
            WHERE ac.customer_id = %s
            """,
            (userid,),
        )
        print(len(result))
        if len(result) == 0:
            return 'None'
        return result

    #################                FUNCTION TO GET ALL REQUESTED FUNDS LIST       #################
    def get_funds_requests(self, customer_id):
        result = self.db.fetch_all(
            "SELECT * FROM Transactions WHERE approver1_id=%s AND status=1",
            (customer_id,),
        )
        print(result)
        if len(result) == 0:
            return 'None'
        return result

    #################                 FUNCTION TO DENY FUND REQUEST             #################
    def deny_funds_requested(self, transaction_no):
        try:
            self.db.execute(
                "UPDATE Transactions SET remark=%s, status=0 WHERE transaction_no = %s",
                ('Request Denied', int(transaction_no)),
            )
            return 'Request Cancelled'
        except Exception as e:
            print(e, ' : Error in  Denying Request')
            return 'Please try again later'

    #################                 FUNCTION TO MAKE APPOINTMENT             #################
    def make_appointment(self, customer_id, time):
        try:
            self.db.execute(
                "INSERT INTO Appointments(customer_id, time, status) VALUES (%s, %s, %s)",
                (customer_id, time, 1),
            )
            return 'Appointment fixed'
        except Exception as e:
            print(e, ' : Error in  getting appointment')
            return 'Try again later'

    #################                 FUNCTION TO HANDLE APPOINTMENT             #################
    def handle_appointment(self, appointment_no):
        try:
            self.db.execute(
                "UPDATE Appointments SET status = 0 WHERE appointment_no=%s",
                (int(appointment_no),),
            )
            return 'Appointment done'
        except Exception as e:
            print(e, ' : Error in  handling appointment')
            return 'Try again later'

    #################                 FUNCTION TO GET APPOINTMENT             #################
    def get_appointment(self, customer_id):
        result = self.db.fetch_all(
            "SELECT * FROM Appointments WHERE customer_id = %s AND status = 1",
            (customer_id,),
        )
        print(len(result))
        if len(result) == 0:
            return 'None'
        return result

    #################        FUNCTION TO RESET PASSORD                    #################
    def reset_password(self, userid, oldPassword, newPassword):
        if self.verify_customer(userid, oldPassword):
            try:
                self.db.execute(
                    "UPDATE Customers SET password = %s WHERE customer_id = %s",
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
                "UPDATE Customers SET password = %s WHERE customer_id = %s",
                (encrypt(newPassword), userid),
            )
            return 'Password Updated'
        except Exception as e:
            print('Cannot make request:', e)
            return 'Try Again Later'

    def retrieve_phone_number(self, userid):
        result = self.db.fetch_one(
            "SELECT contact_no FROM Customers WHERE customer_id = %s",
            (userid,),
        )
        if result:
            phone_number = result[0]
            if not phone_number.startswith('+'):
                phone_number = '+1' + phone_number
            return phone_number
        return None
