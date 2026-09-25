import asyncio
import json
import threading

import pytest
from asgiref.sync import async_to_sync
from django.test import Client
from django.urls import path

from rest_framework import generics, serializers
from rest_framework.exceptions import APIException
from rest_framework.renderers import JSONRenderer, NDJSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

# Per-test response data, set by each test before making a request.
CURRENT = {}


class RecordSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    text = serializers.CharField()


class Record:
    def __init__(self, id, text):
        self.id = id
        self.text = text


class StreamView(APIView):
    renderer_classes = [JSONRenderer, NDJSONRenderer]

    def get(self, request):
        return Response(CURRENT['data'])


class SerializedStreamView(generics.GenericAPIView):
    renderer_classes = [JSONRenderer, NDJSONRenderer]
    serializer_class = RecordSerializer

    def get(self, request):
        return Response(CURRENT['data'])


urlpatterns = [
    path('stream/', StreamView.as_view()),
    path('serialized/', SerializedStreamView.as_view()),
]


@pytest.mark.urls('tests.test_ndjson_streaming')
class TestNDJSONStreaming:
    def get(self, path='/stream/', accept='application/x-ndjson'):
        return Client().get(path, HTTP_ACCEPT=accept)

    def test_first_line_is_sent_before_source_continues(self):
        gate = threading.Event()
        produced_second = False

        def records():
            nonlocal produced_second
            yield {'id': 1, 'text': 'first'}
            if not gate.wait(timeout=10):
                raise RuntimeError('stream was consumed eagerly')
            produced_second = True
            yield {'id': 2, 'text': 'second'}

        CURRENT['data'] = records()
        try:
            response = self.get()
            assert response.status_code == 200
            assert response.streaming
            assert response['Content-Type'] == 'application/x-ndjson; charset=utf-8'

            stream = iter(response.streaming_content)
            # The first line is available even though the generator is
            # still blocked, waiting for the signal.
            assert json.loads(next(stream)) == {'id': 1, 'text': 'first'}
            assert not produced_second

            gate.set()
            remaining = [json.loads(line) for line in stream]
            assert remaining == [{'id': 2, 'text': 'second'}]
            assert produced_second
        finally:
            gate.set()

    def test_each_line_is_valid_json_with_newlines_escaped(self):
        CURRENT['data'] = iter([
            {'id': 1, 'text': 'line1\nline2'},
            {'id': 2, 'text': 'plain'},
        ])
        response = self.get()
        body = b''.join(response.streaming_content)

        lines = body.split(b'\n')
        assert lines.pop() == b''
        assert len(lines) == 2
        assert b'\\n' in lines[0]
        assert json.loads(lines[0]) == {'id': 1, 'text': 'line1\nline2'}
        assert json.loads(lines[1]) == {'id': 2, 'text': 'plain'}

    def test_list_data_is_streamed_as_ndjson(self):
        CURRENT['data'] = [{'id': 1, 'text': 'a'}, {'id': 2, 'text': 'b'}]
        response = self.get()
        assert response.streaming
        lines = [json.loads(line) for line in response.streaming_content]
        assert lines == [{'id': 1, 'text': 'a'}, {'id': 2, 'text': 'b'}]

    def test_empty_stream(self):
        CURRENT['data'] = iter([])
        response = self.get()
        assert response.status_code == 200
        assert b''.join(response.streaming_content) == b''

    def test_normal_exhaustion_closes_source(self):
        closed = False

        def records():
            nonlocal closed
            try:
                yield {'id': 1, 'text': 'a'}
            finally:
                closed = True

        CURRENT['data'] = records()
        response = self.get()
        assert [json.loads(line) for line in response.streaming_content] == [{'id': 1, 'text': 'a'}]
        assert closed

    # `response.close()` fires `request_finished`, which touches the
    # database connections, like it would on a real server.
    @pytest.mark.django_db
    def test_client_disconnect_closes_source(self):
        closed = False

        def records():
            nonlocal closed
            try:
                yield {'id': 1, 'text': 'a'}
                yield {'id': 2, 'text': 'b'}
            finally:
                closed = True

        CURRENT['data'] = records()
        response = self.get()
        stream = iter(response.streaming_content)
        assert json.loads(next(stream)) == {'id': 1, 'text': 'a'}
        assert not closed

        response.close()
        assert closed

    def test_error_before_first_record_uses_drf_exception_handling(self):
        def records():
            raise APIException('not ready')
            yield

        CURRENT['data'] = records()
        response = self.get()
        assert response.status_code == 500
        assert not response.streaming
        assert json.loads(response.content) == {'detail': 'not ready'}

    def test_error_after_first_record_terminates_stream(self):
        closed = False

        def records():
            nonlocal closed
            try:
                yield {'id': 1, 'text': 'a'}
                raise RuntimeError('boom')
            finally:
                closed = True

        CURRENT['data'] = records()
        response = self.get()
        stream = iter(response.streaming_content)
        assert json.loads(next(stream)) == {'id': 1, 'text': 'a'}

        # The original exception propagates; no fake data line is emitted.
        with pytest.raises(RuntimeError, match='boom'):
            next(stream)
        assert closed

    def test_records_are_serialized_with_the_view_serializer(self):
        CURRENT['data'] = iter([Record(1, 'a'), Record(2, 'b')])
        response = self.get('/serialized/')
        lines = [json.loads(line) for line in response.streaming_content]
        assert lines == [{'id': 1, 'text': 'a'}, {'id': 2, 'text': 'b'}]

    def test_json_negotiation_is_unchanged(self):
        CURRENT['data'] = [{'id': 1, 'text': 'a'}, {'id': 2, 'text': 'b'}]
        response = self.get(accept='application/json')
        assert not response.streaming
        assert response['Content-Type'] == 'application/json'
        assert json.loads(response.content) == [{'id': 1, 'text': 'a'}, {'id': 2, 'text': 'b'}]

    def test_default_negotiation_is_unchanged(self):
        CURRENT['data'] = [{'id': 1, 'text': 'a'}]
        response = Client().get('/stream/')
        assert not response.streaming
        assert response['Content-Type'] == 'application/json'
        assert json.loads(response.content) == [{'id': 1, 'text': 'a'}]


