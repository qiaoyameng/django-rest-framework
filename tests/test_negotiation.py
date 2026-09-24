import pytest
from django.http import Http404
from django.test import TestCase

from rest_framework import exceptions, status
from rest_framework.negotiation import (
    BaseContentNegotiation, DefaultContentNegotiation,
    ParameterAwareContentNegotiation, _split_accept_header
)
from rest_framework.renderers import BaseRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.settings import api_settings
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


class MockCompactProfileRenderer(BaseRenderer):
    media_type = 'application/json; profile="compact"'
    format = 'json-compact'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return b'{"profile": "compact"}'


class MockFullProfileRenderer(BaseRenderer):
    media_type = 'application/json; profile="full"'
    format = 'json-full'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return b'{"profile": "full"}'


class MockVersionedProfileRenderer(BaseRenderer):
    media_type = 'application/json; profile="full"; version=2'
    format = 'json-full-v2'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return b'{"profile": "full", "version": 2}'


class ParameterAwareView(APIView):
    renderer_classes = [MockCompactProfileRenderer, MockFullProfileRenderer]
    content_negotiation_class = ParameterAwareContentNegotiation

    def get(self, request, format=None):
        return Response({'example': 'data'})


class ReversedParameterAwareView(APIView):
    renderer_classes = [MockFullProfileRenderer, MockCompactProfileRenderer]
    content_negotiation_class = ParameterAwareContentNegotiation

    def get(self, request, format=None):
        return Response({'example': 'data'})


class RaisingParameterAwareView(APIView):
    renderer_classes = [MockCompactProfileRenderer, MockFullProfileRenderer]
    content_negotiation_class = ParameterAwareContentNegotiation

    def get(self, request):
        raise exceptions.ValidationError({'detail': 'invalid'})


