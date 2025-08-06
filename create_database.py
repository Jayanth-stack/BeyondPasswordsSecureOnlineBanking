import mysql.connector
from customer import Customers
from employee import Employee
from utility.encrypt import encrypt
from datetime import datetime




def getdate():
    now = datetime.now()
    return now.strftime("%d/%m/%Y %H:%M:%S")


db = mysql.connector.connect(
    host="localhost",
    user="root",
    password="root",
    port="3306",
    database="bankingapplication"
)

cursor = db.cursor()

if cursor:
    print("Connected to Banking DataBase")

cursor.execute("SHOW TABLES")
print("Tables in the Banking System Database:")
for table in cursor.fetchall():
    print(table)

#                           Drop Cheque Table if it exists                        #
cursor.execute("""
    SHOW TABLES LIKE 'Cheque'
""")
presence = cursor.fetchone()
if presence is not None:
    print('Cheque Table already exists, so dropping')
    cursor.execute("""
        DROP TABLE Cheque
    """)

#                           commit and rollback if exception occurs                   #
try:
    db.commit()
except Exception as e:
    db.rollback()
    print(e)

#                           Drop Appointment Table if it exists                        #
cursor.execute("""
    SHOW TABLES LIKE 'Appointments'
""")
presence = cursor.fetchone()
if presence is not None:
    print('Appointments Table already exists , so dropping')
    cursor.execute("""
        DROP TABLE Appointments
    """)

#                           Drop UpdateInfo Table if it exists                        #
cursor.execute("""
    SHOW TABLES LIKE 'Updateinfo'
""")
presence = cursor.fetchone()
if presence is not None:
    print('UpdateInfo Table already exists , so dropping')
    cursor.execute("""
        DROP TABLE Updateinfo
    """)

#                           Drop Transactions Table if it exists                      #
cursor.execute("""
    SHOW TABLES LIKE 'Transactions'
""")
presence = cursor.fetchone()
if presence is not None:
    print('Transactions Table already exists , so dropping')
    cursor.execute("""
        DROP TABLE Transactions
    """)

#                           Drop Employess Table if it exists
cursor.execute("""
    SHOW TABLES LIKE 'Employees'
""")
presence = cursor.fetchone()
if presence is not None:
    print('Employee Table already exists , so dropping')
    cursor.execute("""
        DROP TABLE Employees
    """)

#                           Drop Accounts Table if it exists                        #
cursor.execute("SHOW TABLES LIKE 'Accounts'")
presence = cursor.fetchone()
if presence is not None:
    print('Accounts Table already exists , so dropping')
    cursor.execute("""
        DROP TABLE Accounts
    """)

#                           Drop Customers Table if it exists                        #
cursor.execute("""
    SHOW TABLES LIKE 'Customers'
""")
presence = cursor.fetchone()
if presence is not None:
    print('Customers Table already exists , so dropping')
    cursor.execute("""
        DROP TABLE Customers
    """)

#                           commit and rollback if exception occurs                   #
try:
    db.commit()
except Exception as e:
    db.rollback()
    print(e)

#                               creating table : Employee                           #
cursor.execute("""
    CREATE TABLE Employees(
        emp_id VARCHAR(32) NOT NULL,
        last_name VARCHAR(255),
        middle_name VARCHAR(255),
        first_name VARCHAR(255) NOT NULL,
        dob VARCHAR(255) NOT NULL,
        contact_no VARCHAR(20) NOT NULL,
        email_id VARCHAR(255) NOT NULL,
        address VARCHAR(255) NOT NULL,
        password VARCHAR(255) NOT NULL,
        ssn VARCHAR(255) NOT NULL,
        active BOOLEAN NOT NULL,
        tier INT NOT NULL,
        PRIMARY KEY(emp_id),
        UNIQUE(contact_no),
        UNIQUE(email_id),
        CHECK(tier>=1 and tier<=3)
    )
""")
#                           commit and rollback if exception occurs                   #
try:
    db.commit()
    print('Employees Table created')
except Exception as e:
    db.rollback()
    print(e)

