from rest_framework import viewsets


class BaseMemoryViewSet(viewsets.ModelViewSet):
    lookup_field = "name"
    lookup_url_kwarg = "name"

    def get_object(self):
        lookup_value = self.kwargs.pop(self.lookup_url_kwarg)
        return self.get_queryset().get(self.lookup_field, lookup_value).load_full()
