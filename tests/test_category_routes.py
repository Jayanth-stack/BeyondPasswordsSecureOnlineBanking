import unittest

from flask import Flask, session

from utility.category import (
    CategoryPolicy,
    CategoryService,
    MemoryCategoryStore,
    attach_category_routes,
    set_service,
)


class Clock:
    def __init__(self, ts):
        self.ts = ts

    def __call__(self):
        return self.ts


class CategoryRouteTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(1772755200.0)  # 2026-03-06 UTC
        self.store = MemoryCategoryStore()
        self.service = CategoryService(CategoryPolicy(), self.store, clock=self.clock)
        self.service.observe(
            '1001', '40.00', 'withdraw', userid='alice',
            description='atm cash', posted_at=1770076800.0, source_id='wd:alice',
        )
        set_service(self.service)
        app = Flask(__name__)
        app.secret_key = 'test'
        app.config['TESTING'] = True

        def own_accounts(userid):
            return ['1001'] if userid == 'alice' else []

        attach_category_routes(app, self.service, own_accounts_loader=own_accounts)

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

    def test_list_spending_requires_session(self):
        response = self.client.post('/listSpending', json={'userid': 'alice'})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch(self):
        self.login('alice', 'customer')
        response = self.client.post('/listSpending', json={'userid': 'bob'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_list_spending_happy_path(self):
        self.login('alice', 'customer')
        response = self.client.post('/listSpending', json={'userid': 'alice', 'period': '2026-02'})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()['Spending']
        self.assertEqual(body['period'], '2026-02')
        self.assertEqual(body['totals']['debit'], '40.00')
        self.assertEqual(body['movements'][0]['category_id'], 'cash')

    def test_create_category_and_set_budget(self):
        self.login('alice', 'customer')
        created = self.client.post('/createCategory', json={'userid': 'alice', 'label': 'Pets'})
        self.assertEqual(created.status_code, 201)
        category_id = created.get_json()['category']['category_id']
        budgeted = self.client.post('/setBudget', json={
            'userid': 'alice', 'category_id': 'cash', 'amount': '100.00',
        })
        self.assertEqual(budgeted.status_code, 200)
        self.assertEqual(budgeted.get_json()['budget']['amount'], '100.00')
        spending = self.client.post('/listSpending', json={'userid': 'alice', 'period': '2026-02'})
        cash = next(row for row in spending.get_json()['Spending']['budgets'] if row['category_id'] == 'cash')
        self.assertEqual(cash['status'], 'ok')
        listed = self.client.post('/listCategories', json={'userid': 'alice'})
        ids = {item['category_id'] for item in listed.get_json()['categories']}
        self.assertIn(category_id, ids)

    def test_recategorize_movement(self):
        self.login('alice', 'customer')
        listed = self.client.post('/listMovements', json={'userid': 'alice', 'period': '2026-02'})
        movement_id = listed.get_json()['movements'][0]['movement_id']
        updated = self.client.post('/categorizeMovement', json={
            'userid': 'alice', 'movement_id': movement_id, 'category_id': 'groceries',
        })
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()['movement']['category_id'], 'groceries')
        self.assertTrue(updated.get_json()['movement']['manual'])

    def test_missing_movement(self):
        self.login('alice', 'customer')
        response = self.client.post('/categorizeMovement', json={
            'userid': 'alice', 'movement_id': 'missing', 'category_id': 'groceries',
        })
        self.assertEqual(response.status_code, 404)

    def test_future_period_is_400(self):
        self.login('alice', 'customer')
        response = self.client.post('/listSpending', json={'userid': 'alice', 'period': '2026-12'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'period_in_future')

    def test_merchant_tag_and_rule(self):
        self.login('alice', 'customer')
        tagged = self.client.post('/setMerchantTag', json={
            'userid': 'alice', 'merchant': 'netflix', 'category_id': 'entertainment',
        })
        self.assertEqual(tagged.status_code, 200)
        ruled = self.client.post('/addCategoryRule', json={
            'userid': 'alice', 'keyword': 'spotify', 'category_id': 'entertainment',
        })
        self.assertEqual(ruled.status_code, 201)
        self.service.observe(
            '1001', '9.99', 'withdraw', userid='alice',
            description='spotify premium', posted_at=1770076800.0, source_id='wd:spot',
        )
        spending = self.client.post('/listSpending', json={'userid': 'alice', 'period': '2026-02'})
        kinds = {item['category_id'] for item in spending.get_json()['Spending']['movements']}
        self.assertIn('entertainment', kinds)

    def test_staff_can_set_budget_for_customer(self):
        self.login('t1', 'tier1')
        response = self.client.post('/setBudget', json={
            'userid': 't1', 'customer_id': 'alice', 'category_id': 'cash', 'amount': '60.00',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['budget']['userid'], 'alice')

    def test_staff_missing_customer_id(self):
        self.login('t1', 'tier1')
        response = self.client.post('/listSpending', json={'userid': 't1'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'missing_customer_id')

    def test_staff_create_system_category(self):
        self.login('t1', 'tier1')
        response = self.client.post('/createCategory', json={'userid': 't1', 'label': 'Education'})
        self.assertEqual(response.status_code, 201)
        body = response.get_json()['category']
        self.assertEqual(body['kind'], 'system')
        self.assertEqual(body['category_id'], 'education')

    def test_customer_cannot_archive_system(self):
        self.login('alice', 'customer')
        response = self.client.post('/archiveCategory', json={
            'userid': 'alice', 'category_id': 'cash',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'category_system')

    def test_clear_budget(self):
        self.login('alice', 'customer')
        self.client.post('/setBudget', json={
            'userid': 'alice', 'category_id': 'cash', 'amount': '80.00',
        })
        cleared = self.client.post('/clearBudget', json={
            'userid': 'alice', 'category_id': 'cash',
        })
        self.assertEqual(cleared.status_code, 200)
        spending = self.client.post('/listSpending', json={'userid': 'alice', 'period': '2026-02'})
        self.assertEqual(spending.get_json()['Spending']['budgets'], [])


if __name__ == '__main__':
    unittest.main()
