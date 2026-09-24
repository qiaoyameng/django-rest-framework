"""
Content negotiation deals with selecting an appropriate renderer given the
incoming request.  Typically this will be based on the request's Accept header.
"""
from django.http import Http404

from rest_framework import exceptions
from rest_framework.settings import api_settings
from rest_framework.utils.mediatypes import (
    _MediaType, media_type_matches, order_by_precedence
)


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


def _split_accept_header(header):
    """
    Split an ``Accept`` header into media range tokens on commas, while
    honoring quoted strings so that parameter values containing commas
    (eg ``profile="a,b"``) are not split apart.
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
    return tokens


class ParameterAwareContentNegotiation(DefaultContentNegotiation):
    """
    An opt-in content negotiation scheme that, unlike the default scheme,
    takes media type *parameters* (such as ``profile="compact"``) and
    ``q`` weightings into account.

    This allows a single endpoint to serve multiple representations of the
    same media type, for example ``application/json; profile="compact"``
    and ``application/json; profile="full"``, each backed by its own
    renderer.

    The scheme is strictly opt-in.  The default negotiation class is
    unchanged; enable this scheme per view or project-wide::

        REST_FRAMEWORK = {
            'DEFAULT_CONTENT_NEGOTIATION_CLASS':
                'rest_framework.negotiation.ParameterAwareContentNegotiation',
        }

    Matching rules
    --------------

    * Renderers declare their required parameters as part of their
      ``media_type``, eg ``media_type = 'application/json; profile="compact"'``.
      Those parameters participate in matching: a media range such as
      ``application/json; profile="full"`` will *not* match the compact
      renderer.
    * Parameters present on the client media range constrain the match -
      the renderer must declare every parameter the client asked for.
      Parameters declared by the renderer but absent from the media range
      do not prevent a match, so plain ``application/json`` or ``*/*``
      still match parameterized renderers as a fallback.
    * Matching is case-insensitive for the media type and parameter names,
      and insensitive to parameter order.  Parameter *values* remain
      case-sensitive, and quoted values are unquoted before comparison.
    * Quoted parameter values containing commas (eg ``profile="a,b"``)
      are tokenized correctly and are not split into separate media ranges.

    Weighting and precedence
    ------------------------

    * Each renderer is scored with the ``q`` value of the *most specific*
      media range that matches it.  Specificity is ordered by: explicit
      main type, explicit subtype, then number of parameters.
    * The renderer with the highest weight wins.  Ties are broken by
      stable server-side ordering: the renderer listed first in
      ``renderer_classes`` is selected.
    * A renderer whose most specific matching media range has ``q=0`` is
      not acceptable.  It is *not* resurrected by a less specific range
      such as ``*/*`` with a positive weight.
    * Media ranges with an unparseable ``q`` value are treated as ``q=1``.

    Other behavior
    --------------

    * The returned media type is always the renderer's declared
      ``media_type``, so the response ``Content-Type`` corresponds to the
      representation actually selected, including its parameters.
    * If no renderer is acceptable a ``406 Not Acceptable`` response is
      raised, exactly as with the default scheme.
    * Format suffixes (``.json``) and the ``?format=`` query parameter
      behave as with the default scheme: they restrict the candidate
      renderers up front (raising ``404`` if none match), after which
      ``Accept`` header negotiation still applies to the remaining
      candidates.
    * Exception responses are rendered with the renderer negotiated for
      the request, so error responses honor the same parameter-aware
      negotiation as successful responses.
    """

    def get_accept_list(self, request):
        """
        Given the incoming request, return a tokenized list of media
        type strings, splitting on commas that appear outside of quoted
        strings only.
        """
        header = request.headers.get('accept', '*/*')
        return [
            token.strip() for token in _split_accept_header(header)
            if token.strip()
        ]

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
        best_weight = 0.0
        # Iterate in server-side order, so that equal-weight candidates
        # resolve to the renderer listed first on the view.
        for renderer in renderers:
            weight = self.get_accept_weight(renderer, accepts)
            if weight > best_weight:
                best_renderer = renderer
                best_weight = weight

        if best_renderer is None:
            raise exceptions.NotAcceptable(available_renderers=renderers)

        # The returned media type is the renderer's declared media type,
        # so the Content-Type reflects the representation selected.
        return best_renderer, best_renderer.media_type

    def get_accept_weight(self, renderer, accepts):
        """
        Return the ``q`` weight of the most specific media range in the
        accept list that matches the renderer, or ``0`` if the renderer
        is not acceptable.
        """
        weight = 0.0
        best_specificity = None
        for accept in accepts:
            media_type = _MediaType(accept)
            if not self.media_range_matches(media_type, renderer.media_type):
                continue
            specificity = self.media_range_specificity(media_type)
            if best_specificity is None or specificity > best_specificity:
                best_specificity = specificity
                weight = self.get_q_value(media_type)
        return weight

    def media_range_matches(self, media_range, renderer_media_type):
        """
        Return ``True`` if the client media range matches the renderer's
        media type, taking declared parameters into account.
        """
        renderer_type = _MediaType(renderer_media_type)

        if media_range.main_type != '*' and media_range.main_type != renderer_type.main_type:
            return False
        if media_range.sub_type != '*' and media_range.sub_type != renderer_type.sub_type:
            return False

        # Parameters on the media range constrain the match.  Parameters
        # declared by the renderer but absent from the media range do not.
        for key, value in media_range.params.items():
            if key == 'q':
                continue
            if renderer_type.params.get(key) != value:
                return False
        return True

    def media_range_specificity(self, media_type):
        """
        Return a comparable specificity key for a media range, ordered by
        explicit main type, explicit subtype, then parameter count.
        """
        return (
            media_type.main_type != '*',
            media_type.sub_type != '*',
            len([key for key in media_type.params if key != 'q']),
        )

    def get_q_value(self, media_type):
        """
        Return the ``q`` weight for a media range, defaulting to ``1``.
        Unparseable values are treated as ``1``; values are clamped to
        the valid ``0``-``1`` range.
        """
        try:
            q = float(media_type.params.get('q', '1'))
        except ValueError:
            return 1.0
        return min(max(q, 0.0), 1.0)
