"""
Content negotiation deals with selecting an appropriate renderer given the
incoming request.  Typically this will be based on the request's Accept header.
"""
import re

from django.http import Http404
from django.utils.http import parse_header_parameters

from rest_framework import exceptions
from rest_framework.settings import api_settings
from rest_framework.utils.mediatypes import (
    _MediaType, media_type_matches, order_by_precedence
)

TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def split_accept_header(header):
    """
    Split an `Accept` header into individual media range tokens.

    Quoted strings are taken into account, so that a comma inside a quoted
    parameter value (eg `profile="a,b"`) does not split a media range in two.
    """
    tokens = []
    token = []
    in_quotes = False
    escaped = False
    for char in header:
        if escaped:
            token.append(char)
            escaped = False
        elif in_quotes and char == '\\':
            token.append(char)
            escaped = True
        elif char == '"':
            token.append(char)
            in_quotes = not in_quotes
        elif char == ',' and not in_quotes:
            tokens.append(''.join(token))
            token = []
        else:
            token.append(char)
    tokens.append(''.join(token))
    return [token.strip() for token in tokens if token.strip()]


class AcceptMediaRange:
    """
    A single media range parsed from an `Accept` header, as used by
    `ParameterAwareContentNegotiation`.
    """
    __slots__ = ('media_type', 'params', 'q', 'position', 'specificity')

    def __init__(self, media_type, params, q, position):
        self.media_type = media_type
        self.params = params
        self.q = q
        self.position = position
        main_type, _, sub_type = media_type.partition('/')
        self.specificity = (main_type != '*', sub_type != '*', len(params))

    def __repr__(self):
        return '<AcceptMediaRange %s q=%s>' % (self, self.q)

    def __str__(self):
        ret = self.media_type
        for key, value in self.params.items():
            ret += '; %s=%s' % (key, value)
        return ret


class BaseContentNegotiation:
    def select_parser(self, request, parsers):
        raise NotImplementedError('.select_parser() must be implemented')

    def select_renderer(self, request, renderers, format_suffix=None):
        raise NotImplementedError('.select_renderer() must be implemented')


class DefaultContentNegotiation(BaseContentNegotiation):
    settings = api_settings

    def select_parser(self, request, parsers):
        """
        Given a list of parsers and a media type, return the appropriate
        parser to handle the incoming request.
        """
        for parser in parsers:
            if media_type_matches(parser.media_type, request.content_type):
                return parser
        return None

    def select_renderer(self, request, renderers, format_suffix=None):
        """
        Given a request and a list of renderers, return a two-tuple of:
        (renderer, media type).
        """
        # Allow URL style format override.  eg. "?format=json
        format_query_param = self.settings.URL_FORMAT_OVERRIDE
        format = format_suffix or request.query_params.get(format_query_param)

        if format:
            renderers = self.filter_renderers(renderers, format)

        accepts = self.get_accept_list(request)

        # Check the acceptable media types against each renderer,
        # attempting more specific media types first
        # NB. The inner loop here isn't as bad as it first looks :)
        #     Worst case is we're looping over len(accept_list) * len(self.renderers)
        for media_type_set in order_by_precedence(accepts):
            for renderer in renderers:
                for media_type in media_type_set:
                    if media_type_matches(renderer.media_type, media_type):
                        # Return the most specific media type as accepted.
                        media_type_wrapper = _MediaType(media_type)
                        if (
                            _MediaType(renderer.media_type).precedence >
                            media_type_wrapper.precedence
                        ):
                            # Eg client requests '*/*'
                            # Accepted media type is 'application/json'
                            full_media_type = ';'.join(
                                (renderer.media_type,) +
                                tuple(
                                    f'{key}={value}'
                                    for key, value in media_type_wrapper.params.items()
                                )
                            )
                            return renderer, full_media_type
                        else:
                            # Eg client requests 'application/json; indent=8'
                            # Accepted media type is 'application/json; indent=8'
                            return renderer, media_type

        raise exceptions.NotAcceptable(available_renderers=renderers)

    def filter_renderers(self, renderers, format):
        """
        If there is a '.json' style format suffix, filter the renderers
        so that we only negotiation against those that accept that format.
        """
        renderers = [renderer for renderer in renderers
                     if renderer.format == format]
        if not renderers:
            raise Http404
        return renderers

    def get_accept_list(self, request):
        """
        Given the incoming request, return a tokenized list of media
        type strings.
        """
        header = request.headers.get('accept', '*/*')
        return [token.strip() for token in header.split(',')]


