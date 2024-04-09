import logging
import os
from flask_bcrypt import Bcrypt
from flask import Flask, session, jsonify, request, send_from_directory, redirect, url_for
from flask_cors import CORS, cross_origin
import json
from customer import Customers
from markupsafe import escape
from employee import Employee

from werkzeug.utils import secure_filename
from utility.encrypt import encrypt, check_encrypted_password
from otp import OtpInterface
from utility.encrypt import verify_password

logging.basicConfig(level=logging.INFO, filename='SystemLogs/bank.log', filemode='w',
                    format='%(asctime)-15s - %(name)s - %(levelname)s - %(message)s', datefmt='%d-%m-%Y %H:%M:%S')
otpSet = {}

app = Flask(__name__)

app.secret_key = os.urandom(24)

CORS(app)
Bcrypt(app)


@app.route('/', methods=['GET'])
def get_login_page_ui():  # put application's code here
    return send_from_directory('templates', 'Login.html')


@app.route('/customer_dash', methods=['GET'])
def get_customer_dash_ui():
    return send_from_directory('templates', 'customer.html')


@app.route('/admin', methods=['GET'])
def get_admin_dashboard_ui():
    return send_from_directory('templates', 'admin.html')


@app.route('/tier1', methods=['GET'])
def get_tier1_dashboard_ui():
    return send_from_directory('templates', 'tier1.html')


@app.route('/tier2', methods=['GET'])
def get_tier2_dashboard_ui():
    return send_from_directory('templates', 'tier2.html')


###############                   HANDLE FOR NEW REGISTER CUSTOMER             ###############
@app.route('/registerCustomer', methods=['POST', 'GET'])
@cross_origin()
def registerCustomer():
    values = request.get_json()
    logging.debug('Data @registerCustomer' + str(values))
    # print('Data @registerCustomer', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400
    required = ['empid', 'userid', 'password', 'email', 'firstname', 'midname', 'lastname', 'phone', 'dob', 'ssn',
                'address']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400
    c = Customers()
    if c.check_user_id(values['userid']):
        response = {
            'message': 'AcountID exists'
        }
        return jsonify(response), 400
    if c.check_existing_contact(values['phone']):
        response = {
            'message': 'Contact already registered'
        }
        return jsonify(response), 400
    if c.check_existing_email(values['email']):
        response = {
            'message': 'Email already registered'
        }
        return jsonify(response), 400
    if c.check_existing_email(values['ssn']):
        response = {
            'message': 'SSN already registered'
        }
        return jsonify(response), 400
    temp = c.create_customer_id(values['userid'], values['lastname'], values['midname'], values['firstname'],
                                values['phone'], values['email'], values['password'], values['ssn'], values['dob'], 1,
                                values['address'])
    if temp == 1 and values['empid'] != 'None':
        # emp = Employee()
        # tier = emp.get_employee_tier(values['empid'])
        # if tier !=2 :
        #     response = {
        #     'message' : 'Not authorized to create employee'
        #     }
        #     return jsonify(response), 200
        logging.info('New customer created: ' + values['userid'])
        response = {
            'message': 'Done'
        }
        return jsonify(response), 200

    if temp == 1:
        session[values['userid']] = values['userid']
        print('Redirecting to Customer dashboard')
        return redirect(url_for('get_customer_dashboard_ui', _external=True, _scheme='http'))

    response = {
        'message': 'Something Went wrong, Please try again later'
    }
    return jsonify(response), 400


