class FakeCursor:
    def __init__(self, conn, dictionary=False):
        self.conn = conn
        self.dictionary = dictionary
        self.lastrowid = 0
        self.rowcount = 0
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if self.conn.handler:
            result = self.conn.handler(sql, params)
        elif self.conn.queue:
            result = self.conn.queue.pop(0)
        else:
            result = self.conn.default_rows
        if isinstance(result, dict):
            self._rows = result.get('rows', [])
            self.lastrowid = result.get('lastrowid', self.conn.lastrowid)
            self.rowcount = result.get('rowcount', len(self._rows))
        else:
            self._rows = list(result or [])
            self.lastrowid = self.conn.lastrowid
            self.rowcount = len(self._rows) if self._rows else self.conn.rowcount
        self.conn.lastrowid += 1

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class FakeConn:
    def __init__(self, handler=None, default_rows=None):
        self.executed = []
        self.handler = handler
        self.queue = []
        self.default_rows = default_rows or []
        self.lastrowid = 1
        self.rowcount = 1
        self.commits = 0
        self.rollbacks = 0
        self.started = 0

    def cursor(self, dictionary=False):
        return FakeCursor(self, dictionary=dictionary)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def start_transaction(self):
        self.started += 1

    def close(self):
        pass
