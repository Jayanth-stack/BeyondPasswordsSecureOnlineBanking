import os

from dotenv import load_dotenv
import mysql.connector

load_dotenv()


def db_config():
    """Shared MySQL connection settings from the environment."""
    return {
        'host': os.getenv('DB_HOST', 'localhost'),
        'user': os.getenv('DB_USER', 'root'),
        'password': os.getenv('DB_PASSWORD', 'root'),
        'port': os.getenv('DB_PORT', '3306'),
        'database': os.getenv('DB_NAME', 'bankingapplication'),
    }


def get_connection():
    """Single connection factory used by customer, employee, and schema scripts."""
    return mysql.connector.connect(**db_config())
