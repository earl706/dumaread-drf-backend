from django.contrib import admin

from .models import Book, ReadingDay

# -----------------------------------------------------------------------------
# Model registrations
# -----------------------------------------------------------------------------
admin.site.register(Book)
admin.site.register(ReadingDay)
