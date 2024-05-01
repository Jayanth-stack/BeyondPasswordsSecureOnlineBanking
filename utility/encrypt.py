import hashlib
import bcrypt


def encrypt_ssn(ssn):
    salt = "784gz"
    db_ssn = ssn + salt
    h = hashlib.sha256(db_ssn.encode())
    return h.hexdigest()


def encrypt(password):
    password = password.encode('utf-8')  # Convert the password to bytes
    hashed = bcrypt.hashpw(password, bcrypt.gensalt())
    return hashed.decode('utf-8')  # Return the hashed password as a string for storing in the database


def verify_password(stored_password, provided_password):
    stored_password = stored_password.encode('utf-8')
    provided_password = provided_password.encode('utf-8')
    return bcrypt.checkpw(provided_password, stored_password)


def check_encrypted_password(password, hashed):
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
