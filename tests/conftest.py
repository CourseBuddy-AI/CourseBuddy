import pytest

from app import create_app


@pytest.fixture
def app():
    return create_app({"TESTING": True, "SECRET_KEY": "test-only", "APP_ENV": "development"})


@pytest.fixture
def client(app):
    return app.test_client()


class As:
    """以示範帳號登入的測試用 client，POST 會自動帶 CSRF token。"""

    def __init__(self, client, uid):
        self.c = client
        self.c.get("/login")
        r = self.c.post("/login", data={"uid": uid, "csrf": self._token()})
        assert r.status_code == 302, r.status_code
        self.token = self._token()  # 登入後 session 換新，token 也換新

    def _token(self):
        with self.c.session_transaction() as s:
            return s["csrf"]

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        data.setdefault("csrf", self.token)
        return self.c.post(url, data=data, **kw)

    def api_post(self, url, **kw):
        return self.c.post(url, headers={"X-CSRF-Token": self.token}, **kw)


@pytest.fixture
def login(app):
    """每次呼叫都用新的 client，所以 login("alice") 和 login("bob") 是兩個不同的瀏覽器。"""
    return lambda uid: As(app.test_client(), uid)
