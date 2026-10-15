"""Local private-volume signing keys; old public keys survive rotation."""
import base64
import json
import os
import secrets
from pathlib import Path
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey,Ed25519PublicKey
from cryptography.hazmat.primitives import serialization
from app.config import settings

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False,default=str).encode()
def directory():
    target=Path(settings.storage_path)/'verification-keys'
    target.mkdir(parents=True,exist_ok=True)
    if target.is_symlink():raise RuntimeError('Signing key directory must be a private volume directory')
    return target

def exclusive(path,content):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(content)

def rotate():
    root=directory();kid=secrets.token_hex(12);key=Ed25519PrivateKey.generate()
    exclusive(root/(kid+'.pem'),key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    exclusive(root/(kid+'.public.json'),canonical({'key_id':kid,'algorithm':'Ed25519','public_key':base64.b64encode(public).decode()}))
    temporary=root/('current-'+secrets.token_hex(8));exclusive(temporary,kid.encode());os.replace(temporary,root/'current')
    return kid

def signing_key():
    root=directory();pointer=root/'current'
    if not pointer.exists():
        # A database advisory lock serializes first receipt creation across API workers.
        rotate()
    kid=pointer.read_text().strip()
    if len(kid)!=24 or any(c not in '0123456789abcdef' for c in kid):raise RuntimeError('Invalid signing key reference')
    path=root/(kid+'.pem')
    if path.is_symlink():raise RuntimeError('Signing keys cannot be symlinks')
    return kid,serialization.load_pem_private_key(path.read_bytes(),password=None)

def public_keys():return [json.loads(p.read_text()) for p in sorted(directory().glob('*.public.json')) if not p.is_symlink()]

def verify(payload,signature):
    try:
        key=next(k for k in public_keys() if k['key_id']==payload['key_id'])
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key['public_key'],validate=True)).verify(base64.b64decode(signature,validate=True),canonical(payload))
        return True
    except (ValueError,KeyError,StopIteration,TypeError):return False
    except InvalidSignature:return False

if __name__=='__main__':
    import sys
    if sys.argv[1:]!=['rotate']:raise SystemExit('Usage: python -m app.verification_keys rotate')
    print('Activated signing key '+rotate()+'. Old public keys retained.')
