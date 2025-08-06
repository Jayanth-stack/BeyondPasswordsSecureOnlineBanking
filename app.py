import logging
import os
from twilio.rest import Client
from flask_bcrypt import Bcrypt
from flask import Flask, session, jsonify, request, send_from_directory, redirect, url_for
from flask_cors import CORS, cross_origin
from customer import Customers
from employee import Employee
from twilio.base.exceptions import TwilioRestException
from utility.encrypt import check_encrypted_password
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, filename='SystemLogs/bank.log', filemode='w',
                    format='%(asctime)-15s - %(name)s - %(levelname)s - %(message)s', datefmt='%d-%m-%Y %H:%M:%S')
otpSet = {}

app = Flask(__name__)

app.secret_key = os.urandom(24)

CORS(app)
Bcrypt(app)

account_sid = 'your Account_sid'
auth_token = 'your Auth_token'
client = Client(account_sid, auth_token)
verify_sid = 'your verify_sid'


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


@app.route('/otp_page', methods=['GET'])
def get_verifyotp_dashboard_ui():
    return send_from_directory('templates', 'OtpAuthentication.html')


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
@app.route('/login', methods=['POST', 'GET'])
@cross_origin()
def login():
    values = request.get_json()
    logging.debug('Login attempt: ' + str(values))
    if not values:
        return jsonify({'message': 'No data found'}), 400

    required = ['userid', 'password', 'usertype']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    session.clear()
    user_class = Employee if values['usertype'] in ['admin', 'employee', 'tier1', 'tier2'] else Customers
    user = user_class()

    hashed_password = user.retrieve_hashed_password(values['userid'])
    if hashed_password and check_encrypted_password(values['password'], hashed_password):
        session['userid'] = values['userid']
        session['usertype'] = values['usertype']
        if values['usertype'] != 'customer':
            session['emp_tier'] = user.get_employee_tier(values['userid'])

        phone_number = user.retrieve_phone_number(values['userid'])
        if phone_number:
            try:
                client.verify.v2.services(verify_sid).verifications.create(to=phone_number, channel='sms')
                return redirect(url_for('get_verifyotp_dashboard_ui', _external=True, _scheme='http'))
            except TwilioRestException as e:
                return jsonify({'message': 'Failed to send OTP', 'error': str(e)}), 500
        else:
            return jsonify({'message': 'No phone number available'}), 400
    else:
        return jsonify({'message': 'Invalid credentials'}), 401


@app.route('/verify-otp', methods=['POST', 'GET'])
@cross_origin()
def verify_otp():
    logging.debug("Session at start of verify-otp: {}".format(session))

    userid = session.get('userid')
    usertype = session.get('usertype')

    if not userid or not usertype:
        return jsonify({"error": "Session expired or invalid"}), 401

    values = request.get_json()
    otp_code = values.get('otp_code')
    if not otp_code:
        return jsonify({"error": "OTP code is required"}), 400

    user_class = Employee if usertype in ['admin', 'employee', 'tier1', 'tier2'] else Customers
    user = user_class()
    phone_number = user.retrieve_phone_number(userid)

    if phone_number:
        try:
            result = client.verify.v2.services(verify_sid).verification_checks.create(to=phone_number, code=otp_code)
            if result.status == "approved":
                if usertype == 'customer':
                    redirect_url = 'get_customer_dash_ui'
                else:
                    # Assuming there's a common dashboard for all tiers, or specific ones per tier
                    # Adjust this logic based on your actual routing structure for different tiers or admin
                    redirect_url = f'get_tier{session.get("emp_tier", 1)}_dashboard_ui'
                    if usertype == 'admin':
                        # Redirect admins to a specific admin dashboard if exists, or use a common tier dashboard
                        redirect_url = 'get_admin_dashboard_ui'  # Ensure this endpoint is defined in your application
                return redirect(url_for(redirect_url, _external=True, _scheme='http'))
            else:
                return jsonify({"error": "Invalid OTP"}), 401
        except TwilioRestException as e:
            return jsonify({"error": str(e)}), 500
    else:
        return jsonify({"error": "Phone number could not be retrieved"}), 400


###############                 HANDLE FOR FILL CUSTOMER DASH               ###############
@app.route('/loadCustomer', methods=['POST', 'GET'])
@cross_origin()
def get_customer_data():
    # Verify that a customer is logged in and the session contains the necessary data
    if 'userid' not in session or session.get('usertype') != 'customer':
        logging.warning('Attempt to access customer data without proper authorization or session data is missing')
        return jsonify({'message': 'Unauthorized access or session expired'}), 401

    customer_id = session['userid']
    c = Customers()
    try:
        response = {
            'Accounts': c.get_all_account(customer_id),
            'Info': c.get_customer_details(customer_id),
            'FundsRequests': c.get_funds_requests(customer_id)
        }
        return jsonify(response), 200
    except Exception as e:
        logging.error(f"Failed to retrieve customer data for {customer_id}: {str(e)}")
        return jsonify({'message': 'Failed to retrieve data', 'error': str(e)}), 500


