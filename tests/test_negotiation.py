import pytest
from django.http import Http404
from django.test import TestCase

from rest_framework import exceptions
from rest_framework.negotiation import (
    BaseContentNegotiation, DefaultContentNegotiation,
    ParameterAwareContentNegotiation, split_accept_header
)
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.utils.mediatypes import _MediaType
from rest_framework.views import APIView

factory = APIRequestFactory()


class MockOpenAPIRenderer(BaseRenderer):
    media_type = 'application/openapi+json;version=2.0'
    format = 'swagger'


class MockJSONRenderer(BaseRenderer):
    media_type = 'application/json'


class MockHTMLRenderer(BaseRenderer):
    media_type = 'text/html'


class NoCharsetSpecifiedRenderer(BaseRenderer):
    media_type = 'my/media'


class TestAcceptedMediaType(TestCase):
    def setUp(self):
        self.renderers = [MockJSONRenderer(), MockHTMLRenderer(), MockOpenAPIRenderer()]
        self.negotiator = DefaultContentNegotiation()

    def select_renderer(self, request):
        return self.negotiator.select_renderer(request, self.renderers)

    def test_client_without_accept_use_renderer(self):
        request = Request(factory.get('/'))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert accepted_media_type == 'application/json'

    def test_client_underspecifies_accept_use_renderer(self):
        request = Request(factory.get('/', HTTP_ACCEPT='*/*'))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert accepted_media_type == 'application/json'

    def test_client_overspecifies_accept_use_client(self):
        request = Request(factory.get('/', HTTP_ACCEPT='application/json; indent=8'))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert accepted_media_type == 'application/json; indent=8'

    def test_client_specifies_parameter(self):
        request = Request(factory.get('/', HTTP_ACCEPT='application/openapi+json;version=2.0'))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert accepted_media_type == 'application/openapi+json;version=2.0'
        assert accepted_renderer.format == 'swagger'

    def test_match_is_false_if_main_types_not_match(self):
        mediatype = _MediaType('test_1')
        another_mediatype = _MediaType('test_2')
        assert mediatype.match(another_mediatype) is False

    def test_mediatype_match_is_false_if_keys_not_match(self):
        mediatype = _MediaType(';test_param=foo')
        another_mediatype = _MediaType(';test_param=bar')
        assert mediatype.match(another_mediatype) is False

    def test_mediatype_precedence_with_wildcard_subtype(self):
        mediatype = _MediaType('test/*')
        assert mediatype.precedence == 1

    def test_mediatype_string_representation(self):
        mediatype = _MediaType('test/*; foo=bar')
        assert str(mediatype) == 'test/*; foo=bar'

    def test_raise_error_if_no_suitable_renderers_found(self):
        class MockRenderer:
            format = 'xml'
        renderers = [MockRenderer()]
        with pytest.raises(Http404):
            self.negotiator.filter_renderers(renderers, format='json')


class BaseContentNegotiationTests(TestCase):

    def setUp(self):
        self.negotiator = BaseContentNegotiation()

    def test_raise_error_for_abstract_select_parser_method(self):
        with pytest.raises(NotImplementedError):
            self.negotiator.select_parser(None, None)

    def test_raise_error_for_abstract_select_renderer_method(self):
        with pytest.raises(NotImplementedError):
            self.negotiator.select_renderer(None, None)


class CompactProfileJSONRenderer(JSONRenderer):
    media_type = 'application/json; profile=compact'
    format = 'json-compact'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, dict):
            data = dict(data, renderer='compact')
        return super().render(data, accepted_media_type, renderer_context)


class FullProfileJSONRenderer(JSONRenderer):
    media_type = 'application/json; profile=full'
    format = 'json-full'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, dict):
            data = dict(data, renderer='full')
        return super().render(data, accepted_media_type, renderer_context)


class ProfileView(APIView):
    content_negotiation_class = ParameterAwareContentNegotiation
    renderer_classes = [CompactProfileJSONRenderer, FullProfileJSONRenderer]

    def get(self, request):
        return Response({'example': 'data'})


class RaisingProfileView(ProfileView):
    def get(self, request):
        raise exceptions.ValidationError('Invalid input.')


