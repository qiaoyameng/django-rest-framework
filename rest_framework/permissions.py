"""
Provides a set of pluggable permission policies.
"""
from dataclasses import dataclass, field

from django.http import Http404

from rest_framework import exceptions

SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')

UNEVALUATED = 'unevaluated'
EVALUATED = 'evaluated'
ERROR = 'error'


@dataclass
class PermissionEvaluation:
    kind: str
    stage: str | None = None
    permission: str | None = None
    status: str = UNEVALUATED
    result: bool | None = None
    value: object = None
    message: str | None = None
    code: str | None = None
    order: int | None = None
    exception: Exception | None = None
    synthetic: bool = False
    children: list['PermissionEvaluation'] = field(default_factory=list)

    def to_dict(self):
        exception = None
        if self.exception is not None:
            exception = {
                'type': self.exception.__class__.__name__,
                'message': str(self.exception),
            }

        return {
            'kind': self.kind,
            'stage': self.stage,
            'permission': self.permission,
            'status': self.status,
            'result': self.result,
            'value': self.value,
            'message': self.message,
            'code': self.code,
            'order': self.order,
            'exception': exception,
            'synthetic': self.synthetic,
            'children': [child.to_dict() for child in self.children],
        }


@dataclass
class PermissionStageRecord:
    stage: str
    checks: list[PermissionEvaluation]
    object_repr: str | None = None

    def to_dict(self):
        data = {
            'stage': self.stage,
            'checks': [check.to_dict() for check in self.checks],
        }
        if self.object_repr is not None:
            data['object'] = self.object_repr
        return data


class PermissionRecorder:
    def __init__(self):
        self.records: list[PermissionStageRecord] = []
        self._next_order = 0

    def start_view_check(self, permissions):
        stage = PermissionStageRecord(
            stage='view',
            checks=[
                self._build_node(permission, 'view')
                for permission in permissions
            ],
        )
        self.records.append(stage)
        return stage

    def start_object_check(self, permissions, obj):
        stage = PermissionStageRecord(
            stage='object',
            object_repr=repr(obj),
            checks=[
                self._build_node(permission, 'object')
                for permission in permissions
            ],
        )
        self.records.append(stage)
        return stage

    def evaluate_permission(self, permission, request, view):
        node = self._build_node(permission, 'view')
        self.records.append(PermissionStageRecord(stage='view', checks=[node]))
        return self._evaluate(permission, node, 'view', request, view)

    def evaluate_object_permission(self, permission, request, view, obj):
        node = self._build_node(permission, 'object')
        self.records.append(
            PermissionStageRecord(stage='object', object_repr=repr(obj), checks=[node])
        )
        return self._evaluate(permission, node, 'object', request, view, obj)

    def evaluate(self, permission, node, stage, request, view, obj=None):
        return self._evaluate(permission, node, stage, request, view, obj)

    def _build_node(self, permission, stage):
        if isinstance(permission, AND):
            return PermissionEvaluation(
                kind='AND',
                stage=stage,
                children=[
                    self._build_node(permission.op1, stage),
                    self._build_node(permission.op2, stage),
                ],
            )
        if isinstance(permission, NOT):
            return PermissionEvaluation(
                kind='NOT',
                stage=stage,
                children=[self._build_node(permission.op1, stage)],
            )
        if isinstance(permission, OR):
            if stage == 'view':
                children = [
                    self._build_node(permission.op1, 'view'),
                    self._build_node(permission.op2, 'view'),
                ]
            else:
                children = [
                    self._build_or_object_branch(permission.op1),
                    self._build_or_object_branch(permission.op2),
                ]
            return PermissionEvaluation(kind='OR', stage=stage, children=children)

        return PermissionEvaluation(
            kind='PERMISSION',
            stage=stage,
            permission=self._permission_name(permission),
            message=getattr(permission, 'message', None),
            code=getattr(permission, 'code', None),
        )

    def _build_or_object_branch(self, operand):
        return PermissionEvaluation(
            kind='AND',
            stage='object',
            synthetic=True,
            children=[
                self._build_node(operand, 'view'),
                self._build_node(operand, 'object'),
            ],
        )

    def _permission_name(self, permission):
        permission_class = type(permission)
        return f'{permission_class.__module__}.{permission_class.__qualname__}'

    def _evaluate(self, permission, node, stage, request, view, obj=None):
        try:
            value = self._dispatch(permission, node, stage, request, view, obj)
        except Exception as exc:
            node.status = ERROR
            node.exception = exc
            raise
        node.status = EVALUATED
        node.value = value
        node.result = bool(value)
        return value

    def _dispatch(self, permission, node, stage, request, view, obj):
        if node.kind == 'PERMISSION':
            return self._evaluate_leaf(permission, node, stage, request, view, obj)
        if node.kind == 'NOT':
            return not self._evaluate(
                permission.op1, node.children[0], stage, request, view, obj
            )
        if node.kind == 'AND':
            left = self._evaluate(
                permission.op1, node.children[0], stage, request, view, obj
            )
            if not left:
                return left
            return self._evaluate(
                permission.op2, node.children[1], stage, request, view, obj
            )
        if stage == 'view':
            left = self._evaluate(
                permission.op1, node.children[0], 'view', request, view
            )
            if left:
                return left
            return self._evaluate(
                permission.op2, node.children[1], 'view', request, view
            )

        first_branch = self._evaluate_or_object_branch(
            permission.op1, node.children[0], request, view, obj
        )
        if first_branch:
            return first_branch
        return self._evaluate_or_object_branch(
            permission.op2, node.children[1], request, view, obj
        )

    def _evaluate_or_object_branch(self, operand, node, request, view, obj):
        try:
            view_result = self._evaluate(
                operand, node.children[0], 'view', request, view
            )
            if not view_result:
                value = view_result
            else:
                value = self._evaluate(
                    operand, node.children[1], 'object', request, view, obj
                )
        except Exception as exc:
            node.status = ERROR
            node.exception = exc
            raise
        node.status = EVALUATED
        node.value = value
        node.result = bool(value)
        return value

    def _evaluate_leaf(self, permission, node, stage, request, view, obj):
        node.order = self._next_order
        self._next_order += 1
        if stage == 'view':
            value = permission.has_permission(request, view)
        else:
            value = permission.has_object_permission(request, view, obj)
        node.message = getattr(permission, 'message', None)
        node.code = getattr(permission, 'code', None)
        return value

    def to_dict(self):
        return [record.to_dict() for record in self.records]