###############                 HANDLE FOR FILL EMPLOYEE DASH               ###############
@app.route('/loadEmployee', methods=['GET', 'POST'])
@cross_origin()
def get_employee_data():
    # Verify that an employee is logged in and the session contains the necessary data
    if 'userid' not in session or session.get('usertype') not in ['admin', 'employee', 'tier1', 'tier2']:
        logging.warning('Attempt to access employee data without proper authorization or session data is missing')
        return jsonify({'message': 'Unauthorized access or session expired'}), 401

    employee_id = session['userid']
    usertype = session.get('usertype')
    emp_tier = session.get('emp_tier', 1)  # Default tier to 1 if not specified
    e = Employee()

    try:
        response = {
            'Info': e.get_employee_details(employee_id),
            'FundsRequests': e.fund_transfer_requests(employee_id),
            'UpdateInfo': e.update_info_request_list(employee_id)
        }
        return jsonify(response), 200
    except Exception as e:
        logging.error(f"Failed to retrieve employee data for {employee_id}: {str(e)}")
        return jsonify({'message': 'Failed to retrieve data', 'error': str(e)}), 500


###############                 HANDLE to OPEN NEW ACCOUNT              ###############
@app.route('/openNewAccount', methods=['POST', 'GET'])
@cross_origin()
def open_new_account():
    # Ensure the user is logged in
    if 'userid' not in session or 'usertype' not in session:
        logging.warning('Unauthorized attempt to access openNewAccount')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    values = request.get_json()
    logging.debug(f'Data @openNewAccount: {values}')

    if not values:
        logging.error('No data received for new account creation')
        return jsonify({'message': 'No data Found'}), 400

    required_fields = ['userid', 'customer_id', 'account_type']
    if not all(field in values for field in required_fields):
        logging.error('Required data missing for new account creation')
        return jsonify({'message': 'Some data missing'}), 400

    # Verify that the user initiating the request matches the session user
    if values['userid'] != session['userid']:
        logging.warning(f"Session user ID {session['userid']} does not match request user ID {values['userid']}")
        return jsonify({'message': 'User ID mismatch'}), 401

    customer = Customers()
    # Allow customers to open accounts but ensure that account type is valid
    if session['usertype'] in ['customer', 'employee', 'admin']:
        # Further validation can be implemented to check account types or other rules
        try:
            response_message = customer.open_account(values['customer_id'], values['account_type'])
            logging.info(f"New account opened: {response_message}")
            return jsonify({'message': response_message}), 200
        except Exception as e:
            logging.error(f"Failed to open new account for {values['customer_id']} due to {str(e)}")
            return jsonify({'message': 'Failed to open new account', 'error': str(e)}), 500
    else:
        logging.warning(
            f"User {session['userid']} with usertype {session['usertype']} unauthorized to open new accounts")
        return jsonify({'message': 'Unauthorized to open new accounts'}), 403


###############                HANDLE FOR FUND TRASNFER            ###############
@app.route('/fundTransfer', methods=['POST', 'GET'])
@cross_origin()
def fund_transfers():
    if 'userid' not in session:
        logging.warning('Attempt to access fundTransfer without login')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    values = request.get_json()
    logging.debug(f'Data @fundTransfer: {values}')

    if not values:
        return jsonify({'message': 'No data Found'}), 400

    required_fields = ['userid', 'fromAccount', 'toAccount', 'amount']
    if not all(field in values for field in required_fields):
        return jsonify({'message': 'Some data missing'}), 400

    if values['userid'] != session['userid']:
        logging.warning(f"Session user ID does not match request user ID.")
        return jsonify({'message': 'User ID mismatch'}), 401

    values['fromAccount'] = int(values['fromAccount'])
    values['toAccount'] = int(values['toAccount'])
    values['amount'] = float(values['amount'])

    if values['amount'] < 0:
        return jsonify({'message': 'Enter a valid amount'}), 200

    employee = Employee()
    transaction_message = employee.add_transaction(values['fromAccount'], values['toAccount'], values['amount'])
    return jsonify({'message': transaction_message}), 200


###############                HANDLE TO REQUEST FUNDS           ###############
@app.route('/requestFunds', methods=['POST', 'GET'])
@cross_origin()
def request_funds():
    if 'userid' not in session:
        logging.warning('User not logged in - requestFunds')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    values = request.get_json()
    logging.debug(f'Data @requestFunds: {values}')

    required = ['userid', 'fromAccount', 'toAccount', 'amount']
    if not values or not all(key in values for key in required):
        return jsonify({'message': 'Some data missing or invalid'}), 400

    if float(values.get('amount', 0)) < 0:
        return jsonify({'message': 'Enter a valid amount'}), 400

    if values['userid'] != session['userid']:
        return jsonify({'message': 'User ID mismatch'}), 401

    customer = Customers()
    response = customer.fund_request(values['fromAccount'], values['toAccount'], values['amount'])
    return jsonify({'message': response}), 200