###############                   HANDLE FOR NEW REGISTER EMPLOYEE             ###############
@app.route('/registerEmployee', methods=['POST', 'GET'])
@cross_origin()
def registerEmployee():
    values = request.get_json()
    logging.debug('Data @registerEmployee' + str(values))

    if not values:
        return jsonify({'message': 'No data Found'}), 400

    required = ['userid', 'password', 'email', 'firstname', 'midname', 'lastname', 'phone', 'dob', 'ssn', 'address',
                'tier']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    emp = Employee()
    if emp.check_user_id(values['userid']):
        return jsonify({'message': 'AccountID exists'}), 400

    if emp.check_existing_contact(values['phone']):
        return jsonify({'message': 'Contact already registered'}), 400

    if emp.check_existing_email(values['email']):
        return jsonify({'message': 'Email already registered'}), 400

    # Assuming you meant to check if the SSN is already registered with a different method, not check_existing_email
    if emp.check_existing_ssn(values['ssn']):  # Replace 'check_existing_email' with the correct method for checking SSN
        return jsonify({'message': 'SSN already registered'}), 400

    # If none of the above conditions are met, proceed to create the employee.
    response = {
        'message': emp.create_employee(values['userid'], values['lastname'], values['midname'],
                                       values['firstname'], values['phone'], values['email'], values['password'],
                                       values['ssn'], values['dob'], values['tier'], 1, values['address'])
    }
    return jsonify(response), 200


###############                   HANDLE FOR LOGIN (EMP + CUST)             ###############
from flask import request, jsonify, session, redirect, url_for
from flask_cors import cross_origin
import logging


# Import your encrypt function here. If it's in the same file, you don't need to import it.
# from yourmodule import encrypt, Customers, Employee

from flask import request, jsonify, session, redirect, url_for
from flask_cors import cross_origin
import logging
# Assume the Customers and Employee classes are defined elsewhere
# from yourmodule import Customers, Employee, verify_password

@app.route('/login', methods=['POST', 'GET'])
@cross_origin()
def login():
    values = request.get_json()
    logging.debug('Data @login' + str(values))
    if not values:
        response = {
            'message': 'No data found'
        }
        return jsonify(response), 400

    required = ['userid', 'password', 'usertype']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    user_provided_password = values['password']

    if values['usertype'] == 'customer':
        c = Customers()
        # Retrieve the hashed password for the user from the database
        hashed_password_in_db = c.retrieve_hashed_password(values['userid'])
        if hashed_password_in_db and check_encrypted_password(values['password'], hashed_password_in_db):
            session[values['userid']] = values['userid']
            c.update_login_history(values['userid'])
            logging.info("Customer " + values['userid'] + " logged in")
            return redirect(url_for('get_customer_dash_ui', _external=True, _scheme='http'))
        else:
            response = {
                'message': 'UserID/Password doesn\'t match'
            }
            return jsonify(response), 400
    else:
        emp = Employee()
        hashed_password_in_db = emp.retrieve_hashed_password(values['userid'])
        if hashed_password_in_db and verify_password(hashed_password_in_db, user_provided_password):
            session[values['userid']] = values['userid']
            emp_tier = emp.get_employee_tier(values['userid'])
            logging.info("Employee " + values['userid'] + " logged in")
            if emp_tier == 3:
                return redirect(url_for('get_admin_dashboard_ui', _external=True, _scheme='http'))
            else:
                return redirect(url_for('get_tier' + str(emp_tier) + '_dashboard_ui', _external=True, _scheme='http'))
        else:
            response = {
                'message': 'Invalid username/password'
            }
            return jsonify(response), 400



