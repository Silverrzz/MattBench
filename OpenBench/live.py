import asyncio
import json
import logging
from contextlib import suppress
from importlib import import_module
from urllib.parse import parse_qs, urlsplit

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.contrib.auth import get_user
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpRequest, QueryDict
from django.urls import Resolver404, resolve

from OpenBench import views
from OpenBench import training_views
from OpenBench import worker_views
from OpenBench.config import ConfigurationMiddleware


logger = logging.getLogger(__name__)


class LiveUpdates(AsyncJsonWebsocketConsumer):
    @classmethod
    async def encode_json(cls, content):
        return json.dumps(content, cls=DjangoJSONEncoder)

    async def connect(self):
        self.stream_task = None
        headers = dict(self.scope['headers'])
        origin = urlsplit(headers.get(b'origin', b'').decode())
        host = headers.get(b'host', b'').decode()
        if origin.scheme not in ('http', 'https') or origin.netloc.lower() != host.lower():
            await self.close(code=4403)
            return

        query = parse_qs(self.scope['query_string'].decode())
        self.page = urlsplit(query.get('page', ['/'])[0])
        if self.page.scheme or self.page.netloc:
            await self.close(code=4400)
            return
        try:
            self.route = resolve(self.page.path)
        except Resolver404:
            await self.close(code=4404)
            return
        allowed = (
            views.index, views.user, views.greens, views.search, views.workload,
            views.users, views.machines, views.events_actions, views.events_errors, views.networks,
            training_views.training_index, training_views.training_detail, training_views.dataset_upload, training_views.workers,
            worker_views.index, worker_views.detail,
        )
        if self.route.func not in allowed or self.route.kwargs.get('action'):
            await self.close(code=4404)
            return

        self.host = host
        self.subscriptions = frozenset()
        self.previous = {}
        await self.accept()
        self.stream_task = asyncio.create_task(self.stream())

    async def disconnect(self, code):
        if self.stream_task:
            self.stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.stream_task

    async def receive_json(self, content, **kwargs):
        if isinstance(content, dict) and content.get('subscribe') in ('results', 'digest'):
            self.subscriptions = self.subscriptions | {content['subscribe']}

    @database_sync_to_async
    def snapshot(self):
        request = HttpRequest()
        request.method = 'GET'
        request.path = request.path_info = self.page.path
        request.GET = QueryDict(self.page.query)
        request.META['HTTP_HOST'] = self.host
        server = self.scope.get('server') or ('localhost', 80)
        request.META['SERVER_NAME'] = server[0]
        request.META['SERVER_PORT'] = str(server[1])
        request._get_scheme = lambda: urlsplit(dict(self.scope['headers'])[b'origin'].decode()).scheme
        session_store = import_module(settings.SESSION_ENGINE).SessionStore
        request.session = session_store(session_key=self.scope['session'].session_key)
        request.user = get_user(request)
        request.live_regions = {}
        request.live_subscriptions = self.subscriptions.copy()
        response = ConfigurationMiddleware(
            lambda current: self.route.func(current, *self.route.args, **self.route.kwargs)
        )(request)
        if response.status_code != 200:
            return None
        return request.live_payload

    async def stream(self):
        try:
            while True:
                snapshot = await self.snapshot()
                if snapshot is None:
                    await self.send_json({'unavailable': True})
                    await self.close(code=4403)
                    return
                changed = {key: value for key, value in snapshot.items()
                           if key not in self.previous or self.previous[key] != value}
                await self.send_json(changed)
                self.previous = snapshot
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Live updates failed for %s', self.page.path)
            await self.close(code=4500)