###############                HANDLE TO DEPOSIT MONEY            ###############
@app.route('/depositAmount', methods=['POST', 'GET'])
@cross_origin()
def deposit_fund():
    if 'userid' not in session:
        logging.warning('User not logged in - depositAmount')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    values = request.get_json()
    logging.debug(f'Data @depositAmount: {values}')

    required = ['userid', 'account', 'amount']
    if not values or not all(key in values for key in required):
        return jsonify({'message': 'Some data missing or invalid'}), 400

    if float(values.get('amount', 0)) < 0:
        return jsonify({'message': 'Enter a valid amount'}), 400

    if values['userid'] != session['userid']:
        return jsonify({'message': 'User ID mismatch'}), 401

    employee = Employee()
    response = employee.add_transaction_deposit(values['account'], values['amount'])
    return jsonify({'message': response}), 200


###############                HANDLE TO WITHDRAW MONEY            ###############
@app.route('/withdrawAmount', methods=['POST', 'GET'])
@cross_origin()
def withdraw_fund():
    if 'userid' not in session:
        logging.warning('User not logged in - withdrawAmount')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    values = request.get_json()
    logging.debug(f'Data @withdrawAmount: {values}')

    required = ['userid', 'account', 'amount']
    if not values or not all(key in values for key in required):
        return jsonify({'message': 'Some data missing or invalid'}), 400

    if float(values.get('amount', 0)) < 0:
        return jsonify({'message': 'Enter a valid amount'}), 400

    if values['userid'] != session['userid']:
        return jsonify({'message': 'User ID mismatch'}), 401

    customer = Customers()
    response = customer.debit_request(values['account'], values['amount'])
    return jsonify({'message': response}), 200


###############            HANDLE TO APPROVE FUND'S REQUEST By CUSTOMER          ###############
@app.route('/approveRequest', methods=['POST', 'GET'])
@cross_origin()
def approve_request():
    values = request.get_json()
    logging.debug('Data @approveRequest' + str(values))
    if not values:
        return jsonify({'message': 'No data Found'}), 400

    required = ['customer_id', 'transaction_no']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    # Here, using 'customer_id' in session to validate; ensure you are setting this key in your session correctly in login.
    if 'customer_id' in session and session['customer_id'] == values['customer_id']:
        emp = Employee()
        amount = emp.get_amount_of_transaction(values['transaction_no'])

        if amount == 'None':
            return jsonify({'message': 'Wrong Transaction number'}), 400

        if amount > 1000:
            response = {
                'message': emp.transfer_transaction_to_tier2(values['transaction_no'])
            }
            return jsonify(response), 200

        from_account = emp.get_fromAccount_of_transaction(int(values['transaction_no']))
        to_account = emp.get_toAccount_of_transaction(int(values['transaction_no']))
        amount = emp.get_amount_of_transaction(int(values['transaction_no']))
        status = emp.get_transaction_status(int(values['transaction_no']))

        if from_account != -1 and to_account != -1 and amount != -1 and status != 0:
            c = Customers()
            response = {
                'message': c.fund_transfers(from_account, to_account, amount, int(values['transaction_no']))
            }
            return jsonify(response), 200
        else:
            return jsonify({'message': 'Invalid transaction details'}), 400
    else:
        logging.warning('Not logged In or Unauthorized Access - ApproveRequest')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############            HANDLE TO APPROVE FUND'S REQUEST By EMPLOYEE (not Tier1)         ###############
@app.route('/approveRequestEmp', methods=['POST', 'GET'])
@cross_origin()
def approve_request_employee():
    values = request.get_json()
    logging.debug('Data @approveRequestEmp' + str(values))

    if not values:
        return jsonify({'message': 'No data Found'}), 400

    required = ['userid', 'transaction_no']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    # Check if the session has the correct user and they are logged in
    if 'userid' in session and session['userid'] == values['userid']:
        emp = Employee()
        amount = emp.get_amount_of_transaction(values['transaction_no'])

        if amount is None:
            return jsonify({'message': 'Wrong Transaction number'}), 404

        tier = emp.get_employee_tier(values['userid'])

        from_account = emp.get_fromAccount_of_transaction(values['transaction_no'])
        to_account = emp.get_toAccount_of_transaction(values['transaction_no'])
        status = emp.get_transaction_status(values['transaction_no'])

        if from_account != -1 and to_account != -1 and amount != -1 and status != 0:
            c = Customers()
            result = c.fund_transfers(from_account, to_account, amount, int(values['transaction_no']))
            return jsonify({'message': result}), 200
        else:
            return jsonify({'message': 'Invalid transaction_no'}), 400
    else:
        logging.warning('Not logged In - ApproveRequestEmp')
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
    logging.debug('Data @getTransactionHistory: ' + str(values))

    if not values:
        return jsonify({'message': 'No data Found'}), 400

    required = ['userid', 'account_no']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    # Ensure the user is logged in and the session matches the request for customer type
    if 'userid' not in session or session.get('userid') != values['userid']:
        logging.warning('Unauthorized access or session mismatch - getTransactionHistory')
        return jsonify({'message': 'Unauthorized access or session mismatch'}), 403

    # Ensure that only customers can access their transaction history
    if session.get('usertype') != 'customer':
        logging.warning('Insufficient permissions to fetch transaction history')
        return jsonify({'message': 'Insufficient permissions'}), 403

    try:
        customer = Customers()
        transactions = customer.get_transaction_history(values['account_no'])
        return jsonify({'transactions': transactions}), 200
    except Exception as e:
        logging.error(f"Error fetching transaction history for customer: {str(e)}")
        return jsonify({'message': 'Failed to retrieve transaction history', 'error': str(e)}), 500


