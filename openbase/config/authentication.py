from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

User = get_user_model()


class OpenbaseTokenAuthentication(BaseAuthentication):
    """
    Custom authentication class that validates against OPENBASE_API_TOKEN setting.

    Clients should authenticate by passing the token key in the "Authorization"
    HTTP header, prepended with the string "Bearer ".  For example:

        Authorization: Bearer 401f7ac837da42b97f613d789819ff93537bee6a
    """

    keyword = "Bearer"

    def authenticate(self, request):
        auth = request.META.get("HTTP_AUTHORIZATION")
        if not auth:
            return None

        try:
            token_type, token = auth.split()
        except ValueError:
            return None

        if token_type.lower() != self.keyword.lower():
            return None

        return self.authenticate_credentials(token)

    def authenticate_credentials(self, key):
        expected_token = settings.OPENBASE_API_TOKEN

        if key != expected_token:
            raise AuthenticationFailed("Invalid token")

        user = User.objects.first()

        # Return a tuple of (user, auth) - using AnonymousUser since we don't need a real user
        return (user, key)

    def authenticate_header(self, request):
        return self.keyword
