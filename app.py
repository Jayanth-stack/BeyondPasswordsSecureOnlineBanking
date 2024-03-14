import logging

from flask import Flask, session, jsonify, request, send_from_directory, redirect, url_for
from flask_cors import CORS, cross_origin
import json
from customer import customer, Customers
from markupsafe import escape
from employee import Employee
import json

from werkzeug.utils import secure_filename
logging.basicConfig(level=logging.INFO, filename='SystemLogs/bank.log', filemode='w', format='%(asctime)-15s - %(name)s - %(levelname)s - %(message)s', datefmt='%d-%m-%Y %H:%M:%S')
otpSet = {}

app = Flask(__name__)


@app.route('/', methods =['GET'])
def get_login_page_ui():  # put application's code here
    return send_from_directory('templates', 'login.html')

@app.route('/customer_dash', methods =['GET'])
def get_customer_dash_ui():
    return send_from_directory('templates', 'customer.html')

@app.route('/admin', methods =['GET'])
def get_admin_dashboard_ui():
    return send_from_directory('templates', 'admin.html')

@app.route('/tier1', methods =['GET'])
def get_tier1_dashboard_ui():
    return send_from_directory('templates', 'tier1.html')
@app.route('/tier2', methods =['GET'])
def get_tier2_dashboard_ui():
    return send_from_directory('templates', 'tier2.html')


###############                   HANDLE FOR NEW REGISTER CUSTOMER             ###############
@app.route('/registerCustomer', methods=['POST'])
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
        return redirect(url_for('get_customer_dashboard_ui', _external=True, _scheme='https'))

    response = {
        'message': 'Something Went wrong, Please try again later'
    }
    return jsonify(response), 400

if __name__ == '__main__':
    from argparse import ArgumentParser

    logging.debug('Banking Server has Started')
    app.run()
