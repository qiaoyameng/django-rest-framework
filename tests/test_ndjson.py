import asyncio
import json
import threading

import pytest
from django.http import StreamingHttpResponse
from django.test import SimpleTestCase, TestCase

from rest_framework import generics, serializers
from rest_framework.renderers import JSONRenderer, NDJSONRenderer
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView
from tests.models import BasicModel

factory = APIRequestFactory()

NDJSON = 'application/x-ndjson'


class ItemSerializer(serializers.Serializer):
    name = serializers.CharField()


class Item:
    def __init__(self, name):
        self.name = name


class ClosableIterator:
    """Iterator recording whether `.close()` has been called."""

    def __init__(self, items):
        self._iterator = iter(items)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iterator)

    def close(self):
        self.closed = True


class DictStreamView(APIView):
    """View returning an iterable of already-serializable data."""
    renderer_classes = [JSONRenderer, NDJSONRenderer]

    def get(self, request):
        return Response(self.get_stream_source())

    def get_stream_source(self):
        raise NotImplementedError


class SerializerStreamView(generics.GenericAPIView):
    """Generic view returning an iterable of objects to serialize."""
    renderer_classes = [JSONRenderer, NDJSONRenderer]
    serializer_class = ItemSerializer

    def get(self, request):
        return Response(self.get_stream_source())

    def get_stream_source(self):
        raise NotImplementedError


def dict_view(source):
    class View(DictStreamView):
        def get_stream_source(self):
            return source
    return View.as_view()


def serializer_view(source):
    class View(SerializerStreamView):
        def get_stream_source(self):
            return source
    return View.as_view()


def ndjson_request(**kwargs):
    return factory.get('/', HTTP_ACCEPT=NDJSON, **kwargs)


async def read_one(stream):
    return await stream.__anext__()


async def read_rest(stream):
    return [line async for line in stream]


def read_lines(response):
    return [json.loads(line) for line in response.streaming_content]


class DictListView(APIView):
    renderer_classes = [JSONRenderer, NDJSONRenderer]

    def get(self, request):
        return Response([{'name': 'a'}, {'name': 'b'}])


class NegotiationTests(SimpleTestCase):
    def test_accept_json_returns_a_regular_json_response(self):
        response = DictListView.as_view()(factory.get('/', HTTP_ACCEPT='application/json'))
        assert not isinstance(response, StreamingHttpResponse)
        assert response.accepted_renderer.media_type == 'application/json'
        response.render()
        assert response.content == b'[{"name":"a"},{"name":"b"}]'

    def test_default_accept_uses_json(self):
        response = DictListView.as_view()(factory.get('/'))
        assert not isinstance(response, StreamingHttpResponse)
        assert isinstance(response.accepted_renderer, JSONRenderer)
        assert not isinstance(response.accepted_renderer, NDJSONRenderer)

    def test_accept_ndjson_returns_a_streaming_response(self):
        response = DictListView.as_view()(ndjson_request())
        assert isinstance(response, StreamingHttpResponse)
        assert response['Content-Type'] == 'application/x-ndjson; charset=utf-8'
        assert read_lines(response) == [{'name': 'a'}, {'name': 'b'}]

    def test_format_query_parameter_selects_ndjson(self):
        response = DictListView.as_view()(factory.get('/?format=ndjson'))
        assert isinstance(response, StreamingHttpResponse)
        assert read_lines(response) == [{'name': 'a'}, {'name': 'b'}]

    def test_format_query_parameter_selects_json(self):
        response = DictListView.as_view()(factory.get('/?format=json'))
        assert not isinstance(response, StreamingHttpResponse)

    def test_status_and_headers_are_preserved(self):
        class View(APIView):
            renderer_classes = [NDJSONRenderer]

            def get(self, request):
                return Response(
                    iter([{'name': 'a'}]), status=201, headers={'X-Extra': 'yes'}
                )

        response = View.as_view()(ndjson_request())
        assert isinstance(response, StreamingHttpResponse)
        assert response.status_code == 201
        assert response['X-Extra'] == 'yes'
        assert response['Content-Type'] == 'application/x-ndjson; charset=utf-8'


