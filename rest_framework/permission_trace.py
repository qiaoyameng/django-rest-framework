"""
Opt-in, per-request tracing of permission evaluation.

When composed permissions such as `IsAdmin | (IsOwner & IsActive)` reject a
request, it can be hard to tell which operand denied it. This module provides
an *optional* recorder that captures the structure of `AND`/`OR`/`NOT`
compositions, the leaf permission checks that were actually executed (the
record follows real short-circuit evaluation and never calls a permission
method an extra time), and each leaf permission's `message`/`code`.

Branches skipped by short-circuit evaluation are marked as `unevaluated`.
View-level checks (`check_permissions`) and object-level checks
(`check_object_permissions`) are recorded as separate phases.

Tracing is strictly opt-in and scoped to a single request::

    from rest_framework import permission_trace

    class MyView(APIView):
        def initial(self, request, *args, **kwargs):
            permission_trace.enable_permission_tracing(request)
            super().initial(request, *args, **kwargs)

The recorded trace can be read from an exception handler or logging code via
`permission_trace.get_permission_trace(request)`. When tracing is not
enabled, permission evaluation behaves exactly as before and the default
response format is unchanged.
"""
from contextlib import contextmanager

TRACE_REQUEST_ATTRIBUTE = '_permission_tracer'

PHASE_VIEW = 'view'
PHASE_OBJECT = 'object'

STATUS_EVALUATED = 'evaluated'
STATUS_UNEVALUATED = 'unevaluated'
STATUS_RAISED = 'raised'


class ComposedPermission:
    """
    Marker base class for the composed permissions (`AND`, `OR`, `NOT`)
    so the tracer can tell them apart from leaf permission classes.
    """


class PermissionNode:
    """
    A single node in a permission evaluation trace.

    Leaf nodes (`kind == 'permission'`) describe one permission class check.
    Composite nodes (`kind` of `'and'`, `'or'` or `'not'`) describe a
    composed permission and hold their operands in `children`.
    """

    __slots__ = (
        'kind', 'status', 'result', 'permission', 'method',
        'message', 'code', 'exception', 'children',
    )

    def __init__(self, kind):
        self.kind = kind
        self.status = None
        self.result = None
        self.permission = None
        self.method = None
        self.message = None
        self.code = None
        self.exception = None
        self.children = []

    def to_dict(self):
        data = {
            'type': self.kind,
            'status': self.status,
            'result': self.result,
        }
        if self.kind == 'permission':
            data['permission'] = self.permission
            data['method'] = self.method
            data['message'] = self.message
            data['code'] = self.code
        else:
            data['children'] = [child.to_dict() for child in self.children]
        if self.exception is not None:
            data['exception'] = self.exception
        return data


class PhaseRecord:
    """
    The recorded checks of a single permission checking phase, either the
    view-level phase or one object-level phase (one per checked object).
    """

    __slots__ = ('phase', 'object_repr', 'roots')

    def __init__(self, phase, obj=None):
        self.phase = phase
        self.object_repr = None
        if obj is not None:
            try:
                self.object_repr = repr(obj)
            except Exception:
                self.object_repr = None
        self.roots = []

    def to_dict(self):
        return {
            'phase': self.phase,
            'object': self.object_repr,
            'checks': [node.to_dict() for node in self.roots],
        }


class PermissionTracer:
    """
    Records permission evaluation for a single request.

    The tracer mirrors the real evaluation: `evaluate()` performs the actual
    permission call exactly once and records its outcome, while
    `unevaluated()` only records the structure of branches that
    short-circuiting skipped, without calling anything.
    """

    def __init__(self):
        self.phases = []
        self._containers = []

    def _append(self, node):
        self._containers[-1].append(node)

    @contextmanager
    def trace_phase(self, phase, obj=None):
        record = PhaseRecord(phase, obj)
        self.phases.append(record)
        self._containers.append(record.roots)
        try:
            yield record
        finally:
            self._containers.pop()

    @contextmanager
    def composite(self, kind):
        node = PermissionNode(kind)
        self._append(node)
        self._containers.append(node.children)
        try:
            yield node
        except Exception:
            node.status = STATUS_RAISED
            raise
        finally:
            self._containers.pop()
        node.status = STATUS_EVALUATED

    def evaluate(self, permission, method, *args):
        """
        Call `permission.<method>(*args)` exactly once, recording the call
        and its result. Composite permissions record themselves, so they are
        simply called. Exceptions are recorded and re-raised unchanged.
        """
        if isinstance(permission, ComposedPermission):
            return getattr(permission, method)(*args)
        node = PermissionNode('permission')
        node.permission = '%s.%s' % (
            type(permission).__module__, type(permission).__qualname__)
        node.method = method
        node.message = getattr(permission, 'message', None)
        node.code = getattr(permission, 'code', None)
        self._append(node)
        try:
            result = getattr(permission, method)(*args)
        except Exception as exc:
            node.status = STATUS_RAISED
            node.exception = '%s: %s' % (type(exc).__name__, exc)
            raise
        node.status = STATUS_EVALUATED
        node.result = bool(result)
        return result

    def unevaluated(self, permission, method):
        """
        Record the structure of a branch that short-circuit evaluation
        skipped, without calling any permission method.
        """
        self._append(self._unevaluated_node(permission, method))

    def _unevaluated_node(self, permission, method):
        if isinstance(permission, ComposedPermission):
            node = PermissionNode(type(permission).__name__.lower())
            node.status = STATUS_UNEVALUATED
            node.children.append(self._unevaluated_node(permission.op1, method))
            op2 = getattr(permission, 'op2', None)
            if op2 is not None:
                node.children.append(self._unevaluated_node(op2, method))
            return node
        node = PermissionNode('permission')
        node.permission = '%s.%s' % (
            type(permission).__module__, type(permission).__qualname__)
        node.method = method
        node.message = getattr(permission, 'message', None)
        node.code = getattr(permission, 'code', None)
        node.status = STATUS_UNEVALUATED
        return node

    def to_dict(self):
        return {'phases': [phase.to_dict() for phase in self.phases]}


def enable_permission_tracing(request):
    """
    Enable permission evaluation tracing for the given request and return
    the tracer. Any subsequent permission checks made for this request are
    recorded.
    """
    tracer = PermissionTracer()
    setattr(request, TRACE_REQUEST_ATTRIBUTE, tracer)
    return tracer


def disable_permission_tracing(request):
    """
    Disable permission evaluation tracing for the given request, discarding
    any trace recorded so far.
    """
    if hasattr(request, TRACE_REQUEST_ATTRIBUTE):
        delattr(request, TRACE_REQUEST_ATTRIBUTE)


def get_permission_tracer(request):
    """
    Return the active `PermissionTracer` for the request, or `None` if
    tracing is not enabled.
    """
    return getattr(request, TRACE_REQUEST_ATTRIBUTE, None)


def get_permission_trace(request):
    """
    Return the recorded permission evaluation trace for the request as a
    plain dict, or `None` if tracing is not enabled. Intended for use from
    exception handlers and logging.
    """
    tracer = get_permission_tracer(request)
    if tracer is None:
        return None
    return tracer.to_dict()
