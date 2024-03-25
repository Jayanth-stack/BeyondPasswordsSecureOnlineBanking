import hashlib

def encrypt(password):
  salt = "5gz"
  db_password = password + salt
  h = hashlib.sha256(db_password.encode())
  return h.hexdigest()

def encryp_SSN(ssn):
  salt = "6gz"
  db_SSN = ssn + salt
  h = hashlib.sha256(db_SSN.encode())
  return h.hexdigest()