"""
Tests for the opt-in permission evaluation tracing
(`rest_framework.permission_trace`).
"""
import threading

import pytest

from rest_framework import permissions, status, views
from rest_framework.permission_trace import (
    disable_permission_tracing, enable_permission_tracing,
    get_permission_trace, get_permission_tracer
)
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

factory = APIRequestFactory()

# Shared log of every permission method call, as
# (id(request), permission class name, method name) tuples.
CALL_LOG = []

# DRF requests seen by the view, so tests can read back their traces.
CAPTURED_REQUESTS = []


class CountingPermission(permissions.BasePermission):
    view_result = True
    object_result = True
    message = 'counting denied'
    code = 'counting_denied'

    def has_permission(self, request, view):
        CALL_LOG.append((id(request), type(self).__name__, 'has_permission'))
        if isinstance(self.view_result, Exception):
            raise self.view_result
        return self.view_result

    def has_object_permission(self, request, view, obj):
        CALL_LOG.append((id(request), type(self).__name__, 'has_object_permission'))
        if isinstance(self.object_result, Exception):
            raise self.object_result
        return self.object_result


def make_permission(name, view_result=True, object_result=True,
                    message='counting denied', code='counting_denied'):
    return type(name, (CountingPermission,), {
        'view_result': view_result,
        'object_result': object_result,
        'message': message,
        'code': code,
    })


Allow = make_permission('Allow')
Deny = make_permission('Deny', view_result=False, object_result=False,
                       message='denied by leaf', code='leaf_denied')
Admin = make_permission('Admin', view_result=False, object_result=False,
                        message='admin only', code='not_admin')
Owner = make_permission('Owner')
Active = make_permission('Active')
Banned = make_permission('Banned', view_result=False, object_result=False)


def perm_path(cls):
    return '%s.%s' % (__name__, cls.__name__)


class TracedView(views.APIView):
    permission_classes = []
    trace_permissions = False
    objects_to_check = ()

    def initial(self, request, *args, **kwargs):
        if self.trace_permissions:
            enable_permission_tracing(request)
        CAPTURED_REQUESTS.append(request)
        super().initial(request, *args, **kwargs)

    def get(self, request):
        for obj in self.objects_to_check:
            self.check_object_permissions(request, obj)
        return Response({'ok': True})


def run_view(trace, permission_classes, objects_to_check=()):
    CALL_LOG.clear()
    CAPTURED_REQUESTS.clear()
    view = TracedView.as_view(
        trace_permissions=trace,
        permission_classes=permission_classes,
        objects_to_check=objects_to_check,
    )
    response = view(factory.get('/'))
    calls = [entry[1:] for entry in CALL_LOG]
    return CAPTURED_REQUESTS[0], response, calls


def leaf(cls, method, status, result, message=None, code=None):
    return {
        'type': 'permission',
        'status': status,
        'result': result,
        'permission': perm_path(cls),
        'method': method,
        'message': cls.message if message is None else message,
        'code': cls.code if code is None else code,
    }


class TestTracingDisabled:
    def test_no_trace_recorded_by_default(self):
        request, response, calls = run_view(False, [Deny])
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert get_permission_tracer(request) is None
        assert get_permission_trace(request) is None

    def test_enable_then_disable(self):
        request, response, calls = run_view(True, [Allow])
        assert response.status_code == status.HTTP_200_OK
        assert get_permission_trace(request) is not None
        disable_permission_tracing(request)
        assert get_permission_trace(request) is None