###############              HANDLE FOR GET CHEQUE (BY CUSTOMER/BANK)             ###############
@app.route('/getCashierCheque', methods=['POST', 'GET'])
@cross_origin()
def make_cashier_cheque():
    values = request.get_json()
    logging.debug('Data @getCashierCheque' + str(values))

    if not values:
        return jsonify({'message': 'No data Found'}), 400

    required = ['userid', 'to_account', 'from_account', 'amount']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    if float(values['amount']) < 0:
        return jsonify({'message': 'Enter a valid amount'}), 400

    # Validate if the user in the request is the same as the one logged in and check session expiration
    if 'userid' in session and session['userid'] == values['userid']:
        # Further checks can be added here to validate the user's permission if needed
        c = Customers()
        try:
            response = c.make_cashier_check(values['userid'], values['to_account'],
                                            values['from_account'], values['amount'])
            return jsonify({'message': response}), 200
        except Exception as e:
            logging.error(f"Failed to process cashier check: {str(e)}")
            return jsonify({'message': 'Failed to process request', 'error': str(e)}), 500
    else:
        logging.warning('Not logged In - getCashierCheque')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############            HANDLE FOR DEPOSIT CHEQUE (BY CUSTOMER/BANK)          ###############
@app.route('/depositCheck', methods=['POST', 'GET'])
@cross_origin()
def deposit_cheque():
    values = request.get_json()
    logging.debug('Data @depositCheck' + str(values))
    if not values:
        return jsonify({'message': 'No data Found'}), 400

    required = ['userid', 'cheque_no']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    # Ensure that the session is valid for the requested operation
    if 'userid' in session and session.get('usertype') == 'customer' and session['userid'] == values['userid']:
        c = Customers()
        response = {
            'message': c.deposit_check(values['userid'], values['cheque_no'])
        }
        return jsonify(response), 200
    else:
        logging.warning('Not logged in or session/user mismatch - depositCheck')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############         HANDLE FOR GET  ISSUED CHEQUE'S LIST (BY CUSTOMER)           ###############
@app.route('/getChequeList', methods=['POST', 'GET'])
@cross_origin()
def get_cheque_list():
    values = request.get_json()
    logging.debug('Data @getChequeList' + str(values))
    if not values:
        return jsonify({'message': 'No data Found'}), 400

    required = ['userid']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    # Ensure that the session is valid for the requested operation
    if 'userid' in session and session.get('usertype') == 'customer' and session['userid'] == values['userid']:
        c = Customers()
        response = {'message': c.get_cheque_list(values['userid'])}
        return jsonify(response), 200
    else:
        logging.warning('Not logged in or session/user mismatch - getChequeList')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))


###############                         HANDLE FOR MAKE APPOINTMENT (BY CUSTOMER)                    ###############
@app.route('/makeAppointment', methods=['POST', 'GET'])
@cross_origin()
def make_appointment():
    # Check if user is logged in
    if 'userid' not in session or 'usertype' not in session:
        logging.warning('Unauthorized attempt to access makeAppointment')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    values = request.get_json()
    logging.debug('Data @makeAppointment: ' + str(values))

    if not values:
        logging.error('No data received for making an appointment')
        return jsonify({'message': 'No data found'}), 400

    required = ['customer_id', 'time']
    if not all(key in values for key in required):
        logging.error('Required data missing for making an appointment')
        return jsonify({'message': 'Some data missing'}), 400

    # Validate that the session user is the same as the customer ID provided (if usertype is customer)
    if session.get('usertype') == 'customer' and session['userid'] != values['customer_id']:
        logging.warning(f"Session user ID {session['userid']} does not match request user ID {values['customer_id']}")
        return jsonify({'message': 'User ID mismatch'}), 401

    try:
        c = Customers()
        appointment_response = c.make_appointment(values['customer_id'], values['time'])
        logging.info(f"Appointment made successfully for customer {values['customer_id']}")
        return jsonify({'message': appointment_response}), 200
    except Exception as e:
        logging.error(f"Failed to make appointment for {values['customer_id']}: {str(e)}")
        return jsonify({'message': 'Failed to make appointment', 'error': str(e)}), 500


###############                HANDLE TO GET APPOINTMENT LIST (BY CUSTOMER)                 ###############
@app.route('/getAppointmentList', methods=['POST', 'GET'])
@cross_origin()
def get_appointment_list():
    # Verify that a user is logged in and has the appropriate permissions
    if 'userid' not in session or 'usertype' not in session:
        logging.warning('Unauthorized access attempt to get appointment data')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    values = request.get_json()
    logging.debug('Data @getAppointmentList: ' + str(values))
    if not values:
        logging.error('No data received to fetch appointments')
        return jsonify({'message': 'No data found'}), 400

    required = ['customer_id']
    if not all(key in values for key in required):
        logging.error('Required data missing to fetch appointments')
        return jsonify({'message': 'Some data missing'}), 400

    # Ensure that the session user is the same as the customer ID provided (if usertype is customer)
    if session.get('usertype') == 'customer' and session['userid'] != values['customer_id']:
        logging.warning(f"Session user ID {session['userid']} does not match request user ID {values['customer_id']}")
        return jsonify({'message': 'User ID mismatch'}), 401

    try:
        c = Customers()
        appointment_list = c.get_appointment(values['customer_id'])
        if appointment_list:
            return jsonify({'message': appointment_list}), 200
        else:
            logging.info(f"No appointments found for customer {values['customer_id']}")
            return jsonify({'message': 'No appointments found'}), 404
    except Exception as e:
        logging.error(f"Failed to retrieve appointments for {values['customer_id']}: {str(e)}")
        return jsonify({'message': 'Failed to retrieve appointments', 'error': str(e)}), 500


