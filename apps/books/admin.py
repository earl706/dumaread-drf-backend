from django.contrib import admin

from .models import Book, BookReadingDay, ReadingDay

# -----------------------------------------------------------------------------
# Model registrations
# -----------------------------------------------------------------------------
admin.site.register(Book)
admin.site.register(ReadingDay)
admin.site.register(BookReadingDay)
