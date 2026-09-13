import unittest

from flask import Flask, session

from utility.statement import (
    MemoryStatementStore,
    StatementPolicy,
    StatementService,
    attach_statement_routes,
    set_service,
)


class Clock:
    def __init__(self, ts):
        self.ts = ts

    def __call__(self):
        return self.ts


MAR = 1772668800.0  # 2026-03-05-ish; tests set an explicit clock


class StatementRouteTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(1772755200.0)  # 2026-03-06 UTC
        self.store = MemoryStatementStore()
        self.service = StatementService(StatementPolicy(), self.store, clock=self.clock)
        self.service.observe(
            '1001', '250.00', 'open', userid='alice',
            posted_at=1768435200.0, source_id='open:alice:checkin',
        )
        set_service(self.service)
        app = Flask(__name__)
        app.secret_key = 'test'
        app.config['TESTING'] = True

        def own_accounts(userid):
            return ['1001'] if userid == 'alice' else []

        attach_statement_routes(app, self.service, own_accounts_loader=own_accounts)

        @app.route('/login', methods=['POST'])
        def login():
            values = session
            payload = __import__('flask').request.get_json(silent=True) or {}
            values['userid'] = payload.get('userid')
            values['usertype'] = payload.get('usertype')
            return {'ok': True}

        self.app = app
        self.client = app.test_client()

    def tearDown(self):
        set_service(None)

    def login(self, userid='alice', usertype='customer'):
        return self.client.post('/login', json={'userid': userid, 'usertype': usertype})

    def test_generate_requires_session(self):
        response = self.client.post('/generateStatement', json={'account': '1001'})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch(self):
        self.login('alice', 'customer')
        response = self.client.post('/generateStatement', json={'userid': 'bob', 'account': '1001'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_use_foreign_account(self):
        self.login('alice', 'customer')
        response = self.client.post('/generateStatement', json={'userid': 'alice', 'account': '2002', 'period': '2026-02'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'statement_forbidden')

    def test_generate_and_list_happy_path(self):
        self.login('alice', 'customer')
        response = self.client.post('/generateStatement', json={
            'userid': 'alice', 'account': '1001', 'kind': 'monthly', 'period': '2026-01',
        })
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertEqual(body['statement']['period'], '2026-01')
        self.assertEqual(body['statement']['entry_count'], 1)
        listed = self.client.post('/listStatements', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.get_json()['Statements']['statements']), 1)

    def test_generate_twice_returns_existing(self):
        self.login('alice', 'customer')
        payload = {'userid': 'alice', 'account': '1001', 'kind': 'monthly', 'period': '2026-01'}
        first = self.client.post('/generateStatement', json=payload)
        second = self.client.post('/generateStatement', json=payload)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()['already_generated'])
        self.assertEqual(first.get_json()['statement']['statement_id'], second.get_json()['statement']['statement_id'])

    def test_get_statement_includes_lines(self):
        self.login('alice', 'customer')
        created = self.client.post('/generateStatement', json={
            'userid': 'alice', 'account': '1001', 'kind': 'monthly', 'period': '2026-01',
        }).get_json()
        fetched = self.client.post('/getStatement', json={
            'userid': 'alice', 'statement_id': created['statement']['statement_id'],
        })
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(len(fetched.get_json()['statement']['entries']), 1)

    def test_get_missing_statement(self):
        self.login('alice', 'customer')
        response = self.client.post('/getStatement', json={'userid': 'alice', 'statement_id': 'missing'})
        self.assertEqual(response.status_code, 404)

    def test_future_period_is_400(self):
        self.login('alice', 'customer')
        response = self.client.post('/generateStatement', json={
            'userid': 'alice', 'account': '1001', 'kind': 'monthly', 'period': '2026-12',
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'period_in_future')

    def test_official_request_and_staff_fulfill(self):
        self.login('alice', 'customer')
        generated = self.client.post('/generateStatement', json={
            'userid': 'alice', 'account': '1001', 'kind': 'monthly', 'period': '2026-01',
        }).get_json()
        requested = self.client.post('/requestStatement', json={
            'userid': 'alice', 'account': '1001',
            'statement_id': generated['statement']['statement_id'],
            'delivery': 'mail',
        })
        self.assertEqual(requested.status_code, 201)
        request_id = requested.get_json()['request']['request_id']
        self.login('t1', 'tier1')
        decided = self.client.post('/decideStatementRequest', json={
            'userid': 't1', 'request_id': request_id, 'decision': 'approve',
        })
        self.assertEqual(decided.status_code, 200)
        self.assertEqual(decided.get_json()['request']['status'], 'approved')

    def test_customer_cannot_decide_request(self):
        self.login('alice', 'customer')
        generated = self.client.post('/generateStatement', json={
            'userid': 'alice', 'account': '1001', 'kind': 'monthly', 'period': '2026-01',
        }).get_json()
        requested = self.client.post('/requestStatement', json={
            'userid': 'alice', 'account': '1001',
            'statement_id': generated['statement']['statement_id'],
        }).get_json()
        denied = self.client.post('/decideStatementRequest', json={
            'userid': 'alice', 'request_id': requested['request']['request_id'], 'decision': 'approve',
        })
        self.assertEqual(denied.status_code, 403)

    def test_staff_can_generate_for_customer(self):
        self.login('t1', 'tier1')
        response = self.client.post('/generateStatement', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '1001',
            'kind': 'monthly', 'period': '2026-01',
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()['statement']['userid'], 'alice')

    def test_staff_missing_customer_id(self):
        self.login('t1', 'tier1')
        response = self.client.post('/generateStatement', json={'userid': 't1', 'account': '1001'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'missing_customer_id')


if __name__ == '__main__':
    unittest.main()
