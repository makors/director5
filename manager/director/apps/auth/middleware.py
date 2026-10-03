from social_core.exceptions import AuthCanceled, AuthStateForbidden, AuthStateMissing
from social_django.middleware import SocialAuthExceptionMiddleware


class IonAuthExceptionMiddleware(SocialAuthExceptionMiddleware):
    def get_message(self, request, exception):
        """Show useful login errors without exposing provider responses or tokens."""
        if isinstance(exception, AuthCanceled):
            return "Ion sign-in was cancelled. You can try again when you're ready."
        if isinstance(exception, AuthStateForbidden | AuthStateMissing):
            return "Your sign-in session expired. Please try again."
        return "We couldn't sign you in with Ion. Please try again."