###############                HANDLE TO REQUEST UPDATE INFO BY CUSTOMER                 ###############
@app.route('/updateInfo', methods=['POST', 'GET'])
@cross_origin()
def update_info():
    # Check for valid session and user type
    if 'userid' not in session or 'usertype' not in session:
        logging.warning('Unauthorized access attempt to update info')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    values = request.get_json()
    logging.debug('Data @updateInfo: ' + str(values))
    if not values:
        return jsonify({'message': 'No data found'}), 400

    required = ['userid', 'email', 'contact_no', 'address', 'requester']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    # Check if the session user ID matches the requested user ID
    if session['userid'] != values['userid']:
        logging.warning(f"Session user ID {session['userid']} does not match request user ID {values['userid']}")
        return jsonify({'message': 'Unauthorized operation: User ID mismatch'}), 401

    try:
        c = Customers()
        result = c.update_info_reqest(values['requester'], values['userid'], values['email'],
                                      values['contact_no'], values['address'])
        return jsonify({'message': result}), 200
    except Exception as e:
        logging.error(f"Failed to update customer info for {values['userid']}: {str(e)}")
        return jsonify({'message': 'Failed to update customer info', 'error': str(e)}), 500


###############                HANDLE TO APPROVE UPDATE INFO (BY BANK)                 ###############
@app.route('/approveUpdateInfo', methods=['POST', 'GET'])
@cross_origin()
def approve_update_info():
    if 'userid' not in session or 'usertype' not in session:
        logging.warning('Attempt to access approveUpdateInfo without proper session')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    values = request.get_json()
    logging.debug('Data @approveUpdateInfo: ' + str(values))
    if not values:
        return jsonify({'message': 'No data found'}), 400

    required = ['userid', 'update_req_no']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    # Check if the session user ID matches the requested user ID for security reasons
    if session['userid'] != values['userid']:
        logging.warning(f"User session mismatch: {session['userid']} vs {values['userid']}")
        return jsonify({'message': 'Unauthorized operation: User ID mismatch'}), 401

    try:
        emp = Employee()
        # Check if the employee has the right to approve updates
        if session.get('usertype') in ['admin', 'employee'] and session.get('emp_tier',
                                                                            1) >= 2:  # Assuming tier 2 and above can approve
            result = emp.approve_update_info(values['update_req_no'])
            return jsonify({'message': result}), 200
        else:
            logging.warning(f"Insufficient permissions for user {session['userid']} to approve update info")
            return jsonify({'message': 'Insufficient permissions'}), 403
    except Exception as e:
        logging.error(f"Error approving update info: {str(e)}")
        return jsonify({'message': 'Failed to approve update info', 'error': str(e)}), 500


###############                HANDLE TO deny UPDATE INFO (BY BANK)                 ###############
@app.route('/denyUpdateInfo', methods=['POST', 'GET'])
@cross_origin()
def deny_update_info():
    values = request.get_json()
    logging.debug('Data @denyUpdateInfo' + str(values))

    if not values:
        return jsonify({'message': 'No data found'}), 400

    required = ['userid', 'update_req_no']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    if 'userid' not in session or 'usertype' not in session:
        logging.warning('Unauthorized access attempt or session expired')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    # Assuming that certain roles such as 'admin' or higher employee tier have the permission to deny updates
    if session.get('usertype') not in ['admin', 'employee'] or session.get('emp_tier', 1) < 2:
        logging.warning(f"Insufficient permissions to deny update info: {session.get('userid')}")
        return jsonify({'message': 'Insufficient permissions'}), 403

    try:
        emp = Employee()
        response = {
            'message': emp.deny_update_info(values['userid'], values['update_req_no'])
        }
        return jsonify(response), 200
    except Exception as e:
        logging.error(f"Error processing deny update request for {values['userid']}: {str(e)}")
        return jsonify({'message': 'Error processing request', 'error': str(e)}), 500


###############                 HANDLE TO GET WHOLE CUSTOMER INFO (FOR BANK)               ###############
@app.route('/getCustomer', methods=['POST', 'GET'])
@cross_origin()
def get_customer():
    if 'userid' not in session or 'usertype' not in session:
        logging.warning('Unauthorized access attempt to get customer data')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    values = request.get_json()
    logging.debug('Data @getCustomer' + str(values))
    if not values:
        return jsonify({'message': 'No data found'}), 400

    required = ['userid', 'customer_id']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    # Allow Tier 1 and Tier 2 employees access, not admins
    if session['usertype'] in ['tier1', 'tier2', 'employee']:
        try:
            c = Customers()
            response = {
                'Accounts': c.get_all_account(values['customer_id']),
                'Info': c.get_customer_details(values['customer_id'])
            }
            return jsonify(response), 200
        except Exception as e:
            logging.error(f"Failed to retrieve customer data for {values['customer_id']}: {str(e)}")
            return jsonify({'message': 'Failed to retrieve data', 'error': str(e)}), 500
    else:
        logging.warning(
            f'User {session["userid"]} with usertype {session["usertype"]} attempted to access customer data without proper authorization.')
        return jsonify({'message': 'Unauthorized to access customer data'}), 403


