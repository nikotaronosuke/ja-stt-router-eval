"""Per-server session for loopback review pages; no token in URL or logs."""
from http.cookies import SimpleCookie
import secrets


class LocalSession:
    def __init__(self, port):
        self.host = f'127.0.0.1:{port}'
        self.origin = 'http://' + self.host
        self.cookie_name = f'study_session_{port}'
        self._token = secrets.token_urlsafe(32)

    def same_origin(self, headers, *, navigation=False):
        if headers.get_all('Host') != [self.host]:
            return False
        origin = headers.get('Origin')
        site = headers.get('Sec-Fetch-Site')
        if origin is not None and origin != self.origin:
            return False
        if site not in (None, 'same-origin', 'none'):
            return False
        if navigation:
            return True
        return origin == self.origin or site == 'same-origin'

    def authorized(self, headers):
        if not self.same_origin(headers):
            return False
        try:
            cookies = SimpleCookie()
            cookies.load(headers.get('Cookie',''))
            value = cookies.get(self.cookie_name)
            return value is not None and secrets.compare_digest(value.value, self._token)
        except Exception:
            return False

    def cookie(self):
        # HttpOnly and SameSite apply on local HTTP; do not pretend HTTPS is used.
        return f'{self.cookie_name}={self._token}; Path=/; HttpOnly; SameSite=Strict'