class TestParameterAwareContentNegotiation(TestCase):
    def setUp(self):
        self.renderers = [MockCompactProfileRenderer(), MockFullProfileRenderer()]
        self.negotiator = ParameterAwareContentNegotiation()

    def select_renderer(self, request):
        return self.negotiator.select_renderer(request, self.renderers)

    def test_default_negotiation_class_is_unchanged(self):
        assert api_settings.DEFAULT_CONTENT_NEGOTIATION_CLASS is DefaultContentNegotiation

    def test_missing_accept_uses_first_renderer(self):
        request = Request(factory.get('/'))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockCompactProfileRenderer)
        assert accepted_media_type == 'application/json; profile="compact"'

    def test_selects_renderer_by_profile_parameter(self):
        request = Request(factory.get('/', HTTP_ACCEPT='application/json; profile="full"'))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockFullProfileRenderer)
        assert accepted_media_type == 'application/json; profile="full"'

    def test_unquoted_profile_parameter_matches_quoted_declaration(self):
        request = Request(factory.get('/', HTTP_ACCEPT='application/json; profile=compact'))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockCompactProfileRenderer)

    def test_q_weighting_selects_highest_weighted_profile(self):
        request = Request(factory.get(
            '/',
            HTTP_ACCEPT='application/json; profile="compact"; q=0.4, '
                        'application/json; profile="full"; q=0.9',
        ))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockFullProfileRenderer)
        assert accepted_media_type == 'application/json; profile="full"'

    def test_q_weighting_reversed(self):
        request = Request(factory.get(
            '/',
            HTTP_ACCEPT='application/json; profile="compact"; q=0.9, '
                        'application/json; profile="full"; q=0.4',
        ))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockCompactProfileRenderer)

    def test_equal_weights_use_stable_server_order(self):
        request = Request(factory.get('/', HTTP_ACCEPT='application/json'))
        accepted_renderer, _ = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockCompactProfileRenderer)

        reversed_renderers = [MockFullProfileRenderer(), MockCompactProfileRenderer()]
        accepted_renderer, accepted_media_type = self.negotiator.select_renderer(
            request, reversed_renderers
        )
        assert isinstance(accepted_renderer, MockFullProfileRenderer)
        assert accepted_media_type == 'application/json; profile="full"'

    def test_q_zero_candidate_is_not_reselected_by_fallback(self):
        request = Request(factory.get(
            '/',
            HTTP_ACCEPT='application/json; profile="compact"; q=0, '
                        'application/json; q=0.5',
        ))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockFullProfileRenderer)
        assert accepted_media_type == 'application/json; profile="full"'

    def test_q_zero_candidate_is_not_reselected_by_wildcard(self):
        request = Request(factory.get(
            '/',
            HTTP_ACCEPT='application/json; profile="compact"; q=0, */*; q=0.1',
        ))
        accepted_renderer, _ = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockFullProfileRenderer)

    def test_all_q_zero_raises_not_acceptable(self):
        request = Request(factory.get(
            '/',
            HTTP_ACCEPT='application/json; profile="compact"; q=0, '
                        'application/json; profile="full"; q=0',
        ))
        with pytest.raises(exceptions.NotAcceptable):
            self.select_renderer(request)

    def test_wildcard_matches_parameterized_renderer(self):
        request = Request(factory.get('/', HTTP_ACCEPT='*/*'))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockCompactProfileRenderer)
        assert accepted_media_type == 'application/json; profile="compact"'

    def test_subtype_wildcard_matches_parameterized_renderer(self):
        request = Request(factory.get('/', HTTP_ACCEPT='application/*'))
        accepted_renderer, _ = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockCompactProfileRenderer)

    def test_more_specific_media_range_overrides_wildcard_weight(self):
        request = Request(factory.get(
            '/',
            HTTP_ACCEPT='*/*; q=0.1, application/json; profile="full"; q=0.9',
        ))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockFullProfileRenderer)
        assert accepted_media_type == 'application/json; profile="full"'

    def test_media_type_and_parameter_name_case_is_insensitive(self):
        request = Request(factory.get(
            '/', HTTP_ACCEPT='Application/JSON; PROFILE="full"'
        ))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockFullProfileRenderer)
        assert accepted_media_type == 'application/json; profile="full"'

    def test_parameter_order_does_not_matter(self):
        request = Request(factory.get(
            '/', HTTP_ACCEPT='application/json; q=0.9; profile="full"'
        ))
        accepted_renderer, _ = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockFullProfileRenderer)

    def test_multiple_renderer_parameters_all_participate(self):
        renderers = [MockVersionedProfileRenderer(), MockFullProfileRenderer()]
        request = Request(factory.get(
            '/', HTTP_ACCEPT='application/json; version=2; profile="full"'
        ))
        accepted_renderer, accepted_media_type = self.negotiator.select_renderer(
            request, renderers
        )
        assert isinstance(accepted_renderer, MockVersionedProfileRenderer)
        assert accepted_media_type == 'application/json; profile="full"; version=2'

    def test_quoted_parameter_value_with_comma_is_not_split(self):
        header = 'application/json; profile="a,b"; q=0.5, text/html'
        assert _split_accept_header(header) == [
            'application/json; profile="a,b"; q=0.5', ' text/html'
        ]

    def test_quoted_comma_does_not_break_negotiation(self):
        request = Request(factory.get(
            '/',
            HTTP_ACCEPT='application/json; profile="a,b"; q=0.1, '
                        'application/json; profile="full"; q=0.9',
        ))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockFullProfileRenderer)
        assert accepted_media_type == 'application/json; profile="full"'

    def test_invalid_accept_header_raises_not_acceptable(self):
        request = Request(factory.get('/', HTTP_ACCEPT='not-a-media-type'))
        with pytest.raises(exceptions.NotAcceptable):
            self.select_renderer(request)

    def test_invalid_q_value_is_treated_as_one(self):
        request = Request(factory.get(
            '/', HTTP_ACCEPT='application/json; profile="full"; q=banana'
        ))
        accepted_renderer, _ = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockFullProfileRenderer)

    def test_unmatched_profile_parameter_raises_not_acceptable(self):
        request = Request(factory.get(
            '/', HTTP_ACCEPT='application/json; profile="unknown"'
        ))
        with pytest.raises(exceptions.NotAcceptable):
            self.select_renderer(request)

    def test_format_query_param_filters_renderers(self):
        request = Request(factory.get('/?format=json-full'))
        accepted_renderer, accepted_media_type = self.select_renderer(request)
        assert isinstance(accepted_renderer, MockFullProfileRenderer)
        assert accepted_media_type == 'application/json; profile="full"'

    def test_format_suffix_filters_renderers(self):
        request = Request(factory.get('/'))
        accepted_renderer, accepted_media_type = self.negotiator.select_renderer(
            request, self.renderers, format_suffix='json-compact'
        )
        assert isinstance(accepted_renderer, MockCompactProfileRenderer)
        assert accepted_media_type == 'application/json; profile="compact"'

    def test_unknown_format_raises_404(self):
        request = Request(factory.get('/?format=xml'))
        with pytest.raises(Http404):
            self.select_renderer(request)


