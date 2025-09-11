import json
import logging
import threading
from hl_core.utils.logger import DiscordHandler


def test_discord_handler_posts(monkeypatch):
    called = {}
    done = threading.Event()

    def fake_urlopen(req, timeout=0, *args, **kwargs):
        called['url'] = req.full_url
        called['data'] = req.data
        called['headers'] = {k.lower(): v for k, v in req.header_items()}

        class DummyResponse:
            def close(self):
                pass

        done.set()
        return DummyResponse()

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)

    handler = DiscordHandler('https://example.com/webhook')
    handler.setFormatter(logging.Formatter('%(message)s'))

    record = logging.LogRecord(
        name='test', level=logging.ERROR, pathname=__file__, lineno=0,
        msg='hello', args=(), exc_info=None,
    )
    handler.emit(record)

    assert done.wait(1.0), 'urlopen not called'
    assert called['url'] == 'https://example.com/webhook'
    assert json.loads(called['data'].decode()) == {'content': 'hello'}
    assert called['headers']['content-type'] == 'application/json'
