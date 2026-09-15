import base64
import hashlib
import hmac
import secrets
import time
from cryptography.fernet import Fernet

def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return base64.b64encode(salt + digest).decode()

def verify_password(password, encoded):
    try:
        data = base64.b64decode(encoded)
        return hmac.compare_digest(data[16:], hashlib.scrypt(password.encode(), salt=data[:16], n=16384, r=8, p=1))
    except (ValueError, TypeError):
        return False

def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()

class Vault:
    def __init__(self, key):
        self.fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))
        self.key = key.encode()

    def encrypt(self, value):
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value):
        return self.fernet.decrypt(value.encode()).decode()

    def unsubscribe_token(self, workspace_id, email):
        payload = f'{workspace_id}:{email.lower()}'.encode()
        return hmac.new(self.key, payload, hashlib.sha256).hexdigest()

    def verify_unsubscribe(self, workspace_id, email, token):
        return hmac.compare_digest(self.unsubscribe_token(workspace_id, email), token)

    def booking_token(self, workspace_id, contact_id, expires_at=None):
        expires_at = int(expires_at or time.time()+86400*30)
        payload = f'booking:{workspace_id}:{contact_id}:{expires_at}'
        signature = hmac.new(self.key,payload.encode(),hashlib.sha256).hexdigest()
        return f'{expires_at}.{signature}'

    def verify_booking(self, workspace_id, contact_id, token):
        try:
            expiration = int(token.split('.')[0])
            return expiration >= time.time() and hmac.compare_digest(token,self.booking_token(workspace_id,contact_id,expiration))
        except (ValueError,TypeError,AttributeError):
            return False