###############                 HANDLE TO GET WHOLE EMPLOYEE INFO (FOR BANK)               ###############
@app.route('/getEmployee', methods=['POST', 'GET'])
@cross_origin()
def get_employee():
    values = request.get_json()
    logging.debug('Data @getEmployee: ' + str(values))

    if not values:
        return jsonify({'message': 'No data found'}), 400

    required = ['userid', 'emp_id']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    # Check session for necessary data
    if 'userid' not in session or session.get('userid') != values['userid']:
        logging.warning('Unauthorized access attempt or session mismatch')
        return jsonify({'error': 'Unauthorized or invalid session'}), 401

    # Additional security: Verify user role
    if session.get('usertype') not in ['admin', 'employee']:
        logging.warning(f"Insufficient permissions for user {session.get('userid')}")
        return jsonify({'message': 'Insufficient permissions'}), 403

    emp = Employee()
    try:
        employee_info = emp.get_employee_details(values['emp_id'])
        if employee_info:
            return jsonify({'Info': employee_info}), 200
        else:
            logging.info(f"Employee with ID {values['emp_id']} not found")
            return jsonify({'message': 'Employee not found'}), 404
    except Exception as e:
        logging.error(f"Error retrieving employee details for {values['emp_id']}: {str(e)}")
        return jsonify({'message': 'Internal server error', 'error': str(e)}), 500


###############                 HANDLE TO MODIFY CUSTOMER ACCOUNT (FOR BANK-ADMIN)               ###############
@app.route('/modifyCustomer', methods=['POST', 'GET'])
@cross_origin()
def modify_customer():
    values = request.get_json()
    logging.debug('Data @modifyCustomer: ' + str(values))

    # Ensure user is logged in and check for necessary session variables
    if 'userid' not in session or 'usertype' not in session:
        logging.warning('Unauthorized attempt to access modifyCustomer due to missing session data')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    # Check if the session user's type is appropriate to modify customer details
    if session['usertype'] not in ['admin', 'employee', 'tier1', 'tier2']:
        logging.warning(
            f'User {session["userid"]} with usertype {session["usertype"]} unauthorized to modify customer data')
        return jsonify({'message': 'Unauthorized to modify customer data'}), 403

    # Ensure all required data fields are present in the request
    required = ['customer_id', 'last_name', 'middle_name', 'first_name', 'contact_no',
                'email_id', 'ssn', 'dob', 'address']
    if not all(key in values for key in required):
        logging.error('Missing necessary data to modify customer information')
        return jsonify({'message': 'Some data missing'}), 400

    # Ensure the user making the request is either modifying their own data or is an admin
    if session['userid'] != values['userid'] and session['usertype'] != 'admin':
        logging.warning(f'Mismatch in session user ID {session["userid"]} and request user ID {values["userid"]}')
        return jsonify({'message': 'User ID mismatch or insufficient privileges'}), 403

    # Perform the update operation
    c = Customers()
    try:
        success_message = c.update_account_info(values['customer_id'], values['last_name'],
                                                values['middle_name'], values['first_name'], values['contact_no'],
                                                values['email_id'], values['ssn'], values['dob'], values['address'])
        logging.info(f'Successfully updated customer {values["customer_id"]} data by user {session["userid"]}')
        return jsonify({'message': success_message}), 200
    except Exception as e:
        logging.error(f"Failed to update customer data for {values['customer_id']}: {str(e)}")
        return jsonify({'message': 'Failed to update customer info', 'error': str(e)}), 500


###############                 HANDLE TO MODIFY EMPLOYEE ACCOUNT (FOR BANK-ADMIN)               ###############
@app.route('/modifyEmployee', methods=['POST', 'GET'])
@cross_origin()
def modify_employee():
    # First, verify session presence and user permissions
    if 'userid' not in session or 'usertype' not in session or session.get('usertype') not in ['admin', 'employee']:
        logging.warning('Unauthorized access attempt to modify employee details')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    values = request.get_json()
    logging.debug('Data @modifyEmployee' + str(values))
    if not values:
        return jsonify({'message': 'No data found'}), 400

    required = ['userid', 'emp_id', 'last_name', 'middle_name', 'first_name', 'contact_no', 'email_id',
                'ssn', 'dob', 'address', 'tier']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    # Verify that the user making the request is the same as the logged-in user or an admin
    if session['userid'] != values['userid'] and session.get('emp_tier', 1) < 3:
        logging.warning(f"User {session['userid']} attempted to modify another employee's data without proper "
                        f"permissions")
        return jsonify({'message': 'Unauthorized modification attempt'}), 403

    emp = Employee()
    try:
        # Assuming the function is named correctly in your Employee class
        result = emp.update_account_info(values['emp_id'], values['last_name'], values['middle_name'],
                                         values['first_name'], values['contact_no'], values['email_id'],
                                         values['ssn'], values['dob'], values['address'], values['tier'])
        return jsonify({'message': result}), 200
    except Exception as e:
        logging.error(f"Error updating employee data for {values['emp_id']}: {str(e)}")
        return jsonify({'message': 'Failed to update employee info', 'error': str(e)}), 500


