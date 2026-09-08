import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
for key in list(os.environ):
    if any(term in key.upper() for term in ('TOKEN', 'SECRET', 'API_KEY')):
        os.environ.pop(key, None)
os.environ['META_ACCESS_TOKEN'] = 'offline-dummy-token-not-valid-for-meta'
os.environ['META_ADS_REQUIRE_WRITE_CONFIRMATION'] = 'true'
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False

def blocked(*args, **kwargs):
    raise OSError('AUDIT_OFFLINE: network connections disabled')

socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.create_connection = blocked

# Block browser launches; isolate cache/log paths without
# repurposing HOME so tests cannot read or overwrite the operator's OAuth cache.
import tempfile
_temp = tempfile.TemporaryDirectory(prefix="meta-tests-")
Path.home = classmethod(lambda cls: Path(_temp.name))
import webbrowser
webbrowser.open = blocked
import pytest
raise SystemExit(pytest.main(['-q', '--tb=short', '--disable-warnings', *sys.argv[1:]]))