class TestParameterAwareContentNegotiation(TestCase):
    def setUp(self):
        self.view = ProfileView.as_view()

    def get_response(self, accept=None, path='/', view=None):
        kwargs = {'HTTP_ACCEPT': accept} if accept is not None else {}
        request = factory.get(path, **kwargs)
        response = (view or self.view)(request)
        response.render()
        return response

    def test_missing_accept_header_uses_first_renderer(self):
        response = self.get_response()
        assert response.status_code == 200
        assert response['Content-Type'] == 'application/json; profile=compact'
        assert b'"renderer":"compact"' in response.content

    def test_weighted_profiles_select_highest_q(self):
        response = self.get_response(
            'application/json;profile="compact";q=0.4, '
            'application/json;profile="full";q=0.9'
        )
        assert response['Content-Type'] == 'application/json; profile=full'
        assert b'"renderer":"full"' in response.content

    def test_weighted_profiles_reversed(self):
        response = self.get_response(
            'application/json;profile="full";q=0.4, '
            'application/json;profile="compact";q=0.9'
        )
        assert response['Content-Type'] == 'application/json; profile=compact'
        assert b'"renderer":"compact"' in response.content

    def test_equal_weights_use_server_order(self):
        response = self.get_response(
            'application/json;profile="full";q=0.7, '
            'application/json;profile="compact";q=0.7'
        )
        assert response['Content-Type'] == 'application/json; profile=compact'

    def test_parameter_order_and_media_type_case_are_irrelevant(self):
        first = self.get_response('Application/JSON; Profile="full"')
        second = self.get_response('application/json;profile="full"')
        assert first['Content-Type'] == second['Content-Type']
        assert first['Content-Type'] == 'application/json; profile=full'

    def test_generic_media_type_matches_any_profile(self):
        response = self.get_response('application/json')
        assert response['Content-Type'] == 'application/json; profile=compact'

    def test_wildcard_matches_with_required_params_in_content_type(self):
        response = self.get_response('application/*;q=0.5, */*;q=0.1')
        assert response['Content-Type'] == 'application/json; profile=compact'

    def test_q_zero_profile_is_not_reselected_by_fallback(self):
        response = self.get_response(
            'application/json;profile="compact";q=0, application/json;q=0.8'
        )
        assert response['Content-Type'] == 'application/json; profile=full'
        assert b'"renderer":"full"' in response.content

    def test_q_zero_profile_with_wildcard_fallback(self):
        response = self.get_response(
            'application/json;profile="compact";q=0, */*;q=0.5'
        )
        assert response['Content-Type'] == 'application/json; profile=full'

    def test_all_q_zero_is_not_acceptable(self):
        response = self.get_response('application/json;q=0')
        assert response.status_code == 406

    def test_unsupported_media_type_is_not_acceptable(self):
        response = self.get_response('text/html')
        assert response.status_code == 406

    def test_invalid_accept_header_is_treated_as_any(self):
        response = self.get_response('this is not an accept header')
        assert response.status_code == 200
        assert response['Content-Type'] == 'application/json; profile=compact'

    def test_invalid_q_value_is_ignored(self):
        response = self.get_response(
            'application/json;profile="compact";q=bogus, '
            'application/json;profile="full"'
        )
        assert response['Content-Type'] == 'application/json; profile=full'

    def test_quoted_parameter_value_with_comma(self):
        response = self.get_response(
            'application/json;profile="comp,act", '
            'application/json;profile="full";q=0.5'
        )
        assert response['Content-Type'] == 'application/json; profile=full'

    def test_extra_client_parameters_are_preserved(self):
        response = self.get_response(
            'application/json;indent=4;profile="full"'
        )
        assert response['Content-Type'] == 'application/json; profile=full'
        assert response.accepted_media_type == (
            'application/json; profile=full; indent=4'
        )

    def test_format_query_param_filters_renderers(self):
        response = self.get_response(
            'application/json', path='/?format=json-full'
        )
        assert response['Content-Type'] == 'application/json; profile=full'
        assert b'"renderer":"full"' in response.content

    def test_format_query_param_with_incompatible_accept_is_406(self):
        response = self.get_response(
            'application/json;profile="compact"', path='/?format=json-full'
        )
        assert response.status_code == 406

    def test_unknown_format_query_param_raises_404(self):
        response = self.get_response(
            'application/json', path='/?format=xml'
        )
        assert response.status_code == 404

    def test_exception_response_uses_negotiated_profile(self):
        view = RaisingProfileView.as_view()
        response = self.get_response(
            'application/json;profile="full"', view=view
        )
        assert response.status_code == 400
        assert response['Content-Type'] == 'application/json; profile=full'
        assert b'Invalid input.' in response.content

    def test_content_type_of_not_acceptable_response(self):
        response = self.get_response('text/html')
        assert response.status_code == 406
        assert response['Content-Type'].startswith('application/json')


class RequiredParamsRenderer(BaseRenderer):
    media_type = 'application/json'
    format = 'json'
    required_media_type_params = {'profile': 'compact'}


class TestParameterAwareMatching(TestCase):
    def setUp(self):
        self.negotiator = ParameterAwareContentNegotiation()

    def select_renderer(self, accept, renderers):
        request = Request(factory.get('/', HTTP_ACCEPT=accept))
        return self.negotiator.select_renderer(request, renderers)

    def test_required_media_type_params_participate_in_matching(self):
        renderer, media_type = self.select_renderer(
            'application/json;profile="compact"', [RequiredParamsRenderer()]
        )
        assert isinstance(renderer, RequiredParamsRenderer)
        assert media_type == 'application/json; profile=compact'

    def test_required_media_type_params_mismatch(self):
        with pytest.raises(exceptions.NotAcceptable):
            self.select_renderer(
                'application/json;profile="full"', [RequiredParamsRenderer()]
            )

    def test_required_media_type_params_keys_are_case_insensitive(self):
        class UpperKeysRenderer(BaseRenderer):
            media_type = 'application/json'
            format = 'json'
            required_media_type_params = {'PROFILE': 'compact'}

        renderer, media_type = self.select_renderer(
            'application/json;profile="compact"', [UpperKeysRenderer()]
        )
        assert media_type == 'application/json; profile=compact'


class TestSplitAcceptHeader(TestCase):
    def test_quoted_comma_does_not_split(self):
        assert split_accept_header(
            'application/json;profile="a,b", text/html'
        ) == ['application/json;profile="a,b"', 'text/html']

    def test_escaped_quote_in_quotes(self):
        assert split_accept_header(
            'application/json;profile="a\\",b", text/html'
        ) == ['application/json;profile="a\\",b"', 'text/html']

    def test_empty_tokens_are_dropped(self):
        assert split_accept_header('application/json,, text/html,') == [
            'application/json', 'text/html'
        ]