###############                 HANDLE TO DEACTIVATE ACCOUNT (FOR BANK)               ###############
@app.route('/deactivateAccount', methods=['POST', 'GET'])
@cross_origin()
def deactivate_account():
    # Check if the user is logged in
    if 'userid' not in session or 'usertype' not in session:
        logging.warning('Attempt to access deactivateAccount without login')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    values = request.get_json()
    logging.debug(f'Data @deactivateAccount: {values}')

    if not values:
        return jsonify({'message': 'No data found'}), 400

    required_fields = ['userid', 'account_no']
    if not all(field in values for field in required_fields):
        return jsonify({'message': 'Some data missing'}), 400

    # Ensure that the session user ID matches the requested user ID for security reasons
    if session['userid'] != values['userid']:
        logging.warning(f"User session mismatch or unauthorized access attempt by {session['userid']}")
        return jsonify({'message': 'Unauthorized operation: User ID mismatch'}), 401

    # Restrict the operation to tier2 employees
    if session['usertype'] != 'tier2':
        logging.warning(f"User {session['userid']} with usertype {session['usertype']} attempted unauthorized access")
        return jsonify({'message': 'Insufficient permissions: Only tier2 employees may deactivate accounts'}), 403

    try:
        emp = Employee()
        result = emp.deactivate_account(values['account_no'])
        logging.info(f"Account {values['account_no']} deactivated by tier2 employee {session['userid']}")
        return jsonify({'message': result}), 200
    except Exception as e:
        logging.error(f"Error deactivating account: {str(e)}")
        return jsonify({'message': 'Failed to deactivate account', 'error': str(e)}), 500


###############                 HANDLE TO DEACTIVATE CUSTOMER (FOR BANK)               ###############
@app.route('/deactivateCustomer', methods=['POST', 'GET'])
@cross_origin()
def deactivate_customer():
    if 'userid' not in session or 'usertype' not in session or session['usertype'] != 'tier2':
        logging.warning('Unauthorized attempt to deactivate customer')
        return jsonify({'message': 'Unauthorized access or session expired'}), 401

    values = request.get_json()
    logging.debug('Data @deactivateCustomer' + str(values))

    if not values:
        return jsonify({'message': 'No data Found'}), 400

    required = ['userid', 'customer_id']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    # Assuming the `deactivate_customer` function takes `customer_id` as an argument
    try:
        emp = Employee()
        response = emp.deactivate_customer(values['customer_id'])
        return jsonify({'message': response}), 200
    except Exception as e:
        logging.error(f"Failed to deactivate customer {values['customer_id']}: {str(e)}")
        return jsonify({'message': 'Failed to deactivate customer', 'error': str(e)}), 500


###############                 HANDLE TO UPDATE EMPLOYEE (FOR BANK)               ###############
@app.route('/updateEmployee', methods=['POST'])
@cross_origin()
def update_employee():
    if 'userid' not in session or 'usertype' not in session:
        logging.warning("Attempt to update employee without being logged in.")
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    values = request.get_json()
    logging.debug('Data @updateEmployee: ' + str(values))
    if not values:
        return jsonify({'message': 'No data provided'}), 400

    required_fields = ['userid', 'emp_id', 'email', 'firstname', 'midname', 'lastname', 'phone', 'dob', 'ssn',
                       'address']
    if any(field not in values for field in required_fields):
        missing = [field for field in required_fields if field not in values]
        logging.info(f"Missing fields in update request: {missing}")
        return jsonify({'message': 'Some data missing', 'missing_fields': missing}), 400

    # Authorization check: Only admins or HR can update employee details, or employees updating their own data
    if session['usertype'] not in ['admin', 'hr'] and session['userid'] != values['userid']:
        logging.error(f"Unauthorized update attempt by user {session['userid']}")
        return jsonify({'message': 'Unauthorized to update this employee'}), 403

    emp = Employee()
    try:
        result = emp.update_employee(
            emp_id=values['emp_id'],
            email=values['email'],
            firstname=values['firstname'],
            midname=values['midname'],
            lastname=values['lastname'],
            phone=values['phone'],
            dob=values['dob'],
            ssn=values['ssn'],
            address=values['address']
        )
        logging.info(f"Employee {values['emp_id']} updated successfully by user {session['userid']}")
        return jsonify({'message': 'Employee updated successfully', 'result': result}), 200
    except Exception as e:
        logging.error(f"Error updating employee data: {e}")
        return jsonify({'message': 'Failed to update employee', 'error': str(e)}), 500


