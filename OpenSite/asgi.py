import os

from django.core.asgi import get_asgi_application


os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OpenSite.settings')
django_application = get_asgi_application()

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.urls import path
from OpenBench.live import LiveUpdates


application = ProtocolTypeRouter({
    'http': django_application,
    'websocket': AuthMiddlewareStack(URLRouter([
        path('ws/live/', LiveUpdates.as_asgi()),
    ])),
})
