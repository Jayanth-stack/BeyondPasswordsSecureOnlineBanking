# Beyond Passwords Secure online banking application  

We, a group of 4 individuals have come together to try to tackle an issue of security in the banking application by trying to implement a multifactor authentication 
inorder to improve security, It is a capstone Project aimed at solving pressing issues related to logger mechanisms as well. Currently in progress.
Our aim is to achieve few goals we set for ourselves inorder make a safer banking application.

# Clone the Repository

1. git clone https://github.com/Jayanth-stack/BeyondPasswordsSecureOnlineBanking.git
2. cd yourprojectname

# Set Up a Virtual Environment (optional but recommended)
Instructions for setting up a virtual environment to isolate package dependencies locally
1. python -m venv venv
# On Windows
venv\Scripts\activate
# On Unix or MacOS
source venv/bin/activate
Install Dependencies
Command to install all dependencies listed in requirements.txt

pip install -r requirements.txt

Environment Variables Explain how to set up necessary environment variables, including database connection settings. Provide a sample .env file or explain the variables needed.
makefile

# Example .env content
FLASK_APP=run.py
MYSQL_USER='yourusername'
MYSQL_PASSWORD='yourpassword'
MYSQL_HOST='localhost'
MYSQL_DB='yourdbname'
MySQL Database Setup
Instructions for creating the MySQL database and user, and running any migrations or seed data scripts.
sql
Copy code
# Log into MySQL
mysql -u root -p
# Create the database
CREATE DATABASE bankingapplication;
# Create a user and grant privileges
CREATE USER 'yourusername'@'localhost' IDENTIFIED BY 'yourpassword';
GRANT ALL PRIVILEGES ON yourdbname.* TO 'yourusername'@'localhost';
FLUSH PRIVILEGES;
EXIT;

# Database Implementation
After installing all the required packages from the requirement.txt file 
click on create_databse.py file to run the database script

After running the script with your required database username and password,
you can see your database and tables created under the name of banking application.

Ensure to give the same credentials given in create_database.py file to all the class files which are
employee.py and customer.py

# Run migrations if you are using a migration tool like Flask-Migrate
flask db upgrade

# Run the Application
How to start the server locally
1. flask run
2. gunicorn -w 4 -b 127.0.0.1:5000 run:app

# Creating Twilio Client details for OTP verification
1. Create a free Twilio account now.
2. Once the account is created, navigate to the Twilio Console
3. Click Explore Products on the left sidebar.
4. Find and click on Verify in the Account Security section (You can pin for easy access).
5. Create a new service by clicking on the blue "Create new" button.
6. Provide a friendly name for the service.
7. Toggle the SMS channel on.
8. Click the "Create" button

