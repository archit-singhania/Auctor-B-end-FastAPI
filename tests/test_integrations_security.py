import asyncio
import socket
import pytest
from fastapi import HTTPException
from app import integrations
from app.config import settings

@pytest.mark.parametrize('url',[
 'http://receiver.example/events','https://name:secret@receiver.example/events',
 'https://receiver.example/events#fragment','https://receiver.example:22/events',
 'https://receiver.example/\r\nInjected:header','https://[invalid]/events',
])
def test_malformed_or_unsafe_destinations(url,monkeypatch):
    monkeypatch.setattr(settings,'webhook_development_mode',False)
    with pytest.raises(HTTPException):asyncio.run(integrations.destination(url))

@pytest.mark.parametrize('address',['127.0.0.1','169.254.169.254','10.0.0.1','192.168.1.1','::1','fc00::1','2002:7f00:1::','224.0.0.1'])
def test_dns_private_transition_and_multicast_addresses_rejected(address,monkeypatch):
    monkeypatch.setattr(settings,'webhook_development_mode',False)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *_:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,443))])
    with pytest.raises(HTTPException):asyncio.run(integrations.destination('https://receiver.example/events'))

def test_delivery_pins_checked_ip_uses_sni_and_rejects_redirect(monkeypatch):
    monkeypatch.setattr(settings,'webhook_development_mode',False)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *_:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])
    calls=[]
    class Reader:
        async def readline(self):return b'HTTP/1.1 302 Found\r\n'
    class Writer:
        def write(self,data):assert b'Host: receiver.example\r\n' in data
        async def drain(self):pass
        def close(self):pass
        async def wait_closed(self):pass
    async def connect(host,port,**options):
        calls.append((host,port,options['server_hostname']));assert options['ssl'] is not None
        return Reader(),Writer()
    monkeypatch.setattr(asyncio,'open_connection',connect)
    with pytest.raises(ValueError,match='redirects'):asyncio.run(integrations.pinned_post('https://receiver.example/events',b'{}',{}))
    assert calls==[('8.8.8.8',443,'receiver.example')]
    calls.clear()
    async def withdrawn():return False
    with pytest.raises(PermissionError):asyncio.run(integrations.pinned_post('https://receiver.example/events',b'{}',{},withdrawn))
    assert not calls