@pytest.mark.urls('tests.test_ndjson_streaming')
class TestAsyncNDJSONStreaming:
    def test_async_source_is_not_fully_read_upfront(self):
        pulled = []
        closed = False

        async def records():
            nonlocal closed
            try:
                for i in (1, 2, 3):
                    pulled.append(i)
                    yield {'id': i, 'text': str(i)}
            finally:
                closed = True

        CURRENT['data'] = records()
        response = Client().get('/stream/', HTTP_ACCEPT='application/x-ndjson')
        assert response.status_code == 200
        assert response.streaming
        # Only the first record was pulled eagerly, the async source was
        # not consumed up front.
        assert pulled == [1]

        async def collect():
            return [part async for part in response.streaming_content]

        chunks = async_to_sync(collect)()
        lines = [json.loads(chunk) for chunk in chunks]
        assert lines == [
            {'id': 1, 'text': '1'},
            {'id': 2, 'text': '2'},
            {'id': 3, 'text': '3'},
        ]
        assert closed

    # `response.close()` fires `request_finished`, which touches the
    # database connections, like it would on a real server.
    @pytest.mark.django_db
    def test_async_client_disconnect_releases_source(self):
        closed = False

        async def records():
            nonlocal closed
            try:
                yield {'id': 1, 'text': 'a'}
                yield {'id': 2, 'text': 'b'}
            finally:
                closed = True

        CURRENT['data'] = records()
        response = Client().get('/stream/', HTTP_ACCEPT='application/x-ndjson')

        # Drive the stream on a dedicated loop in its own thread, like an
        # ASGI server would, and keep it suspended after the first record.
        first = {}
        pulled = threading.Event()
        release = threading.Event()

        def run_stream():
            async def pull():
                first['chunk'] = await response.streaming_content.__anext__()
                pulled.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(pull())
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

        thread = threading.Thread(target=run_stream)
        thread.start()
        try:
            assert pulled.wait(timeout=5)
            assert json.loads(first['chunk']) == {'id': 1, 'text': 'a'}
            assert not closed

            # The server closes the response from a synchronous context
            # once the client connection is gone.
            response.close()
            assert closed
        finally:
            release.set()
            thread.join(timeout=5)

    def test_async_error_after_first_record_terminates_stream(self):
        closed = False

        async def records():
            nonlocal closed
            try:
                yield {'id': 1, 'text': 'a'}
                raise RuntimeError('async boom')
            finally:
                closed = True

        CURRENT['data'] = records()
        response = Client().get('/stream/', HTTP_ACCEPT='application/x-ndjson')

        async def collect():
            return [part async for part in response.streaming_content]

        with pytest.raises(RuntimeError, match='async boom'):
            async_to_sync(collect)()
        assert closed