class NDJSONRendererTests(SimpleTestCase):
    def test_render_single_object_as_one_line(self):
        assert NDJSONRenderer().render({'a': 1}) == b'{"a":1}\n'

    def test_render_none(self):
        assert NDJSONRenderer().render(None) == b''

    def test_newlines_in_data_are_escaped(self):
        rendered = NDJSONRenderer().render({'name': 'a\nb'})
        assert rendered == b'{"name":"a\\nb"}\n'
        # The only raw newline is the line terminator.
        assert rendered.count(b'\n') == 1
        assert json.loads(rendered) == {'name': 'a\nb'}

    def test_unicode_line_separators_are_escaped(self):
        rendered = NDJSONRenderer().render({'name': 'a\u2028\u2029b'})
        assert rendered == b'{"name":"a\\u2028\\u2029b"}\n'

    def test_each_line_is_valid_json(self):
        view = dict_view(iter([{'name': 'x\ny'}, {'name': 'z'}]))
        response = view(ndjson_request())
        content = b''.join(response.streaming_content)
        lines = content.split(b'\n')
        assert lines[-1] == b''
        assert len(lines) == 3
        assert [json.loads(line) for line in lines[:-1]] == [
            {'name': 'x\ny'}, {'name': 'z'}
        ]


class StreamingTests(TestCase):
    def test_first_line_is_sent_before_second_item_is_requested(self):
        second_requested = threading.Event()
        allow_second = threading.Event()

        def gen():
            yield {'name': 'first'}
            # The client must have received the first line before the
            # generator is asked for the second item.
            second_requested.set()
            assert allow_second.wait(timeout=5)
            yield {'name': 'second'}

        response = dict_view(gen())(ndjson_request())
        assert isinstance(response, StreamingHttpResponse)

        stream = iter(response.streaming_content)
        assert json.loads(next(stream)) == {'name': 'first'}
        assert not second_requested.is_set()

        allow_second.set()
        assert [json.loads(line) for line in stream] == [{'name': 'second'}]

    def test_serializer_is_called_per_record(self):
        view = serializer_view(iter([Item('a'), Item('b')]))
        response = view(ndjson_request())
        assert read_lines(response) == [{'name': 'a'}, {'name': 'b'}]

    def test_iterator_is_closed_on_exhaustion(self):
        iterator = ClosableIterator([{'name': 'a'}, {'name': 'b'}])
        response = dict_view(iterator)(ndjson_request())
        assert read_lines(response) == [{'name': 'a'}, {'name': 'b'}]
        assert iterator.closed

    def test_iterator_is_closed_on_client_disconnect(self):
        iterator = ClosableIterator([{'name': 'a'}, {'name': 'b'}, {'name': 'c'}])
        response = dict_view(iterator)(ndjson_request())
        stream = iter(response.streaming_content)
        assert json.loads(next(stream)) == {'name': 'a'}
        # The client goes away after the first line.
        response.close()
        assert iterator.closed

    def test_iterator_is_closed_when_first_item_fails(self):
        iterator = ClosableIterator([object()])
        view = dict_view(iterator)
        with pytest.raises(TypeError):
            view(ndjson_request())
        assert iterator.closed

    def test_midstream_source_error_propagates_and_closes(self):
        class ExplodingIterator:
            def __init__(self):
                self.closed = False
                self._step = 0

            def __iter__(self):
                return self

            def __next__(self):
                self._step += 1
                if self._step == 1:
                    return {'name': 'ok'}
                raise RuntimeError('boom')

            def close(self):
                self.closed = True

        iterator = ExplodingIterator()
        response = dict_view(iterator)(ndjson_request())
        stream = iter(response.streaming_content)
        assert json.loads(next(stream)) == {'name': 'ok'}
        # The original exception terminates the stream; no error object is
        # inserted into the stream.
        with pytest.raises(RuntimeError, match='boom'):
            next(stream)
        assert iterator.closed

    def test_midstream_serialization_error_propagates_and_closes(self):
        iterator = ClosableIterator([{'name': 'ok'}, object()])
        response = dict_view(iterator)(ndjson_request())
        stream = iter(response.streaming_content)
        assert json.loads(next(stream)) == {'name': 'ok'}
        with pytest.raises(TypeError):
            next(stream)
        assert iterator.closed

    def test_empty_source_streams_nothing(self):
        iterator = ClosableIterator([])
        response = dict_view(iterator)(ndjson_request())
        assert isinstance(response, StreamingHttpResponse)
        assert list(response.streaming_content) == []
        assert iterator.closed


class BrokenSerializer(serializers.Serializer):
    def to_representation(self, instance):
        raise serializers.ValidationError('broken')


class BrokenSerializerView(generics.GenericAPIView):
    renderer_classes = [JSONRenderer, NDJSONRenderer]
    serializer_class = BrokenSerializer

    def get(self, request):
        return Response(iter([object()]))


