import json
from urllib.parse import urlparse

from django.contrib.auth import logout
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import FormView, View
from jwcrypto import jwk
from oauthlib.common import add_params_to_uri

from ..exceptions import (
    ClientIdMissmatch,
    InvalidIDTokenError,
    InvalidOIDCClientError,
    InvalidOIDCRedirectURIError,
    LogoutDenied,
    OIDCError,
)
from ..forms import ConfirmLogoutForm
from ..http import OAuth2ResponseRedirect
from ..models import get_application_model
from ..settings import oauth2_settings
from .mixins import OAuthLibMixin, OIDCLogoutOnlyMixin, OIDCOnlyMixin


Application = get_application_model()


class ConnectDiscoveryInfoView(OIDCOnlyMixin, View):
    """
    View used to show oidc provider configuration information per
    `OpenID Provider Metadata <https://openid.net/specs/openid-connect-discovery-1_0.html#ProviderMetadata>`_
    """

    def get(self, request, *args, **kwargs):
        issuer_url = oauth2_settings.OIDC_ISS_ENDPOINT

        if not issuer_url:
            issuer_url = oauth2_settings.oidc_issuer(request)
            authorization_endpoint = request.build_absolute_uri(reverse("oauth2_provider:authorize"))
            token_endpoint = request.build_absolute_uri(reverse("oauth2_provider:token"))
            userinfo_endpoint = oauth2_settings.OIDC_USERINFO_ENDPOINT or request.build_absolute_uri(
                reverse("oauth2_provider:user-info")
            )
            jwks_uri = request.build_absolute_uri(reverse("oauth2_provider:jwks-info"))
            if oauth2_settings.OIDC_RP_INITIATED_LOGOUT_ENABLED:
                end_session_endpoint = request.build_absolute_uri(
                    reverse("oauth2_provider:rp-initiated-logout")
                )
        else:
            parsed_url = urlparse(oauth2_settings.OIDC_ISS_ENDPOINT)
            host = parsed_url.scheme + "://" + parsed_url.netloc
            authorization_endpoint = "{}{}".format(host, reverse("oauth2_provider:authorize"))
            token_endpoint = "{}{}".format(host, reverse("oauth2_provider:token"))
            userinfo_endpoint = oauth2_settings.OIDC_USERINFO_ENDPOINT or "{}{}".format(
                host, reverse("oauth2_provider:user-info")
            )
            jwks_uri = "{}{}".format(host, reverse("oauth2_provider:jwks-info"))
            if oauth2_settings.OIDC_RP_INITIATED_LOGOUT_ENABLED:
                end_session_endpoint = "{}{}".format(host, reverse("oauth2_provider:rp-initiated-logout"))

        signing_algorithms = [Application.HS256_ALGORITHM]
        if oauth2_settings.OIDC_RSA_PRIVATE_KEY:
            signing_algorithms = [Application.RS256_ALGORITHM, Application.HS256_ALGORITHM]

        validator_class = oauth2_settings.OAUTH2_VALIDATOR_CLASS
        validator = validator_class()
        oidc_claims = list(set(validator.get_discovery_claims(request)))
        scopes_class = oauth2_settings.SCOPES_BACKEND_CLASS
        scopes = scopes_class()
        scopes_supported = [scope for scope in scopes.get_available_scopes()]

        data = {
            "issuer": issuer_url,
            "authorization_endpoint": authorization_endpoint,
            "token_endpoint": token_endpoint,
            "userinfo_endpoint": userinfo_endpoint,
            "jwks_uri": jwks_uri,
            "scopes_supported": scopes_supported,
            "response_types_supported": oauth2_settings.OIDC_RESPONSE_TYPES_SUPPORTED,
            "subject_types_supported": oauth2_settings.OIDC_SUBJECT_TYPES_SUPPORTED,
            "id_token_signing_alg_values_supported": signing_algorithms,
            "token_endpoint_auth_methods_supported": (
                oauth2_settings.OIDC_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED
            ),
            "claims_supported": oidc_claims,
        }
        if oauth2_settings.OIDC_RP_INITIATED_LOGOUT_ENABLED:
            data["end_session_endpoint"] = end_session_endpoint
        response = JsonResponse(data)
        response["Access-Control-Allow-Origin"] = "*"
        return response


