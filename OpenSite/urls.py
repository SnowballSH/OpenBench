from django.contrib import admin
from django.urls import path, include
from django.contrib.staticfiles.urls import staticfiles_urlpatterns

import OpenBench.urls

urlpatterns = [
    path(r'admin/', admin.site.urls),
    path(r'', include(OpenBench.urls.urlpatterns)),
]

urlpatterns += staticfiles_urlpatterns()
handler403 = 'OpenBench.security.error_pages.permission_denied'
handler404 = 'OpenBench.security.error_pages.page_not_found'
handler500 = 'OpenBench.security.error_pages.server_error'