class ErrorHandlingTests(SimpleTestCase):
    def test_api_exception_before_first_record_uses_exception_handling(self):
        response = BrokenSerializerView.as_view()(ndjson_request())
        assert not isinstance(response, StreamingHttpResponse)
        assert response.status_code == 400
        response.render()
        assert response.content == b'["broken"]\n'

    def test_unserializable_first_item_propagates(self):
        view = dict_view(iter([object()]))
        with pytest.raises(TypeError):
            view(ndjson_request())


class CountingSerializer(serializers.ModelSerializer):
    calls = 0

    class Meta:
        model = BasicModel
        fields = ('id', 'text')

    def to_representation(self, instance):
        CountingSerializer.calls += 1
        return super().to_representation(instance)


class QuerySetStreamView(generics.GenericAPIView):
    renderer_classes = [JSONRenderer, NDJSONRenderer]
    serializer_class = CountingSerializer
    queryset = BasicModel.objects.all()

    def get(self, request):
        return Response(self.get_queryset())


class QuerySetStreamTests(TestCase):
    def setUp(self):
        CountingSerializer.calls = 0
        for text in ('one', 'two', 'three'):
            BasicModel.objects.create(text=text)

    def test_queryset_is_serialized_one_record_at_a_time(self):
        response = QuerySetStreamView.as_view()(ndjson_request())
        assert isinstance(response, StreamingHttpResponse)
        # Only the first record has been serialized so far.
        assert CountingSerializer.calls == 1

        stream = iter(response.streaming_content)
        assert json.loads(next(stream))['text'] == 'one'
        assert CountingSerializer.calls == 1

        rest = [json.loads(line) for line in stream]
        assert [line['text'] for line in rest] == ['two', 'three']
        assert CountingSerializer.calls == 3


class AsyncStreamTests(TestCase):
    """
    Async sources are consumed through asynchronous iteration, one item at
    a time, without being read into a list first.
    """

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def test_async_source_is_consumed_lazily(self):
        second_requested = threading.Event()
        allow_second = threading.Event()
        source_closed = threading.Event()

        async def gen():
            try:
                yield {'name': 'first'}
                # The client must have received the first line before the
                # source is asked for the second item.
                second_requested.set()
                assert allow_second.wait(timeout=5)
                yield {'name': 'second'}
            finally:
                source_closed.set()

        response = dict_view(gen())(ndjson_request())
        assert isinstance(response, StreamingHttpResponse)
        assert response.is_async

        stream = response.streaming_content
        first = self.loop.run_until_complete(read_one(stream))
        assert json.loads(first) == {'name': 'first'}
        assert not second_requested.is_set()

        allow_second.set()
        rest = self.loop.run_until_complete(read_rest(stream))
        assert [json.loads(line) for line in rest] == [{'name': 'second'}]
        # Exhaustion released the source.
        assert source_closed.is_set()

    def test_async_source_is_released_on_client_disconnect(self):
        source_closed = threading.Event()

        async def gen():
            try:
                yield {'name': 'first'}
                yield {'name': 'second'}
            finally:
                source_closed.set()

        response = dict_view(gen())(ndjson_request())
        stream = response.streaming_content
        first = self.loop.run_until_complete(read_one(stream))
        assert json.loads(first) == {'name': 'first'}
        # The client goes away after the first line.
        response.close()
        assert source_closed.is_set()

    def test_async_source_is_released_on_serialization_error(self):
        source_closed = threading.Event()

        async def gen():
            try:
                yield {'name': 'ok'}
                yield object()
            finally:
                source_closed.set()

        response = dict_view(gen())(ndjson_request())
        stream = response.streaming_content
        first = self.loop.run_until_complete(read_one(stream))
        assert json.loads(first) == {'name': 'ok'}
        with pytest.raises(TypeError):
            self.loop.run_until_complete(read_one(stream))
        assert source_closed.is_set()

    def test_async_first_item_error_uses_exception_handling(self):
        source_closed = threading.Event()

        async def gen():
            try:
                yield object()
            finally:
                source_closed.set()

        with pytest.raises(TypeError):
            dict_view(gen())(ndjson_request())
        assert source_closed.is_set()

    def test_async_empty_source_streams_nothing(self):
        async def gen():
            return
            yield

        response = dict_view(gen())(ndjson_request())
        assert isinstance(response, StreamingHttpResponse)
        rest = self.loop.run_until_complete(read_rest(response.streaming_content))
        assert rest == []