class TestParameterAwareNegotiationWithViews(TestCase):
    def test_weighted_profiles_select_highest_weight(self):
        view = ParameterAwareView.as_view()
        request = factory.get(
            '/',
            HTTP_ACCEPT='application/json; profile="compact"; q=0.4, '
                        'application/json; profile="full"; q=0.9',
        )
        response = view(request).render()
        assert response.status_code == status.HTTP_200_OK
        assert response.content == b'{"profile": "full"}'
        assert response['Content-Type'] == 'application/json; profile="full"; charset=utf-8'

    def test_content_type_matches_selected_representation(self):
        view = ParameterAwareView.as_view()
        request = factory.get('/', HTTP_ACCEPT='application/json; profile="compact"')
        response = view(request).render()
        assert response.content == b'{"profile": "compact"}'
        assert response['Content-Type'] == 'application/json; profile="compact"; charset=utf-8'

    def test_missing_accept_header_uses_first_renderer(self):
        view = ParameterAwareView.as_view()
        request = factory.get('/')
        response = view(request).render()
        assert response.status_code == status.HTTP_200_OK
        assert response['Content-Type'] == 'application/json; profile="compact"; charset=utf-8'

    def test_invalid_accept_header_returns_406(self):
        view = ParameterAwareView.as_view()
        request = factory.get('/', HTTP_ACCEPT='not-a-media-type')
        response = view(request).render()
        assert response.status_code == status.HTTP_406_NOT_ACCEPTABLE

    def test_exception_response_uses_negotiated_renderer(self):
        view = RaisingParameterAwareView.as_view()
        request = factory.get('/', HTTP_ACCEPT='application/json; profile="full"')
        response = view(request).render()
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response['Content-Type'] == 'application/json; profile="full"; charset=utf-8'

    def test_format_query_param_selects_representation(self):
        view = ParameterAwareView.as_view()
        request = factory.get('/?format=json-full')
        response = view(request).render()
        assert response.status_code == status.HTTP_200_OK
        assert response['Content-Type'] == 'application/json; profile="full"; charset=utf-8'

    def test_format_suffix_selects_representation(self):
        view = ParameterAwareView.as_view()
        request = factory.get('/')
        response = view(request, format='json-compact').render()
        assert response.status_code == status.HTTP_200_OK
        assert response['Content-Type'] == 'application/json; profile="compact"; charset=utf-8'

    def test_unknown_format_returns_404(self):
        view = ParameterAwareView.as_view()
        request = factory.get('/?format=xml')
        response = view(request).render()
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_server_order_breaks_ties_on_views(self):
        view = ReversedParameterAwareView.as_view()
        request = factory.get('/', HTTP_ACCEPT='application/json')
        response = view(request).render()
        assert response.status_code == status.HTTP_200_OK
        assert response['Content-Type'] == 'application/json; profile="full"; charset=utf-8'