###############                 HANDLE TO DEACTIVATE EMPLOYEE (FOR BANK)               ###############
@app.route('/deactivateEmployee', methods=['POST'])
@cross_origin()
def deactivate_employee():
    try:
        # Get JSON data and log it
        data = request.get_json()
        logging.debug(f"Data for deactivating employee: {data}")

        # Validate data presence and required keys
        if not data:
            return jsonify({'message': 'No data found'}), 400

        required_keys = {'userid', 'emp_id'}
        missing_keys = required_keys - set(data.keys())
        if missing_keys:
            return jsonify({'message': f"Missing required keys: {', '.join(missing_keys)}"}), 400

        # Check user login and authorization
        if 'userid' not in session:
            logging.warning('Unauthorized access attempt to deactivate employee')
            return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

        if session.get('usertype') != 'admin':
            logging.warning('Unauthorized attempt by non-admin to deactivate employee')
            return jsonify({'message': 'Unauthorized: Only admins can deactivate employees'}), 403

        if 'emp_id' not in data:
            logging.error("Missing 'emp_id' in the request data")
            return jsonify({'message': "Missing 'emp_id' in the request"}), 400

        # Deactivate employee and handle success/failure
        emp = Employee()
        response = emp.deactivate_employee(data['emp_id'])
        return jsonify({'message': response}), 200

    except Exception as e:
        logging.error(f"Error deactivating employee: {str(e)}")
        return jsonify({'message': 'Failed to deactivate employee', 'error': str(e)}), 500


###############                HANDLE TO REQUEST OTP                 ###############

twilio_client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
verify_sid = os.getenv('TWILIO_VERIFY_SID')


@app.route('/sendOTP', methods=['POST', 'GET'])
@cross_origin()
def send_otp():
    values = request.get_json()
    logging.debug('Data @sendOTP: ' + str(values))

    if not values:
        return jsonify({'message': 'No data Found'}), 400

    required = ['userid']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    user = Customers() if values.get('requester', '') == 'Customer' else Employee()
    phone_number = user.retrieve_phone_number(values['userid'])

    if phone_number:
        verification = twilio_client.verify.v2.services(verify_sid).verifications.create(to=phone_number, channel='sms')
        return jsonify({'message': 'OTP sent successfully', 'sid': verification.sid}), 200
    else:
        return jsonify({'message': 'Invalid User ID or Contact Number'}), 404


@app.route('/resetPassword', methods=['POST', 'GET'])
@cross_origin()
def reset_password():
    values = request.get_json()
    logging.debug('Data @resetPassword' + str(values))

    if not values:
        return jsonify({'message': 'No data Found'}), 400

    required = ['userid', 'newPassword', 'otp']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    user = Customers() if values.get('requester', '') == 'Customer' else Employee()
    phone_number = user.retrieve_phone_number(values['userid'])

    if phone_number:
        verification_check = twilio_client.verify.v2.services(verify_sid).verification_checks.create(to=phone_number,
                                                                                                     code=values[
                                                                                                         'otp'])
        if verification_check.status == "approved":
            response = user.reset_password(values['userid'], values['newPassword'])
            return jsonify({'message': response}), 200
        else:
            return jsonify({'message': 'OTP verification failed, cannot reset password'}), 401
    else:
        return jsonify({'message': 'User ID not found'}), 404


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

from flask import current_app, jsonify, send_from_directory, session, request, redirect, url_for
import logging
import os


@app.route("/getSystemLogs", methods=['POST', 'GET'])
def get_system_logs():
    # Check if the user is logged in and has admin privileges
    if 'userid' not in session or 'usertype' not in session or session.get('usertype') != 'admin':
        logging.warning('Unauthorized attempt to access system logs')
        return redirect(url_for('get_login_page_ui', _external=True, _scheme='http'))

    # Retrieve and validate the incoming data
    values = request.get_json()
    logging.debug('Data @getSystemLogs: ' + str(values))
    if not values:
        return jsonify({'message': 'No data Found'}), 400

    required = ['userid']
    if not all(key in values for key in required):
        return jsonify({'message': 'Some data missing'}), 400

    # Validate that the user making the request matches the logged-in user for additional security
    if values['userid'] != session['userid']:
        logging.warning("User session mismatch or unauthorized access attempt by user ID: {}".format(session['userid']))
        return jsonify({'message': 'Unauthorized operation: User ID mismatch'}), 401

    # Construct the path to the log file
    logs_directory = 'SystemLogs'
    log_file_name = 'bank.log'
    log_file_path = os.path.join(logs_directory, log_file_name)
    logging.debug(f"Attempting to send file from {log_file_path}")

    # Ensure the log file exists
    if not os.path.exists(log_file_path):
        logging.error('System log file not found')
        return jsonify({'message': 'System log file not found'}), 404

    print(os.path.exists(log_file_path))
    # Attempt to send the log file
    try:
        return send_from_directory(directory=logs_directory, filename=log_file_name, as_attachment=True)
    except Exception as e:
        logging.error(f'An error occurred when trying to send the log file: {str(e)}')
        return jsonify({'message': 'Failed to retrieve system logs', 'error': str(e)}), 500


app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

if __name__ == '__main__':
    from argparse import ArgumentParser

    logging.debug('Banking Server has Started')
    app.run(ssl_context="adhoc", debug=True, host='0.0.0.0')