class TestTraceStructure:
    def test_complex_nesting(self):
        composed = (Admin | Owner) & ~Banned
        request, response, calls = run_view(True, [composed])
        assert response.status_code == status.HTTP_200_OK
        assert calls == [
            ('Admin', 'has_permission'),
            ('Owner', 'has_permission'),
            ('Banned', 'has_permission'),
        ]
        assert get_permission_trace(request) == {
            'phases': [
                {
                    'phase': 'view',
                    'object': None,
                    'checks': [
                        {
                            'type': 'and', 'status': 'evaluated', 'result': True,
                            'children': [
                                {
                                    'type': 'or', 'status': 'evaluated', 'result': True,
                                    'children': [
                                        leaf(Admin, 'has_permission', 'evaluated', False),
                                        leaf(Owner, 'has_permission', 'evaluated', True),
                                    ],
                                },
                                {
                                    'type': 'not', 'status': 'evaluated', 'result': True,
                                    'children': [
                                        leaf(Banned, 'has_permission', 'evaluated', False),
                                    ],
                                },
                            ],
                        },
                    ],
                },
            ],
        }

    def test_and_short_circuit_marks_branch_unevaluated(self):
        composed = Deny & (Allow | Owner)
        request, response, calls = run_view(True, [composed])
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert calls == [('Deny', 'has_permission')]
        check, = get_permission_trace(request)['phases'][0]['checks']
        assert check['type'] == 'and'
        assert check['result'] is False
        assert check['children'][0] == leaf(Deny, 'has_permission', 'evaluated', False)
        skipped = check['children'][1]
        assert skipped == {
            'type': 'or', 'status': 'unevaluated', 'result': None,
            'children': [
                leaf(Allow, 'has_permission', 'unevaluated', None),
                leaf(Owner, 'has_permission', 'unevaluated', None),
            ],
        }

    def test_or_short_circuit_marks_branch_unevaluated(self):
        composed = Allow | Deny
        request, response, calls = run_view(True, [composed])
        assert response.status_code == status.HTTP_200_OK
        assert calls == [('Allow', 'has_permission')]
        check, = get_permission_trace(request)['phases'][0]['checks']
        assert check['type'] == 'or'
        assert check['result'] is True
        assert check['children'][1]['status'] == 'unevaluated'

    def test_leaf_message_and_code_recorded(self):
        _, plain_response, _ = run_view(False, [Deny])
        request, response, calls = run_view(True, [Deny])
        assert response.status_code == status.HTTP_403_FORBIDDEN
        check, = get_permission_trace(request)['phases'][0]['checks']
        assert check['message'] == 'denied by leaf'
        assert check['code'] == 'leaf_denied'
        # Default response format is unchanged.
        assert str(response.data) == str(plain_response.data)


class ObjectWithRepr:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return '<Object %s>' % self.name


class TestObjectPermissions:
    def test_or_object_check_keeps_view_permission_rule(self):
        # OR checks the view permission of each operand before its object
        # permission; the trace must reflect that order in the object phase.
        First = make_permission('First', view_result=True, object_result=False)
        Second = make_permission('Second', view_result=True, object_result=True)
        composed = First | Second
        request, response, calls = run_view(
            True, [composed], objects_to_check=[ObjectWithRepr('one')])
        assert response.status_code == status.HTTP_200_OK
        assert calls == [
            # View phase: OR short-circuits on First.
            ('First', 'has_permission'),
            # Object phase: OR re-checks each view permission first.
            ('First', 'has_permission'),
            ('First', 'has_object_permission'),
            ('Second', 'has_permission'),
            ('Second', 'has_object_permission'),
        ]
        phases = get_permission_trace(request)['phases']
        assert [phase['phase'] for phase in phases] == ['view', 'object']
        object_phase = phases[1]
        assert object_phase['object'] == '<Object one>'
        check, = object_phase['checks']
        assert check['type'] == 'or'
        assert [child['method'] for child in check['children']] == [
            'has_permission', 'has_object_permission',
            'has_permission', 'has_object_permission',
        ]
        assert [child['result'] for child in check['children']] == [
            True, False, True, True,
        ]

    def test_or_object_check_short_circuits_view_permission(self):
        composed = Deny | Owner
        request, response, calls = run_view(
            True, [composed], objects_to_check=[ObjectWithRepr('one')])
        assert response.status_code == status.HTTP_200_OK
        # Deny.has_permission fails, so its object check is skipped.
        assert calls == [
            # View phase.
            ('Deny', 'has_permission'),
            ('Owner', 'has_permission'),
            # Object phase.
            ('Deny', 'has_permission'),
            ('Owner', 'has_permission'),
            ('Owner', 'has_object_permission'),
        ]
        phases = get_permission_trace(request)['phases']
        check, = phases[1]['checks']
        assert check['children'][0] == leaf(Deny, 'has_permission', 'evaluated', False)
        assert check['children'][1] == leaf(Deny, 'has_object_permission', 'unevaluated', None)

    def test_multiple_object_checks_record_separate_phases(self):
        objects = [ObjectWithRepr('one'), ObjectWithRepr('two')]
        request, response, calls = run_view(
            True, [Owner & Active], objects_to_check=objects)
        assert response.status_code == status.HTTP_200_OK
        phases = get_permission_trace(request)['phases']
        assert [phase['phase'] for phase in phases] == ['view', 'object', 'object']
        assert [phase['object'] for phase in phases[1:]] == [
            '<Object one>', '<Object two>']
        for phase in phases[1:]:
            check, = phase['checks']
            assert check['type'] == 'and'
            assert check['result'] is True

    def test_object_permission_denied(self):
        ObjDeny = make_permission('ObjDeny', view_result=True, object_result=False)
        request, response, calls = run_view(
            True, [ObjDeny], objects_to_check=[ObjectWithRepr('one')])
        assert response.status_code == status.HTTP_403_FORBIDDEN
        phases = get_permission_trace(request)['phases']
        assert [phase['phase'] for phase in phases] == ['view', 'object']
        check, = phases[1]['checks']
        assert check['result'] is False
        assert check['method'] == 'has_object_permission'