class JwksInfoView(OIDCOnlyMixin, View):
    """
    View used to show oidc json web key set document
    """

    def get(self, request, *args, **kwargs):
        keys = []
        if oauth2_settings.OIDC_RSA_PRIVATE_KEY:
            for pem in [
                oauth2_settings.OIDC_RSA_PRIVATE_KEY,
                *oauth2_settings.OIDC_RSA_PRIVATE_KEYS_INACTIVE,
            ]:
                key = jwk.JWK.from_pem(pem.encode("utf8"))
                data = {"alg": "RS256", "use": "sig", "kid": key.thumbprint()}
                data.update(json.loads(key.export_public()))
                keys.append(data)
        response = JsonResponse({"keys": keys})
        response["Access-Control-Allow-Origin"] = "*"
        response["Cache-Control"] = (
            "Cache-Control: public, "
            + f"max-age={oauth2_settings.OIDC_JWKS_MAX_AGE_SECONDS}, "
            + f"stale-while-revalidate={oauth2_settings.OIDC_JWKS_MAX_AGE_SECONDS}, "
            + f"stale-if-error={oauth2_settings.OIDC_JWKS_MAX_AGE_SECONDS}"
        )
        return response


@method_decorator(csrf_exempt, name="dispatch")
class UserInfoView(OIDCOnlyMixin, OAuthLibMixin, View):
    """
    View used to show Claims about the authenticated End-User
    """

    def get(self, request, *args, **kwargs):
        return self._create_userinfo_response(request)

    def post(self, request, *args, **kwargs):
        return self._create_userinfo_response(request)

    def _create_userinfo_response(self, request):
        url, headers, body, status = self.create_userinfo_response(request)
        response = HttpResponse(content=body or "", status=status)

        for k, v in headers.items():
            response[k] = v
        return response


def validate_logout_request(user, id_token_hint, client_id, post_logout_redirect_uri):
    """
    Validate an OIDC RP-Initiated Logout Request.
    `(prompt_logout, (post_logout_redirect_uri, application))` is returned.

    `prompt_logout` indicates whether the logout has to be confirmed by the user. This happens if the
    specifications force a confirmation, or it is enabled by `OIDC_RP_INITIATED_LOGOUT_ALWAYS_PROMPT`.
    `post_logout_redirect_uri` is the validated URI where the User should be redirected to after the
    logout. Can be None. None will redirect to "/" of this app. If it is set `application` will also
    be set to the Application that is requesting the logout.

    The `id_token_hint` will be validated if given. If both `client_id` and `id_token_hint` are given they
    will be validated against each other.
    """
    validator = oauth2_settings.OAUTH2_VALIDATOR_CLASS()

    id_token = None
    must_prompt_logout = True
    if id_token_hint:
        # Note: The standard states that expired tokens should still be accepted.
        # This implementation only accepts tokens that are still valid.
        id_token = validator._load_id_token(id_token_hint)

        if not id_token:
            raise InvalidIDTokenError()

        if id_token.user == user:
            # A logout without user interaction (i.e. no prompt) is only allowed
            # if an ID Token is provided that matches the current user.
            must_prompt_logout = False

        # If both id_token_hint and client_id are given it must be verified that they match.
        if client_id:
            if id_token.application.client_id != client_id:
                raise ClientIdMissmatch()

    # The standard states that a prompt should always be shown.
    # This behaviour can be configured with OIDC_RP_INITIATED_LOGOUT_ALWAYS_PROMPT.
    prompt_logout = must_prompt_logout or oauth2_settings.OIDC_RP_INITIATED_LOGOUT_ALWAYS_PROMPT

    application = None
    # Determine the application that is requesting the logout.
    if client_id:
        application = get_application_model().objects.get(client_id=client_id)
    elif id_token:
        application = id_token.application

    # Validate `post_logout_redirect_uri`
    if post_logout_redirect_uri:
        if not application:
            raise InvalidOIDCClientError()
        scheme = urlparse(post_logout_redirect_uri)[0]
        if not scheme:
            raise InvalidOIDCRedirectURIError("A Scheme is required for the redirect URI.")
        if scheme == "http" and application.client_type != "confidential":
            raise InvalidOIDCRedirectURIError("http is only allowed with confidential clients.")
        if scheme not in application.get_allowed_schemes():
            raise InvalidOIDCRedirectURIError(f'Redirect to scheme "{scheme}" is not permitted.')
        if not application.post_logout_redirect_uri_allowed(post_logout_redirect_uri):
            raise InvalidOIDCRedirectURIError("This client does not have this redirect uri registered.")

    return prompt_logout, (post_logout_redirect_uri, application)