###############                 HANDLE FOR FILL CUSTOMER DASH               ###############
@app.route('/loadCustomer', methods=['POST', 'GET'])
@cross_origin()
def get_customer_data():
    values = request.get_json()
    logging.debug('Data @loadCustomer' + str(values))
    print('Data @loadCustomer:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['customer_id', 'usertype']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['usertype'] != 'customer':
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    if values['customer_id'] in session:
        c = Customers()
        response = {
            'Accounts': c.get_all_account(values['customer_id']),
            'Info': c.get_customer_details(values['customer_id']),
            'FundsRequests': c.get_funds_requests(values['customer_id'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                 HANDLE FOR FILL EMPLOYEE DASH               ###############
@app.route('/loadEmployee', methods=['GET', 'POST'])
@cross_origin()
def get_employee_data():
    values = request.get_json()
    logging.debug('Data @loadEmployee' + str(values))
    # print('Data @loadEmployee:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['employee_id', 'usertype']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['usertype'] == 'customer':
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    if values['employee_id'] is None or values['usertype'] is None:
        # print('Heyyy')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    e = Employee()
    emp_tier = e.get_employee_tier(values['employee_id'])
    if ((values['usertype'] == 'tier1' and emp_tier != 1) or
            (values['usertype'] == 'admin' and emp_tier != 3) or
            (values['usertype'] == 'tier2' and emp_tier != 2)):
        # print('Heyyy')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    if values['employee_id'] in session:
        # e = Employee()
        response = {
            'Info': e.get_employee_details(values['employee_id']),
            'FundsRequests': e.fund_transfer_requests(values['employee_id']),
            'UpdateInfo': e.update_info_reqest_list(values['employee_id'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                 HANDLE to OPEN NEW ACCOUNT              ###############
@app.route('/openNewAccount', methods=['POST', 'GET'])
@cross_origin()
def open_new_account():
    values = request.get_json()
    logging.debug('Data @openNewAccount' + str(values))
    # print('Data @openNewAccount:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'customer_id', 'account_type']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        c = Customers()
        response = {
            'message': c.open_account(values['customer_id'], values['account_type'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                HANDLE FOR FUND TRASNFER            ###############
@app.route('/fundTransfer', methods=['POST', 'GET'])
@cross_origin()
def fund_transfers():
    values = request.get_json()
    logging.debug('Data @fundTransfer' + str(values))
    # print('Data @fundTransfer:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'fromAccount', 'toAccount', 'amount']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    values['fromAccount'] = int(values['fromAccount'])
    values['toAccount'] = int(values['toAccount'])
    values['amount'] = float(values['amount'])
    if float(values['amount'] < 0):
        response = {
            'message': 'Enter a valid amount'
        }
        return jsonify(response), 200

    if values['userid'] in session:
        emp = Employee()
        response = {
            'message': emp.add_transaction(values['fromAccount'], values['toAccount'], values['amount'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                HANDLE TO REQUEST FUNDS           ###############
@app.route('/requestFunds', methods=['POST', 'GET'])
@cross_origin()
def request_funds():
    values = request.get_json()
    logging.debug('Data @requestFunds' + str(values))
    # print('Data @requestFunds:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'fromAccount', 'toAccount', 'amount']
    if float(values['amount']) < 0:
        response = {
            'message': 'Enter a valid amount'
        }
        return jsonify(response), 200
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        c = Customers()
        response = {
            'message': c.fund_request(values['fromAccount'], values['toAccount'], values['amount'])
        }
        return jsonify(response), 200

    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                HANDLE TO DEPOSIT MONEY            ###############
@app.route('/depositAmount', methods=['POST', 'GET'])
@cross_origin()
def deposit_fund():
    values = request.get_json()
    logging.debug('Data @depositAmount' + str(values))
    # print('Data @depositAmount:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'account', 'amount']
    if float(values['amount']) < 0:
        response = {
            'message': 'Enter a valid amount'
        }
        return jsonify(response), 200
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        emp = Employee()
        response = {
            'message': emp.add_transaction_deposit(values['account'], values['amount'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                HANDLE TO WITHDRAW MONEY            ###############
@app.route('/withdrawAmount', methods=['POST', 'GET'])
@cross_origin()
def withdraw_fund():
    values = request.get_json()
    logging.debug('Data @withdrawAmount' + str(values))
    # print('Data @depositAmount:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'account', 'amount']

    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if float(values['amount']) < 0:
        response = {
            'message': 'Enter a valid amount'
        }
        return jsonify(response), 200

    if values['userid'] in session:
        c = Customers()
        response = {
            'message': c.debit_request(values['account'], values['amount'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############            HANDLE TO APPROVE FUND'S REQUEST By CUSTOMER          ###############
@app.route('/approveRequest', methods=['POST', 'GET'])
@cross_origin()
def approve_request():
    values = request.get_json()
    logging.debug('Data @approveRequest' + str(values))
    # print('Data @applicationroveRequest:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['customer_id', 'transaction_no']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['customer_id'] in session:
        emp = Employee()
        amount = emp.get_amount_of_transaction(values['transaction_no'])
        if amount == 'None':
            response = {
                'message': 'Wrong Transaction number'
            }
            return jsonify(response), 200

        if amount > 1000:
            response = {
                'message': emp.transfer_transaction_to_tier2(values['transaction_no'])
            }
            return jsonify(response), 200

        from_account = emp.get_fromAccount_of_transaction(int(values['transaction_no']))
        to_account = emp.get_toAccount_of_transaction(int(values['transaction_no']))
        amount = emp.get_amount_of_transaction(int(values['transaction_no']))
        status = emp.get_transaction_status(int(values['transaction_no']))
        response = {
            'message': 'Invalid transaction_no'
        }
        print(from_account, to_account, amount)
        if from_account != -1 and to_account != -1 and amount != -1 and status != 0:
            c = Customers()
            response = {
                'message': c.fund_transfers(from_account, to_account, amount, int(values['transaction_no']))
            }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http', ))


###############            HANDLE TO APPROVE FUND'S REQUEST By EMPLOYEE (not Tier1)         ###############
@app.route('/approveRequestEmp', methods=['POST', 'GET'])
@cross_origin()
def approve_request_employee():
    values = request.get_json()
    logging.debug('Data @approveRequestEmp' + str(values))
    # print('Data @approveRequest:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'transaction_no']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        emp = Employee()
        amount = emp.get_amount_of_transaction(values['transaction_no'])
        if amount == 'None':
            response = {
                'message': 'Wrong Transaction number'
            }
            return jsonify(response), 200
        tier = emp.get_employee_tier(values['userid'])

        from_account = emp.get_fromAccount_of_transaction(values['transaction_no'])
        to_account = emp.get_toAccount_of_transaction(values['transaction_no'])
        amount = emp.get_amount_of_transaction(values['transaction_no'])
        status = emp.get_transaction_status(values['transaction_no'])
        response = {
            'message': 'Invalid transaction_no'
        }
        print(from_account, to_account, amount)
        if from_account != -1 and to_account != -1 and amount != -1 and status != 0:
            c = Customers()
            response = {
                'message': c.fund_transfers(from_account, to_account, amount, int(values['transaction_no']))
            }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                HANDLE TO deny FUND TRANSFER REQUEST           ###############
@app.route('/denyRequest', methods=['POST', 'GET'])
@cross_origin()
def deny_request():
    values = request.get_json()
    logging.debug('Data @denyRequest' + str(values))
    # print('Data @denyRequest:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'transaction_no']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        c = Customers()
        response = {
            'message': c.deny_funds_requested(values['transaction_no'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                HANDLE TO GET TRANSACTION HISTORY            ###############
@app.route('/getTransactionHistory', methods=['POST', 'GET'])
@cross_origin()
def get_transaction_history():
    values = request.get_json()
    logging.debug('Data @getTransactionHistory' + str(values))
    # print('Data @getTransactionHistory:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'account_no']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        c = Customers()
        response = {
            'message': c.get_transaction_history(values['account_no'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############              HANDLE FOR GET CHEQUE (BY CUSTOMER/BANK)             ###############
@app.route('/getCashierCheque', methods=['POST', 'GET'])
@cross_origin()
def make_cashier_cheque():
    values = request.get_json()
    logging.debug('Data @getCashierCheque' + str(values))
    # print('Data @getCashierCheque:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'to_account', 'from_account', 'amount']

    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if float(values['amount']) < 0:
        response = {
            'message': 'Enter a valid amount'
        }
        return jsonify(response), 200

    if values['userid'] in session:
        c = Customers()
        response = {
            'message': c.make_cashier_check(values['userid'], values['to_account'],
                                            values['from_account'], values['amount'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############            HANDLE FOR DEPOSIT CHEQUE (BY CUSTOMER/BANK)          ###############
@app.route('/depositCheck', methods=['POST', 'GET'])
@cross_origin()
def deposit_cheuqe():
    values = request.get_json()
    logging.debug('Data @depositCheck' + str(values))
    # print('Data @depositCheck:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'cheque_no']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        c = Customers()
        response = {
            'message': c.deposit_check(values['userid'], values['cheque_no'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############         HANDLE FOR GET  ISSUED CHEQUE'S LIST (BY CUSTOMER)           ###############
@app.route('/getChequeList', methods=['POST', 'GET'])
@cross_origin()
def get_cheque_list():
    values = request.get_json()
    logging.debug('Data @getChequeList' + str(values))
    # print('Data @getChequeList:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        c = Customers()
        response = {
            'message': c.get_cheque_list(values['userid'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                         HANDLE FOR MAKE APPOINTMENT (BY CUSTOMER)                    ###############
@app.route('/makeAppointment', methods=['POST', 'GET'])
@cross_origin()
def make_appointment():
    values = request.get_json()
    logging.debug('Data @makeAppointment' + str(values))
    # print('Data @makeAppointment:',values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400
    required = ['customer_id', 'time']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400
    if values['customer_id'] in session:
        c = Customers()
        response = {
            'message': c.make_appointment(values['customer_id'], values['time'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                HANDLE TO GET APPOINTMENT LIST (BY CUSTOMER)                 ###############
@app.route('/getAppointmentList', methods=['POST', 'GET'])
@cross_origin()
def get_appointment_list():
    values = request.get_json()
    logging.debug('Data @getAppointmentList' + str(values))
    # print('Data @getAppointmentList:',values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400
    required = ['customer_id']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400
    if values['customer_id'] in session:
        c = Customers()
        response = {
            'message': c.get_appointment(values['customer_id'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                HANDLE TO REQUEST UPDATE INFO BY CUSTOMER                 ###############
@app.route('/updateInfo', methods=['POST', 'GET'])
@cross_origin()
def update_info():
    values = request.get_json()
    logging.debug('Data @updateInfo' + str(values))
    # print('Data @updateInfo:',values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400
    required = ['userid', 'email', 'contact_no', 'address', 'requester']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        c = Customers()
        response = {
            'message': c.update_info_reqest(values['requester'], values['userid'], values['email'],
                                            values['contact_no'], values['address'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                HANDLE TO APPROVE UPDATE INFO (BY BANK)                 ###############
@app.route('/approveUpdateInfo', methods=['POST', 'GET'])
@cross_origin()
def approve_update_info():
    values = request.get_json()
    logging.debug('Data @approveUpdateInfo' + str(values))
    # print('Data @updateInfo:',values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400
    required = ['userid', 'update_req_no']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400
    if values['userid'] in session:
        emp = Employee()
        response = {
            'message': emp.approve_update_info(values['userid'], values['update_req_no'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                HANDLE TO deny UPDATE INFO (BY BANK)                 ###############
@app.route('/denyUpdateInfo', methods=['POST', 'GET'])
@cross_origin()
def deny_update_info():
    values = request.get_json()
    logging.debug('Data @denyUpdateInfo' + str(values))
    # print('Data @updateInfo:',values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400
    required = ['userid', 'update_req_no']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400
    if values['userid'] in session:
        emp = Employee()
        response = {
            'message': emp.deny_update_info(values['userid'], values['update_req_no'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                 HANDLE TO GET WHOLE CUSTOMER INFO (FOR BANK)               ###############
@app.route('/getCustomer', methods=['POST', 'GET'])
@cross_origin()
def get_customer():
    values = request.get_json()
    logging.debug('Data @getCustomer' + str(values))
    # print('Data @getCustomer:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'customer_id']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        c = Customers()
        response = {
            'Accounts': c.get_all_account(values['customer_id']),
            'Info': c.get_customer_details(values['customer_id'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                 HANDLE TO GET WHOLE EMPLOYEE INFO (FOR BANK)               ###############
@app.route('/getEmployee', methods=['POST', 'GET'])
@cross_origin()
def get_employee():
    values = request.get_json()
    logging.debug('Data @getEmployee' + str(values))
    # print('Data @getEmployee:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'emp_id']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        emp = Employee()
        response = {
            # 'Accounts'      : c.get_all_account(values['customer_id']),
            'Info': emp.get_employee_details(values['emp_id'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                 HANDLE TO MODIFY CUSTOMER ACCOUNT (FOR BANK-ADMIN)               ###############
@app.route('/modifyCustomer', methods=['POST', 'GET'])
@cross_origin()
def modify_customer():
    values = request.get_json()
    logging.debug('Data @modifyCustomer' + str(values))
    # print('Data @modifyCustomer:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'customer_id', 'last_name', 'middle_name', 'first_name', 'contact_no', 'email_id',
                'ssn', 'dob', 'address']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        c = Customers()
        response = {
            'message': c.update_account_info(values['customer_id'], values['last_name'],
                                             values['middle_name'], values['first_name'], values['contact_no'],
                                             values['email_id'],
                                             values['ssn'], values['dob'], values['address'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                 HANDLE TO MODIFY EMPLOYEE ACCOUNT (FOR BANK-ADMIN)               ###############
@app.route('/modifyEmployee', methods=['POST', 'GET'])
@cross_origin()
def modify_employee():
    values = request.get_json()
    logging.debug('Data @modifyEmployee' + str(values))
    # print('Data @modifyEmployee:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'emp_id', 'last_name', 'middle_name', 'first_name', 'contact_no', 'email_id',
                'ssn', 'dob', 'address', 'tier']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        emp = Employee()
        response = {
            'message': emp.update_account_info(values['emp_id'], values['last_name'],
                                               values['middle_name'], values['first_name'], values['contact_no'],
                                               values['email_id'],
                                               values['ssn'], values['dob'], values['address'], values['tier'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                 HANDLE TO DEACTIVATE ACCOUNT (FOR BANK)               ###############
@app.route('/deactivateAccount', methods=['POST', 'GET'])
@cross_origin()
def deactivate_account():
    values = request.get_json()
    logging.debug('Data @deactivateAccount' + str(values))
    # print('Data @deactivateAccount:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'account_no']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        emp = Employee()
        response = {
            'message': emp.deactivate_account(values['userid'], values['account_no'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                 HANDLE TO DEACTIVATE CUSTOMER (FOR BANK)               ###############
@app.route('/deactivateCustomer', methods=['POST', 'GET'])
@cross_origin()
def deactivate_customer():
    values = request.get_json()
    logging.debug('Data @deactivateCustomer' + str(values))
    # print('Data @deactivateCustomer:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'customer_id']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        emp = Employee()
        response = {
            'message': emp.deactivate_customer(values['userid'], values['customer_id'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                 HANDLE TO UPDATE EMPLOYEE (FOR BANK)               ###############
# @application.route('/updateEmployee', methods=['POST'])
# def update_employee():
#     values = request.get_json()
#     logging.debug('Data @updateEmployee' + str(values))
#     # print('Data @closeAccount:', values)
#     if not values:
#         response = {
#             'message' : 'No data Found'
#         }
#         return jsonify(response), 400

#     required = ['userid', 'emp_id', 'email', 'firstname', 'midname', 'lastname', 'phone', 'dob', 'ssn', 'address' ]
#     if not all(key in values for key in required):
#         response = {
#             'message' : 'Some data missing'
#         }
#         return jsonify(response), 400

#     if values['userid'] in session:
#         emp = Employee()
#         response = {
#                 'message' : emp.update_employee(values['userid'], values['emp_id'], values['email'],
#                 values['firstname'], values['midname'], values['lastname'], values['phone'],
#                 values['dob'], values['address'])
#         }
#         return jsonify(response), 200
#     else:
#         print('Not logged In')
#         return redirect(url_for('get_login_page_ui', _external=True, _scheme='https'))

###############                 HANDLE TO DEACTIVATE EMPLOYEE (FOR BANK)               ###############
@app.route('/deactivateEmployee', methods=['POST', 'GET'])
@cross_origin()
def deactivate_employee():
    values = request.get_json()
    logging.debug('Data @deactivateEmployee' + str(values))
    # print('Data @closeAccount:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid', 'emp_id']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        emp = Employee()
        response = {
            'message': emp.deactivate_employee(values['userid'], values['emp_id'])
        }
        return jsonify(response), 200
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                HANDLE TO REQUEST OTP                 ###############
@app.route('/sendOTP', methods=['POST', 'GET'])
@cross_origin()
def send_otp():
    values = request.get_json()
    logging.debug('Data @sendOTP' + str(values))
    # print('Data @sendOTP:',values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400
    required = ['userid', 'requester']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    c = Customers()
    if values['requester'] == 'Employee':
        c = Employee()

    if c.check_user_id(values['userid']):
        otpi = OtpInterface()
        # session[values['userid'] + 'pyotp'] = otpi
        otpSet[values['userid'] + 'pyotp' + values['requester']] = otpi
        print(otpi.getObj())
        contact_no = c.get_customer_contactNo(values['userid'])
        response = {
            'message': otpi.send_otp(contact_no)
        }
        return jsonify(response), 200
    else:
        response = {
            'message': 'Invalid User ID'
        }
        return jsonify(response), 200


###############                HANDLE TO VERIFY OTP                 ###############
@app.route('/verifyOTP', methods=['POST', 'GET'])
@cross_origin()
def verify_otp():
    values = request.get_json()
    logging.debug('Data @verifyOTP' + str(values))
    # print('Data @verifyOTP:',values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400
    required = ['userid', 'otp', 'requester']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    c = otpSet[values['userid'] + 'pyotp' + values['requester']]
    # print(c)
    if c != None:
        response = {
            'message': c.verify_otp(values['otp'])
        }
        return jsonify(response), 200
    else:
        response = {
            'message': 'Try Again'
        }
        return jsonify(response), 200


###############                HANDLE TO RESET PASSWORD                 ###############
@app.route('/resetPassword', methods=['POST', 'GET'])
@cross_origin()
def reset_password():
    values = request.get_json()
    logging.debug('Data @resetPassword' + str(values))
    # print('Data @resetPassword:',values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400
    required = ['userid', 'oldPassword', 'newPassword', 'requester', 'flag']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    obj = Employee()
    if values['requester'] == 'Customer':
        obj = Customers()

    if int(values['flag']):
        if values['userid'] in session:
            response = {
                'message': obj.reset_password(values['userid'], values['oldPassword'], values['newPassword'])
            }
            return jsonify(response), 200
        else:
            print('Not logged In')
            return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))
    else:
        response = {
            'message': obj.reset_fpassword(values['userid'], values['newPassword'])
        }
        return jsonify(response), 200


###############                         HANDLE FOR LOGOUT                    ###############
@app.route('/logout', methods=['POST', 'GET'])
@cross_origin()
def logout():
    values = request.get_json()
    logging.debug('Data @logout' + str(values))
    # print('Data @logout:',values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400
    required = ['userid']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400
    session.pop(values['userid'], None)
    return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                         HANDLE TO GET SYSTEM LOGS                     ###############
@app.route("/getSystemLogs", methods=['POST', 'GET'])
@cross_origin()
def get_report():
    values = request.get_json()
    logging.debug('Data @getSystemLogs' + str(values))
    # print('Data @modifyEmployee:', values)
    if not values:
        response = {
            'message': 'No data Found'
        }
        return jsonify(response), 400

    required = ['userid']
    if not all(key in values for key in required):
        response = {
            'message': 'Some data missing'
        }
        return jsonify(response), 400

    if values['userid'] in session:
        try:
            print('At get System Logs')
            return send_from_directory('SystemLogs', filename='bank.log', as_attachment=True)
        except FileNotFoundError:
            response = {
                'message': 'Error in getting System logs'
            }
            return jsonify(response), 400
    else:
        print('Not logged In')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


if __name__ == '__main__':
    from argparse import ArgumentParser

    logging.debug('Banking Server has Started')
    app.run(ssl_context="adhoc", debug=True, host='0.0.0.0')