class OperationHolderMixin:
    def __and__(self, other):
        return OperandHolder(AND, self, other)

    def __or__(self, other):
        return OperandHolder(OR, self, other)

    def __rand__(self, other):
        return OperandHolder(AND, other, self)

    def __ror__(self, other):
        return OperandHolder(OR, other, self)

    def __invert__(self):
        return SingleOperandHolder(NOT, self)


class SingleOperandHolder(OperationHolderMixin):
    def __init__(self, operator_class, op1_class):
        self.operator_class = operator_class
        self.op1_class = op1_class

    def __call__(self, *args, **kwargs):
        op1 = self.op1_class(*args, **kwargs)
        return self.operator_class(op1)


class OperandHolder(OperationHolderMixin):
    def __init__(self, operator_class, op1_class, op2_class):
        self.operator_class = operator_class
        self.op1_class = op1_class
        self.op2_class = op2_class

    def __call__(self, *args, **kwargs):
        op1 = self.op1_class(*args, **kwargs)
        op2 = self.op2_class(*args, **kwargs)
        return self.operator_class(op1, op2)

    def __eq__(self, other):
        return (
            isinstance(other, OperandHolder) and
            self.operator_class == other.operator_class and
            self.op1_class == other.op1_class and
            self.op2_class == other.op2_class
        )

    def __hash__(self):
        return hash((self.operator_class, self.op1_class, self.op2_class))


class AND:
    def __init__(self, op1, op2):
        self.op1 = op1
        self.op2 = op2

    def has_permission(self, request, view):
        return (
            self.op1.has_permission(request, view) and
            self.op2.has_permission(request, view)
        )

    def has_object_permission(self, request, view, obj):
        return (
            self.op1.has_object_permission(request, view, obj) and
            self.op2.has_object_permission(request, view, obj)
        )


class OR:
    def __init__(self, op1, op2):
        self.op1 = op1
        self.op2 = op2

    def has_permission(self, request, view):
        return (
            self.op1.has_permission(request, view) or
            self.op2.has_permission(request, view)
        )

    def has_object_permission(self, request, view, obj):
        return (
            self.op1.has_permission(request, view)
            and self.op1.has_object_permission(request, view, obj)
        ) or (
            self.op2.has_permission(request, view)
            and self.op2.has_object_permission(request, view, obj)
        )


class NOT:
    def __init__(self, op1):
        self.op1 = op1

    def has_permission(self, request, view):
        return not self.op1.has_permission(request, view)

    def has_object_permission(self, request, view, obj):
        return not self.op1.has_object_permission(request, view, obj)


class BasePermissionMetaclass(OperationHolderMixin, type):
    pass


class BasePermission(metaclass=BasePermissionMetaclass):
    """
    A base class from which all permission classes should inherit.
    """

    def has_permission(self, request, view):
        """
        Return `True` if permission is granted, `False` otherwise.
        """
        return True

    def has_object_permission(self, request, view, obj):
        """
        Return `True` if permission is granted, `False` otherwise.
        """
        return True


class AllowAny(BasePermission):
    """
    Allow any access.
    This isn't strictly required, since you could use an empty
    permission_classes list, but it's useful because it makes the intention
    more explicit.
    """

    def has_permission(self, request, view):
        return True