class TestExceptions:
    def test_view_permission_exception_propagates_and_is_recorded(self):
        Boom = make_permission('Boom', view_result=RuntimeError('boom'))
        composed = Allow & Boom
        view = TracedView.as_view(trace_permissions=True, permission_classes=[composed])
        with pytest.raises(RuntimeError, match='boom'):
            view(factory.get('/'))
        request = CAPTURED_REQUESTS.pop()
        check, = get_permission_trace(request)['phases'][0]['checks']
        assert check['type'] == 'and'
        assert check['status'] == 'raised'
        failing = check['children'][1]
        assert failing['status'] == 'raised'
        assert failing['exception'] == 'RuntimeError: boom'

    def test_object_permission_exception_propagates_and_is_recorded(self):
        Boom = make_permission('Boom', view_result=False,
                               object_result=ValueError('bad object'))
        view = TracedView.as_view(
            trace_permissions=True, permission_classes=[~Boom],
            objects_to_check=[ObjectWithRepr('one')])
        with pytest.raises(ValueError, match='bad object'):
            view(factory.get('/'))
        request = CAPTURED_REQUESTS.pop()
        phases = get_permission_trace(request)['phases']
        check, = phases[1]['checks']
        assert check['type'] == 'not'
        assert check['status'] == 'raised'
        assert check['children'][0]['status'] == 'raised'
        assert check['children'][0]['exception'] == 'ValueError: bad object'

    def test_same_exception_with_and_without_tracing(self):
        error = RuntimeError('boom')
        Boom = make_permission('Boom', view_result=error)
        raised = []
        for trace in (False, True):
            view = TracedView.as_view(trace_permissions=trace, permission_classes=[Boom])
            request = factory.get('/')
            with pytest.raises(RuntimeError) as exc_info:
                view(request)
            raised.append(exc_info.value)
        assert raised[0] is error and raised[1] is error


class TestIdenticalBehavior:
    """
    Enabling tracing must not change the allow/deny outcome, the response,
    or the exact permission call sequence.
    """

    scenarios = [
        [Allow],
        [Deny],
        [Allow & Owner],
        [Deny & Owner],
        [Owner & Deny],
        [Allow | Deny],
        [Deny | Allow],
        [Deny | Admin],
        [~Deny],
        [~Allow],
        [(Admin | Owner) & ~Banned],
        [Admin | (Owner & Active)],
        [(Allow & Deny) | (Owner & ~Banned)],
        [Allow & (Banned | (Deny & Allow))],
    ]

    @pytest.mark.parametrize('permission_classes', scenarios)
    def test_same_outcome_and_call_order(self, permission_classes):
        objects = [ObjectWithRepr('one'), ObjectWithRepr('two')]
        _, plain_response, plain_calls = run_view(
            False, permission_classes, objects_to_check=objects)
        request, traced_response, traced_calls = run_view(
            True, permission_classes, objects_to_check=objects)
        assert traced_response.status_code == plain_response.status_code
        assert str(traced_response.data) == str(plain_response.data)
        assert traced_calls == plain_calls
        # The trace accounts for every call that was made.
        assert get_permission_trace(request) is not None

    def test_no_extra_calls_when_tracing(self):
        _, _, plain_calls = run_view(False, [Owner & Active])
        run_view(True, [Owner & Active])
        assert len(CALL_LOG) == len(plain_calls)


class TestConcurrency:
    def test_traces_are_isolated_per_request(self):
        composed = (Admin | Owner) & ~Banned
        results = []
        errors = []
        barrier = threading.Barrier(17)

        def worker(trace):
            try:
                barrier.wait()
                view = TracedView.as_view(
                    trace_permissions=trace, permission_classes=[composed])
                wsgi_request = factory.get('/')
                response = view(wsgi_request)
                request = next(
                    req for req in CAPTURED_REQUESTS
                    if req._request is wsgi_request
                )
                results.append((request, response, trace))
            except Exception as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i % 2 == 0,))
            for i in range(16)
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        assert not errors
        assert len(results) == 16
        expected_calls = [
            ('Admin', 'has_permission'),
            ('Owner', 'has_permission'),
            ('Banned', 'has_permission'),
        ]
        for request, response, trace in results:
            assert response.status_code == status.HTTP_200_OK
            request_calls = [
                entry[1:] for entry in CALL_LOG if entry[0] == id(request)]
            assert request_calls == expected_calls
            if trace:
                recorded = get_permission_trace(request)
                assert recorded is not None
                assert len(recorded['phases']) == 1
                check, = recorded['phases'][0]['checks']
                assert check['type'] == 'and'
                assert check['result'] is True
            else:
                assert get_permission_trace(request) is None