#                               creating table : Customers                           #
cursor.execute("""
    CREATE TABLE Customers(
        customer_id VARCHAR(32) NOT NULL,
        last_name VARCHAR(255),
        middle_name VARCHAR(255),
        first_name VARCHAR(255) NOT NULL,
        dob VARCHAR(255) NOT NULL,
        contact_no VARCHAR(20) NOT NULL,
        email_id VARCHAR(255) NOT NULL,
        address VARCHAR(255) NOT NULL,
        password VARCHAR(255) NOT NULL,
        ssn VARCHAR(255) NOT NULL,
        active BOOLEAN NOT NULL,
        login_history TEXT NOT NULL,
        PRIMARY KEY(customer_id),
        UNIQUE(contact_no),
        UNIQUE(email_id),
        UNIQUE(customer_id)
    )
""")
#                           commit and rollback if exception occurs                   #
try:
    db.commit()
    print('Customers Table created')
except Exception as e:
    db.rollback()
    print(e)

#                               creating table : Accounts                           #
cursor.execute("""
    CREATE TABLE Accounts(
        account_no INT NOT NULL AUTO_INCREMENT,
        account_type ENUM('checkin', 'savings', 'credit') NOT NULL,
        customer_id VARCHAR(32) NOT NULL,
        balance FLOAT NOT NULL default 0,
        active BOOLEAN NOT NULL default False,
        transaction_history TEXT,
        PRIMARY KEY(account_no),
        FOREIGN KEY (customer_id) REFERENCES Customers(customer_id)
    )
""")
#                           commit and rollback if exception occurs                   #
try:
    db.commit()
    print('Accounts Table created')
except Exception as e:
    db.rollback()
    print(e)

#                               creating table : Transactions                           #
cursor.execute("""
    CREATE TABLE Transactions(
        transaction_no INT NOT NULL AUTO_INCREMENT,
        from_account INT NOT NULL,
        to_account INT NOT NULL,
        approver1_id VARCHAR(32) ,
        approver2 int,
        amount FLOAT NOT NULL,
        deposit INT NOT NULL DEFAULT 0,
        status BOOLEAN NOT NULL,
        remark varchar(100) DEFAULT '',
        PRIMARY KEY(transaction_no)
    )
""")
#                           commit and rollback if exception occurs                   #
try:
    db.commit()
    print('Transactions Table created')
except Exception as e:
    db.rollback()
    print(e)

#                               creating table : UpdateInfo                           #
cursor.execute("""
    CREATE TABLE Updateinfo(
        update_req_no INT AUTO_INCREMENT,
        requester VARCHAR(32) NOT NULL,
        userid VARCHAR(32) NOT NULL,
        # last_name VARCHAR(255),
        # middle_name VARCHAR(255),
        # first_name VARCHAR(255) NOT NULL,
        # dob VARCHAR(255) NOT NULL,
        contact_no VARCHAR(20) NOT NULL,
        email_id VARCHAR(255) NOT NULL,
        address VARCHAR(255),
        # password VARCHAR(255) NOT NULL,
        status BOOLEAN NOT NULL,
        approver INT NOT NULL,
        remark text,
        PRIMARY KEY(update_req_no)
        # FOREIGN KEY (customer_id) REFERENCES Customers(customer_id)
    )
""")
#                           commit and rollback if exception occurs                   #
try:
    db.commit()
    print('UpdateInfo Table created')
except Exception as e:
    db.rollback()
    print(e)

#                               creating table : Appointments                           #
cursor.execute("""
    CREATE TABLE Appointments(
        appointment_no INT NOT NULL AUTO_INCREMENT,
        customer_id VARCHAR(32) NOT NULL,
        time VARCHAR(32) NOT NULL,
        status BOOLEAN NOT NULL,
        PRIMARY KEY(appointment_no),
        FOREIGN KEY (customer_id) REFERENCES Customers(customer_id)
    )
""")
#                           commit and rollback if exception occurs                   #
try:
    db.commit()
    print('Appointments Table created')
except Exception as e:
    db.rollback()
    print(e)

#                               creating table : CHEQUE                           #
cursor.execute("""
    CREATE TABLE Cheque(
        cheque_no INT NOT NULL AUTO_INCREMENT,
        issuer_id VARCHAR(32), 
        to_account INT NOT NULL,
        from_account INT NOT NULL,
        amount FLOAT NOT NULL,
        active BOOLEAN NOT NULL,
        PRIMARY KEY(cheque_no)
    )
""")
#                           commit and rollback if exception occurs                   #
try:
    db.commit()
    print('Cheque Table created')
except Exception as e:
    db.rollback()
    print(e)