class IsAuthenticated(BasePermission):
    """
    Allows access only to authenticated users.
    """

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)


class IsAdminUser(BasePermission):
    """
    Allows access only to admin users.
    """

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_staff)


class IsAuthenticatedOrReadOnly(BasePermission):
    """
    The request is authenticated as a user, or is a read-only request.
    """

    def has_permission(self, request, view):
        return bool(
            request.method in SAFE_METHODS or
            request.user and
            request.user.is_authenticated
        )


class DjangoModelPermissions(BasePermission):
    """
    The request is authenticated using `django.contrib.auth` permissions.
    See: https://docs.djangoproject.com/en/dev/topics/auth/#permissions

    It ensures that the user is authenticated, and has the appropriate
    `add`/`change`/`delete` permissions on the model.

    This permission can only be applied against view classes that
    provide a `.queryset` attribute.
    """

    # Map methods into required permission codes.
    # Override this if you need to also provide 'view' permissions,
    # or if you want to provide custom permission codes.
    perms_map = {
        'GET': [],
        'OPTIONS': [],
        'HEAD': [],
        'POST': ['%(app_label)s.add_%(model_name)s'],
        'PUT': ['%(app_label)s.change_%(model_name)s'],
        'PATCH': ['%(app_label)s.change_%(model_name)s'],
        'DELETE': ['%(app_label)s.delete_%(model_name)s'],
    }

    authenticated_users_only = True

    def get_required_permissions(self, method, model_cls):
        """
        Given a model and an HTTP method, return the list of permission
        codes that the user is required to have.
        """
        kwargs = {
            'app_label': model_cls._meta.app_label,
            'model_name': model_cls._meta.model_name
        }

        if method not in self.perms_map:
            raise exceptions.MethodNotAllowed(method)

        return [perm % kwargs for perm in self.perms_map[method]]

    def _queryset(self, view):
        assert hasattr(view, 'get_queryset') \
            or getattr(view, 'queryset', None) is not None, (
            'Cannot apply {} on a view that does not set '
            '`.queryset` or have a `.get_queryset()` method.'
        ).format(self.__class__.__name__)

        if hasattr(view, 'get_queryset'):
            queryset = view.get_queryset()
            assert queryset is not None, (
                f'{view.__class__.__name__}.get_queryset() returned None'
            )
            return queryset
        return view.queryset

    def has_permission(self, request, view):
        if not request.user or (
           not request.user.is_authenticated and self.authenticated_users_only):
            return False

        # Workaround to ensure DjangoModelPermissions are not applied
        # to the root view when using DefaultRouter.
        if getattr(view, '_ignore_model_permissions', False):
            return True

        queryset = self._queryset(view)
        perms = self.get_required_permissions(request.method, queryset.model)

        return request.user.has_perms(perms)


class DjangoModelPermissionsOrAnonReadOnly(DjangoModelPermissions):
    """
    Similar to DjangoModelPermissions, except that anonymous users are
    allowed read-only access.
    """
    authenticated_users_only = False


class DjangoObjectPermissions(DjangoModelPermissions):
    """
    The request is authenticated using Django's object-level permissions.
    It requires an object-permissions-enabled backend, such as Django Guardian.

    It ensures that the user is authenticated, and has the appropriate
    `add`/`change`/`delete` permissions on the object using .has_perms.

    This permission can only be applied against view classes that
    provide a `.queryset` attribute.
    """
    perms_map = {
        'GET': [],
        'OPTIONS': [],
        'HEAD': [],
        'POST': ['%(app_label)s.add_%(model_name)s'],
        'PUT': ['%(app_label)s.change_%(model_name)s'],
        'PATCH': ['%(app_label)s.change_%(model_name)s'],
        'DELETE': ['%(app_label)s.delete_%(model_name)s'],
    }

    def get_required_object_permissions(self, method, model_cls):
        kwargs = {
            'app_label': model_cls._meta.app_label,
            'model_name': model_cls._meta.model_name
        }

        if method not in self.perms_map:
            raise exceptions.MethodNotAllowed(method)

        return [perm % kwargs for perm in self.perms_map[method]]

    def has_object_permission(self, request, view, obj):
        # authentication checks have already executed via has_permission
        queryset = self._queryset(view)
        model_cls = queryset.model
        user = request.user

        perms = self.get_required_object_permissions(request.method, model_cls)

        if not user.has_perms(perms, obj):
            # If the user does not have permissions we need to determine if
            # they have read permissions to see 403, or not, and simply see
            # a 404 response.

            if request.method in SAFE_METHODS:
                # Read permissions already checked and failed, no need
                # to make another lookup.
                raise Http404

            read_perms = self.get_required_object_permissions('GET', model_cls)
            if not user.has_perms(read_perms, obj):
                raise Http404

            # Has read permissions.
            return False

        return True
