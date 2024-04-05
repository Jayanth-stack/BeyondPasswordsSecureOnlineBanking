from time import time
# from employee import Employee
from utility.encrypt import encrypt, encrypt_ssn
from datetime import datetime
import mysql.connector

import pymysql

db = mysql.connector.connect(
    host="localhost",
    user="root",
    port ="3306",
    password="root",
    database="bankingapplication"
)

cursor = db.cursor()


def getdate():
    now = datetime.now()
    return now.strftime("%d/%m/%Y %H:%M:%S")


class Customers:
    def __init__(self):
        pass

    # #################        FUNCTION TO CREATE NEW CUSTOMER                     #################
    def create_customer_id(self, customer_id, last_name, middle_name, first_name, contact_no, email_id, password, ssn,
                           dob, active=1, address="", login_history=""):
        # print(self.check_user_id(customer_id, password))
        if self.check_user_id(customer_id) == 1:
            return 'EmpID already Exists'

        if self.check_existing_contact(contact_no) == 1:
            return 'Contact already Exists'

        if self.check_existing_ssn(ssn) == 1:
            return 'SSN already Exists'

        if self.check_existing_email(email_id) == 1:
            return 'Email already Exists'

        # print(self.check_user_id(customer_id, password))
        query = """
                INSERT INTO Customers(customer_id, last_name, middle_name, first_name, dob, contact_no, email_id, address, password, ssn, active, login_history)
                VALUES('%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', %d, '%s');""" % (
        customer_id, last_name, middle_name,
        first_name, dob, contact_no, email_id, address, encrypt(password), encrypt_ssn(ssn), active, login_history)
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            print('Customer added $$$' + customer_id)
            return 1
        except Exception as e:
            db.rollback()
            print('Cannot add this customer:', e)
            return -1

        ############             OPEN NEW CUSTOMER ACCOUNT (DEFAULT: CHECKIN ACCOUNT)         ############

    # def open_account(self, account_type, customer_id, last_name, middle_name, first_name, contact_no, email_id,  password, ssn, active, dob=getdate(), login_history="sdk", address="", balance=500):

    #     # res = self.create_customer_id(customer_id, last_name, middle_name, first_name, contact_no, email_id,  password, ssn, active, dob=getdate(), login_history="sdk", address="")

    #     if self.check_account(customer_id, account_type) == 0:
    #         query = """
    #             INSERT INTO Accounts(customer_id, account_type, balance, active, transaction_history) VALUES('%s', '%s', %d, 1, 'Open');
    #             """ % (customer_id, account_type, balance)
    #         cursor.execute(query) 
    #         try:
    #             db.commit()
    #             # result = cursor.fetchall()
    #             print('New account opened')
    #             return 'done'
    #         except Exception as e:
    #             db.rollback()
    #             print('Cannot open account:', e)
    #             return 'Try again later'
    #     else:
    #         print(customer_id, 'is already having', account_type)
    #         return customer_id, 'is already having', account_type

    #################        FUNCTION TO OPEN NEW ACCOUNT                    #################
    def open_account(self, customer_id, account_type):
        if self.check_account(customer_id, account_type) == 1:
            return 'Customer already have ', account_type, 'account'

        query = """
                INSERT INTO Accounts(customer_id, account_type, balance, active, transaction_history) VALUES('%s', '%s', 250.0, 1, 'Bonus amount credited');
                """ % (customer_id, account_type)
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            print('New account opened')
            return "Done"
        except Exception as e:
            db.rollback()
            print('Cannot open account:', e)
            return 'Try Again'

    ##################        FUNCTION TO UPDATE CUSTOMER ACCOUNT INFO                     #################
    def update_account_info(self, customer_id, last_name, middle_name, first_name, contact_no, email_id, ssn, dob,
                            address):

        query = """
            UPDATE Customers 
            SET customer_id='%s', last_name='%s', middle_name='%s', first_name='%s', dob='%s', contact_no='%s', email_id='%s', address='%s', ssn='%s'  
            WHERE customer_id='%s';""" % (
        customer_id, last_name, middle_name, first_name, dob, contact_no, email_id, address, ssn, customer_id)
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            print('Customer Updated')
            return 'updated'
        except Exception as e:
            db.rollback()
            print('Cannot update customer:', e)
            return 'Try Again Later'

    #################        FUNCTION TO MAKE ACCOUNT UPDATE REQUEST                    #################
    def update_info_reqest(self, requester, userid, email, contact_no, address):
        approver = 1
        if requester == 'Employee':
            approver = 3

        query = """
            INSERT INTO Updateinfo(requester, userid, email_id, contact_no, address, status, approver) 
            VALUES ('%s', '%s', '%s', '%s', '%s', 1, %d);""" % (requester, userid, email, contact_no, address, approver)
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall(
            return 'Update Info Request Placed'
        except Exception as e:
            db.rollback()
            print('Cannot make request:', e)
            return 'Try Again Later'

    #################        FUNCTION TO GET ACCOUNT UPDATE REQUEST's LIST                    #################
    def update_info_reqest_list(self, userid):
        query = """
            SELECT userid, contact_no, email_id, address, status, approver FROM  Updateinfo  
            WHERE userid = '%s' and status = 1 and requester = 'Customer';""" % (userid)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            # print('Customer with this ID not exists')
            return 'None'
        return result

    #################        FUNCTION TRANSFER FUNDS                    #################
    def fund_transfers(self, account1, account2, amount, transaction_no=-1):
        amount = float(amount)
        account1 = int(account1)
        account2 = int(account2)
        transaction_no = int(transaction_no)

        query = """ 
                Select active from Accounts where account_no = %d; 
            """ % (int(account2))
        cursor.execute(query)
        result = cursor.fetchall()
        # print(result[0])
        if result is not None:
            if result[0][0] != 1:
                print('Receiver\'s Account not active')
                return 'Receiver\'s Account not active'
        else:
            print('Receiver\'s Account doesn\'t exists')
            return 'Receiver\'s Account doesn\'t exists'

        print(amount, account1, account2)

        db.commit()  # REQUIRED DO NOT DELETE
        # CHECK IF THE "TRANSFER" IS A DEPOSIT
        query = """ 
                Select deposit from Transactions WHERE transaction_no = %d; 
            """ % (transaction_no)
        cursor.execute(query)
        result = cursor.fetchall()
        # IF DEPOSIT
        if (result[0][0] == 1):
            query = """ 
                    UPDATE Accounts SET balance=balance + %f where account_no = %d; 
                """ % (amount, account2)
            cursor.execute(query)

            str1 = '$' + str(amount) + ' deposited to ' + str(account2) + ' on ' + getdate() + ',<br>'
            query = """ 
                    UPDATE Accounts SET transaction_history=concat('%s', transaction_history) where account_no = %d; 
                """ % (str1, account2)
            cursor.execute(query)
        # IF REGULAR TRANSFER
        else:
            query = """ 
                Select balance, active, account_type  from Accounts where account_no = %d; 
            """ % (int(account1))
            cursor.execute(query)
            result = cursor.fetchall()
            # bal = result[0][0]
            if result is not None:
                if result[0][1] != 1:
                    print('Sender\'s Account not active')
                    return 'Sender\'s Account not active'
                if result[0][2] == 'credit' and float(result[0][0]) - amount < -5000.0:
                    print('Insufficient Balance')
                    print(transaction_no)
                    if transaction_no != -1:
                        self.deny_funds_requested(transaction_no)
                    return 'Insufficient Balance in Credit Card'
                if result[0][0] < amount and result[0][2] != 'credit':
                    print('Insufficient Balance')
                    return 'Insufficient Balance'
            else:
                print('Sender\'s Account doesn\'t exists')
                return 'Sender\'s Account doesn\'t exists'

            query = """ 
                    UPDATE Accounts SET balance=balance-%f where account_no = %d; 
                """ % (amount, account1)
            cursor.execute(query)

            query = """ 
                    UPDATE Accounts SET balance=balance + %f where account_no = %d; 
                """ % (amount, account2)
            cursor.execute(query)

            str1 = '$' + str(amount) + ' credited from ' + str(account1) + ' on ' + getdate() + ',<br>'
            query = """ 
                    UPDATE Accounts SET transaction_history=concat('%s', transaction_history) where account_no = %d; 
                """ % (str1, account2)
            cursor.execute(query)
            str2 = '$' + str(amount) + ' transfered to ' + str(account2) + ' on ' + getdate() + ',<br>'
            query = """ 
                    UPDATE Accounts SET transaction_history=concat('%s', transaction_history) where account_no = %d; 
                """ % (str2, account1)
            cursor.execute(query)

        if int(transaction_no) != -1:
            query = """
                UPDATE Transactions SET status=0, remark='Request Approved'
                WHERE transaction_no = %d;""" % (int(transaction_no))
            cursor.execute(query)

        try:
            db.commit()
            # result = cursor.fetchall()
            print('Fund transfered')
            return 'done'
        except Exception as e:
            db.rollback()
            print('Cannot transfer funds:', e)
            return 'Try Again later'

    #################         FUNCTION TO MAKE FUND REQUEST (STATUS/LIVE_REQ = 1 )    #################
    def fund_request(self, fromAccount, toAccount, amount):
        customer_id = self.get_customerID_from_account(int(fromAccount))
        query = """ 
                    INSERT into Transactions(from_account, to_account, approver1_id, amount, status) VALUES(%d, %d, '%s', %f, %d)
                    """ % (int(fromAccount), int(toAccount), customer_id, float(amount), 1)
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            print('Request Sent')
            return 'Request Sent'
        except Exception as e:
            db.rollback()
            print('Cannot Request:', e)
            return 'Try Again later'

    #################        FUNCTION TO DEBIT FUNDS                     #################
    def debit_request(self, account, amount):
        account = int(account)
        amount = float(amount)
        query = """ 
                Select balance, active, account_type  from Accounts where account_no = %d; 
            """ % (int(account))
        cursor.execute(query)
        result = cursor.fetchall()
        # bal = result[0][0]
        # print(result)
        if len(result) != 0:
            if result[0][1] != 1:
                print('Account not active')
                return 'Account not active'
            if result[0][2] == 'credit' and float(result[0][0]) - amount < -5000.0:
                print('Insufficient Balance')
                # print(transaction_no)
                return 'Insufficient Balance in Credit Card'
            if result[0][0] < amount and result[0][2] != 'credit':
                print('Insufficient Balance')
                return 'Insufficient Balance'
        else:
            print('Account doesn\'t exists')
            return 'Account doesn\'t exists'

        query = """ 
                UPDATE Accounts SET balance=balance-%f where account_no = %d; 
            """ % (float(amount), int(account))
        cursor.execute(query)

        str1 = '$' + str(amount) + ' debited  ' + ' on ' + getdate() + ',<br>'
        query = """ 
                UPDATE Accounts SET transaction_history=concat('%s', transaction_history) where account_no = %d; 
            """ % (str1, int(account))
        cursor.execute(query)
        try:
            db.commit()
            print('Amount Debited')
            return 'Amount Debited'
        except Exception as e:
            db.rollback()
            print('Cannot Debit funds:', e)
            return 'Cannot Debit funds:'

    #################        FUNCTION TO CREDIT FUNDS                     #################
    def credit_request(self, account, amount):
        query = """ 
                Select active from Accounts where account_no = %d; 
            """ % (int(account))
        cursor.execute(query)
        result = cursor.fetchall()
        # print(result[0])
        if len(result) != 0:
            if result[0][0] != 1:
                print('Account(to credit) not active')
                return 'Account(to credit) not active'
        else:
            print('Account(to credit) doesn\'t exists')
            return 'Account(to credit) doesn\'t exists'

        query = """ 
                UPDATE Accounts SET balance=balance+%f where account_no = %d; 
            """ % (float(amount), int(account))
        cursor.execute(query)

        str1 = '$' + str(amount) + ' direct deposited  ' + ' on ' + getdate() + ',<br>'
        print(str1)
        query = """ 
                UPDATE Accounts SET transaction_history=concat('%s', transaction_history) where account_no = %d; 
            """ % (str1, int(account))
        cursor.execute(query)

        try:
            db.commit()
            # result = cursor.fetchall()
            print('Amount Credited')
            return 'Success'
        except Exception as e:
            db.rollback()
            print('Cannot credit funds:', e)
            return 'Try again later'

    # #################        FUNCTION TO CHECK EXISTING SSN                     #################
    #     def get_statement(self, account_no):
    #         query = """
    #                 SELECT  * from
    #                 Accounts  where account_no = %d;
    #             """ % (account_no)
    #         cursor.execute(query)
    #         print(cursor.fetchall())

    #################        FUNCTION TO GET TRANSACTION HISTOY               #################
    def get_transaction_history(self, account_no):
        query = """
            SELECT transaction_history FROM Accounts where account_no = %d;""" % (int(account_no))
        cursor.execute(query)
        result = cursor.fetchall()
        # print(result)
        return result

    #################        FUNCTION TO CHECK EXISTING CUSTOMER ID          #################
    def check_user_id(self, customer_id):
        query = """
            SELECT customer_id FROM Customers WHERE customer_id='%s' and active=1""" % (customer_id)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            # print('Customer with this ID not exists')
            return 0
        return 1

    #################        FUNCTION TO VERIFY CUSTOMER                    #################
    def verify_customer(self, customer_id, password):
        query = """
            SELECT customer_id FROM Customers WHERE customer_id='%s' and active=1 and password='%s';""" % (
        customer_id, encrypt(password))
        cursor.execute(query)
        result = cursor.fetchall()
        print(encrypt(password))
        if len(result) == 0:
            # print('Customer with this ID not exists')
            return 0
        return 1

    #################        FUNCTION TO GET CUSTOMER'S CONTACT_NO                    #################
    def get_customer_contactNo(self, customer_id):
        query = """
            SELECT contact_no FROM Customers 
            WHERE customer_id='%s';""" % (customer_id)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        # if len(result) == 0:
        #     # print('Customer with this ID not exists')
        #     return 0
        return result[0][0]

    #################        FUNCTION TO CHECK CUSTOMER'S EXISTING ACCOUNT               #################
    def check_account(self, customer_id, account_type):
        query = """
            SELECT account_type FROM Accounts WHERE customer_id='%s' and account_type='%s';""" % (
        customer_id, account_type)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            # print('Account doesn\'t exists')
            return 0
        return 1

    #################        FUNCTION TO VERIFY EXISTING ACCOUNT               #################
    def verify_account(self, account_no):
        query = """
            SELECT * FROM Accounts WHERE account_no=%d;""" % (int(account_no))
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            # print('Account doesn\'t exists')
            return 0
        return 1

    #################        FUNCTION TO GET CUSTOMER'S ALL ACCOUNTS                     #################
    def get_all_account(self, customer_id):
        query = """
            SELECT account_no, account_type, balance FROM Accounts WHERE customer_id='%s';""" % (customer_id)
        cursor.execute(query)
        # result = cursor.fetchall()
        response = {'checkin': 'None', 'savings': 'None', 'credit': 'None'}
        for val in cursor.fetchall():
            response[val[1]] = {
                'Account': val[0],
                'Balance': val[2]
            }
            # print(val)
        # print(response)

        try:
            db.commit()
            # result = cursor.fetchall()
            # print('Amount Credited') 
            return response
        except Exception as e:
            db.rollback()
            print(e)
            return 'Try again later'

    #################        FUNCTION TO GET CUSTOMER'S ALL INFO              #################
    def get_customer_details(self, customer_id):
        query = """
            SELECT first_name, middle_name, last_name, dob, contact_no, email_id, address, ssn, active, login_history 
            FROM Customers WHERE customer_id = '%s';""" % (customer_id)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            print('Customer doesn\'t exists')
            return 'None'
        res = {'first_name': result[0][0],
               'middle_name': result[0][1],
               'last_name': result[0][2],
               'dob': result[0][3],
               'contact_no': result[0][4],
               'email_id': result[0][5],
               'address': result[0][6],
               'ssn': result[0][7],
               'active': result[0][8],
               'login_history': result[0][9]
               }
        return res

    #################        FUNCTION TO UPDATE LOGIN HISTORY                 #################
    def update_login_history(self, customer_id):
        query = """ 
                UPDATE Customers SET login_history=concat('%s', login_history) where customer_id = '%s'; 
            """ % (getdate(), customer_id)
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            print('Loging History of customer : ', customer_id, 'updated')
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  updating Loging History of customer : ', customer_id)

    #################        FUNCTION TO CHECK EXISTING CONTACT                     #################
    def check_existing_contact(self, contact_no):
        query = """
            SELECT * FROM Customers WHERE contact_no='%s';""" % (contact_no)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            print('Contact number not registered')
            return 0
        return 1

    #################        FUNCTION TO CHECK EXISTING EMAIL                     #################
    def check_existing_email(self, email):
        query = """
            SELECT * FROM Customers WHERE email_id='%s';""" % (email)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            print('Email not registered')
            return 0
        return 1

    #################        FUNCTION TO CHECK EXISTING SSN                     #################
    def check_existing_ssn(self, ssn):
        query = """
            SELECT * FROM Customers WHERE ssn='%s';""" % (ssn)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            print('SSN not registered')
            return 0
        return 1

    #################        FUNCTION TO GET CUSTOMER ID FROM ACCOUNT NO          #################
    def get_customerID_from_account(self, account):
        query = """
            SELECT customer_id FROM Accounts WHERE account_no = %d;""" % (account)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            print('SSN not registered')
            return -1
        return result[0][0]

    #################                 FUNCTION TO MAKE CASHIER CHEQUE            #################
    def make_cashier_check(self, issuer_id, to_account, from_account, amount):
        if self.verify_account(int(from_account)) == 0:
            return 'Sender\'s Account doesn\'t Exists'
        if self.verify_account(int(to_account)) == 0:
            return 'Receiver\'s Account doesn\'t Exists'
        query = """
            INSERT INTO Cheque(issuer_id, to_account, from_account, amount, active) VALUES('%s', %d, %d, %f, 1); """ % (
        issuer_id, int(to_account), int(from_account), float(amount))
        cursor.execute(query)
        try:
            db.commit()
            return "Success"
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  creating check')
            return "Fail"

    #################                 FUNCTION TO GET CHEQUE DETAILS            #################
    def get_cashier_check(self, cheque_no):
        query = """
            SELECT * FROM Cheque WHERE cheque_no=%d; """ % (int(cheque_no))
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            return 'None'
        return result

    #################                 FUNCTION TO DEPOSIT CHECK                 #################
    def deposit_check(self, userid, cheque_no):
        query = """
        SELECT c.to_account, c.from_account, c.amount, c.active FROM Cheque AS c 
        INNER JOIN Accounts AS ac ON c.to_account = ac.account_no
        WHERE ac.customer_id = '%s' AND c.cheque_no=%d;""" % (userid, int(cheque_no))
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
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

        query = """
            UPDATE Cheque SET active=0 WHERE cheque_no=%d ;""" % (int(cheque_no))
        cursor.execute(query)
        try:
            db.commit()
            return "Success"
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  approving check')
            return "fail"

    ################                    FUNCTION TO GET CHEQUE LIST                 #################
    def get_cheque_list(self, userid):
        query = """
        SELECT c.cheque_no, c.to_account, c.from_account, c.amount, c.active FROM Cheque AS c 
        INNER JOIN Accounts AS ac ON c.from_account = ac.account_no OR c.to_account = ac.account_no
        WHERE ac.customer_id = '%s';""" % (userid)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            return 'None'
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  update cache after get_cheque_list request List')

        if len(result) == 0:
            # print('Customer with this ID not exists')
            return 0
        return result

    #################                FUNCTION TO GET ALL REQUESTED FUNDS LIST       #################
    def get_funds_requests(self, customer_id):
        query = """
            SELECT * FROM Transactions WHERE approver1_id='%s' and status=1; """ % (customer_id)
        cursor.execute(query)
        result = cursor.fetchall()
        print(result)
        if len(result) == 0:
            return 'None'
        return result

    #################                 FUNCTION TO DENY FUND REQUEST             #################
    def deny_funds_requested(self, transaction_no):
        # from_account = se
        # self.fund_transfers(int(from_account), int(to_account), int(amount))
        query = """
            UPDATE Transactions SET remark='Request Denied', status=0
            WHERE transaction_no = %d;""" % (int(transaction_no))
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            return 'Request Cancelled'
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  Denying Request')
            return 'Please try again later'

    #################                 FUNCTION TO MAKE APPOINTMENT             #################
    def make_appointment(self, customer_id, time):
        query = """
            INSERT INTO Appointments(customer_id, time, status) VALUES('%s', '%s', 1); """ % (customer_id, time)
        cursor.execute(query)
        try:
            db.commit()
            return 'Appointment fixed'
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  getting appointment')
            return 'Try again later'

    #################                 FUNCTION TO HANDLE APPOINTMENT             #################
    def handle_appointment(self, appointment_no):
        query = """
            UPDATE Appointments  SET status = 0 where appointment_no=%d ; """ % (int(appointment_no))
        cursor.execute(query)
        try:
            db.commit()
            return 'Appointment done'
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  handling appointment')
            return 'Try again later'

    #################                 FUNCTION TO GET APPOINTMENT             #################
    def get_appointment(self, customer_id):
        query = """
            SELECT * FROM Appointments where customer_id = '%s' and status = 1 ; """ % (customer_id)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            return 'None'
        return result

    #################        FUNCTION TO RESET PASSORD                    #################
    def reset_password(self, userid, oldPassword, newPassword):
        query = """
                UPDATE Customers Set password = '%s' 
                WHERE customer_id = '%s';""" % (encrypt(newPassword), userid)

        if self.verify_customer(userid, oldPassword):
            cursor.execute(query)
            try:
                db.commit()
                return 'Password Updated'
            except Exception as e:
                db.rollback()
                print('Cannot make request:', e)
                return 'Try Again Later'
        else:
            return 'Invalid UserID/Password'

    #################        FUNCTION TO FORCE RESET PASSORD                    #################
    def reset_fpassword(self, userid, newPassword):
        if self.check_user_id(userid) == 0:
            return 'UserID doesn\'t exists'

        query = """
                UPDATE Customers Set password = '%s' 
                WHERE customer_id = '%s';""" % (encrypt(newPassword), userid)
        print(encrypt(newPassword))
        cursor.execute(query)
        try:
            db.commit()
            return 'Password Updated'
        except Exception as e:
            db.rollback()
            print('Cannot make request:', e)
            return 'Try Again Later'


#customer = Customers()
#customer.create_customer_id('anilkh', "khadwal",  "", "ROHIT",  "8894141486", "anil@gmail.com", "password", "ssn", 1)

#customer.open_account('savings', 'anilkh',last_name="khadwal", middle_name="", first_name="ROHIT", dob=getdate(), contact_no="8894141786", email_id="anil.khadwal@gmail.com", address="abc", password="samsung", ssn="123756901", active=True )
#customer.update_account_info('rohitkh',last_name="khadwal", middle_name="", first_name="ROHIT", dob=getdate(), contact_no="8894141786", email_id="anil.khadwal@gmail.com", address="abc", password="samsung", ssn="123756901", active=True)
#customer.fund_transfers(1, 2, 5000)
#customer.get_statement(1)
#customer.open_another_account('savings')
#customer.printcustomer(), customer.debit_request(1, 50)
#customer.credit_request(1, 50), customer.check_account('rohitkh', 'samsung')
#customer.check_user_id('rohitkh', 'samsung')
#customer.get_all_account('rohitkh')
#customer.get_customer_details('rohitkh')
#print(customer.credit_request(1, 1000))