class RPInitiatedLogoutView(OIDCLogoutOnlyMixin, FormView):
    template_name = "oauth2_provider/logout_confirm.html"
    form_class = ConfirmLogoutForm

    def get_initial(self):
        return {
            "id_token_hint": self.oidc_data.get("id_token_hint", None),
            "logout_hint": self.oidc_data.get("logout_hint", None),
            "client_id": self.oidc_data.get("client_id", None),
            "post_logout_redirect_uri": self.oidc_data.get("post_logout_redirect_uri", None),
            "state": self.oidc_data.get("state", None),
            "ui_locales": self.oidc_data.get("ui_locales", None),
        }

    def dispatch(self, request, *args, **kwargs):
        self.oidc_data = {}
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        id_token_hint = request.GET.get("id_token_hint")
        client_id = request.GET.get("client_id")
        post_logout_redirect_uri = request.GET.get("post_logout_redirect_uri")
        state = request.GET.get("state")

        try:
            prompt, (redirect_uri, application) = validate_logout_request(
                user=request.user,
                id_token_hint=id_token_hint,
                client_id=client_id,
                post_logout_redirect_uri=post_logout_redirect_uri,
            )
        except OIDCError as error:
            return self.error_response(error)

        if not prompt:
            return self.do_logout(application, redirect_uri, state)

        self.oidc_data = {
            "id_token_hint": id_token_hint,
            "client_id": client_id,
            "post_logout_redirect_uri": post_logout_redirect_uri,
            "state": state,
        }
        form = self.get_form(self.get_form_class())
        kwargs["form"] = form
        if application:
            kwargs["application"] = application

        return self.render_to_response(self.get_context_data(**kwargs))

    def form_valid(self, form):
        id_token_hint = form.cleaned_data.get("id_token_hint")
        client_id = form.cleaned_data.get("client_id")
        post_logout_redirect_uri = form.cleaned_data.get("post_logout_redirect_uri")
        state = form.cleaned_data.get("state")

        try:
            prompt, (redirect_uri, application) = validate_logout_request(
                user=self.request.user,
                id_token_hint=id_token_hint,
                client_id=client_id,
                post_logout_redirect_uri=post_logout_redirect_uri,
            )

            if not prompt or form.cleaned_data.get("allow"):
                return self.do_logout(application, redirect_uri, state)
            else:
                raise LogoutDenied()

        except OIDCError as error:
            return self.error_response(error)

    def do_logout(self, application=None, post_logout_redirect_uri=None, state=None):
        logout(self.request)
        if post_logout_redirect_uri:
            if state:
                return OAuth2ResponseRedirect(
                    add_params_to_uri(post_logout_redirect_uri, [("state", state)]),
                    application.get_allowed_schemes(),
                )
            else:
                return OAuth2ResponseRedirect(post_logout_redirect_uri, application.get_allowed_schemes())
        else:
            return OAuth2ResponseRedirect(
                self.request.build_absolute_uri("/"),
                oauth2_settings.ALLOWED_REDIRECT_URI_SCHEMES,
            )

    def error_response(self, error):
        error_response = {"error": error}
        return self.render_to_response(error_response, status=error.status_code)
