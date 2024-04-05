import hashlib

def encrypt(password):
  salt = "5gz"
  db_password = password + salt
  h = hashlib.sha256(db_password.encode())
  return h.hexdigest()

def encrypt_ssn(ssn):
  salt = "784gz"
  db_ssn = ssn + salt
  h = hashlib.sha256(db_ssn.encode())
  return h.hexdigest()
