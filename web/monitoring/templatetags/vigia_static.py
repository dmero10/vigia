"""`{% versioned_static 'ruta' %}`: como `{% static %}`, pero agrega `?v=<fecha del archivo>`.

Sin esto, el navegador puede seguir usando una copia vieja de session.js o vigia.css
después de un cambio (o de un `git pull`) y la página se comporta como antes.
Con el número de versión, cada cambio del archivo es una dirección nueva.
"""

from pathlib import Path

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def versioned_static(path):
    url = static(path)
    found = finders.find(path)
    if not found:
        return url
    return f'{url}?v={int(Path(found).stat().st_mtime)}'
