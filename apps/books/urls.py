from rest_framework.routers import DefaultRouter

from .views import BookViewSet


# -----------------------------------------------------------------------------
# Router & URL patterns
# -----------------------------------------------------------------------------
router = DefaultRouter()
router.register("books", BookViewSet, basename="book")

urlpatterns = router.urls
