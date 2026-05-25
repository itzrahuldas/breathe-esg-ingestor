"""
WSGI config for Breathe ESG Ingestor.

Exposes the WSGI callable as a module-level variable named ``application``.
Used by gunicorn in production: gunicorn config.wsgi
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()