class ParameterAwareContentNegotiation(DefaultContentNegotiation):
    """
    An opt-in content negotiation scheme that, unlike the default scheme,
    takes media type parameters and `q` weightings into account.

    Renderers may declare the media type parameters they satisfy, either by
    including them in their `.media_type` attribute or by setting a
    `.required_media_type_params` dictionary.  For example, to serve two
    JSON profiles from a single endpoint:

        class CompactProfileRenderer(JSONRenderer):
            required_media_type_params = {'profile': 'compact'}

        class FullProfileRenderer(JSONRenderer):
            required_media_type_params = {'profile': 'full'}

    The renderer with the highest weighted (`q`) matching media range is
    selected.  Media ranges that do not specify a parameter match renderers
    requiring any value of that parameter, so a plain `application/json`
    request still matches a `profile="compact"` renderer.  When several
    renderers share the highest weight, the renderer listed first on the
    view wins, keeping the server-side ordering stable.

    Media ranges with `q=0` are treated as "not acceptable" and are never
    selected, even as a fallback.  If no renderer is acceptable a
    `406 Not Acceptable` response is returned.

    The returned media type always includes the selected renderer's
    required parameters, so the `Content-Type` of the response corresponds
    to the representation that is actually returned.
    """

    def select_renderer(self, request, renderers, format_suffix=None):
        """
        Given a request and a list of renderers, return a two-tuple of:
        (renderer, media type).
        """
        # Allow URL style format override.  eg. "?format=json
        format_query_param = self.settings.URL_FORMAT_OVERRIDE
        format = format_suffix or request.query_params.get(format_query_param)

        if format:
            renderers = self.filter_renderers(renderers, format)

        accepts = self.get_accept_list(request)

        best_renderer = None
        best_media_range = None
        for renderer in renderers:
            media_range = self.get_best_media_range(renderer, accepts)
            if media_range is None or media_range.q <= 0:
                # `q=0` means "not acceptable" - never fall back to it.
                continue
            if best_media_range is None or media_range.q > best_media_range.q:
                best_renderer = renderer
                best_media_range = media_range

        if best_renderer is None:
            raise exceptions.NotAcceptable(available_renderers=renderers)

        return best_renderer, self.get_accepted_media_type(best_renderer, best_media_range)

    def get_accept_list(self, request):
        """
        Given the incoming request, return a list of `AcceptMediaRange`
        instances, one for each valid media range in the `Accept` header.

        Media ranges that cannot be parsed are ignored.  If the header is
        missing or contains no valid media ranges, a single `*/*` range is
        returned, treating the client as accepting any media type.
        """
        header = request.headers.get('accept', '')
        accepts = []
        for position, token in enumerate(split_accept_header(header)):
            media_range = self.parse_media_range(token, position)
            if media_range is not None:
                accepts.append(media_range)
        if not accepts:
            accepts = [AcceptMediaRange('*/*', {}, 1.0, 0)]
        return accepts

    def parse_media_range(self, token, position):
        """
        Parse a single `Accept` header token into an `AcceptMediaRange`,
        returning `None` if the token is not a valid media range.
        """
        media_type, params = parse_header_parameters(token)
        main_type, _, sub_type = media_type.partition('/')
        if not main_type or not sub_type:
            return None
        q = params.pop('q', '1')
        try:
            q = float(q)
        except (TypeError, ValueError):
            return None
        if not 0 <= q <= 1:
            return None
        return AcceptMediaRange(media_type, params, q, position)

    def get_renderer_media_type_params(self, renderer):
        """
        Return a two-tuple of (media type, parameters) for a renderer,
        combining any parameters declared in its `.media_type` attribute
        with its `.required_media_type_params` dictionary.
        """
        media_type, params = parse_header_parameters(renderer.media_type or '')
        required = getattr(renderer, 'required_media_type_params', None) or {}
        for key, value in required.items():
            params[key.lower()] = self.normalize_param_value(value)
        return media_type, params

    def get_best_media_range(self, renderer, accepts):
        """
        Return the most specific media range that matches the renderer,
        or `None` if no media range matches.
        """
        media_type, params = self.get_renderer_media_type_params(renderer)
        best = None
        for media_range in accepts:
            if not self.media_range_matches(media_range, media_type, params):
                continue
            if best is None or media_range.specificity > best.specificity:
                best = media_range
        return best

    @staticmethod
    def media_range_matches(media_range, media_type, params):
        """
        Return `True` if the media range matches a renderer with the given
        media type and required parameters.

        Parameters present in both the media range and the renderer must
        have equal values.  Parameters declared by only one of the two do
        not prevent a match, so a generic range such as `application/json`
        matches renderers requiring any parameter values.
        """
        range_main_type, _, range_sub_type = media_range.media_type.partition('/')
        main_type, _, sub_type = media_type.partition('/')
        if range_main_type != '*' and range_main_type != main_type:
            return False
        if range_sub_type != '*' and range_sub_type != sub_type:
            return False
        for key, value in media_range.params.items():
            if key in params and params[key] != value:
                return False
        return True

    def get_accepted_media_type(self, renderer, media_range):
        """
        Build the media type for the response, based on the selected
        renderer's media type and required parameters, plus any additional
        parameters supplied by the client's media range.
        """
        media_type, params = self.get_renderer_media_type_params(renderer)
        accepted = dict(params)
        for key, value in media_range.params.items():
            if key not in accepted:
                accepted[key] = value
        return media_type + ''.join(
            '; %s=%s' % (key, self.format_param_value(value))
            for key, value in accepted.items()
        )

    @staticmethod
    def normalize_param_value(value):
        value = str(value).strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        return value

    @staticmethod
    def format_param_value(value):
        if TOKEN_RE.match(value):
            return value
        return '"%s"' % value.replace('\\', '\\\\').replace('"', '\\"')
