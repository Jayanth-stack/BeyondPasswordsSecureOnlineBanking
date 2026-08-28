"""Import helpers that avoid requiring a live MySQL server at import time."""
import sys
from unittest.mock import MagicMock

if 'mysql' not in sys.modules:
    mysql_mod = MagicMock()
    sys.modules['mysql'] = mysql_mod
    sys.modules['mysql.connector'] = mysql_mod.connector

if 'pymysql' not in sys.modules:
    sys.modules['pymysql'] = MagicMock()
