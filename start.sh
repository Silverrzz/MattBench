#!/usr/bin/bash
. bin/activate
gunicorn OpenSite.wsgi
