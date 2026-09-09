#!/usr/bin/bash
. bin/activate
exec daphne -b 127.0.0.1 -p 8000 OpenSite.asgi:application
