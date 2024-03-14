from time import time
from customer import Customers

from utility.encrypt import encrypt
from datetime import datetime
import mysql.connector
import pymysql

db = mysql.connector.connect(
    host="localhost",
    user="root",
    password="root",
    port="3306",
    database="bankingapplication"
)

cursor = db.cursor()


def getdate():
    now = datetime.now()
    return now.strftime('%Y-%m-%d')


class Employee:
    def __init__(self):
        pass

    #################        FUNCTION TO CREATE EMPLOYEE          #################
    def create_employee(self, emp_id, last_name, middle_name, first_name, contact_no, email_id, password, ssn, dob,
                        tier, active=1, address=""):

        # print(self.check_user_id(customer_id, password))
        if self.check_user_id(emp_id) == 1:
            return 'EmpID already Exists'

        if self.check_existing_contact(contact_no) == 1:
            return 'Contact already Exists'

        if self.check_existing_ssn(ssn) == 1:
            return 'SSN already Exists'

        if self.check_existing_email(email_id) == 1:
            return 'Email already Exists'

        query = """
                INSERT INTO Employees(emp_id, last_name, middle_name, first_name, dob, contact_no, email_id, address, password, ssn, active, tier)
                VALUES('%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', %d, %d);""" % (
        emp_id, last_name, middle_name,
        first_name, dob, contact_no, email_id, address, encrypt(password), ssn, int(active), int(tier))
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            print('Employee added $$ ' + emp_id)
            return 1
        except Exception as e:
            db.rollback()
            print('Cannot add this Employee:', e)
            return -1

        # #################        FUNCTION TO UPDATE EMPLOYEE ACCOUNT INFO                     #################

    def update_account_info(self, emp_id, last_name, middle_name, first_name, contact_no, email_id, ssn, dob, address,
                            tier):

        query = """
            UPDATE Employees  
            SET emp_id='%s', last_name='%s', middle_name='%s', first_name='%s', dob='%s', contact_no='%s', email_id='%s', address='%s', ssn='%s',  tier=%d 
            WHERE emp_id='%s';""" % (
        emp_id, last_name, middle_name, first_name, dob, contact_no, email_id, address, ssn, int(tier), emp_id)
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

    #################        FUNCTION TO CHECK EXISTANCE OF EMPLOYEE ID      #################
    def check_user_id(self, emp_id):
        query = """
            SELECT emp_id FROM Employees 
            WHERE emp_id='%s';""" % (emp_id)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            # print('Customer with this ID not exists')
            return 0
        return 1

    #################        FUNCTION TO VERIFY EMPLOYEE                 #################
    def verify_employee(self, emp_id, password):
        query = """
            SELECT emp_id FROM Employees 
            WHERE emp_id='%s' and active=1 and password='%s';""" % (emp_id, encrypt(password))
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            # print('Customer with this ID not exists')
            return 0
        return 1

    #################        FUNCTION TO CHECK CONTACT NO EXISTANCE                  #################
    def check_existing_contact(self, contact_no):
        query = """
            SELECT * FROM Employees 
            WHERE contact_no='%s';""" % (contact_no)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            print('Contact number not registered')
            return 0
        return 1

    #################        FUNCTION TO CHECK EMAIL EXISTANCE                  #################
    def check_existing_email(self, email):
        query = """
            SELECT * FROM Employees 
            WHERE email_id='%s';""" % (email)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            print('Email not registered')
            return 0
        return 1

    #################        FUNCTION TO CHECK SSN EXISTANCE                  #################
    def check_existing_ssn(self, ssn):
        query = """
            SELECT * FROM Employees 
            WHERE ssn='%s';""" % (ssn)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            print('SSN not registered')
            return 0
        return 1

    #################        FUNCTION TO GET ANY TIER1 EMPLOYEE                   #################
    def getTier1_emp(self):
        query = """
            SELECT * FROM Employees 
            WHERE active = 1 and tier = 1;"""
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            print('No tier1 employee found')
            return 'None'
        print(result[0][0])
        return result[0][0]

    #################        FUNCTION TO GET ANY TIER2 EMPLOYEE                    #################
    def getTier2_emp(self):
        query = """
            SELECT * FROM Employees 
            WHERE active = 1 and tier = 2;"""
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            print('No tier2 employee found')
            return 'None'
        print(result[0][0])
        return result[0][0]

    # #################        FUNCTION TO VERIFY EMPLOYEE                   #################
    #     def verify_employee(self, emp_id, password):
    #         query = """
    #             SELECT customer_id FROM Employees
    #             WHERE emp_id='%s' and password='%s';""" % (emp_id, encrypt(password))
    #         cursor.execute(query)
    #         result = cursor.fetchall()
    #         # print(len(result))
    #         if len(result) == 0:
    #             # print('Customer with this ID not exists')
    #             return 0
    #         return 1

    #################        FUNCTION TO ADD TRANSACTIONS (APPROVER: TIER2)                    #################
    def add_transaction(self, account1, account2, amount):
        c = Customers()
        if c.verify_account(int(account1)) == 0:
            return 'account ' + str(account1) + ' doesn\'t exists'

        if c.verify_account(int(account2)) == 0:
            return 'account ' + str(account2) + ' doesn\'t exists'

        approver = 1
        print('at add_transaction')
        if float(amount) > 1000:
            approver = 2
        query = """ 
                    INSERT into Transactions(from_account, to_account, approver1_id, approver2, amount, status, deposit) 
                    VALUES(%d, %d, '-1', %d, %d, 1, 0)
                    """ % (int(account1), int(account2), approver, float(amount))
        # Status 1 means still pending
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            msg = 'Request to be approved by tier' + str(approver) + ' employee'
            print(msg)
            return msg
        except Exception as e:
            db.rollback()
            print('Cannot transfer funds:', e)
            return 'Try Again later'

    #################        FUNCTION TO ADD TRANSACTION DEPOSIT (APPROVER: TIER2)                    #################
    def add_transaction_deposit(self, account, amount):
        approver = 1
        print('at add_transaction_deposit')
        if float(amount) > 1000:
            approver = 2
        query = """ 
                    INSERT into Transactions(from_account, to_account, approver1_id, approver2, amount, status, deposit) 
                    VALUES(%d, %d, '-1', %d, %d, 1, 1)
                    """ % (int(account), int(account), approver, float(amount))
        # Status 1 means still pending
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            msg = 'Request to be approved by tier' + str(approver) + ' employee'
            print(msg)
            return msg
        except Exception as e:
            db.rollback()
            print('Cannot deposit funds:', e)
            return 'Try Again later'

        #################                 FUNCTION TO DENY FUND REQUEST             #################

    def deny_funds_requested(self, employee_id, transaction_no):
        tier = self.get_employee_tier(employee_id)
        if tier < 2:
            return 'Not authorized to GET/Approve transactions'

        query = """
            UPDATE Transactions SET remark='Request Denied by Bank', status=0
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

    #################                 FUNCTION TO APPROVE FUND TRANFER REQESTS           #################
    def approve_fund_request(self, transaction_no):
        from_account = emp.get_fromAccount_of_transaction(transaction_no)
        to_account = emp.get_toAccount_of_transaction(transaction_no)
        amount = emp.get_amount_of_transaction(transaction_no)
        status = emp.get_transaction_status(transaction_no)
        result = 'Invalid transaction_no'
        print(from_account, to_account, amount)
        if from_account != -1 and to_account != -1 and amount != -1 and status != 0:
            c = Customers()
            result = c.fund_transfers(from_account, to_account, amount, int(transaction_no))
        return result

    #################        FUNCTION TO GET FUND TRANSFER REQUESTs LIST                    #################
    def fund_transfer_requests(self, employee_id):
        tier = self.get_employee_tier(employee_id)
        # if tier != 2 :
        #     return 'None'

        query = """
            SELECT * FROM Transactions 
            WHERE approver2 = %d and status = 1; """ % (tier)
        # print(len(result))
        try:
            db.commit()
            cursor.execute(query)
            result = cursor.fetchall()
            if len(result) == 0:
                return 'None'
            return result
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  getting  fund_transfer_requests List')

    #################        FUNCTION TO GET ACCOUNT UPDATE REQUEST's LIST                    #################
    def update_info_reqest_list(self, employee_id):
        tier = self.get_employee_tier(employee_id)
        approver = 1
        if tier == 3:
            approver = 3

        query = """
            SELECT *  FROM  Updateinfo 
            WHERE  status = 1 and approver = %d ; """ % (approver)

        cursor.execute(query)
        result = cursor.fetchall()
        # print(result)

        try:
            db.commit()
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  update cache after undateInfo request List')

        if len(result) == 0:
            # print('Customer with this ID not exists')
            return 0
        return result

    def handle_appointment(self):
        pass
        # query = """
        # DELETE FROM Appointments WHERE emp_id='%s';""" % (self.__emp_id);
        # cursor.execute(query)
        # try:
        #     db.commit()
        #     result = cursor.fetchall()
        #     print('Appointment attended.')
        # except Exception as e:
        #     db.rollback()
        #     print('Cannot Delete Appointment:', e)

    def get_employee_tier(self, emp_id):
        query = """
            Select tier from Employees 
            WHERE emp_id = '%s';
            """ % (emp_id)
        cursor.execute(query)
        result = cursor.fetchall()
        if len(result) == 0:
            # print('Account doesn\'t exists')
            return "None"
        return result[0][0]

    def get_amount_of_transaction(self, transaction_no):
        query = """
            SELECT amount FROM Transactions 
            WHERE transaction_no=%d ; """ % (int(transaction_no))
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            return -1
        return result[0][0]

    def get_fromAccount_of_transaction(self, transaction_no):
        query = """
            SELECT from_account FROM Transactions 
            WHERE transaction_no=%d ; """ % (int(transaction_no))
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            return -1
        return result[0][0]

    def get_toAccount_of_transaction(self, transaction_no):
        query = """
            SELECT to_account FROM Transactions 
            WHERE transaction_no=%d ; """ % (int(transaction_no))
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            return -1
        return result[0][0]

    def get_transaction_status(self, transaction_no):
        query = """
            SELECT status FROM Transactions 
            WHERE transaction_no=%d ; """ % (int(transaction_no))
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            return -1
        return result[0][0]

    def transfer_transaction_to_tier2(self, transaction_no):
        # tier2_id = self.getTier2_emp()
        query = """
            UPDATE Transactions SET approver1_id='-1', approver2 = 2 
            WHERE transaction_no = %d;""" % (int(transaction_no))
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            return 'Request Sent to Tier2 employee'
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  approving transaction')
            return 0

    def system_logs(self):  # TIER 3
        pass
        # if(self.__tier == '3'):
        #     query = """
        #         SELECT * FROM System_Log;
        #         """
        #     cursor.execute(query)
        #     try:
        #         #db.commit()
        #         result = cursor.fetchall()
        #         for x in result:
        #             print(x)
        #     except Exception as e:
        #         #db.rollback()
        #         print('Cannot view System Logs:', e)
        # else:
        #     print("You are not authorized to view System logs.")

    #################        FUNCTION TO UPDATE EMPLOYEE ACCOUNT INFO                     #################
    def update_employee(self, userid, emp_id, email, firstname, midname, lastname, contact_no, dob, address):
        tier = self.get_employee_tier(userid)
        if tier != 3:
            return 'Not authorized to update customer'

        query = """
            UPDATE Employees 
            SET last_name='%s', middle_name='%s', first_name='%s', dob='%s', contact_no='%s', email_id='%s', address='%s' 
            WHERE emp_id='%s';""" % (lastname, midname, firstname, dob, contact_no, email, address, emp_id)
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            print('Employee Updated')
            return 'Employee Updated'
        except Exception as e:
            db.rollback()
            print('Cannot update Employee:', e)
            return 'Cannot update Employee'

    #################        FUNCTION TO GET EMPLOYEE'S ALL INFO              #################
    def get_employee_details(self, emp_id):
        query = """
            SELECT first_name, middle_name, last_name, dob, contact_no, email_id, address, ssn, active, tier 
            FROM Employees WHERE emp_id = '%s';""" % (emp_id)
        cursor.execute(query)
        result = cursor.fetchall()
        # print(len(result))
        if len(result) == 0:
            print('Employee doesn\'t exists')
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
               'tier': result[0][9]
               }
        return res

    def deactivate_account(self, userid, account):
        # tier = self.get_employee_tier(userid)
        # if tier !=2:
        #     return 'Not authorized to dectivate accounts'
        # tier2_id = self.getTier2_emp()
        # query = """
        #     UPDATE Accounts SET active = 0
        #     WHERE account_no = %d;""" % (int(account))
        c = Customers()
        if c.verify_account(int(account)) == 0:
            return 'account doesn\'t exists'
        query = """
            DELETE FROM Accounts  
            WHERE account_no = %d;""" % (int(account))
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            return 'Account Closed'
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  Account cannot be Closed')
            return 'Account cannot be Closed'

    def deactivate_customer(self, userid, customer_id):
        # tier = self.get_employee_tier(userid)
        # if tier !=2:
        #     return 'Not authorized to dectivate customer'
        # tier2_id = self.getTier2_emp()
        # query = """
        #     UPDATE Customers SET active = 0
        #     WHERE customer_id = %d;""" % (customer_id)
        c = Customers()
        if c.check_user_id(customer_id) == 0:
            return 'customer doesn\'t exists'

        query = """
            DELETE FROM Accounts
            WHERE customer_id = '%s';""" % (customer_id)
        cursor.execute(query)

        query = """
            DELETE FROM Customers
            WHERE customer_id = '%s';""" % (customer_id)
        cursor.execute(query)

        try:
            db.commit()
            # result = cursor.fetchall()
            return 'Customer deactivated'
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  deactivate Customer')
            return 'Cannot deactivate Customer'

    def deactivate_employee(self, userid, emp_id):
        if self.check_user_id(emp_id) == 0:
            return 'Employee doesn\'t exists'

        tier = self.get_employee_tier(userid)
        if tier != 3:
            return 'Not authorized to dectivate employee'
        # tier2_id = self.getTier2_emp()
        query = """
            UPDATE Employees SET active = 0 
            WHERE emp_id = '%s';""" % (emp_id)
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            return 'Employee deactivated'
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  deactivate Employee')
            return 'Cannot deactivate Employee'

    def approve_update_info(self, userid, update_req_no):
        # tier = self.get_employee_tier(userid)
        # if tier !=2:
        #     return 'Not authorized to update customer'
        # tier2_id = self.getTier2_emp()
        query = """
            SELECT requester, userid, contact_no, email_id, address FROM Updateinfo
            WHERE update_req_no = %d;""" % (int(update_req_no))
        cursor.execute(query)
        values = cursor.fetchall()
        if (len(values)) == 0:
            return 'Invalid Update reqest ID'
        requester = values[0][0]
        query = ''
        if requester == 'Customer':
            query = """
                UPDATE Customers SET  contact_no = '%s', email_id = '%s', address = '%s' 
                WHERE customer_id = '%s';""" % (values[0][2], values[0][3], values[0][4], values[0][1])
        else:
            query = """
                UPDATE Employees SET  contact_no = '%s', email_id = '%s', address = '%s' 
                WHERE emp_id = '%s';""" % (values[0][2], values[0][3], values[0][4], values[0][1])
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            # return requester + ' Updated'
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  Update ')
            return 'Cannot Update ' + requester

        query = """
                UPDATE Updateinfo set status = 0 WHERE update_req_no=%d""" % (int(update_req_no))
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            return requester + ' Updated'
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  Update ')
            return 'Cannot Update ' + requester

    def deny_update_info(self, userid, update_req_no):
        # tier = self.get_employee_tier(userid)
        # if tier !=2:
        #     return 'Not authorized to update customer'
        # tier2_id = self.getTier2_emp()
        query = """
            UPDATE Updateinfo SET status = 0
            WHERE update_req_no = %d;""" % (int(update_req_no))
        cursor.execute(query)
        try:
            db.commit()
            # result = cursor.fetchall()
            return 'Done'
        except Exception as e:
            db.rollback()
            print(e, ' : Error in  deny update info')
            return 'Try again later'

    #################        FUNCTION TO RESET PASSORD                    #################
    def reset_password(self, userid, oldPassword, newPassword):
        query = """
                UPDATE Employees Set password = '%s' 
                WHERE emp_id = '%s';""" % (encrypt(newPassword), userid)

        if self.verify_employee(userid, oldPassword):
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
                UPDATE Employees Set password = '%s' 
                WHERE emp_id = '%s';""" % (encrypt(newPassword), userid)

        cursor.execute(query)
        try:
            db.commit()
            return 'Password Updated'
        except Exception as e:
            db.rollback()
            print('Cannot make request:', e)
            return 'Try Again Later'

        # emp = Employee('1', '2')


# emp.get_employee_tier('1')
emp = Employee()
print(emp.create_employee('anilkh', "khadwal",  "", "ROHIT",  "6205709721", "ani1asq@gmail.com", "password","ssn1sa214", getdate(), 1))
#create_employee(self, emp_id, last_name, middle_name, first_name, contact_no, email_id,  password, dob, ssn, tier, active=1,  address="")
